import Foundation
import os

private let syncLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera.sync")

final class CameraSyncCoordinator {
    struct RecoveredAnchor {
        let syncId: String
        let anchorTimestampS: Double
    }

    struct Dependencies {
        let getState: () -> CameraViewController.AppState
        let getCameraRole: () -> String
        let standbyFps: Double
        let uploader: () -> ServerUploader
        let healthMonitor: () -> HeartbeatScheduler?
        let chirpDetector: () -> AudioChirpDetector?
        let setupAudioCapture: () -> Void
        let startCapture: (Double) -> Void
        let reconcileStandbyCaptureState: () -> Void
        let transitionState: (CameraViewController.AppState) -> Void
        let setStatusText: (String) -> Void
        let hideBanner: () -> Void
        let flashErrorBanner: (String, TimeInterval) -> Void
        let refreshUI: () -> Void
        let makeMutualSyncAudio: ([Double], Double) -> MutualSyncAudio
    }

    private let deps: Dependencies

    private var lastSyncAnchor: RecoveredAnchor?
    private var pendingTimeSyncId: String?
    private var timeSyncTimeoutWork: DispatchWorkItem?

    private var syncAudio: MutualSyncAudio?
    private var pendingSyncId: String?
    // No boot default — these MUST be written by `applyMutualSync` before
    // `startMutualSync` reads them. A silent default would mask a missing
    // server push and run with timing that disagrees with server logs.
    private var pendingSyncEmitAtS: [Double]?
    private var pendingSyncRecordDurationS: Double?
    private var syncWatchdog: DispatchWorkItem?

    init(dependencies: Dependencies) {
        self.deps = dependencies
    }

    var lastSyncAnchorTimestampS: Double? { lastSyncAnchor?.anchorTimestampS }
    var lastSyncId: String? { lastSyncAnchor?.syncId }

    func clearRecoveredAnchor() {
        lastSyncAnchor = nil
        deps.healthMonitor()?.updateTimeSyncId(nil)
    }

    func startTimeSync(syncId: String) {
        // server is the single source of truth for sync_id — every
        // caller is the WS `sync_command` dispatch (via CommandRouter /
        // TransportCoordinator / VC), and `sync_command` only fires after
        // the server has already minted the id. The legacy nil-arg path
        // that POSTed `/sync/claim` to mint client-side is dead.
        beginTimeSync(syncId: syncId)
    }

    func cancelTimeSync(reason: String = "cancelled") {
        syncLog.info("camera cancel time-sync reason=\(reason, privacy: .public) cam=\(self.deps.getCameraRole(), privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        deps.chirpDetector()?.onChirpDetected = nil
        pendingTimeSyncId = nil
        deps.transitionState(.standby)
        deps.reconcileStandbyCaptureState()
        deps.setStatusText("時間校正 · \(Self.localizedCancelReason(reason))")

        if reason == "timeout" {
            syncLog.warning("camera time-sync timeout cam=\(self.deps.getCameraRole(), privacy: .public)")
            deps.flashErrorBanner("時間校正逾時：確認 chirp 音訊與麥克風", 3)
        } else {
            deps.hideBanner()
        }
        deps.refreshUI()
    }

    func applyMutualSync(syncId: String, emitAtS: [Double], recordDurationS: Double) {
        guard deps.getState() == .standby else {
            syncLog.warning("sync_run ignored state=\(CameraViewController.stateText(self.deps.getState()), privacy: .public) sync_id=\(syncId, privacy: .public)")
            deps.uploader().postSyncLog(event: "ignored", detail: [
                "reason": .string("not_standby"),
                "state": .string(CameraViewController.stateText(deps.getState())),
                "sync_id": .string(syncId),
            ])
            return
        }
        syncLog.info("camera entering mutual-sync sync_id=\(syncId, privacy: .public) cam=\(self.deps.getCameraRole(), privacy: .public) n_bursts=\(emitAtS.count, privacy: .public) record_s=\(recordDurationS, privacy: .public)")
        deps.uploader().postSyncLog(event: "enter", detail: [
            "sync_id": .string(syncId),
            "role": .string(deps.getCameraRole()),
            "n_bursts": .int(emitAtS.count),
        ])
        pendingSyncId = syncId
        // Out-of-contract values (empty emit_at_s / record_duration_s < 1.0)
        // are atomic-dropped at the route layer in CameraCommandRouter so
        // we never see them here. Internal-invariant assert; no silent
        // clamp — quietly substituting [0.3] / 1.0s would mask server bugs
        // and quietly run with different timing than the server logs claim.
        assert(!emitAtS.isEmpty, "sync_run: empty emit_at_s leaked past route guard")
        assert(recordDurationS >= 1.0, "sync_run: record_duration_s=\(recordDurationS) below 1.0 floor leaked past route guard")
        pendingSyncEmitAtS = emitAtS
        pendingSyncRecordDurationS = recordDurationS
        startMutualSync()
    }

    func abortMutualSync(reason: String) {
        syncLog.warning("sync aborted reason=\(reason, privacy: .public) sync_id=\(self.pendingSyncId ?? "nil", privacy: .public)")
        var detail: [String: ServerUploader.AnyJSONValue] = ["reason": .string(reason)]
        if let syncId = pendingSyncId {
            detail["sync_id"] = .string(syncId)
        }
        deps.uploader().postSyncLog(event: "abort", detail: detail)
        syncWatchdog?.cancel()
        syncWatchdog = nil
        teardownMutualSync(status: "Mutual sync · \(reason)")
    }

    private func beginTimeSync(syncId: String) {
        // Cancel any in-flight timeout from a prior sync attempt — the
        // dashboard's Quick chirp can re-fire while we're still in
        // .timeSyncWaiting, and the stale work item would otherwise
        // bounce us back to .standby mid-listen.
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        // Drop the previous successful anchor at the moment a new
        // attempt starts. Heartbeats during the listen window now
        // report time_sync_id=nil + anchor=nil, which on the server
        // gates `time_synced` to False until either the chirp lands
        // (giving us the new anchor) or the operator triggers again.
        // Without this, a missed chirp would silently leave us
        // claiming the old anchor — readiness then accepts a stereo
        // session whose A/B anchors point at different physical chirps.
        lastSyncAnchor = nil
        deps.healthMonitor()?.updateTimeSyncId(nil)
        pendingTimeSyncId = syncId
        guard let detector = deps.chirpDetector() else {
            deps.setupAudioCapture()
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
        deps.transitionState(.timeSyncWaiting)

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
            syncLog.error("camera complete time-sync without sync_id cam=\(self.deps.getCameraRole(), privacy: .public)")
            cancelTimeSync(reason: "missing_sync_id")
            return
        }
        syncLog.info("camera complete time-sync anchor_frame=\(event.anchorFrameIndex) anchor_ts=\(event.anchorTimestampS) sync_id=\(syncId, privacy: .public) cam=\(self.deps.getCameraRole(), privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        deps.chirpDetector()?.onChirpDetected = nil
        pendingTimeSyncId = nil
        lastSyncAnchor = RecoveredAnchor(syncId: syncId, anchorTimestampS: event.anchorTimestampS)
        deps.healthMonitor()?.updateTimeSyncId(syncId)
        deps.transitionState(.standby)
        deps.reconcileStandbyCaptureState()
        deps.hideBanner()
        deps.setStatusText("時間校正完成")
        deps.refreshUI()
    }

    private func startMutualSync() {
        let role = deps.getCameraRole()
        // Mutual chirp sync is intrinsically pair-wise: the two phones
        // exchange chirps on distinct frequency bands (A vs B, defined
        // in MutualSyncAudio.swift). A third camera (e.g. role "C")
        // joining the rig CAN run capture / detection / heartbeat
        // perfectly fine, but it does NOT participate in this pair-
        // wise sync — broadcasting from a third role would require
        // additional band assignments and is a future-phase change.
        // Today this guard is the explicit firewall that keeps non-
        // A/B roles from emitting on an undefined band.
        guard role == "A" || role == "B" else {
            syncLog.error("sync_run skipped non-pair role=\(role, privacy: .public)")
            deps.uploader().postSyncLog(event: "reject", detail: [
                "reason": .string("non_pair_role_skips_mutual_sync"),
                "role": .string(role),
            ])
            pendingSyncId = nil
            return
        }
        // Same anchor-clearing rationale as beginTimeSync: a /sync/start
        // run that fails to recover an anchor must not leave us claiming
        // the previous one. Heartbeats during the recording window will
        // report id=nil until handleMutualSyncRecording lands.
        lastSyncAnchor = nil
        deps.healthMonitor()?.updateTimeSyncId(nil)

        // Invariant: applyMutualSync (the only public entry into the
        // sync flow) writes both pending fields before calling here.
        // Empty / sub-floor values are atomic-dropped by the router and
        // additionally asserted in applyMutualSync, so unwrapping with
        // precondition makes the invariant explicit instead of substituting
        // a silent default that would disagree with server-pushed timing.
        guard let emitAtS = pendingSyncEmitAtS,
              let recordDurationS = pendingSyncRecordDurationS else {
            preconditionFailure("startMutualSync called without pendingSyncEmitAtS / pendingSyncRecordDurationS — applyMutualSync must write both before transitioning to mutualSyncing")
        }

        let audio = deps.makeMutualSyncAudio(emitAtS, recordDurationS)
        syncAudio = audio
        deps.uploader().postSyncLog(event: "recording_started", detail: [
            "role": .string(role),
        ])

        deps.transitionState(.mutualSyncing)
        deps.setStatusText("Mutual sync · recording")
        deps.refreshUI()

        let roleCaptured = role
        audio.beginSync(
            emittedRole: roleCaptured,
            onEmitted: { [weak self] in
                self?.deps.uploader().postSyncLog(event: "emit", detail: [
                    "role": .string(roleCaptured),
                ])
            },
            onRecordingComplete: { [weak self] result in
                self?.handleMutualSyncRecording(result: result, role: roleCaptured)
            },
            onError: { [weak self] message in
                self?.abortMutualSync(reason: "audio_init_failed: \(message)")
            }
        )

        let timeoutS = recordDurationS + 3.0
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.deps.getState() == .mutualSyncing else { return }
            self.abortMutualSync(reason: "timeout")
        }
        syncWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + timeoutS, execute: work)
    }

    private func handleMutualSyncRecording(
        result: MutualSyncAudio.RecordingResult,
        role: String
    ) {
        guard deps.getState() == .mutualSyncing else { return }
        guard let syncId = pendingSyncId else {
            syncLog.error("recording complete without pending sync_id — ignoring")
            teardownMutualSync(status: "Mutual sync · orphan")
            return
        }
        syncWatchdog?.cancel()
        syncWatchdog = nil

        deps.uploader().postSyncLog(event: "recording_complete", detail: [
            "sync_id": .string(syncId),
            "wav_bytes": .int(result.wavData.count),
            "duration_s": .double(
                Double(result.wavData.count) / max(1.0, result.sampleRate * 2.0)
            ),
            "audio_start_pts_s": .double(result.audioStartPtsS),
        ])

        deps.setStatusText("Mutual sync · uploading")
        deps.refreshUI()

        let meta = ServerUploader.SyncAudioUploadMeta(
            sync_id: syncId,
            camera_id: role,
            role: role,
            audio_start_pts_s: result.audioStartPtsS,
            sample_rate: Int(result.sampleRate),
            emission_pts_s: result.emissionPtsS
        )
        syncLog.info("sync uploading wav_bytes=\(result.wavData.count) n_emissions=\(result.emissionPtsS.count, privacy: .public)")
        deps.uploader().uploadSyncAudio(meta: meta, wavData: result.wavData) { [weak self] upResult in
            DispatchQueue.main.async {
                guard let self else { return }
                switch upResult {
                case .success:
                    self.deps.setStatusText("Mutual sync · done")
                case .failure(let error):
                    syncLog.error("sync audio upload failed: \(error.localizedDescription, privacy: .public)")
                    self.deps.setStatusText("Mutual sync · upload failed")
                }
                self.deps.refreshUI()
            }
        }

        teardownMutualSync(status: "Mutual sync · uploaded")
    }

    private func teardownMutualSync(status: String) {
        syncAudio?.endSync()
        syncAudio = nil
        pendingSyncId = nil
        deps.transitionState(.standby)
        deps.reconcileStandbyCaptureState()
        deps.hideBanner()
        deps.setStatusText(status)
        deps.refreshUI()
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
