import Foundation
import UIKit
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.sync")

final class CameraTimeSyncCoordinator {
    struct Dependencies {
        let cameraRole: () -> String
        let getState: () -> CameraAppState
        let setState: (CameraAppState) -> Void
        let updateFrameState: (CameraAppState, Bool, String?) -> Void
        let updateUIForState: () -> Void
        let startCapture: (Double) -> Void
        let setupAudioCapture: () -> Void
        let chirpDetector: () -> AudioChirpDetector?
        let setLatestChirpSnapshot: (AudioChirpDetector.Snapshot?) -> Void
        let setWarning: (String?, UIColor?, Bool) -> Void
        let setLastUploadStatusText: (String) -> Void
        let uploader: ServerUploader
        let healthMonitor: ServerHealthMonitor
        let standbyFps: Double
    }

    private let deps: Dependencies

    private var syncDetector: AudioSyncDetector?
    private var syncAudio: MutualSyncAudio?
    private var pendingSyncId: String?
    private var syncSelfPTS: Double?
    private var syncFromOtherPTS: Double?
    private var syncWatchdog: DispatchWorkItem?
    private var lastSyncAnchor: SyncAnchor?
    private var pendingTimeSyncId: String?
    private var timeSyncClaimGeneration: Int = 0
    private var timeSyncTimeoutWork: DispatchWorkItem?

    init(dependencies: Dependencies) {
        self.deps = dependencies
    }

    var lastSyncAnchorTimestampS: Double? { lastSyncAnchor?.anchorTimestampS }
    var lastSyncId: String? { lastSyncAnchor?.syncId }

    func consumeSyncAnchorAfterCycleIfNeeded(syncId: String?) {
        guard syncId != nil else { return }
        lastSyncAnchor = nil
        deps.healthMonitor.updateTimeSyncId(nil)
        DispatchQueue.main.async {
            self.deps.updateUIForState()
        }
    }

    func onTapTimeCalibration() {
        if deps.getState() == .timeSyncWaiting {
            cancelTimeSync()
        } else if deps.getState() == .standby {
            startTimeSync()
        }
        deps.updateUIForState()
    }

    func startTimeSync(syncId: String? = nil) {
        if let syncId {
            beginTimeSync(syncId: syncId)
            return
        }
        timeSyncClaimGeneration &+= 1
        let generation = timeSyncClaimGeneration
        deps.setWarning("向伺服器請求時間校正識別碼…", nil, false)
        deps.setLastUploadStatusText("時間校正中 · 取得 sync id")
        deps.uploader.claimTimeSyncIntent { [weak self] result in
            guard let self else { return }
            DispatchQueue.main.async {
                guard generation == self.timeSyncClaimGeneration else { return }
                guard self.deps.getState() == .standby else { return }
                switch result {
                case .success(let response):
                    self.beginTimeSync(syncId: response.sync_id)
                case .failure(let error):
                    log.error("time-sync claim failed cam=\(self.deps.cameraRole(), privacy: .public) err=\(error.localizedDescription, privacy: .public)")
                    self.pendingTimeSyncId = nil
                    self.deps.setWarning("無法取得同步識別碼：檢查伺服器連線", DesignTokens.Colors.destructive, false)
                    self.deps.setLastUploadStatusText("時間校正失敗 · sync id")
                    self.deps.updateUIForState()
                }
            }
        }
    }

    func cancelTimeSync(reason: String = "cancelled") {
        let cameraRole = self.deps.cameraRole()
        log.info("camera cancel time-sync reason=\(reason, privacy: .public) cam=\(cameraRole, privacy: .public)")
        timeSyncClaimGeneration &+= 1
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        deps.chirpDetector()?.onChirpDetected = nil
        deps.setLatestChirpSnapshot(nil)
        pendingTimeSyncId = nil
        deps.setState(.standby)
        deps.updateFrameState(.standby, false, nil)
        deps.setLastUploadStatusText("時間校正 · \(Self.localizedCancelReason(reason))")

        if reason == "timeout" {
            log.warning("camera time-sync timeout cam=\(cameraRole, privacy: .public)")
            deps.setWarning("時間校正逾時：確認 chirp 音訊與麥克風", DesignTokens.Colors.destructive, false)
        } else {
            deps.setWarning(nil, nil, true)
        }
        deps.updateUIForState()
    }

    func applyMutualSync(syncId: String) {
        guard deps.getState() == .standby else {
            let stateText = CameraViewController.stateText(self.deps.getState())
            log.warning("sync_run ignored state=\(stateText, privacy: .public) sync_id=\(syncId, privacy: .public)")
            deps.uploader.postSyncLog(event: "ignored", detail: [
                "reason": .string("not_standby"),
                "state": .string(stateText),
                "sync_id": .string(syncId),
            ])
            return
        }
        let cameraRole = self.deps.cameraRole()
        log.info("camera entering mutual-sync sync_id=\(syncId, privacy: .public) cam=\(cameraRole, privacy: .public)")
        deps.uploader.postSyncLog(event: "enter", detail: [
            "sync_id": .string(syncId),
            "role": .string(cameraRole),
        ])
        pendingSyncId = syncId
        syncSelfPTS = nil
        syncFromOtherPTS = nil
        startMutualSync()
    }

    func abortMutualSync(reason: String) {
        let pendingId = self.pendingSyncId ?? "nil"
        log.warning("sync aborted reason=\(reason, privacy: .public) sync_id=\(pendingId, privacy: .public)")
        deps.uploader.postSyncLog(event: "abort", detail: [
            "reason": .string(reason),
            "sync_id": .string(self.pendingSyncId ?? ""),
            "have_self": .bool(syncSelfPTS != nil),
            "have_other": .bool(syncFromOtherPTS != nil),
        ])
        if let syncId = pendingSyncId {
            let role = deps.cameraRole()
            let traceSelf: [ServerUploader.TraceSamplePayload]
            let traceOther: [ServerUploader.TraceSamplePayload]
            if let detector = syncDetector {
                let traces = detector.drainTraces(role: role)
                traceSelf = traces.self_.map {
                    ServerUploader.TraceSamplePayload(t: $0.t, peak: $0.peak, psr: $0.psr)
                }
                traceOther = traces.other.map {
                    ServerUploader.TraceSamplePayload(t: $0.t, peak: $0.peak, psr: $0.psr)
                }
            } else {
                traceSelf = []
                traceOther = []
            }
            let report = ServerUploader.SyncReportPayload(
                camera_id: role,
                sync_id: syncId,
                role: role,
                t_self_s: syncSelfPTS,
                t_from_other_s: syncFromOtherPTS,
                emitted_band: role,
                trace_self: traceSelf.isEmpty ? nil : traceSelf,
                trace_other: traceOther.isEmpty ? nil : traceOther,
                aborted: true,
                abort_reason: reason
            )
            deps.uploader.postSyncReport(report) { result in
                if case .failure(let err) = result {
                    log.error("abort report upload failed: \(err.localizedDescription, privacy: .public)")
                }
            }
        }
        syncWatchdog?.cancel()
        syncWatchdog = nil
        teardownMutualSync(status: "Mutual sync · \(reason)")
    }

    private func beginTimeSync(syncId: String) {
        pendingTimeSyncId = syncId
        guard let detector = deps.chirpDetector() else {
            deps.setupAudioCapture()
            deps.setWarning("正在啟動麥克風…", nil, false)
            return
        }
        deps.startCapture(deps.standbyFps)
        detector.reset()
        detector.onChirpDetected = { [weak self] event in
            guard let self else { return }
            DispatchQueue.main.async {
                self.completeTimeSync(event)
            }
        }
        deps.setState(.timeSyncWaiting)
        deps.updateFrameState(.timeSyncWaiting, false, nil)
        deps.setWarning("等待同步音頻觸發中… (把兩機並排，第三裝置播 chirp)", DesignTokens.Colors.ink, false)
        deps.setLastUploadStatusText("時間校正中 · 等待聲波")

        let work = DispatchWorkItem { [weak self] in
            guard let self, self.deps.getState() == .timeSyncWaiting else { return }
            self.cancelTimeSync(reason: "timeout")
        }
        timeSyncTimeoutWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: work)
    }

    private func completeTimeSync(_ event: AudioChirpDetector.ChirpEvent) {
        guard deps.getState() == .timeSyncWaiting else { return }
        guard let syncId = pendingTimeSyncId else {
            let cameraRole = self.deps.cameraRole()
            log.error("camera complete time-sync without sync_id cam=\(cameraRole, privacy: .public)")
            cancelTimeSync(reason: "missing_sync_id")
            return
        }
        let cameraRole = self.deps.cameraRole()
        log.info("camera complete time-sync anchor_frame=\(event.anchorFrameIndex) anchor_ts=\(event.anchorTimestampS) sync_id=\(syncId, privacy: .public) cam=\(cameraRole, privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        deps.chirpDetector()?.onChirpDetected = nil
        pendingTimeSyncId = nil
        lastSyncAnchor = SyncAnchor(syncId: syncId, anchorTimestampS: event.anchorTimestampS)
        deps.healthMonitor.updateTimeSyncId(syncId)
        deps.setLatestChirpSnapshot(nil)
        deps.setState(.standby)
        deps.updateFrameState(.standby, false, nil)
        deps.setWarning(nil, nil, true)
        deps.setLastUploadStatusText("時間校正完成")
        deps.updateUIForState()
    }

    private func startMutualSync() {
        let role = deps.cameraRole()
        guard role == "A" || role == "B" else {
            log.error("sync_run rejected unknown role=\(role, privacy: .public)")
            deps.uploader.postSyncLog(event: "reject", detail: [
                "reason": .string("unknown_role"),
                "role": .string(role),
            ])
            pendingSyncId = nil
            return
        }

        let detector = AudioSyncDetector()
        detector.onDetection = { [weak self] event in
            guard let self else { return }
            DispatchQueue.main.async { self.onSyncDetection(event) }
        }
        syncDetector = detector

        let audio = MutualSyncAudio(detector: detector)
        syncAudio = audio
        deps.uploader.postSyncLog(event: "detector_installed", detail: [
            "role": .string(role),
        ])

        deps.setState(.mutualSyncing)
        deps.updateFrameState(.mutualSyncing, false, nil)
        deps.setWarning("互相時間校正中… (\(role))", nil, false)
        deps.setLastUploadStatusText("Mutual sync · emitting")
        deps.updateUIForState()

        audio.beginSync(emittedRole: role) { [weak self] in
            self?.deps.uploader.postSyncLog(event: "emit", detail: [
                "role": .string(role),
            ])
        }

        let work = DispatchWorkItem { [weak self] in
            guard let self, self.deps.getState() == .mutualSyncing else { return }
            self.abortMutualSync(reason: "timeout")
        }
        syncWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 6.0, execute: work)
    }

    private func onSyncDetection(_ event: AudioSyncDetector.DetectionEvent) {
        guard deps.getState() == .mutualSyncing else { return }
        let ownRole = deps.cameraRole()
        let isSelfHear = event.band.rawValue == ownRole
        deps.uploader.postSyncLog(event: "band_fired", detail: [
            "band": .string(event.band.rawValue),
            "is_self": .bool(isSelfHear),
            "peak": .double(Double(event.peakNorm)),
            "center_s": .double(event.centerPTS),
        ])
        if isSelfHear {
            if syncSelfPTS == nil {
                syncSelfPTS = event.centerPTS
                log.info("sync self-hear band=\(event.band.rawValue, privacy: .public) center_s=\(event.centerPTS, privacy: .public) peak=\(event.peakNorm, privacy: .public)")
            }
        } else if syncFromOtherPTS == nil {
            syncFromOtherPTS = event.centerPTS
            log.info("sync cross-hear band=\(event.band.rawValue, privacy: .public) center_s=\(event.centerPTS, privacy: .public) peak=\(event.peakNorm, privacy: .public)")
        }
        if let tSelf = syncSelfPTS, let tOther = syncFromOtherPTS {
            completeMutualSync(tSelf: tSelf, tFromOther: tOther)
        }
    }

    private func completeMutualSync(tSelf: Double, tFromOther: Double) {
        guard let syncId = pendingSyncId else {
            log.error("sync complete without pending sync_id — ignoring")
            return
        }
        log.info("sync complete sync_id=\(syncId, privacy: .public) t_self=\(tSelf, privacy: .public) t_from_other=\(tFromOther, privacy: .public)")
        deps.uploader.postSyncLog(event: "complete", detail: [
            "sync_id": .string(syncId),
            "t_self_s": .double(tSelf),
            "t_from_other_s": .double(tFromOther),
        ])
        syncWatchdog?.cancel()
        syncWatchdog = nil

        let role = deps.cameraRole()
        let traces: (self_: [AudioSyncDetector.TraceSample], other: [AudioSyncDetector.TraceSample])
        if let detector = syncDetector {
            traces = detector.drainTraces(role: role)
        } else {
            traces = ([], [])
        }
        let traceSelfPayload = traces.self_.map {
            ServerUploader.TraceSamplePayload(t: $0.t, peak: $0.peak, psr: $0.psr)
        }
        let traceOtherPayload = traces.other.map {
            ServerUploader.TraceSamplePayload(t: $0.t, peak: $0.peak, psr: $0.psr)
        }
        let report = ServerUploader.SyncReportPayload(
            camera_id: role,
            sync_id: syncId,
            role: role,
            t_self_s: tSelf,
            t_from_other_s: tFromOther,
            emitted_band: role,
            trace_self: traceSelfPayload.isEmpty ? nil : traceSelfPayload,
            trace_other: traceOtherPayload.isEmpty ? nil : traceOtherPayload,
            aborted: false,
            abort_reason: nil
        )
        deps.uploader.postSyncReport(report) { [weak self] result in
            switch result {
            case .success:
                DispatchQueue.main.async {
                    self?.deps.setLastUploadStatusText("Mutual sync · uploaded")
                    self?.deps.updateUIForState()
                }
            case .failure(let error):
                log.error("sync report upload failed: \(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async {
                    self?.deps.setLastUploadStatusText("Mutual sync · upload failed")
                    self?.deps.updateUIForState()
                }
            }
        }

        teardownMutualSync(status: "Mutual sync · done")
    }

    private func teardownMutualSync(status: String) {
        syncDetector?.onDetection = nil
        syncDetector = nil
        syncAudio?.endSync()
        syncAudio = nil
        pendingSyncId = nil
        syncSelfPTS = nil
        syncFromOtherPTS = nil
        deps.setState(.standby)
        deps.updateFrameState(.standby, false, nil)
        deps.setWarning(nil, nil, true)
        deps.setLastUploadStatusText(status)
        deps.updateUIForState()
    }

    private static func localizedCancelReason(_ reason: String) -> String {
        switch reason {
        case "timeout": return "逾時"
        case "cancelled": return "已取消"
        case "disarmed": return "已取消（dashboard 停止）"
        case "missing_sync_id": return "缺少 sync id"
        default: return reason
        }
    }
}
