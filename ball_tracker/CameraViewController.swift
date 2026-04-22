import UIKit
import AVFoundation
import AudioToolbox
import CoreMedia
import CoreVideo
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "camera")

/// Main camera view. State machine:
/// - STANDBY: live preview, chirp detector off, no recording
/// - TIME_SYNC_WAITING: listens on the mic for the reference chirp, saves
///   session-clock PTS of the peak as the anchor (manual 時間校正 only)
/// - RECORDING: dashboard-armed; H.264 clip being written to disk
/// - UPLOADING: cycle persisted, handed to the upload queue
///
/// The phone is a pure capture client — no ball detection runs on-device.
/// Dashboard arm goes straight to `.recording`; dashboard stop (or server
/// session timeout) is the sole exit back to standby. Server ingests the
/// MOV and does HSV detection + triangulation.
///
/// Heavy side concerns live in dedicated helpers:
/// - `ServerHealthMonitor` owns the 1 Hz heartbeat, backoff, and
///   "last contact" tick timer.
/// - `PayloadUploadQueue` owns the cached-pitch upload worker.
final class CameraViewController: UIViewController, AVCaptureVideoDataOutputSampleBufferDelegate {
    enum AppState {
        case standby
        case timeSyncWaiting
        case mutualSyncing
        case recording
        case uploading
    }

    private struct SyncAnchor {
        let syncId: String
        let anchorTimestampS: Double
    }

    // Adaptive capture rate. Idle / time-sync runs at 60 fps to keep the
    // sensor + ISP cool and save battery; `.recording` switches to 240 fps
    // so the 8 ms A/B pair window gets sub-frame resolution server-side.
    // The format swap costs ~300-500 ms (stopRunning → activeFormat →
    // startRunning) so we only do it at state boundaries.
    private let standbyFps: Double = 60
    private let trackingFps: Double = 240

    // Session + outputs.
    private let session = AVCaptureSession()
    private var previewLayer: AVCaptureVideoPreviewLayer?
    private let videoOutput = AVCaptureVideoDataOutput()
    // `.userInitiated` QoS is required for 240 fps frame delivery. With default
    // QoS the queue can be throttled by the scheduler which amplifies any
    // detection stalls into dropped-frame cascades (AVCaptureVideoDataOutput
    // has `alwaysDiscardsLateVideoFrames = true`).
    private let processingQueue = DispatchQueue(label: "camera.frame.queue", qos: .userInitiated)
    // Dedicated queue for `session.startRunning / stopRunning / activeFormat`
    // lifecycle ops. Apple's Thread Performance Checker warns when these hit
    // the main thread — startRunning on a 240 fps format can block for
    // 300-500 ms, stalling the run loop and causing visible HUD stutter +
    // deferred frame delivery cascades. Keep this separate from the frame
    // queue so a format swap can't interleave with sample-buffer delivery.
    private let sessionQueue = DispatchQueue(label: "camera.session.queue", qos: .userInitiated)

    // Audio chirp sync. Mic input is always installed once granted so the
    // chirp detector can run as soon as the user taps 時間校正.
    private var audioInput: AVCaptureDeviceInput?
    private var audioOutput: AVCaptureAudioDataOutput?
    private var chirpDetector: AudioChirpDetector?
    // Mutual chirp sync (dashboard-triggered). Runs on its own
    // `AVAudioEngine` (owned by `syncAudio`) — fully decoupled from
    // `AVCaptureSession`, so sync works regardless of capture state
    // (parked / standby-fps / tracking-fps). Phase A: iOS only records;
    // detection happens server-side. `syncAudio` drives the 3-second
    // listening window + WAV upload; timeout is a belt-and-braces
    // watchdog in case MutualSyncAudio never fires its completion.
    private var syncAudio: MutualSyncAudio?
    private var pendingSyncId: String?
    private var syncWatchdog: DispatchWorkItem?

    // State + frame index. `state` is read/written on main; `captureOutput`
    // (frame queue, up to 240 Hz) reads it via `frameStateBox.snapshot()` so
    // it never observes a partially-updated AppState while `applyRemoteDisarm`
    // mutates it on main. `frameIndex` is touched only on the frame queue.
    private var state: AppState = .standby
    private var frameIndex: Int = 0
    private var horizontalFovRadians: Double = 1.0

    // Lock-protected mirror of the three fields `captureOutput` reads
    // across queues. Main thread is the sole writer; the frame queue
    // takes a single locked snapshot per delivered sample.
    private let frameStateBox = FrameStateBox()

    // Collaborators.
    private var settings: AppSettings!
    private var trackingExposureCapMode: ServerUploader.TrackingExposureCapMode = .frameDuration
    private var recorder: PitchRecorder!
    private var uploader: ServerUploader!
    private var serverConfig: ServerUploader.ServerConfig!
    private var healthMonitor: ServerHealthMonitor!
    private var uploadQueue: PayloadUploadQueue!
    private var commandRouter: CameraCommandRouter!

    // UI state.
    private var lastUploadStatusText: String = ""
    private var lastResultText: String = ""
    private var lastFrameTimestampForFps: CFTimeInterval = CACurrentMediaTime()
    private var framesSinceLastFpsTick: Int = 0
    private var fpsEstimate: Double = 0
    private var displayLink: CADisplayLink?

    // Last observed capture dimensions — used only for FPS debug and the
    // capture-dim change log. Phase 6: no longer mirrored to UserDefaults;
    // the server owns all calibration state now.
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0

    // Chirp detector snapshot for the HUD — written on audio queue.
    private var latestChirpSnapshot: AudioChirpDetector.Snapshot?

    // The `.recording`-was-just-entered bootstrap flag and the snapshot of
    // `currentSessionId` taken at arm time both live in `frameStateBox`;
    // the camera VC itself never reads them after the push, so duplicating
    // them as instance vars would just be two stores out of sync.

    // Remote-control state (driven by WS heartbeat / settings traffic).
    /// Last (command, sync_id) tuple we acted on. Plain "arm" / "disarm"
    /// use a nil sync_id; `"sync_run"` carries the server-minted run id so
    /// back-to-back runs (same command string, different run) still fire.
    /// Only state *transitions* cause local actions so repeated replies
    /// during an active command don't re-trigger handlers.
    /// Reserved for future branching in the cycle-complete path (e.g. a
    /// re-arm that wants to skip the standby flash). Always true today
    /// because `.recording` always returns to `.standby` now.
    private var returnToStandbyAfterCycle: Bool = false
    /// Server-minted pairing key for the currently armed session. Read
    /// off WS arm/settings traffic; tagged onto every recording that starts
    /// while the session is armed. Nil when the server has no active
    /// session. iPhones never mint this themselves.
    private var currentSessionId: String?

    // Payload persistence.
    private let payloadStore = PitchPayloadStore()
    private let analysisStore = AnalysisJobStore()
    private var analysisQueue: AnalysisUploadQueue!

    // Per-cycle H.264 clip writer, created on entry to .recording and
    // finalised when the cycle ends. Phase-1 raw-video experiment: the clip
    // travels alongside the JSON payload so server-side detection can be
    // iterated against a canonical source of truth.
    private var clipRecorder: ClipRecorder?
    private let captureTelemetryLock = NSLock()
    private var appliedCaptureWidthPx: Int = 1920
    private var appliedCaptureHeightPx: Int = 1080
    private var appliedCaptureFps: Double = 60
    private var appliedFormatFovDeg: Double?
    private var appliedFormatIndex: Int?
    private var appliedFormatIsVideoBinned: Bool?
    private var appliedMaxExposureS: Double?

    // Live detection is advisory only. PR61 moves authoritative on-device
    // results to a post-recording analysis pass over the finalized MOV; this
    // queue stays useful for HUD/debug and as a degraded fallback if clip
    // writing fails.
    private let detectionPool = ConcurrentDetectionPool(maxConcurrency: 3)
    /// Streams per-frame detection results over WebSocket when the live path
    /// is active. Created lazily (after settings are available).
    private var frameDispatcher: LiveFrameDispatcher?
    private let detectionStateLock = NSLock()
    /// Accumulated per-frame detection results for the current cycle.
    /// Drained at cycle-complete and either discarded (mode-one) or
    /// attached to the upload payload (mode-two).
    private var detectionFramesBuffer: [ServerUploader.FramePayload] = []

    // Most recently recovered chirp anchor — session-clock PTS of the
    // chirp peak from the mic matched filter. Stamped onto outgoing
    // payloads as `sync_anchor_timestamp_s`. Nil until the user completes
    // a 時間校正; server rejects unpaired sessions whose anchor is nil.
    private var lastSyncAnchor: SyncAnchor?
    private var pendingTimeSyncId: String?
    private var timeSyncClaimGeneration: Int = 0
    private var lastSyncAnchorTimestampS: Double? { lastSyncAnchor?.anchorTimestampS }
    private var lastSyncId: String? { lastSyncAnchor?.syncId }
    private var serverTimeSyncConfirmed: Bool = false
    private var serverTimeSyncId: String?

    // Time-sync timeout task so we can cancel if the user aborts early.
    private var timeSyncTimeoutWork: DispatchWorkItem?

    // UI containers. Preview stays full-screen; a small overlay panel exposes
    // the only remaining local controls: server endpoint, role, and link /
    // preview status.
    private let topStatusChip = StatusChip()
    private let controlPanel = UIView()
    private let serverIPField = UITextField()
    private let serverPortField = UITextField()
    private let roleControl = UISegmentedControl(items: ["A", "B"])
    private let applySettingsButton = UIButton(type: .system)
    private let connectionLabel = UILabel()
    private let previewLabel = UILabel()
    /// Last-known capture mode from the server. Starts at cameraOnly so a
    /// network-unreachable launch degrades to the pre-split behaviour. Step 2
    /// reads this at cycle-complete to decide whether to upload the MOV or
    /// just the detection JSON.
    private var currentCaptureMode: ServerUploader.CaptureMode = .cameraOnly
    private var currentSessionPaths: Set<ServerUploader.DetectionPath> = [.serverPost]
    /// WebSocket transport. Lazily initialized so the base URL (built from
    /// settings) is read after `viewDidLoad` where UserDefaults are stable.
    private var ws: ServerWebSocketConnection?
    /// Cache of the server-pushed runtime tunables so the WS settings
    /// callback can skip hot-apply when the value hasn't changed. `nil`
    /// means "never heard from the server yet" — the first settings payload
    /// triggers the initial apply regardless of whether the server
    /// value happens to equal the local Settings bootstrap default.
    private var lastServerChirpThreshold: Double?
    private var lastServerMutualThreshold: Double?
    private var lastServerHeartbeatInterval: Double?
    private var lastServerTrackingExposureCapMode: ServerUploader.TrackingExposureCapMode?
    /// Currently-applied capture image height. Initialised from
    /// `AppSettings.captureHeightFixed` (1080). Server pushes a
    /// value via WS settings; when it differs, rebuild the capture session
    /// — but only while in .standby so an armed clip isn't disrupted.
    private var currentCaptureHeight: Int = AppSettings.captureHeightFixed
    /// Paired 16:9 width for `currentCaptureHeight`. Used by every
    /// `configureCaptureFormat` call site so fps swaps preserve the
    /// active resolution (no snap-back to 1080p after server pushes 720).
    private var currentCaptureWidth: Int { captureWidthForHeight(currentCaptureHeight) }

    private func captureWidthForHeight(_ h: Int) -> Int {
        switch h {
        case 720:  return 1280
        case 1080: return 1920
        default:   return AppSettings.captureWidthFixed
        }
    }

    // Phase 4a live preview. `previewRequestedByServer` mirrors the
    // WS settings flag for THIS camera; `previewUploader` lazily
    // constructs on first push. When the flag flips true→false we reset
    // the uploader so a stale in-flight POST doesn't land after toggle-off.
    // Phase 6: capture session is always live at standbyFps when idle, so
    // pixel buffers are guaranteed to be flowing whenever preview is asked.
    private var previewRequestedByServer: Bool = false
    private var previewUploader: PreviewUploader?
    /// One-shot latch for the server's `calibration_frame_requested` flag.
    /// When true, the next `captureOutput` sample will be encoded at
    /// native resolution (NO downsample) and POSTed to
    /// `/camera/{id}/calibration_frame`. Cleared after upload regardless
    /// of success — the server-side flag drains the moment ANY request
    /// arrives, so retrying from the same heartbeat would just double-POST.
    private var calibrationFrameCaptureArmed: Bool = false
    private let warningLabel = UILabel()

    // Full-screen colored border that reflects AppState. Stroke width +
    // tint change per state; pulses opacity in WAITING states so the
    // operator can see the mode change from across the field.
    private let stateBorderLayer = CAShapeLayer()

    // Top-right "● REC 2.3s" indicator, shown only during .recording.
    private let recIndicator = UIView()
    private let recDotView = UIView()
    private let recTimerLabel = UILabel()
    private var recTimer: Timer?
    private var recStartTime: CFTimeInterval = 0

    // Last state whose visuals (border stroke, pulse animation, REC timer)
    // were applied. `updateUIForState` fires on every heartbeat tick and
    // upload-queue callback — if we re-ran `applyStateVisuals` each time
    // the REC timer would reset to 0.0 s and the pulse animation would be
    // torn down and re-added every beat. Gating on this sentinel means
    // transition-side-effects run exactly once per state change, while
    // pure label/colour updates (Server / FPS / Upload / Last) still
    // refresh every call.
    private var lastRenderedState: AppState?

    // Haptic feedback generators. Kept as properties so prepare() is
    // honored (trigger latency drops from ~100 ms to <20 ms).
    private let armHaptic = UIImpactFeedbackGenerator(style: .light)
    private let startRecHaptic = UIImpactFeedbackGenerator(style: .medium)
    private let endRecHaptic = UINotificationFeedbackGenerator()

    // System-sound IDs used at recording start/end. 1113 / 1114 are short
    // iOS system tones with audibly different pitches, so the operator
    // can distinguish start vs end without looking at the screen.
    private let startRecSoundID: SystemSoundID = 1113
    private let endRecSoundID: SystemSoundID = 1114

    // MARK: - Lifecycle

    // Camera preview assumes sensor long-edge → horizontal pitcher-to-plate
    // direction; switching to portrait would flip the axes and invalidate
    // the ChArUco intrinsic calibration captured in landscape.
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
        [.landscapeLeft, .landscapeRight]
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black

        detectionPool.onFrame = { [weak self] frame in
            self?.handleDetectedFrame(frame)
        }

        settings = AppSettingsStore.load()
        serverConfig = ServerUploader.ServerConfig(serverIP: settings.serverIP, serverPort: settings.serverPort)

        recorder = PitchRecorder()
        recorder.setCameraId(settings.cameraRole)
        recorder.onRecordingStarted = { [weak self] idx in
            DispatchQueue.main.async {
                guard let self else { return }
                // Keep the "尚未時間校正" warning up while recording — the
                // upload will still go but the server will skip triangulation,
                // and the operator needs to keep seeing why.
                if self.serverTimeSyncConfirmed {
                    self.warningLabel.isHidden = true
                }
                // Start-of-recording feedback: short tone + a medium
                // haptic so the operator registers the transition even
                // if looking away from the screen.
                AudioServicesPlaySystemSound(self.startRecSoundID)
                self.startRecHaptic.impactOccurred()
            }
        }
        recorder.onCycleComplete = { [weak self] payload in
            guard let self else { return }
            let finishingClip = self.clipRecorder
            self.clipRecorder = nil
            if payload.sync_id != nil {
                self.lastSyncAnchor = nil
                self.healthMonitor?.updateTimeSyncId(nil)
                DispatchQueue.main.async { self.updateUIForState() }
            }
            log.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) has_clip=\(finishingClip != nil)")
            // End-of-recording feedback — haptic + system sound so the
            // operator knows the cycle finished without looking down.
            DispatchQueue.main.async {
                AudioServicesPlaySystemSound(self.endRecSoundID)
                self.endRecHaptic.notificationOccurred(.success)
            }
            // Phase 1 of the iOS decoupling refactor: intrinsics /
            // homography / image dims no longer ride along on the pitch
            // payload — server reads them from its calibration DB
            // (seeded by CalibrationViewController's POST /calibration).
            // Upload shape is now just session-level metadata + frames.
            if let finishingClip {
                finishingClip.finish { [weak self] videoURL in
                    self?.handleFinishedClip(enriched: payload, videoURL: videoURL)
                }
            } else {
                // Degenerate path: clip bootstrap failed before any MOV
                // existed. `handleFinishedClip` falls back to the advisory
                // live-detection buffer so the cycle isn't silently lost.
                self.handleFinishedClip(enriched: payload, videoURL: nil)
            }
        }

        do {
            try payloadStore.ensureDirectory()
        } catch {
            lastUploadStatusText = "暫存初始化失敗 · \(error.localizedDescription)"
        }

        uploader = ServerUploader(config: serverConfig)
        uploadQueue = PayloadUploadQueue(store: payloadStore, uploader: uploader)
        analysisQueue = AnalysisUploadQueue(store: analysisStore, uploader: uploader)
        wireUploadQueueCallbacks()
        wireAnalysisQueueCallbacks()
        // Rehydrate whatever payloads are sitting in Documents from a
        // previous run (or a prior cycle that hit a transient network
        // error) and kick the worker. Done at viewDidLoad so the queue
        // lifecycle is decoupled from session arm/disarm — cached pitches
        // upload as soon as the server is reachable, not "next time the
        // operator arms a session".
        try? uploadQueue.reloadPending()
        uploadQueue.processNextIfNeeded()
        try? analysisStore.ensureDirectory()
        try? analysisQueue.reloadPending()
        analysisQueue.processNextIfNeeded()

        healthMonitor = ServerHealthMonitor(
            baseIntervalS: 1.0
        )
        wireHealthMonitorCallbacks()
        commandRouter = buildCommandRouter()

        setupUI()
        setupPreviewAndCapture()
        setupAudioCapture()
        setupDisplayLink()
        healthMonitor.start()
        connectWebSocket()
        // frameDispatcher needs the ws connection (set up inside connectWebSocket())
        if let wsConn = ws {
            frameDispatcher = LiveFrameDispatcher(
                connection: wsConn,
                cameraId: settings.cameraRole,
                currentSessionId: { [weak self] in self?.currentSessionId },
                currentPaths: { [weak self] in self?.currentSessionPaths ?? [] }
            )
        }
        updateUIForState()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        applyUpdatedSettings()
        updateUIForState()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        // Capture session is gated on the dashboard `preview_requested` flag
        // (Phase 7 power gate) — stays parked until heartbeat says preview
        // is on, so idle phones don't burn camera/mic for nothing.
        healthMonitor.start()
        connectWebSocket()
        displayLink?.isPaused = false
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        healthMonitor.stop()
        disconnectWebSocket()
        displayLink?.isPaused = true
        if state == .timeSyncWaiting {
            cancelTimeSync()
        }
        if state == .mutualSyncing {
            abortMutualSync(reason: "view dismissed")
        }
    }

    deinit {
        // CADisplayLink holds a strong reference to its target — without an
        // explicit invalidate the controller can't deallocate, and the
        // selector keeps firing on main. Pausing in viewWillDisappear is
        // not enough; the link must be invalidated for the retain cycle to
        // break. ServerHealthMonitor.stop() is belt-and-braces —
        // viewWillDisappear already calls it, but cover the "deinit without
        // viewWillDisappear" edge case too.
        displayLink?.invalidate()
        displayLink = nil
        healthMonitor?.stop()
    }

    // MARK: - Recording controls

    /// Dashboard arm landed — spin up the capture session at 240 fps and
    /// move to `.recording`. Session was parked (stopped) in standby to keep
    /// the phone cool, so this is both a start *and* an fps swap. ClipRecorder
    /// is *not* created here; we defer that to the first captureOutput so we
    /// can use the real pixel-buffer dimensions instead of the
    /// Settings-declared 1920×1080. The PitchRecorder is also started from
    /// captureOutput, once the first appended sample's session-clock PTS is
    /// known.
    func enterRecordingMode() {
        guard state == .standby else { return }
        // Snapshot the server-minted session id at arm time and freeze it
        // into the frame-state box. `self.currentSessionId` may flip during
        // the ~300-500 ms fps-switch window, so the captureOutput path
        // reads this snapshot rather than the live property.
        let snapshotSessionId = currentSessionId
        log.info("camera entering recording session=\(snapshotSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        if !serverTimeSyncConfirmed {
            warningLabel.text = "尚未時間校正，將無法三角化"
            warningLabel.textColor = DesignTokens.Colors.ink
            warningLabel.isHidden = false
            log.warning("arm without server-confirmed time sync — server will skip triangulation")
        } else {
            warningLabel.isHidden = true
        }
        startCapture(at: trackingFps)
        recorder.reset()
        resetBallDetectionState()
        state = .recording
        frameStateBox.update(state: .recording, pendingBootstrap: true, sessionId: snapshotSessionId)
        updateUIForState()
        // Acknowledge arm with a light tap; pre-warm the next two haptics
        // so their first fire isn't lazy-initialised.
        armHaptic.impactOccurred()
        startRecHaptic.prepare()
        endRecHaptic.prepare()
    }

    /// Return to `.standby` after a cycle was flushed (or the recording
    /// never produced a frame between arm and disarm). Clears any live
    /// clip writer on the processing queue so we can't race with an
    /// in-flight `append` from captureOutput. The upload queue is NOT
    /// touched here — a pitch just enqueued by `persistCompletedCycle`
    /// needs to keep marching even after we've flipped back to standby,
    /// and the queue lifecycle is now owned by `viewDidLoad` instead of
    /// the sync-mode enter/exit boundaries.
    func exitRecordingToStandby() {
        log.info("camera exit recording → standby session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public) state=\(Self.stateText(self.state), privacy: .public)")
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        recorder.reset()
        // Bump the detection generation so any closure still running on
        // detectionQueue discards its result; also clears the buffer of
        // anything that landed before this point.
        resetBallDetectionState()
        processingQueue.async { [weak self] in
            self?.clipRecorder?.cancel()
            self?.clipRecorder = nil
        }
        warningLabel.isHidden = true
        // Drop the sensor back to whatever standby currently requires.
        reconcileStandbyCaptureState()
        updateUIForState()
    }

    /// Cycle-complete router. PR61 makes the finalized local MOV the
    /// authority for any on-device result:
    ///   - `cameraOnly` → raw MOV upload only; server remains authoritative.
    ///   - `onDevice` → persist the MOV locally, run post-pass analysis,
    ///     upload frames-only once analysis completes.
    ///   - `dual` → upload the raw MOV immediately AND enqueue a late
    ///     post-pass sidecar upload carrying `frames_on_device`.
    ///
    /// Live detection is drained here only as a degraded fallback when clip
    /// writing failed and no MOV exists to analyze.
    private func handleFinishedClip(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        let advisoryFrames = drainDetectedFrames()
        let paths = currentSessionPaths
        log.info("cycle complete session=\(enriched.session_id, privacy: .public) paths=\(paths.map(\.rawValue).sorted().joined(separator: ","), privacy: .public) advisory_frames=\(advisoryFrames.count) ball_frames=\(advisoryFrames.filter { $0.ball_detected }.count) has_video=\(videoURL != nil)")
        let payload = enriched.withPaths(Array(paths))

        guard let videoURL else {
            handleFallbackCycleWithoutVideo(
                enriched: payload,
                advisoryFrames: advisoryFrames,
                paths: paths
            )
            return
        }

        if paths.contains(.serverPost) {
            handleCameraOnlyCycle(enriched: payload, videoURL: videoURL)
        }
        if paths.contains(.iosPost) {
            let uploadMode: AnalysisJobStore.Job.UploadMode = paths.contains(.serverPost) ? .dualSidecar : .onDevicePrimary
            let analysisVideoURL = paths.contains(.serverPost) ? duplicateVideoForAnalysis(from: videoURL) : videoURL
            if let analysisVideoURL {
                persistAnalysisJob(
                    payload: payload,
                    videoURL: analysisVideoURL,
                    uploadMode: uploadMode
                )
            } else {
                log.error("camera failed to prepare clip for iOS post-pass session=\(payload.session_id, privacy: .public)")
            }
        }
        if paths.contains(.live) {
            frameDispatcher?.dispatchCycleEnd(sessionId: payload.session_id, reason: "disarmed")
        }
        if !paths.contains(.serverPost) && !paths.contains(.iosPost) {
            // Live-only fallback still persists metadata so the cycle
            // remains recoverable if the WS stream degraded mid-flight.
            persistCompletedCycle(payload, videoURL: videoURL)
        }
    }

    private func handleFallbackCycleWithoutVideo(
        enriched: ServerUploader.PitchPayload,
        advisoryFrames: [ServerUploader.FramePayload],
        paths: Set<ServerUploader.DetectionPath>
    ) {
        if paths.contains(.serverPost) {
            persistCompletedCycle(enriched, videoURL: nil)
        } else if paths.contains(.iosPost) {
            persistCompletedCycle(enriched.withFrames(advisoryFrames), videoURL: nil)
        } else {
            persistCompletedCycle(enriched.withFrames(advisoryFrames), videoURL: nil)
        }
    }

    /// Mode-one / dual: ship the full recorded MOV as-is. Server runs
    /// authoritative detection on the received clip.
    private func handleCameraOnlyCycle(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        persistCompletedCycle(enriched, videoURL: videoURL)
    }

    private func persistCompletedCycle(
        _ payload: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        do {
            let fileURL = try payloadStore.save(payload, videoURL: videoURL)
            DispatchQueue.main.async {
                self.lastUploadStatusText = "暫存完成 · 等待上傳"
                self.uploadQueue.enqueue(fileURL)
                // Every cycle-complete returns to standby. Dashboard re-arm
                // happens via the next heartbeat.
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
            }
        } catch {
            log.error("camera cycle persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            // Even if the JSON save failed, drop any orphan tmp video.
            if let videoURL {
                try? FileManager.default.removeItem(at: videoURL)
            }
            DispatchQueue.main.async {
                self.lastUploadStatusText = "暫存失敗 · \(error.localizedDescription)"
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
            }
        }
    }

    private func persistAnalysisJob(
        payload: ServerUploader.PitchPayload,
        videoURL: URL,
        uploadMode: AnalysisJobStore.Job.UploadMode
    ) {
        let job = AnalysisJobStore.Job(uploadMode: uploadMode, pitch: payload)
        do {
            let fileURL = try analysisStore.save(job, videoURL: videoURL)
            DispatchQueue.main.async {
                self.lastUploadStatusText = "暫存完成 · 等待錄後分析"
                self.analysisQueue.enqueue(fileURL)
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
            }
        } catch {
            log.error("camera analysis persist failed session=\(payload.session_id, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
            try? FileManager.default.removeItem(at: videoURL)
            DispatchQueue.main.async {
                self.lastUploadStatusText = "錄後分析暫存失敗 · \(error.localizedDescription)"
                self.returnToStandbyAfterCycle = false
                self.exitRecordingToStandby()
            }
        }
    }

    private func duplicateVideoForAnalysis(from sourceURL: URL) -> URL? {
        let ext = sourceURL.pathExtension.isEmpty ? "mov" : sourceURL.pathExtension
        let dest = FileManager.default.temporaryDirectory
            .appendingPathComponent("analysis_\(UUID().uuidString).\(ext)")
        do {
            try FileManager.default.copyItem(at: sourceURL, to: dest)
            return dest
        } catch {
            log.error("camera analysis clip copy failed src=\(sourceURL.lastPathComponent, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            return nil
        }
    }

    // MARK: - Time calibration (chirp anchor)

    @objc private func onTapTimeCalibration() {
        if state == .timeSyncWaiting {
            cancelTimeSync()
        } else if state == .standby {
            startTimeSync()
        }
        updateUIForState()
    }

    private func startTimeSync(syncId: String? = nil) {
        if let syncId {
            beginTimeSync(syncId: syncId)
            return
        }
        timeSyncClaimGeneration &+= 1
        let generation = timeSyncClaimGeneration
        warningLabel.text = "向伺服器請求時間校正識別碼…"
        warningLabel.isHidden = false
        lastUploadStatusText = "時間校正中 · 取得 sync id"
        uploader.claimTimeSyncIntent { [weak self] result in
            guard let self else { return }
            DispatchQueue.main.async {
                guard generation == self.timeSyncClaimGeneration else { return }
                guard self.state == .standby else { return }
                switch result {
                case .success(let response):
                    self.beginTimeSync(syncId: response.sync_id)
                case .failure(let error):
                    log.error("time-sync claim failed cam=\(self.settings.cameraRole, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
                    self.pendingTimeSyncId = nil
                    self.warningLabel.text = "無法取得同步識別碼：檢查伺服器連線"
                    self.warningLabel.textColor = DesignTokens.Colors.destructive
                    self.warningLabel.isHidden = false
                    self.lastUploadStatusText = "時間校正失敗 · sync id"
                    self.updateUIForState()
                }
            }
        }
    }

    private func beginTimeSync(syncId: String) {
        pendingTimeSyncId = syncId
        guard let detector = chirpDetector else {
            // Mic permission still pending or denied — try once more.
            setupAudioCapture()
            warningLabel.text = "正在啟動麥克風…"
            warningLabel.isHidden = false
            return
        }
        // Session is parked while in standby. Spin it up at the idle fps so
        // the mic starts delivering samples to the chirp detector. 60 fps of
        // video is a free byproduct — cheaper than carving the audio input
        // out to its own session, and the time-sync window is bounded at 15 s.
        startCapture(at: standbyFps)
        detector.reset()
        detector.onChirpDetected = { [weak self] event in
            guard let self else { return }
            DispatchQueue.main.async {
                self.completeTimeSync(event)
            }
        }
        state = .timeSyncWaiting
        frameStateBox.update(state: .timeSyncWaiting, pendingBootstrap: false, sessionId: nil)
        warningLabel.text = "等待同步音頻觸發中… (把兩機並排，第三裝置播 chirp)"
        warningLabel.textColor = DesignTokens.Colors.ink
        warningLabel.isHidden = false
        lastUploadStatusText = "時間校正中 · 等待聲波"

        let work = DispatchWorkItem { [weak self] in
            guard let self, self.state == .timeSyncWaiting else { return }
            self.cancelTimeSync(reason: "timeout")
        }
        timeSyncTimeoutWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 15, execute: work)
    }

    private func cancelTimeSync(reason: String = "cancelled") {
        log.info("camera cancel time-sync reason=\(reason, privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        timeSyncClaimGeneration &+= 1
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        latestChirpSnapshot = nil
        pendingTimeSyncId = nil
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        reconcileStandbyCaptureState()
        lastUploadStatusText = "時間校正 · \(Self.localizedCancelReason(reason))"

        if reason == "timeout" {
            log.warning("camera time-sync timeout cam=\(self.settings.cameraRole, privacy: .public)")
            // Flash a red banner for 3 s so the operator notices the miss;
            // the HUD's warning label is otherwise yellow for the "waiting"
            // state and hiding it immediately on timeout made it easy to
            // miss that the chirp never arrived.
            let originalBg = warningLabel.backgroundColor
            let originalFg = warningLabel.textColor
            warningLabel.backgroundColor = DesignTokens.Colors.destructive
            warningLabel.textColor = DesignTokens.Colors.cardBackground
            warningLabel.text = "時間校正逾時：確認 chirp 音訊與麥克風"
            warningLabel.isHidden = false
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in
                guard let self else { return }
                self.warningLabel.isHidden = true
                self.warningLabel.backgroundColor = originalBg
                self.warningLabel.textColor = originalFg
            }
        } else {
            warningLabel.isHidden = true
        }
        updateUIForState()
    }

    private func completeTimeSync(_ event: AudioChirpDetector.ChirpEvent) {
        guard state == .timeSyncWaiting else { return }
        guard let syncId = pendingTimeSyncId else {
            log.error("camera complete time-sync without sync_id cam=\(self.settings.cameraRole, privacy: .public)")
            cancelTimeSync(reason: "missing_sync_id")
            return
        }
        log.info("camera complete time-sync anchor_frame=\(event.anchorFrameIndex) anchor_ts=\(event.anchorTimestampS) sync_id=\(syncId, privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        pendingTimeSyncId = nil
        lastSyncAnchor = SyncAnchor(syncId: syncId, anchorTimestampS: event.anchorTimestampS)
        // Surface the freshly-acquired anchor to the dashboard via the
        // next heartbeat so the sidebar's "time sync" dot flips green.
        healthMonitor?.updateTimeSyncId(syncId)
        latestChirpSnapshot = nil
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        reconcileStandbyCaptureState()
        warningLabel.isHidden = true
        lastUploadStatusText = "時間校正完成"
        updateUIForState()
    }

    // MARK: - Capture setup

    private func setupPreviewAndCapture() {
        session.beginConfiguration()

        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            dumpAvailableFormats(for: device)
            do {
                try configureCaptureFormat(
                    device,
                    targetWidth: self.currentCaptureWidth,
                    targetHeight: self.currentCaptureHeight,
                    targetFps: standbyFps
                )

                let input = try AVCaptureDeviceInput(device: device)
                if session.canAddInput(input) {
                    session.addInput(input)
                }
            } catch {
                log.error("camera capture format configuration failed error=\(error.localizedDescription, privacy: .public)")
                // TODO: surface error to UI
            }
        }

        videoOutput.setSampleBufferDelegate(self, queue: processingQueue)
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        if session.canAddOutput(videoOutput) {
            session.addOutput(videoOutput)
        }

        session.commitConfiguration()

        let preview = AVCaptureVideoPreviewLayer(session: session)
        // With tracking locked to 16:9 @ 240 fps, show the exact recorded
        // frame instead of cropping it to fill the phone's taller display.
        preview.videoGravity = .resizeAspect
        preview.frame = previewFrame(in: view.bounds)
        // App is locked to landscape (Info.plist). Pin the preview connection
        // to sensor-native angle 0 so the on-screen image matches the raw
        // CVPixelBuffer orientation ArUco calibration consumes; a stale
        // 90° rotation here was why "holding phone landscape" still rendered
        // a portrait-oriented preview.
        if let connection = preview.connection, connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
        // Preview starts hidden — session isn't running yet, and when we
        // later stopCapture() AVCaptureVideoPreviewLayer keeps its last
        // frame around, so toggling isHidden is what actually gives us
        // black in standby (view.backgroundColor is .black).
        preview.isHidden = true
        view.layer.insertSublayer(preview, at: 0)
        previewLayer = preview
    }

    private func previewFrame(in bounds: CGRect) -> CGRect {
        let aspect = CGSize(width: currentCaptureWidth, height: currentCaptureHeight)
        return AVMakeRect(aspectRatio: aspect, insideRect: bounds).integral
    }

    private func dumpAvailableFormats(for device: AVCaptureDevice) {
        log.info("camera format dump begin device=\(device.localizedName, privacy: .public) uniqueID=\(device.uniqueID, privacy: .public)")
        for (index, format) in device.formats.enumerated() {
            let desc = format.formatDescription
            let dims = CMVideoFormatDescriptionGetDimensions(desc)
            let width = Int(dims.width)
            let height = Int(dims.height)
            let aspect = String(format: "%.4f", Double(width) / Double(height))
            let fpsRanges = format.videoSupportedFrameRateRanges.map { range in
                String(format: "%.0f-%.0f", range.minFrameRate, range.maxFrameRate)
            }.joined(separator: ",")
            let supports120 = format.videoSupportedFrameRateRanges.contains { range in
                range.minFrameRate <= 120 && range.maxFrameRate >= 120
            }
            let supports240 = format.videoSupportedFrameRateRanges.contains { range in
                range.minFrameRate <= 240 && range.maxFrameRate >= 240
            }
            let mediaSubType = CMFormatDescriptionGetMediaSubType(desc)
            let isBinned = format.isVideoBinned
            let fov = format.videoFieldOfView
            let fovText = String(format: "%.3f", fov)
            let subTypeText = String(format: "%08X", mediaSubType)
            log.info(
                "camera format[\(index)] \(width)x\(height) aspect=\(aspect, privacy: .public) fps_ranges=[\(fpsRanges, privacy: .public)] supports120=\(supports120, privacy: .public) supports240=\(supports240, privacy: .public) fov_deg=\(fovText, privacy: .public) binned=\(isBinned, privacy: .public) subtype=\(subTypeText, privacy: .public)"
            )
        }
        log.info("camera format dump end count=\(device.formats.count)")
    }

    enum CaptureFormatError: LocalizedError {
        case noMatchingFormat(width: Int, height: Int, fps: Double)

        var errorDescription: String? {
            switch self {
            case .noMatchingFormat(let w, let h, let fps):
                return "No AVCaptureDevice.Format matches \(w)×\(h) @ \(Int(fps)) fps"
            }
        }
    }

    private func configureCaptureFormat(
        _ device: AVCaptureDevice,
        targetWidth: Int,
        targetHeight: Int,
        targetFps: Double
    ) throws {
        var candidates: [(index: Int, format: AVCaptureDevice.Format, maxFrameRate: Double)] = []
        for (index, format) in device.formats.enumerated() {
            let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            let w = Int(dims.width)
            let h = Int(dims.height)
            let matchesRes = (w == targetWidth && h == targetHeight) || (w == targetHeight && h == targetWidth)
            let matchingRanges = format.videoSupportedFrameRateRanges.filter { range in
                range.minFrameRate <= targetFps && range.maxFrameRate >= targetFps
            }
            if matchesRes, let bestRange = matchingRanges.max(by: { $0.maxFrameRate < $1.maxFrameRate }) {
                candidates.append((index: index, format: format, maxFrameRate: bestRange.maxFrameRate))
            }
        }
        let selectedCandidate = candidates.max { lhs, rhs in
            let lhsFov = lhs.format.videoFieldOfView
            let rhsFov = rhs.format.videoFieldOfView
            if lhsFov != rhsFov {
                return lhsFov < rhsFov
            }
            if lhs.maxFrameRate != rhs.maxFrameRate {
                return lhs.maxFrameRate < rhs.maxFrameRate
            }
            return lhs.index > rhs.index
        }
        let selected = selectedCandidate?.format
        guard let selected else {
            // Dump every (w×h @ fps_range) the device offers so Console.app
            // shows exactly why the search missed — usually either the target
            // resolution is not supported at all on this device, or it is
            // supported but not at the requested fps.
            for (i, format) in device.formats.enumerated() {
                let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
                let ranges = format.videoSupportedFrameRateRanges
                    .map { "\($0.minFrameRate)-\($0.maxFrameRate)" }
                    .joined(separator: ",")
                log.error("camera format[\(i)] \(dims.width)x\(dims.height) fps_ranges=[\(ranges, privacy: .public)]")
            }
            log.error("camera no matching format target=\(targetWidth)x\(targetHeight)@\(targetFps)fps device=\(device.localizedName, privacy: .public) uniqueID=\(device.uniqueID, privacy: .public)")
            throw CaptureFormatError.noMatchingFormat(width: targetWidth, height: targetHeight, fps: targetFps)
        }

        try device.lockForConfiguration()
        defer { device.unlockForConfiguration() }

        device.activeFormat = selected
        if let selectedCandidate {
            let selectedDims = CMVideoFormatDescriptionGetDimensions(selected.formatDescription)
            log.info(
                "camera format selected idx=\(selectedCandidate.index) \(selectedDims.width)x\(selectedDims.height) target_fps=\(targetFps) fov_deg=\(String(format: "%.3f", selected.videoFieldOfView), privacy: .public) max_fps=\(selectedCandidate.maxFrameRate, privacy: .public) binned=\(selected.isVideoBinned, privacy: .public)"
            )
        }
        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        device.activeVideoMinFrameDuration = frameDuration
        device.activeVideoMaxFrameDuration = frameDuration
        let appliedMaxExposureS = try applyExposureConfiguration(device, format: selected, targetFps: targetFps)

        horizontalFovRadians = Double(device.activeFormat.videoFieldOfView) * Double.pi / 180.0
        let applied = device.activeVideoMinFrameDuration
        let appliedFps = applied.value > 0
            ? Double(applied.timescale) / Double(applied.value)
            : targetFps
        let selectedDims = CMVideoFormatDescriptionGetDimensions(selected.formatDescription)
        setAppliedCaptureTelemetry(
            widthPx: Int(selectedDims.width),
            heightPx: Int(selectedDims.height),
            appliedFps: appliedFps,
            formatFovDeg: Double(selected.videoFieldOfView),
            formatIndex: selectedCandidate?.index,
            isVideoBinned: selected.isVideoBinned,
            appliedMaxExposureS: appliedMaxExposureS
        )
    }

    private func applyExposureConfiguration(
        _ device: AVCaptureDevice,
        format: AVCaptureDevice.Format,
        targetFps: Double
    ) throws -> Double? {
        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        // Cap the AE exposure time to the target frame duration. Without this,
        // iOS lengthens individual exposures in low light to brighten the
        // image, which drags the effective capture rate down (a room that
        // wants 70 ms exposures drops a "240 fps" session to ~14 fps). Capped
        // AE keeps the sensor locked to the target rate and compensates with
        // ISO — noisier in dim rooms, but frame rate holds.
        let lo = format.minExposureDuration
        let hi = format.maxExposureDuration
        let requestedCap = requestedExposureCapDuration(targetFps: targetFps) ?? frameDuration
        let capped: CMTime
        if CMTimeCompare(requestedCap, lo) < 0 {
            capped = lo
        } else if CMTimeCompare(requestedCap, hi) > 0 {
            capped = hi
        } else {
            capped = requestedCap
        }
        device.activeMaxExposureDuration = capped
        device.exposureMode = .continuousAutoExposure
        let cappedExposureS = CMTimeGetSeconds(capped)
        let exposureCapText = cappedExposureS > 0
            ? String(format: "1/%.0f", 1.0 / cappedExposureS)
            : "n/a"
        log.info(
            "camera exposure configured target_fps=\(targetFps) mode=\(self.trackingExposureCapMode.rawValue, privacy: .public) max_exposure=\(exposureCapText, privacy: .public) iso_range=[\(format.minISO, privacy: .public),\(format.maxISO, privacy: .public)] current_iso=\(device.iso, privacy: .public)"
        )
        return cappedExposureS.isFinite ? cappedExposureS : nil
    }

    private func setAppliedCaptureTelemetry(
        widthPx: Int,
        heightPx: Int,
        appliedFps: Double,
        formatFovDeg: Double?,
        formatIndex: Int?,
        isVideoBinned: Bool?,
        appliedMaxExposureS: Double?
    ) {
        captureTelemetryLock.lock()
        appliedCaptureWidthPx = widthPx
        appliedCaptureHeightPx = heightPx
        self.appliedCaptureFps = appliedFps
        appliedFormatFovDeg = formatFovDeg
        appliedFormatIndex = formatIndex
        appliedFormatIsVideoBinned = isVideoBinned
        self.appliedMaxExposureS = appliedMaxExposureS
        captureTelemetryLock.unlock()
    }

    private func currentCaptureTelemetry(targetFps: Double) -> ServerUploader.CaptureTelemetry {
        captureTelemetryLock.lock()
        defer { captureTelemetryLock.unlock() }
        return ServerUploader.CaptureTelemetry(
            width_px: latestImageWidth > 0 ? latestImageWidth : appliedCaptureWidthPx,
            height_px: latestImageHeight > 0 ? latestImageHeight : appliedCaptureHeightPx,
            target_fps: targetFps,
            applied_fps: appliedCaptureFps,
            format_fov_deg: appliedFormatFovDeg,
            format_index: appliedFormatIndex,
            is_video_binned: appliedFormatIsVideoBinned,
            tracking_exposure_cap: trackingExposureCapMode.rawValue,
            applied_max_exposure_s: appliedMaxExposureS
        )
    }

    private func requestedExposureCapDuration(targetFps: Double) -> CMTime? {
        guard abs(targetFps - trackingFps) < 0.5,
              let seconds = trackingExposureCapMode.maxExposureSeconds else {
            return nil
        }
        return CMTime(seconds: seconds, preferredTimescale: 1_000_000)
    }

    private func currentTargetFps() -> Double {
        switch state {
        case .recording:
            return trackingFps
        case .standby, .timeSyncWaiting, .mutualSyncing, .uploading:
            return standbyFps
        }
    }

    /// Swap the active capture format to a new frame rate at the current
    /// fixed resolution. Used for the idle↔tracking FPS transition
    /// (`standbyFps` ↔ `trackingFps`) triggered by enter/exit of sync mode,
    /// and by the resolution-change path in `applyUpdatedSettings`. The
    /// format swap requires `stopRunning → activeFormat = X → startRunning`,
    /// which blocks the session for ~300-500 ms — deliberately called only
    /// at moments with no time-critical frame work in flight (entering
    /// sync while the user is placing the ball, or exiting back to idle).
    /// Swap the capture format to a new fps on the session queue. Blocks
    /// `~300-500 ms` inside `stopRunning → activeFormat = X → startRunning`
    /// — *MUST* run off-main so the HUD / ReadyCard refresh and pending UI
    /// gestures aren't stalled. All callers are fire-and-forget (no caller
    /// needs the new fps to have applied synchronously).
    /// Apply a dashboard-pushed capture resolution. Only reachable via the
    /// heartbeat handler, which guards to `.standby` — rebuilding the
    /// capture session mid-recording would lose the clip. Updates
    /// `currentCaptureHeight` and reconfigures the live session.
    private func applyServerCaptureHeight(_ newHeight: Int) {
        guard let device = currentCaptureDevice else { return }
        // 16:9 standard widths for the allowed heights.
        let width: Int
        switch newHeight {
        case 720:  width = 1280
        case 1080: width = 1920
        default:
            log.warning("ignore unsupported capture_height \(newHeight)")
            return
        }
        currentCaptureHeight = newHeight
        previewLayer?.frame = previewFrame(in: view.bounds)
        previewLayer?.isHidden = false
        sessionQueue.async { [weak self] in
            guard let self else { return }
            let wasRunning = self.session.isRunning
            if wasRunning { self.session.stopRunning() }
            defer { if wasRunning { self.session.startRunning() } }
            do {
                try self.configureCaptureFormat(
                    device,
                    targetWidth: width,
                    targetHeight: newHeight,
                    targetFps: self.standbyFps
                )
                log.info("camera resolution swapped to \(width)x\(newHeight) from server push")
            } catch {
                log.error("camera resolution swap failed target=\(width)x\(newHeight) error=\(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async {
                    self.warningLabel.text = "解析度切換失敗 (\(newHeight)p 不支援)"
                    self.warningLabel.isHidden = false
                }
            }
        }
    }

    /// Encode the given pixel buffer at its NATIVE resolution (no
    /// downsample, no scale) as a high-quality JPEG and POST it to the
    /// server's calibration-frame endpoint. Runs on the capture queue so
    /// as not to block the main thread; hop off-queue for the HTTP call
    /// so the capture queue doesn't stall on network latency.
    private func uploadCalibrationFrame(_ pixelBuffer: CVPixelBuffer) {
        let w = CVPixelBufferGetWidth(pixelBuffer)
        let h = CVPixelBufferGetHeight(pixelBuffer)
        let cam = settings.cameraRole
        // CIImage is retain-counted + thread-safe; constructing here on
        // the capture queue is fine. The encode itself we hop onto a
        // utility queue so the next sample isn't delayed.
        let ci = CIImage(cvPixelBuffer: pixelBuffer)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let ctx = CIContext(options: [.useSoftwareRenderer: false])
            guard let cs = CGColorSpace(name: CGColorSpace.sRGB),
                  let jpeg = ctx.jpegRepresentation(
                      of: ci, colorSpace: cs,
                      options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.9]
                  )
            else {
                log.warning("calibration frame: native-res JPEG encode failed")
                return
            }
            log.info("calibration frame: encoded \(w)x\(h) bytes=\(jpeg.count)")
            let path = "/camera/\(cam)/calibration_frame"
            self.uploader.postRawJPEG(path: path, jpeg: jpeg) { result in
                switch result {
                case .success:
                    log.info("calibration frame upload ok cam=\(cam, privacy: .public) bytes=\(jpeg.count)")
                case .failure(let err):
                    log.error("calibration frame upload failed cam=\(cam, privacy: .public) err=\(err.localizedDescription, privacy: .public)")
                }
            }
        }
    }

    private func switchCaptureFps(_ targetFps: Double) {
        guard let device = currentCaptureDevice else { return }
        // Surface the preview synchronously on main so there's no window
        // where the layer is hidden while the sessionQueue hop is pending.
        previewLayer?.isHidden = false
        sessionQueue.async { [weak self] in
            guard let self else { return }
            let wasRunning = self.session.isRunning
            if wasRunning { self.session.stopRunning() }
            defer { if wasRunning { self.session.startRunning() } }
            do {
                try self.configureCaptureFormat(
                    device,
                    targetWidth: self.currentCaptureWidth,
                    targetHeight: self.currentCaptureHeight,
                    targetFps: targetFps
                )
                // Read back the actually-applied rate so an operator can
                // tell from logs whether the sensor is honouring our
                // request. Field of view can differ across same-resolution
                // formats, so log it per switch to catch accidental
                // selection of a narrower crop path.
                let applied = device.activeVideoMinFrameDuration
                let appliedFps = applied.value > 0
                    ? Double(applied.timescale) / Double(applied.value)
                    : 0
                log.info("camera fps switched target=\(targetFps) applied=\(appliedFps) fov_rad=\(self.horizontalFovRadians)")
            } catch {
                log.error("camera fps switch failed target=\(targetFps) error=\(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async {
                    self.warningLabel.text = "FPS 切換失敗 (\(Int(targetFps))fps 不支援)"
                    self.warningLabel.isHidden = false
                }
            }
        }
    }

    /// Spin the capture session up at `targetFps`. Used when leaving the
    /// parked standby state — either for an armed recording (`trackingFps`)
    /// or a manual 時間校正 window (`standbyFps`). If the session is already
    /// running, delegates to `switchCaptureFps` so the fps swap still
    /// happens. Safe to call from any state; no-op if no capture device.
    /// Lifecycle ops run on `sessionQueue` so the caller (main thread)
    /// doesn't block on `startRunning`.
    private func startCapture(at targetFps: Double) {
        guard let device = currentCaptureDevice else { return }
        previewLayer?.isHidden = false
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if self.session.isRunning {
                // Already running — delegate to fps swap, directly on this
                // queue (we're already on sessionQueue; no dispatch needed).
                self.reconfigureActiveSession(device: device, targetFps: targetFps)
                return
            }
            do {
                try self.configureCaptureFormat(
                    device,
                    targetWidth: self.currentCaptureWidth,
                    targetHeight: self.currentCaptureHeight,
                    targetFps: targetFps
                )
                self.session.startRunning()
                log.info("camera capture started fps=\(targetFps)")
            } catch {
                log.error("camera capture start failed error=\(error.localizedDescription, privacy: .public)")
                DispatchQueue.main.async {
                    self.warningLabel.text = "相機啟動失敗 (\(Int(targetFps))fps)"
                    self.warningLabel.isHidden = false
                }
            }
        }
    }

    /// Reconcile camera hardware to the current standby owner. Transient
    /// flows (time sync, mutual sync, recording) can legitimately defer
    /// preview on/off decisions until they end; when we return to
    /// `.standby`, resolve that deferred intent in one place so the iOS
    /// capture session never drifts away from dashboard preview state.
    private func reconcileStandbyCaptureState() {
        if previewRequestedByServer || calibrationFrameCaptureArmed {
            startCapture(at: standbyFps)
        } else {
            stopCapture()
        }
    }

    /// Inner of `switchCaptureFps` (same body, no dispatch) for callers that
    /// already ran themselves onto `sessionQueue`. Keeps the stop → apply
    /// → start sequence atomic inside one queue task.
    private func reconfigureActiveSession(device: AVCaptureDevice, targetFps: Double) {
        let wasRunning = session.isRunning
        if wasRunning { session.stopRunning() }
        defer { if wasRunning { session.startRunning() } }
        do {
            try configureCaptureFormat(
                device,
                targetWidth: self.currentCaptureWidth,
                targetHeight: self.currentCaptureHeight,
                targetFps: targetFps
            )
            let applied = device.activeVideoMinFrameDuration
            let appliedFps = applied.value > 0
                ? Double(applied.timescale) / Double(applied.value)
                : 0
            log.info("camera fps switched target=\(targetFps) applied=\(appliedFps) fov_rad=\(self.horizontalFovRadians)")
        } catch {
            log.error("camera fps switch failed target=\(targetFps) error=\(error.localizedDescription, privacy: .public)")
            DispatchQueue.main.async {
                self.warningLabel.text = "FPS 切換失敗 (\(Int(targetFps))fps 不支援)"
                self.warningLabel.isHidden = false
            }
        }
    }

    /// Park the capture session. Camera + mic hardware go idle so the phone
    /// doesn't heat up under a long idle preview — only heartbeat keeps
    /// running in standby. Safe to call when already stopped. The fps HUD
    /// reset stays on main (UI state); the hardware stop is off-main so we
    /// don't hit the `startRunning/stopRunning on main thread` perf check.
    private func stopCapture() {
        // Hide immediately on main so the last rendered frame doesn't linger
        // while the sessionQueue hop runs stopRunning — the preview layer
        // keeps its last frame until a new sample arrives, which looks
        // identical to a still-live preview.
        previewLayer?.isHidden = true
        fpsEstimate = 0
        framesSinceLastFpsTick = 0
        lastFrameTimestampForFps = CACurrentMediaTime()
        sessionQueue.async { [weak self] in
            guard let self else { return }
            guard self.session.isRunning else { return }
            self.session.stopRunning()
            log.info("camera capture stopped")
        }
    }

    private var currentCaptureDevice: AVCaptureDevice? {
        (session.inputs.compactMap { $0 as? AVCaptureDeviceInput }
            .first(where: { $0.device.hasMediaType(.video) }))?.device
    }

    // MARK: - Audio (chirp) capture

    private func setupAudioCapture() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            guard let self else { return }
            DispatchQueue.main.async {
                if granted {
                    self.configureAudioCapture()
                } else {
                    log.error("camera mic permission denied cam=\(self.settings.cameraRole, privacy: .public)")
                    self.lastUploadStatusText = "麥克風未授權 · 無法時間校正"
                    self.updateUIForState()
                }
            }
        }
    }

    private func configureAudioCapture() {
        guard chirpDetector == nil else { return }
        guard let mic = AVCaptureDevice.default(for: .audio) else { return }
        let input: AVCaptureDeviceInput
        do {
            input = try AVCaptureDeviceInput(device: mic)
        } catch {
            log.error("camera mic input init failed error=\(error.localizedDescription, privacy: .public)")
            lastUploadStatusText = "麥克風啟動失敗 · \(error.localizedDescription)"
            return
        }

        // Force a flat-response mic path. iOS defaults for AVCaptureSession
        // audio enable voice preprocessing (AGC, beamforming, AEC) which
        // aggressively attenuates anything not shaped like human speech —
        // our 2–8 kHz chirp sweep looks exactly like "noise" to that
        // pipeline and gets crushed to near-silence (field observation:
        // inp_peak < 0.03 with a chirp that a human hears at full volume).
        // `.measurement` mode disables all of it; `.playAndRecord` matches
        // what MutualSyncAudio already uses so the two sync paths don't
        // fight each other over category ownership. Must set
        // `automaticallyConfiguresApplicationAudioSession = false` or the
        // capture session will stomp our category on `startRunning`.
        session.automaticallyConfiguresApplicationAudioSession = false
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(
                .playAndRecord,
                mode: .measurement,
                options: [.defaultToSpeaker, .allowBluetoothA2DP]
            )
            try audioSession.setActive(true, options: [])
            log.info("camera AVAudioSession set to .measurement for flat mic response")
        } catch {
            log.error("camera AVAudioSession config failed error=\(error.localizedDescription, privacy: .public)")
        }

        session.beginConfiguration()
        guard session.canAddInput(input) else {
            session.commitConfiguration()
            log.error("camera session rejected audio input cam=\(self.settings.cameraRole, privacy: .public)")
            lastUploadStatusText = "擷取階段拒絕麥克風"
            return
        }
        session.addInput(input)

        let output = AVCaptureAudioDataOutput()
        guard session.canAddOutput(output) else {
            session.removeInput(input)
            session.commitConfiguration()
            return
        }
        session.addOutput(output)

        // Phase 3: dashboard pushes chirp_threshold via WS settings;
        // 0.18 is the bootstrap default used before the first payload lands.
        let detector = AudioChirpDetector(threshold: 0.18)
        output.setSampleBufferDelegate(detector, queue: detector.deliveryQueue)
        session.commitConfiguration()

        audioInput = input
        audioOutput = output
        chirpDetector = detector
    }

    // MARK: - Layout + overlays

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = previewFrame(in: view.bounds)
        // Border path follows the root view bounds; regenerated on every
        // layout pass so rotation / safe-area changes stay in sync.
        stateBorderLayer.frame = view.bounds
        stateBorderLayer.path = UIBezierPath(rect: view.bounds).cgPath
    }

    private func setupDisplayLink() {
        let link = CADisplayLink(target: self, selector: #selector(handleDisplayTick))
        link.add(to: .main, forMode: .common)
        displayLink = link
    }

    @objc private func handleDisplayTick() {
        updateChirpDebugOverlay()
    }

    private func updateChirpDebugOverlay() {
        guard state == .timeSyncWaiting, let detector = chirpDetector else { return }
        latestChirpSnapshot = detector.lastSnapshot
    }

    // MARK: - Server health + upload queue wiring

    private func webSocketURL() -> URL? {
        guard
            let host = serverConfig.serverIP.addingPercentEncoding(withAllowedCharacters: .urlHostAllowed),
            !host.isEmpty
        else { return nil }
        var comps = URLComponents()
        comps.scheme = "ws"
        comps.host = host
        comps.port = Int(serverConfig.serverPort)
        // path is built inside ServerWebSocketConnection; just build the base URL here
        return comps.url
    }

    private func connectWebSocket() {
        guard let baseURL = webSocketURL() else { return }
        if ws == nil {
            let conn = ServerWebSocketConnection(baseURL: baseURL, cameraId: settings.cameraRole)
            conn.delegate = self
            ws = conn
        }
        ws?.connect(initialHello: [
            "type": "hello",
            "cam": settings.cameraRole,
            "session_id": currentSessionId as Any,
            "time_sync_id": lastSyncId as Any,
            "sync_anchor_timestamp_s": lastSyncAnchorTimestampS as Any,
        ])
    }

    private func disconnectWebSocket() {
        ws?.disconnect()
        ws = nil
    }

    private func sendWebSocketJSON(_ obj: [String: Any]) {
        ws?.send(obj)
    }

    private func handlePushedTrackingExposureCap(_ modeStr: String) {
        let exposureMode = ServerUploader.TrackingExposureCapMode(rawValue: modeStr) ?? .frameDuration
        if self.lastServerTrackingExposureCapMode != exposureMode {
            self.lastServerTrackingExposureCapMode = exposureMode
            self.trackingExposureCapMode = exposureMode
            if let device = self.currentCaptureDevice {
                self.sessionQueue.async { [weak self] in
                    guard let self else { return }
                    do {
                        try device.lockForConfiguration()
                        defer { device.unlockForConfiguration() }
                        let appliedMaxExposureS = try self.applyExposureConfiguration(
                            device,
                            format: device.activeFormat,
                            targetFps: self.currentTargetFps()
                        )
                        let dims = CMVideoFormatDescriptionGetDimensions(device.activeFormat.formatDescription)
                        let applied = device.activeVideoMinFrameDuration
                        let appliedFps = applied.value > 0
                            ? Double(applied.timescale) / Double(applied.value)
                            : self.currentTargetFps()
                        self.setAppliedCaptureTelemetry(
                            widthPx: Int(dims.width),
                            heightPx: Int(dims.height),
                            appliedFps: appliedFps,
                            formatFovDeg: Double(device.activeFormat.videoFieldOfView),
                            formatIndex: self.appliedFormatIndex,
                            isVideoBinned: device.activeFormat.isVideoBinned,
                            appliedMaxExposureS: appliedMaxExposureS
                        )
                        log.info("tracking exposure cap hot-applied from server: \(exposureMode.rawValue, privacy: .public)")
                    } catch {
                        log.error("tracking exposure cap apply failed mode=\(exposureMode.rawValue, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
                    }
                }
            }
        }
    }

    private func buildCommandRouter() -> CameraCommandRouter {
        CameraCommandRouter(
            dependencies: .init(
                getState: { [weak self] in self?.state ?? .standby },
                getCameraRole: { [weak self] in self?.settings.cameraRole ?? "?" },
                healthMonitor: healthMonitor,
                getCurrentSessionPaths: { [weak self] in self?.currentSessionPaths ?? [] },
                setCurrentSessionId: { [weak self] in self?.currentSessionId = $0 },
                setCurrentSessionPaths: { [weak self] in self?.currentSessionPaths = $0 },
                refreshModeLabel: { [weak self] in self?.refreshModeLabel() },
                startTimeSync: { [weak self] syncId in self?.startTimeSync(syncId: syncId) },
                applyMutualSync: { [weak self] syncId in self?.applyMutualSync(syncId: syncId) },
                applyRemoteArm: { [weak self] in self?.applyRemoteArm() },
                applyRemoteDisarm: { [weak self] in self?.applyRemoteDisarm() },
                updateTimeSyncServerState: { [weak self] confirmed, syncId in
                    self?.updateServerTimeSyncState(confirmed: confirmed, syncId: syncId)
                },
                chirpThresholdDidPush: { [weak self] threshold in
                    self?.applyPushedChirpThreshold(threshold)
                },
                mutualSyncThresholdDidPush: { [weak self] threshold in
                    self?.applyPushedMutualSyncThreshold(threshold)
                },
                heartbeatIntervalDidPush: { [weak self] interval in
                    self?.applyPushedHeartbeatInterval(interval)
                },
                handleTrackingExposureCap: { [weak self] cap in
                    self?.handlePushedTrackingExposureCap(cap)
                },
                currentCaptureHeight: { [weak self] in self?.currentCaptureHeight ?? AppSettings.captureHeightFixed },
                applyServerCaptureHeight: { [weak self] height in self?.applyServerCaptureHeight(height) },
                isPreviewRequested: { [weak self] in self?.previewRequestedByServer ?? false },
                setPreviewRequested: { [weak self] in self?.previewRequestedByServer = $0 },
                ensurePreviewUploader: { [weak self] in self?.ensurePreviewUploader() },
                resetPreviewUploader: { [weak self] in self?.previewUploader?.reset() },
                startStandbyCapture: { [weak self] in self?.startCapture(at: self?.standbyFps ?? 60) },
                stopCapture: { [weak self] in self?.stopCapture() },
                isCalibrationFrameCaptureArmed: { [weak self] in self?.calibrationFrameCaptureArmed ?? false },
                setCalibrationFrameCaptureArmed: { [weak self] in self?.calibrationFrameCaptureArmed = $0 }
            )
        )
    }

    private func refreshModeLabel() {
        let pathLabel = currentSessionPaths.map(\.displayLabel).sorted().joined(separator: " + ")
        let previewState = previewRequestedByServer ? "REMOTE ON" : (session.isRunning ? "LOCAL ACTIVE" : "OFF")
        previewLabel.text = "PREVIEW · \(previewState) · \(pathLabel.uppercased())"
    }

    private func updateServerTimeSyncState(confirmed: Bool, syncId: String?) {
        if serverTimeSyncConfirmed != confirmed || serverTimeSyncId != syncId {
            serverTimeSyncConfirmed = confirmed
            serverTimeSyncId = syncId
            DispatchQueue.main.async { self.updateUIForState() }
        }
    }

    private func applyPushedChirpThreshold(_ threshold: Double) {
        guard lastServerChirpThreshold != threshold else { return }
        lastServerChirpThreshold = threshold
        DispatchQueue.main.async {
            self.chirpDetector?.setThreshold(Float(threshold))
            log.info("quick-chirp threshold hot-applied from server: \(threshold)")
        }
    }

    private func applyPushedMutualSyncThreshold(_ threshold: Double) {
        // iOS no longer runs the mutual-sync matched filter; cache only.
        if lastServerMutualThreshold != threshold {
            lastServerMutualThreshold = threshold
        }
    }

    private func applyPushedHeartbeatInterval(_ interval: Double) {
        guard lastServerHeartbeatInterval != interval else { return }
        lastServerHeartbeatInterval = interval
        DispatchQueue.main.async {
            self.healthMonitor.updateBaseInterval(interval)
            log.info("heartbeat interval hot-applied: \(interval)s")
        }
    }

    private func ensurePreviewUploader() {
        if previewUploader == nil {
            previewUploader = PreviewUploader(uploader: uploader, cameraId: settings.cameraRole)
        }
    }

    private func wireHealthMonitorCallbacks() {
        healthMonitor.onStatusChanged = { [weak self] _, _ in
            self?.updateUIForState()
        }
        healthMonitor.sendWSHeartbeat = { [weak self] timeSyncId in
            guard let self else { return }
            var payload: [String: Any] = [
                "type": "heartbeat",
                "cam": self.settings.cameraRole,
                "t_session_s": CACurrentMediaTime(),
                "time_sync_id": timeSyncId as Any,
                "sync_anchor_timestamp_s": self.lastSyncAnchorTimestampS as Any,
            ]
            // Live quick-chirp telemetry for the /sync debug dashboard.
            // Only attached while the detector is actively listening
            // (state == .timeSyncWaiting) — outside that window the
            // detector is torn down, so the values would be stale.
            if self.state == .timeSyncWaiting, let s = self.chirpDetector?.lastSnapshot {
                payload["sync_telemetry"] = [
                    "mode": "quick_chirp",
                    "armed": s.armed,
                    "input_rms": s.inputRMS,
                    "input_peak": s.inputPeak,
                    "up_peak": s.lastPeak,
                    "down_peak": s.lastDownPeak,
                    "cfar_up_floor": s.cfarUpFloor,
                    "cfar_down_floor": s.cfarDownFloor,
                    "threshold": s.threshold,
                    "pending_up": s.pendingUp,
                ]
            }
            self.sendWebSocketJSON(payload)
        }
        healthMonitor.onLastContactTick = { [weak self] date in
            self?.updateLastContactLabel(from: date)
        }
    }

    private func wireUploadQueueCallbacks() {
        uploadQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        uploadQueue.onLastResultChanged = { [weak self] text in
            guard let self else { return }
            self.lastResultText = text
            self.updateUIForState()
        }
        uploadQueue.onUploadingChanged = { [weak self] _ in
            self?.updateUIForState()
        }
        uploadQueue.onPayloadDropped = { [weak self] fileURL, error in
            guard let self else { return }
            let basename = fileURL.deletingPathExtension().lastPathComponent
            let detail = Self.describeUploadError(error)
            log.error("camera payload dropped file=\(basename, privacy: .public) reason=\(detail, privacy: .public)")
            self.lastResultText = "上傳失敗已丟棄: \(basename) — \(detail)"
            self.updateUIForState()
        }
    }

    private func wireAnalysisQueueCallbacks() {
        analysisQueue.onStatusTextChanged = { [weak self] text in
            self?.lastUploadStatusText = text
            self?.updateUIForState()
        }
        analysisQueue.onLastResultChanged = { [weak self] text in
            guard let self else { return }
            self.lastResultText = text
            self.updateUIForState()
        }
    }

    /// Short human-readable detail for `UploadError`. `PayloadUploadQueue`
    /// has its own one-word categoriser for the small "Upload:" line; this
    /// one is for the more prominent "Last:" alert and includes status
    /// codes so the operator can grep server logs.
    private static func describeUploadError(_ error: ServerUploader.UploadError) -> String {
        switch error {
        case .network(let urlError):
            return "network (\(urlError.code.rawValue))"
        case .client(let code, _):
            return "HTTP \(code) (client)"
        case .server(let code, _):
            return "HTTP \(code) (server)"
        case .decoding:
            return "decode error"
        case .invalidResponse:
            return "no response"
        }
    }



    private func applyRemoteArm() {
        log.info("camera received arm command state=\(Self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            enterRecordingMode()
        case .timeSyncWaiting, .mutualSyncing, .recording, .uploading:
            // Active state — arm is a no-op. `.timeSyncWaiting` finishes
            // on its own and returns to standby; next heartbeat re-sends
            // the arm command and this branch flips us into recording.
            // Server's session ↔ sync precondition makes the
            // `.mutualSyncing` path impossible in practice.
            break
        }
    }

    private func applyRemoteDisarm() {
        log.info("camera received disarm command state=\(Self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            break
        case .timeSyncWaiting:
            cancelTimeSync(reason: "disarmed")
        case .mutualSyncing:
            abortMutualSync(reason: "disarmed")
        case .uploading:
            // Upload flow runs its own course; state transitions back to
            // standby via persistCompletedCycle once the queue accepts.
            break
        case .recording:
            returnToStandbyAfterCycle = true
            processingQueue.async { [weak self] in
                guard let self else { return }
                let active = self.recorder.isActive
                log.info("camera disarm while recording: recorder_active=\(active) clip_exists=\(self.clipRecorder != nil)")
                if active {
                    // Normal path: flush whatever frames the clip contains;
                    // onCycleComplete drives persist + standby transition.
                    self.recorder.forceFinishIfRecording()
                } else {
                    // Disarm arrived before the first frame reached us
                    // (happens if stop is pressed in the ~300 ms fps
                    // switch window). Tear down cleanly on main.
                    log.warning("camera disarm before first frame: no payload produced — frames never reached captureOutput or clip.append failed")
                    self.clipRecorder?.cancel()
                    self.clipRecorder = nil
                    DispatchQueue.main.async {
                        self.returnToStandbyAfterCycle = false
                        self.exitRecordingToStandby()
                    }
                }
            }
        }
    }

    // MARK: - Mutual chirp sync

    /// Enter mutual-sync mode. Both phones receive this command (distinct
    /// `syncId` per run); each emits its own band's chirp, listens for
    /// both bands, and POSTs the resulting 4 timestamps to the server.
    /// `.mutualSyncing` can only be entered from `.standby` — arriving in
    /// any other state is a server/client race we ignore (server guards
    /// against sync ↔ session overlap, so this is a defense-in-depth log).
    private func applyMutualSync(syncId: String) {
        guard state == .standby else {
            log.warning("sync_run ignored state=\(Self.stateText(self.state), privacy: .public) sync_id=\(syncId, privacy: .public)")
            uploader.postSyncLog(event: "ignored", detail: [
                "reason": .string("not_standby"),
                "state": .string(Self.stateText(state)),
                "sync_id": .string(syncId),
            ])
            return
        }
        log.info("camera entering mutual-sync sync_id=\(syncId, privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        uploader.postSyncLog(event: "enter", detail: [
            "sync_id": .string(syncId),
            "role": .string(settings.cameraRole),
        ])
        pendingSyncId = syncId
        startMutualSync()
    }

    private func startMutualSync() {
        let role = settings.cameraRole
        guard role == "A" || role == "B" else {
            log.error("sync_run rejected unknown role=\(role, privacy: .public)")
            uploader.postSyncLog(event: "reject", detail: [
                "reason": .string("unknown_role"),
                "role": .string(role),
            ])
            pendingSyncId = nil
            return
        }

        // Phase A mutual sync: iOS is a dumb recorder. MutualSyncAudio
        // emits this cam's chirp and records the full 3 s listening
        // window as raw PCM; the completion callback hands us an
        // encoded WAV which we POST to /sync/audio_upload, where the
        // server runs the matched filter and feeds the shared state
        // machine. No on-device detector / CFAR / PSR gating here.
        let audio = MutualSyncAudio()
        syncAudio = audio
        uploader.postSyncLog(event: "recording_started", detail: [
            "role": .string(role),
        ])

        state = .mutualSyncing
        frameStateBox.update(state: .mutualSyncing, pendingBootstrap: false, sessionId: nil)
        warningLabel.text = "互相時間校正中… (\(role))"
        warningLabel.isHidden = false
        lastUploadStatusText = "Mutual sync · recording"
        updateUIForState()

        let roleCaptured = role
        audio.beginSync(
            emittedRole: roleCaptured,
            onEmitted: { [weak self] in
                self?.uploader.postSyncLog(event: "emit", detail: [
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

        // Watchdog: server has an 8 s timeout; set ours to 6 s so we
        // clean up + log before the server gives up. In the Phase A
        // flow the recording duration alone is 3 s plus ~1 s of
        // engine-start / upload latency — watchdog exists for the edge
        // case where MutualSyncAudio never hits its own completion.
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.state == .mutualSyncing else { return }
            self.abortMutualSync(reason: "timeout")
        }
        syncWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 6.0, execute: work)
    }

    private func handleMutualSyncRecording(
        result: MutualSyncAudio.RecordingResult,
        role: String
    ) {
        guard state == .mutualSyncing else { return }
        guard let syncId = pendingSyncId else {
            log.error("recording complete without pending sync_id — ignoring")
            teardownMutualSync(status: "Mutual sync · orphan")
            return
        }
        syncWatchdog?.cancel()
        syncWatchdog = nil

        uploader.postSyncLog(event: "recording_complete", detail: [
            "sync_id": .string(syncId),
            "wav_bytes": .int(result.wavData.count),
            "duration_s": .double(
                Double(result.wavData.count) / max(1.0, result.sampleRate * 2.0)
            ),
            "audio_start_pts_s": .double(result.audioStartPtsS),
        ])

        lastUploadStatusText = "Mutual sync · uploading"
        updateUIForState()

        let meta = ServerUploader.SyncAudioUploadMeta(
            sync_id: syncId,
            camera_id: role,
            role: role,
            audio_start_pts_s: result.audioStartPtsS,
            sample_rate: Int(result.sampleRate),
            emission_pts_s: result.emissionPtsS
        )
        uploader.uploadSyncAudio(meta: meta, wavData: result.wavData) { [weak self] upResult in
            DispatchQueue.main.async {
                guard let self else { return }
                switch upResult {
                case .success:
                    self.lastUploadStatusText = "Mutual sync · done"
                case .failure(let error):
                    log.error("sync audio upload failed: \(error.localizedDescription, privacy: .public)")
                    self.lastUploadStatusText = "Mutual sync · upload failed"
                }
                self.updateUIForState()
            }
        }

        teardownMutualSync(status: "Mutual sync · uploaded")
    }

    private func abortMutualSync(reason: String) {
        log.warning("sync aborted reason=\(reason, privacy: .public) sync_id=\(self.pendingSyncId ?? "nil", privacy: .public)")
        uploader.postSyncLog(event: "abort", detail: [
            "reason": .string(reason),
            "sync_id": .string(pendingSyncId ?? ""),
        ])
        // Phase A: aborted runs don't ship a partial report — server
        // has its own timeout that produces the aborted SyncResult and
        // renders whatever diagnostic it has (none yet, since there's
        // no trace data to ship). Future enhancement: if the recording
        // reached non-zero length before abort, upload the truncated
        // WAV too so we can still see "how far we got".
        syncWatchdog?.cancel()
        syncWatchdog = nil
        teardownMutualSync(status: "Mutual sync · \(reason)")
    }

    /// Common cleanup from both success and timeout paths. Tears down the
    /// mutual-sync audio engine (releases the AVAudioSession), returns to
    /// `.standby`, then reconciles capture ownership back to preview /
    /// calibration-frame / parked standby.
    private func teardownMutualSync(status: String) {
        syncAudio?.endSync()
        syncAudio = nil
        pendingSyncId = nil
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        reconcileStandbyCaptureState()
        warningLabel.isHidden = true
        lastUploadStatusText = status
        updateUIForState()
    }

    /// Force an immediate heartbeat probe, resetting backoff. Exposed for
    /// the Settings → Diagnostics "Test connection" action (formerly a
    /// HUD button on the main screen).
    func testServerConnection() {
        healthMonitor.probeNow()
    }

    /// Settings-dismiss callback. Re-diffs UserDefaults and reconfigures
    /// anything settings-driven. Phase 6: Settings is bootstrap-only
    /// (IP / port / role), so the only knobs here are the server endpoint
    /// and the camera role. Chirp threshold + heartbeat interval come from
    /// the dashboard via WS settings (Phase 3).
    private func applyUpdatedSettings() {
        let latest = AppSettingsStore.load()
        let serverChanged = latest.serverIP != settings.serverIP
            || latest.serverPort != settings.serverPort
        let cameraRoleChanged = latest.cameraRole != settings.cameraRole

        settings = latest

        if serverChanged {
            serverConfig = ServerUploader.ServerConfig(serverIP: latest.serverIP, serverPort: latest.serverPort)
            uploader = ServerUploader(config: serverConfig)
            uploadQueue.updateUploader(uploader)
            analysisQueue.updateUploader(uploader)
            previewUploader?.updateUploader(uploader)
            disconnectWebSocket()
            connectWebSocket()
            healthMonitor.probeNow()
        }
        if cameraRoleChanged {
            previewUploader = nil
            disconnectWebSocket()
            connectWebSocket()
        }
        recorder?.setCameraId(latest.cameraRole)
        syncInlineControlsFromSettings()
    }

    private func updateLastContactLabel(from date: Date?) {
        _ = date
    }

    private func setupUI() {
        topStatusChip.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(topStatusChip)

        setupControlPanel()

        warningLabel.font = DesignTokens.Fonts.sans(size: 18, weight: .bold)
        warningLabel.textColor = DesignTokens.Colors.ink
        warningLabel.backgroundColor = DesignTokens.Colors.warning.withAlphaComponent(0.85)
        warningLabel.layer.cornerRadius = DesignTokens.CornerRadius.chip
        warningLabel.layer.masksToBounds = true
        warningLabel.textAlignment = .center
        warningLabel.numberOfLines = 0
        warningLabel.translatesAutoresizingMaskIntoConstraints = false
        warningLabel.isHidden = true
        view.addSubview(warningLabel)

        NSLayoutConstraint.activate([
            topStatusChip.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: DesignTokens.Spacing.m),
            topStatusChip.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),

            controlPanel.topAnchor.constraint(equalTo: topStatusChip.bottomAnchor, constant: DesignTokens.Spacing.s),
            controlPanel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),
            controlPanel.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -DesignTokens.Spacing.xl),

            warningLabel.topAnchor.constraint(equalTo: controlPanel.bottomAnchor, constant: DesignTokens.Spacing.s),
            warningLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            warningLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
        ])

        setupStateBorder()
        setupRecIndicator()
        syncInlineControlsFromSettings()
    }

    private func setupControlPanel() {
        controlPanel.translatesAutoresizingMaskIntoConstraints = false
        controlPanel.backgroundColor = DesignTokens.Colors.hudSurface
        controlPanel.layer.cornerRadius = DesignTokens.CornerRadius.card
        controlPanel.layer.borderWidth = 1
        controlPanel.layer.borderColor = DesignTokens.Colors.cardBorder.cgColor
        view.addSubview(controlPanel)

        let serverLabel = makePanelLabel("SERVER")
        let roleLabel = makePanelLabel("ROLE")

        serverIPField.translatesAutoresizingMaskIntoConstraints = false
        serverIPField.borderStyle = .roundedRect
        serverIPField.autocapitalizationType = .none
        serverIPField.autocorrectionType = .no
        serverIPField.clearButtonMode = .whileEditing
        serverIPField.keyboardType = .numbersAndPunctuation
        serverIPField.placeholder = "192.168.1.100"
        serverIPField.font = DesignTokens.Fonts.mono(size: 14, weight: .medium)

        serverPortField.translatesAutoresizingMaskIntoConstraints = false
        serverPortField.borderStyle = .roundedRect
        serverPortField.keyboardType = .numberPad
        serverPortField.placeholder = "8765"
        serverPortField.font = DesignTokens.Fonts.mono(size: 14, weight: .medium)

        roleControl.translatesAutoresizingMaskIntoConstraints = false
        roleControl.selectedSegmentTintColor = DesignTokens.Colors.accent
        roleControl.setTitleTextAttributes([.foregroundColor: DesignTokens.Colors.ink], for: .normal)
        roleControl.setTitleTextAttributes([.foregroundColor: DesignTokens.Colors.cardBackground], for: .selected)

        applySettingsButton.translatesAutoresizingMaskIntoConstraints = false
        applySettingsButton.configuration = .filled()
        applySettingsButton.configuration?.title = "Apply"
        applySettingsButton.configuration?.baseBackgroundColor = DesignTokens.Colors.accent
        applySettingsButton.configuration?.baseForegroundColor = DesignTokens.Colors.cardBackground
        applySettingsButton.addTarget(self, action: #selector(applyInlineSettingsTapped), for: .touchUpInside)

        connectionLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        connectionLabel.textColor = DesignTokens.Colors.ink

        previewLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        previewLabel.textColor = DesignTokens.Colors.sub

        let serverRow = UIStackView(arrangedSubviews: [serverLabel, serverIPField, serverPortField])
        serverRow.axis = .horizontal
        serverRow.alignment = .center
        serverRow.spacing = DesignTokens.Spacing.s

        let roleRow = UIStackView(arrangedSubviews: [roleLabel, roleControl, applySettingsButton])
        roleRow.axis = .horizontal
        roleRow.alignment = .center
        roleRow.spacing = DesignTokens.Spacing.s

        let statusRow = UIStackView(arrangedSubviews: [connectionLabel, previewLabel])
        statusRow.axis = .vertical
        statusRow.alignment = .leading
        statusRow.spacing = DesignTokens.Spacing.xs

        let root = UIStackView(arrangedSubviews: [serverRow, roleRow, statusRow])
        root.axis = .vertical
        root.spacing = DesignTokens.Spacing.s
        root.translatesAutoresizingMaskIntoConstraints = false
        controlPanel.addSubview(root)

        NSLayoutConstraint.activate([
            serverLabel.widthAnchor.constraint(equalToConstant: 52),
            roleLabel.widthAnchor.constraint(equalToConstant: 52),
            serverIPField.widthAnchor.constraint(equalToConstant: 156),
            serverPortField.widthAnchor.constraint(equalToConstant: 76),
            roleControl.widthAnchor.constraint(equalToConstant: 120),
            applySettingsButton.widthAnchor.constraint(equalToConstant: 84),

            root.topAnchor.constraint(equalTo: controlPanel.topAnchor, constant: DesignTokens.Spacing.m),
            root.leadingAnchor.constraint(equalTo: controlPanel.leadingAnchor, constant: DesignTokens.Spacing.m),
            root.trailingAnchor.constraint(equalTo: controlPanel.trailingAnchor, constant: -DesignTokens.Spacing.m),
            root.bottomAnchor.constraint(equalTo: controlPanel.bottomAnchor, constant: -DesignTokens.Spacing.m),
        ])
    }

    private func makePanelLabel(_ text: String) -> UILabel {
        let label = UILabel()
        label.font = DesignTokens.Fonts.mono(size: 12, weight: .bold)
        label.textColor = DesignTokens.Colors.sub
        label.text = text
        return label
    }

    private func syncInlineControlsFromSettings() {
        serverIPField.text = settings.serverIP
        serverPortField.text = "\(settings.serverPort)"
        roleControl.selectedSegmentIndex = settings.cameraRole == "B" ? 1 : 0
    }

    @objc private func applyInlineSettingsTapped() {
        view.endEditing(true)
        let serverIP = serverIPField.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !serverIP.isEmpty else {
            warningLabel.text = "Server IP 不可為空"
            warningLabel.isHidden = false
            return
        }
        guard let portText = serverPortField.text?.trimmingCharacters(in: .whitespacesAndNewlines),
              let serverPort = Int(portText),
              (1...65535).contains(serverPort) else {
            warningLabel.text = "Port 必須是 1-65535"
            warningLabel.isHidden = false
            return
        }

        let updated = AppSettings(
            serverIP: serverIP,
            serverPort: serverPort,
            cameraRole: roleControl.selectedSegmentIndex == 1 ? "B" : "A"
        )
        AppSettingsStore.save(updated)
        warningLabel.isHidden = true
        applyUpdatedSettings()
        updateUIForState()
    }

    private func setupStateBorder() {
        stateBorderLayer.fillColor = UIColor.clear.cgColor
        stateBorderLayer.strokeColor = UIColor.clear.cgColor
        stateBorderLayer.lineWidth = 0
        // Sit above the preview layer (index 0) but below the HUD views.
        // Border is decorative — never intercept touches (CAShapeLayer
        // doesn't by default, belt and braces).
        view.layer.addSublayer(stateBorderLayer)
    }

    private func setupRecIndicator() {
        recIndicator.translatesAutoresizingMaskIntoConstraints = false
        recIndicator.backgroundColor = DesignTokens.Colors.hudSurface
        recIndicator.layer.cornerRadius = 14
        recIndicator.layer.borderColor = DesignTokens.Colors.destructive.cgColor
        recIndicator.layer.borderWidth = 1
        recIndicator.isHidden = true

        recDotView.backgroundColor = DesignTokens.Colors.destructive
        recDotView.layer.cornerRadius = 7
        recDotView.translatesAutoresizingMaskIntoConstraints = false

        recTimerLabel.text = "REC 0.0s"
        recTimerLabel.textColor = DesignTokens.Colors.ink
        recTimerLabel.font = DesignTokens.Fonts.mono(size: 16, weight: .bold)
        recTimerLabel.translatesAutoresizingMaskIntoConstraints = false

        recIndicator.addSubview(recDotView)
        recIndicator.addSubview(recTimerLabel)
        view.addSubview(recIndicator)

        NSLayoutConstraint.activate([
            recIndicator.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 12),
            recIndicator.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -12),
            recIndicator.heightAnchor.constraint(equalToConstant: 32),

            recDotView.leadingAnchor.constraint(equalTo: recIndicator.leadingAnchor, constant: 10),
            recDotView.centerYAnchor.constraint(equalTo: recIndicator.centerYAnchor),
            recDotView.widthAnchor.constraint(equalToConstant: 14),
            recDotView.heightAnchor.constraint(equalToConstant: 14),

            recTimerLabel.leadingAnchor.constraint(equalTo: recDotView.trailingAnchor, constant: 8),
            recTimerLabel.trailingAnchor.constraint(equalTo: recIndicator.trailingAnchor, constant: -12),
            recTimerLabel.centerYAnchor.constraint(equalTo: recIndicator.centerYAnchor),
        ])
    }

    private func updateUIForState() {
        connectionLabel.text = "LINK · \((healthMonitor?.statusText ?? "offline").uppercased())"
        if !lastUploadStatusText.isEmpty, lastUploadStatusText != "Idle" {
            connectionLabel.text = "\(connectionLabel.text ?? "") · \(lastUploadStatusText.uppercased())"
        }
        let previewState = previewRequestedByServer ? "REMOTE ON" : (session.isRunning ? "LOCAL ACTIVE" : "OFF")
        let pathLabel = currentSessionPaths.map(\.displayLabel).sorted().joined(separator: " + ")
        previewLabel.text = "PREVIEW · \(previewState) · \(pathLabel.uppercased())"

        // State-transition-only side effects: re-running applyStateVisuals
        // on every tick resets the REC timer and rebuilds the pulse
        // animation, so edge-trigger it here. Label / colour refresh above
        // stays level-triggered.
        if lastRenderedState != state {
            applyStateVisuals()
            lastRenderedState = state
        }
    }

    static func stateText(_ state: AppState) -> String {
        switch state {
        case .standby: return "STANDBY"
        case .timeSyncWaiting: return "TIME_SYNC"
        case .mutualSyncing: return "MUTUAL_SYNC"
        case .recording: return "RECORDING"
        case .uploading: return "UPLOADING"
        }
    }

    /// Translate the internal `cancelTimeSync(reason:)` tag into user copy.
    private static func localizedCancelReason(_ reason: String) -> String {
        switch reason {
        case "timeout": return "逾時"
        case "cancelled": return "已取消"
        case "disarmed": return "已取消（dashboard 停止）"
        case "missing_sync_id": return "缺少 sync id"
        default: return reason
        }
    }

    // MARK: - State visuals

    private func applyStateVisuals() {
        let cfg = stateVisualConfig(for: state)

        // Border
        stateBorderLayer.strokeColor = cfg.borderColor.cgColor
        stateBorderLayer.lineWidth = cfg.borderWidth
        stateBorderLayer.removeAnimation(forKey: "pulse")
        if cfg.pulse {
            let anim = CABasicAnimation(keyPath: "opacity")
            anim.fromValue = 0.35
            anim.toValue = 1.0
            anim.duration = 0.85
            anim.autoreverses = true
            anim.repeatCount = .infinity
            stateBorderLayer.add(anim, forKey: "pulse")
        } else {
            stateBorderLayer.opacity = 1.0
        }

        topStatusChip.text = cfg.chipText
        topStatusChip.setStyle(cfg.chipStyle)

        if state == .recording {
            recIndicator.isHidden = false
            startRecTimer()
        } else {
            recIndicator.isHidden = true
            stopRecTimer()
        }
    }

    private struct StateVisualConfig {
        let borderColor: UIColor
        let borderWidth: CGFloat
        let pulse: Bool
        let chipText: String
        let chipStyle: StatusChip.Style
    }

    private func stateVisualConfig(for s: AppState) -> StateVisualConfig {
        switch s {
        case .standby:
            return .init(
                borderColor: DesignTokens.Colors.cardBorder, borderWidth: 2, pulse: false,
                chipText: "待機", chipStyle: .neutral
            )
        case .timeSyncWaiting:
            return .init(
                borderColor: DesignTokens.Colors.accent, borderWidth: 8, pulse: true,
                chipText: "時間校正中", chipStyle: .pending
            )
        case .mutualSyncing:
            return .init(
                borderColor: DesignTokens.Colors.accent, borderWidth: 8, pulse: true,
                chipText: "互相同步中", chipStyle: .pending
            )
        case .recording:
            return .init(
                borderColor: DesignTokens.Colors.destructive, borderWidth: 14, pulse: false,
                chipText: "● 錄影中", chipStyle: .fail
            )
        case .uploading:
            return .init(
                borderColor: DesignTokens.Colors.warning, borderWidth: 6, pulse: false,
                chipText: "上傳中", chipStyle: .pending
            )
        }
    }

    private func startRecTimer() {
        recStartTime = CACurrentMediaTime()
        recTimerLabel.text = "REC 0.0s"
        recDotView.alpha = 1.0
        recTimer?.invalidate()
        recTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let elapsed = CACurrentMediaTime() - self.recStartTime
            self.recTimerLabel.text = String(format: "REC %.1fs", elapsed)
            // 2 Hz blink so the operator can read the dot from distance.
            self.recDotView.alpha = (Int(elapsed * 2) % 2 == 0) ? 1.0 : 0.25
        }
    }

    private func stopRecTimer() {
        recTimer?.invalidate()
        recTimer = nil
        recDotView.alpha = 1.0
    }


    // MARK: - Video frame processing

    nonisolated func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // Only the video data output reaches here; audio samples go through
        // the chirp detector's own delegate on its own queue.
        guard connection.output is AVCaptureVideoDataOutput else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        let ts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let timestampS = CMTimeGetSeconds(ts)
        if timestampS.isNaN || timestampS.isInfinite { return }

        updateFpsEstimate()

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        latestImageWidth = width
        latestImageHeight = height

        frameIndex += 1

        // Locked snapshot of state, pending-bootstrap, and the
        // session id frozen at arm time. Read once per sample; the
        // rest of this method only touches `snap`, never `self.state`
        // / `self.currentSessionId` directly, so a 240 Hz sample can't
        // race a main-thread mutation mid-method.
        let snap = frameStateBox.snapshot()

        // Rate-limited debug heartbeat so Xcode console can confirm frames
        // are actually flowing and which state the capture thread sees.
        // Log the first 3 frames (catches "session started but zero frames"
        // bugs) then once every 240 frames (~1 s at tracking fps, ~4 s at
        // idle) for steady monitoring.
        if frameIndex <= 3 || frameIndex % 240 == 0 {
            log.info("camera frame idx=\(self.frameIndex) state=\(Self.stateText(snap.state), privacy: .public) pendingBootstrap=\(snap.pendingBootstrap) sid=\(snap.sessionId ?? "nil", privacy: .public)")
        }

        // Phase 4a: push a preview JPEG to the server when requested AND
        // we're not doing anything time-critical. `.recording` owns every
        // frame for ClipRecorder; `.timeSyncWaiting` is mic-centric and
        // the preview encode would compete for CPU. Outside those two
        // states the capture queue is otherwise idle on idle frames,
        // which makes this the cheapest place to hook in.
        if self.previewRequestedByServer
            && snap.state != .recording
            && snap.state != .timeSyncWaiting {
            self.previewUploader?.pushFrame(pixelBuffer)
        }
        // Phase 7: one-shot native-resolution calibration frame. The
        // heartbeat arms the latch; the next captured sample gets
        // encoded at full (uncropped, un-downsampled) resolution and
        // POSTed to /camera/{id}/calibration_frame. Drain the latch
        // synchronously so a slow POST can't double-fire if the next
        // heartbeat flips the flag back on.
        if self.calibrationFrameCaptureArmed
            && snap.state != .recording
            && snap.state != .timeSyncWaiting {
            self.calibrationFrameCaptureArmed = false
            self.uploadCalibrationFrame(pixelBuffer)
        }

        // Only the `.recording` state cares about samples.
        guard snap.state == .recording else { return }

        // PR61: all modes write a local MOV so any on-device result can be
        // generated later from the finalized file instead of the live callback.
        if frameStateBox.consumePendingBootstrap() {
            log.info("camera clip bootstrap start width=\(width) height=\(height) sid=\(snap.sessionId ?? "nil", privacy: .public)")
            startClipRecorder(width: width, height: height)
            if clipRecorder == nil {
                log.error("camera clip bootstrap failed session=\(snap.sessionId ?? "nil", privacy: .public)")
                DispatchQueue.main.async {
                    self.returnToStandbyAfterCycle = false
                    self.exitRecordingToStandby()
                }
                return
            }
            log.info("camera clip bootstrap ok session=\(snap.sessionId ?? "nil", privacy: .public)")
        }
        clipRecorder?.append(sampleBuffer: sampleBuffer)

        // Detection now fans out over a bounded concurrent pool. Live WS
        // streaming consumes the same FramePayloads as post-pass fallback.
        dispatchDetection(pixelBuffer: pixelBuffer, timestampS: timestampS)

        // Bootstrap the PitchRecorder on the first sample regardless of
        // mode. In mode-one this fires on the same sample clipRecorder
        // just consumed, so the payload's `video_start_pts_s` still
        // matches `clip.firstSamplePTS` (both are this sample's PTS).
        // In mode-two there's no clip — we lean on the captured sample's
        // session-clock PTS directly.
        if !recorder.isActive {
            guard let sid = snap.sessionId, !sid.isEmpty else {
                log.error("camera recording started without session_id cam=\(self.settings.cameraRole, privacy: .public)")
                return
            }
            log.info("camera first frame, starting recorder session=\(sid, privacy: .public) mode=\(self.currentCaptureMode.rawValue, privacy: .public) video_start_pts=\(timestampS) anchor=\(self.lastSyncAnchorTimestampS ?? .nan)")
            recorder.startRecording(
                sessionId: sid,
                syncId: lastSyncId,
                anchorTimestampS: lastSyncAnchorTimestampS,
                videoStartPtsS: timestampS,
                captureTelemetry: currentCaptureTelemetry(targetFps: trackingFps)
            )
        }
    }

    private func dispatchDetection(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) {
        _ = detectionPool.enqueue(pixelBuffer: pixelBuffer, timestampS: timestampS)
    }

    private func handleDetectedFrame(_ frame: ServerUploader.FramePayload) {
        detectionStateLock.lock()
        detectionFramesBuffer.append(frame)
        detectionStateLock.unlock()

        frameDispatcher?.dispatchFrame(frame)
    }

    /// Bump the detection generation, clear the buffer, reset the throttle.
    /// Called at both ends of a recording cycle so no stale detections
    /// bleed across arms. Detector itself is stateless — nothing to rebuild.
    private func resetBallDetectionState() {
        detectionStateLock.lock()
        detectionFramesBuffer.removeAll()
        detectionStateLock.unlock()
        detectionPool.invalidateGeneration()
        detectionPool.reset()
    }

    /// Take ownership of the accumulated per-frame detection results for
    /// this recording cycle. Used by the cycle-complete path (mode-one
    /// uses it as a trim oracle; mode-two ships it to the server).
    func drainDetectedFrames() -> [ServerUploader.FramePayload] {
        detectionStateLock.lock()
        defer { detectionStateLock.unlock() }
        let out = detectionFramesBuffer
        detectionFramesBuffer.removeAll()
        return out
    }

    private func startClipRecorder(width: Int, height: Int) {
        let tmpURL = payloadStore.makeTempVideoURL()
        let cr = ClipRecorder(outputURL: tmpURL)
        do {
            try cr.prepare(width: width, height: height)
            clipRecorder = cr
        } catch {
            // Clip writing is a Phase-1 experiment — if AVAssetWriter rejects
            // the configuration we degrade to JSON-only and keep recording.
            log.error("camera clip recorder prepare failed error=\(error.localizedDescription, privacy: .public)")
            clipRecorder = nil
        }
    }

    private func updateFpsEstimate() {
        framesSinceLastFpsTick += 1
        let now = CACurrentMediaTime()
        let elapsed = now - lastFrameTimestampForFps
        guard elapsed >= 1.0 else { return }
        fpsEstimate = Double(framesSinceLastFpsTick) / elapsed
        framesSinceLastFpsTick = 0
        lastFrameTimestampForFps = now
        // Fan-out to the Diagnostics screen (Settings → Diagnostics).
    }

}

// MARK: - ServerWebSocketDelegate

extension CameraViewController: ServerWebSocketDelegate {

    func webSocketDidConnect(_ connection: ServerWebSocketConnection) {
        // Transport open is not proof the server is actually reachable.
        // URLSession lets `resume()` succeed before the WS handshake (or
        // a first server payload) completes, which caused false-green
        // "server connected" HUD states after the backend had already
        // died. Reachability is promoted only on real inbound traffic in
        // `didReceive`.
    }

    func webSocketDidDisconnect(_ connection: ServerWebSocketConnection, reason: String?) {
        commandRouter.didDisconnect()
    }

    func webSocket(_ connection: ServerWebSocketConnection, didReceive message: [String: Any]) {
        commandRouter.handle(message: message)
    }
}

/// Lock-protected mirror of the three fields `captureOutput` reads across
/// queues (`state`, `pendingRecordingBootstrap`, `pendingSessionId`). Main
/// thread is the sole writer — `applyRemoteArm` / `applyRemoteDisarm` /
/// `enterRecordingMode` / `exitRecordingToStandby` push every transition;
/// the frame queue takes one locked snapshot per delivered sample so a
/// 240 Hz read can never observe a partially-mutated state struct.
final class FrameStateBox {
    struct Snapshot {
        let state: CameraViewController.AppState
        let pendingBootstrap: Bool
        let sessionId: String?
    }

    private var lock = os_unfair_lock_s()
    private var _state: CameraViewController.AppState = .standby
    private var _pendingBootstrap: Bool = false
    private var _sessionId: String?

    func snapshot() -> Snapshot {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        return Snapshot(
            state: _state,
            pendingBootstrap: _pendingBootstrap,
            sessionId: _sessionId
        )
    }

    func update(state: CameraViewController.AppState, pendingBootstrap: Bool, sessionId: String?) {
        os_unfair_lock_lock(&lock)
        _state = state
        _pendingBootstrap = pendingBootstrap
        _sessionId = sessionId
        os_unfair_lock_unlock(&lock)
    }

    /// Edge-trigger helper for the frame queue: clears the bootstrap flag
    /// once the writer is up. Returns the previous value so the caller can
    /// branch on whether *this* sample owned the bootstrap.
    func consumePendingBootstrap() -> Bool {
        os_unfair_lock_lock(&lock)
        defer { os_unfair_lock_unlock(&lock) }
        let prev = _pendingBootstrap
        _pendingBootstrap = false
        return prev
    }
}
