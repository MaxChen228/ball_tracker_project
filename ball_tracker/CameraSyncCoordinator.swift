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
        /// Quick-sync (single-emitter, N-listener) audio engine factory.
        /// `(emitAtS, recordDurationS, isEmitter)` — a listener passes
        /// `isEmitter: false` and never plays; the band is always A.
        let makeQuickSyncAudio: ([Double], Double, Bool) -> QuickSyncAudio
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

    // Quick-sync (single-emitter, N-listener) recording state. Mirrors the
    // mutual fields above but is a fully separate flow: any cam can emit
    // (band fixed A), the anchor comes back via the `quick_sync_applied`
    // push (adoptQuickSyncAnchor) rather than local detection.
    private var quickSyncAudio: QuickSyncAudio?
    private var pendingQuickSyncId: String?
    private var pendingQuickEmitAtS: [Double]?
    private var pendingQuickRecordDurationS: Double?
    private var pendingQuickIsEmitter: Bool?
    private var quickSyncWatchdog: DispatchWorkItem?

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

    // MARK: - Quick sync (single-emitter, N-listener)

    /// Adopt the server-solved quick-sync anchor pushed back via
    /// `quick_sync_applied`. The anchor is cross-correlated server-side
    /// (this device never locally detected the chirp), so we set
    /// `lastSyncAnchor` + the heartbeat sync id exactly like `completeTimeSync`
    /// does for the local-detection path — the next heartbeat then reports
    /// this value as `sync_anchor_timestamp_s` / `time_sync_id`, which is
    /// what keeps the server registry from wiping the anchor (the heartbeat
    /// is the registry's source of truth). No state gate: this is a passive
    /// adoption the device should record regardless of current state (unlike
    /// the local-detection completeTimeSync which only fires in
    /// `.timeSyncWaiting`).
    func adoptQuickSyncAnchor(syncId: String, anchorTimestampS: Double) {
        syncLog.info("camera adopt quick-sync anchor anchor_ts=\(anchorTimestampS) sync_id=\(syncId, privacy: .public) cam=\(self.deps.getCameraRole(), privacy: .public)")
        lastSyncAnchor = RecoveredAnchor(syncId: syncId, anchorTimestampS: anchorTimestampS)
        deps.healthMonitor()?.updateTimeSyncId(syncId)
    }

    func applyQuickSync(syncId: String, isEmitter: Bool, emitAtS: [Double], recordDurationS: Double) {
        guard deps.getState() == .standby else {
            syncLog.warning("sync_quick_run ignored state=\(CameraViewController.stateText(self.deps.getState()), privacy: .public) sync_id=\(syncId, privacy: .public)")
            deps.uploader().postSyncLog(event: "ignored", detail: [
                "reason": .string("not_standby"),
                "state": .string(CameraViewController.stateText(deps.getState())),
                "sync_id": .string(syncId),
                "flow": .string("quick"),
            ])
            return
        }
        syncLog.info("camera entering quick-sync sync_id=\(syncId, privacy: .public) cam=\(self.deps.getCameraRole(), privacy: .public) is_emitter=\(isEmitter, privacy: .public) record_s=\(recordDurationS, privacy: .public)")
        deps.uploader().postSyncLog(event: "enter", detail: [
            "sync_id": .string(syncId),
            "role": .string(deps.getCameraRole()),
            "is_emitter": .bool(isEmitter),
            "flow": .string("quick"),
        ])
        pendingQuickSyncId = syncId
        // Out-of-contract values are atomic-dropped at the route layer; assert
        // the invariant here, no silent clamp (same rationale as mutual).
        assert(recordDurationS >= 1.0, "sync_quick_run: record_duration_s=\(recordDurationS) below 1.0 floor leaked past route guard")
        assert(!isEmitter || !emitAtS.isEmpty, "sync_quick_run: emitter with empty emit_at_s leaked past route guard")
        pendingQuickEmitAtS = emitAtS
        pendingQuickRecordDurationS = recordDurationS
        pendingQuickIsEmitter = isEmitter
        startQuickSync()
    }

    func abortQuickSync(reason: String) {
        syncLog.warning("quick-sync aborted reason=\(reason, privacy: .public) sync_id=\(self.pendingQuickSyncId ?? "nil", privacy: .public)")
        var detail: [String: ServerUploader.AnyJSONValue] = [
            "reason": .string(reason), "flow": .string("quick"),
        ]
        if let syncId = pendingQuickSyncId {
            detail["sync_id"] = .string(syncId)
        }
        deps.uploader().postSyncLog(event: "abort", detail: detail)
        quickSyncWatchdog?.cancel()
        quickSyncWatchdog = nil
        teardownQuickSync(status: "Quick sync · \(reason)")
    }

    private func startQuickSync() {
        let role = deps.getCameraRole()
        // No A/B-only role guard (unlike mutual): the emitter always plays
        // band A and any cam can be the emitter — that is the N-cam enabler.
        // Same anchor-clearing rationale as startMutualSync: a run that fails
        // to recover an anchor must not leave us claiming the previous one.
        lastSyncAnchor = nil
        deps.healthMonitor()?.updateTimeSyncId(nil)

        guard let emitAtS = pendingQuickEmitAtS,
              let recordDurationS = pendingQuickRecordDurationS,
              let isEmitter = pendingQuickIsEmitter else {
            preconditionFailure("startQuickSync called without pendingQuick fields — applyQuickSync must write all three before transitioning to quickSyncing")
        }

        let audio = deps.makeQuickSyncAudio(emitAtS, recordDurationS, isEmitter)
        quickSyncAudio = audio
        deps.uploader().postSyncLog(event: "recording_started", detail: [
            "role": .string(role), "flow": .string("quick"),
            "is_emitter": .bool(isEmitter),
        ])

        deps.transitionState(.quickSyncing)
        deps.setStatusText("Quick sync · recording")
        deps.refreshUI()

        audio.beginSync(
            onRecordingComplete: { [weak self] result in
                self?.handleQuickSyncRecording(result: result)
            },
            onError: { [weak self] message in
                self?.abortQuickSync(reason: "audio_init_failed: \(message)")
            }
        )

        let timeoutS = recordDurationS + 3.0
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.deps.getState() == .quickSyncing else { return }
            self.abortQuickSync(reason: "timeout")
        }
        quickSyncWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + timeoutS, execute: work)
    }

    private func handleQuickSyncRecording(result: QuickSyncAudio.RecordingResult) {
        guard deps.getState() == .quickSyncing else { return }
        guard let syncId = pendingQuickSyncId else {
            syncLog.error("quick recording complete without pending sync_id — ignoring")
            teardownQuickSync(status: "Quick sync · orphan")
            return
        }
        quickSyncWatchdog?.cancel()
        quickSyncWatchdog = nil

        deps.uploader().postSyncLog(event: "recording_complete", detail: [
            "sync_id": .string(syncId),
            "flow": .string("quick"),
            "wav_bytes": .int(result.wavData.count),
            "audio_start_pts_s": .double(result.audioStartPtsS),
        ])

        deps.setStatusText("Quick sync · uploading")
        deps.refreshUI()

        let meta = ServerUploader.QuickSyncUploadMeta(
            sync_id: syncId,
            camera_id: deps.getCameraRole(),
            audio_start_pts_s: result.audioStartPtsS
        )
        syncLog.info("quick-sync uploading wav_bytes=\(result.wavData.count)")
        deps.uploader().uploadQuickSyncAudio(meta: meta, wavData: result.wavData) { [weak self] upResult in
            DispatchQueue.main.async {
                guard let self else { return }
                switch upResult {
                case .success:
                    self.deps.setStatusText("Quick sync · done")
                case .failure(let error):
                    syncLog.error("quick-sync audio upload failed: \(error.localizedDescription, privacy: .public)")
                    self.deps.setStatusText("Quick sync · upload failed")
                }
                self.deps.refreshUI()
            }
        }

        teardownQuickSync(status: "Quick sync · uploaded")
    }

    private func teardownQuickSync(status: String) {
        // Cancel here too, not only at the call sites: the orphan path in
        // handleQuickSyncRecording reaches teardown before its own cancel,
        // and any future caller would otherwise leak the watchdog. Idempotent
        // — safe to stack with the call-site cancels.
        quickSyncWatchdog?.cancel()
        quickSyncWatchdog = nil
        quickSyncAudio?.endSync()
        quickSyncAudio = nil
        pendingQuickSyncId = nil
        deps.transitionState(.standby)
        deps.reconcileStandbyCaptureState()
        deps.hideBanner()
        deps.setStatusText(status)
        deps.refreshUI()
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
        // Same orphan-path / future-caller leak guard as teardownQuickSync.
        syncWatchdog?.cancel()
        syncWatchdog = nil
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
