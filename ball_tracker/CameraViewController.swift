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
/// - `IntrinsicsStore` owns UserDefaults keys for calibration artefacts.
final class CameraViewController: UIViewController, AVCaptureVideoDataOutputSampleBufferDelegate {
    enum AppState {
        case standby
        case timeSyncWaiting
        case recording
        case uploading
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
    private var settings: SettingsViewController.Settings!
    private var recorder: PitchRecorder!
    private var uploader: ServerUploader!
    private var serverConfig: ServerUploader.ServerConfig!
    private var healthMonitor: ServerHealthMonitor!
    private var uploadQueue: PayloadUploadQueue!

    // UI state.
    private var lastUploadStatusText: String = ""
    private var lastResultText: String = CameraViewController.initialLastResultText
    private var lastFrameTimestampForFps: CFTimeInterval = CACurrentMediaTime()
    private var framesSinceLastFpsTick: Int = 0
    private var fpsEstimate: Double = 0
    private var displayLink: CADisplayLink?

    // Last observed capture dimensions; mirrored into IntrinsicsStore so
    // the payload enrichment path and the server-side detection pipeline
    // agree on what resolution the MOV was recorded at.
    private var latestImageWidth: Int = 0
    private var latestImageHeight: Int = 0

    // Chirp detector snapshot for the HUD — written on audio queue.
    private var latestChirpSnapshot: AudioChirpDetector.Snapshot?

    // The `.recording`-was-just-entered bootstrap flag and the snapshot of
    // `currentSessionId` taken at arm time both live in `frameStateBox`;
    // the camera VC itself never reads them after the push, so duplicating
    // them as instance vars would just be two stores out of sync.

    // Remote-control state (driven by the heartbeat response).
    /// Last command key we acted on for this device (`"arm"` / `"disarm"` /
    /// nil). Only state *transitions* cause local actions so repeated arm
    /// replies during an armed session don't re-trigger enterSyncMode.
    private var lastAppliedCommand: String?
    /// Reserved for future branching in the cycle-complete path (e.g. a
    /// re-arm that wants to skip the standby flash). Always true today
    /// because `.recording` always returns to `.standby` now.
    private var returnToStandbyAfterCycle: Bool = false
    /// Server-minted pairing key for the currently armed session. Read
    /// off each heartbeat reply; tagged onto every recording that starts
    /// while the session is armed. Nil when the server has no active
    /// session. iPhones never mint this themselves.
    private var currentSessionId: String?

    // Payload persistence.
    private let payloadStore = PitchPayloadStore()

    // Per-cycle H.264 clip writer, created on entry to .recording and
    // finalised when the cycle ends. Phase-1 raw-video experiment: the clip
    // travels alongside the JSON payload so server-side detection can be
    // iterated against a canonical source of truth.
    private var clipRecorder: ClipRecorder?

    // Offloaded on-device ball detection. Runs on `detectionQueue` at
    // ≤60 Hz while the state is `.recording`, so 240 fps sample delivery on
    // `processingQueue` never stalls. Serves two roles depending on
    // currentCaptureMode:
    //   - mode-one (camera_only): output is the trim-window oracle. Results
    //     not uploaded — server re-runs its own authoritative detection on
    //     the received MOV.
    //   - mode-two (on_device): output IS the ground truth. Shipped to the
    //     server as `PitchPayload.frames` with no MOV; server pairs +
    //     triangulates directly.
    // The same BTDetectionSession (shape gate + MOG2) drives both so the
    // algorithm stays identical between modes and aligned with the server.
    private let detectionQueue = DispatchQueue(label: "camera.detection.queue", qos: .utility)
    private let detectionStateLock = NSLock()
    private var detectionInFlight = false
    private var lastDetectionDispatchTimeS: TimeInterval = 0
    private static let detectionIntervalS: TimeInterval = 1.0 / 60.0
    /// BTDetectionSession (MOG2 + shape-gated detection). One per recording
    /// cycle — rebuilt on entry so the background model starts fresh.
    /// Access serialised through `detectionStateLock` (or confined to
    /// `detectionQueue`).
    private var detectionSession: BTDetectionSession?
    /// Accumulated per-frame detection results for the current cycle.
    /// Drained at cycle-complete and either discarded (mode-one) or
    /// attached to the upload payload (mode-two).
    private var detectionFramesBuffer: [ServerUploader.FramePayload] = []
    /// Monotonic counter used as `FramePayload.frame_index`. Bumped on
    /// every dispatched detection call so the index mirrors the order
    /// detections were run — not the capture-queue frame order.
    private var detectionCallIndex: Int = 0
    /// Bumped on every cycle boundary so a detection closure dispatched for
    /// cycle N can discard its result if cycle N+1 has already started.
    private var detectionGeneration: Int = 0

    // Most recently recovered chirp anchor — session-clock PTS of the
    // chirp peak from the mic matched filter. Stamped onto outgoing
    // payloads as `sync_anchor_timestamp_s`. Nil until the user completes
    // a 時間校正; server rejects unpaired sessions whose anchor is nil.
    private var lastSyncAnchorTimestampS: Double?

    // Time-sync timeout task so we can cancel if the user aborts early.
    private var timeSyncTimeoutWork: DispatchWorkItem?

    // UI containers. Layout is a Ready card (left-centered, standby only)
    // plus the existing state chip (top-left) + REC indicator (top-right).
    // FPS / last-contact / Test are Settings → Diagnostics now.
    private let topStatusChip = StatusChip()
    /// Small HUD chip showing the currently-effective capture mode
    /// (session snapshot if armed, otherwise the dashboard's global
    /// toggle). Driven by `/heartbeat` replies.
    private let modeLabel = UILabel()
    /// Last-known capture mode from the server. Starts at cameraOnly so a
    /// network-unreachable launch degrades to the pre-split behaviour. Step 2
    /// reads this at cycle-complete to decide whether to upload the MOV or
    /// just the detection JSON.
    private var currentCaptureMode: ServerUploader.CaptureMode = .cameraOnly
    private let readyCard = ReadyCard()
    private let lastResultLabel = UILabel()
    private let warningLabel = UILabel()
    private let chirpDebugLabel = UILabel()
    /// Last upload status, short label: "暫存完成", "時間校正完成" etc.
    private let uploadStatusLabel = UILabel()

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

    // Nav-bar toggle that flips `settings.parkCameraInStandby`. Title
    // reflects the **current** state so a tap is read as "flip this".
    // Lives next to Settings so the operator can jump between "live
    // framing" and "parked / cool" in one tap without entering a modal.
    private lazy var previewToggleItem: UIBarButtonItem = {
        let item = UIBarButtonItem(
            title: "",
            style: .plain,
            target: self,
            action: #selector(togglePreviewPark)
        )
        return item
    }()

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

        navigationItem.leftBarButtonItems = [
            UIBarButtonItem(
                title: "Settings",
                style: .plain,
                target: self,
                action: #selector(openSettings)
            ),
            previewToggleItem,
        ]
        navigationItem.rightBarButtonItems = [
            UIBarButtonItem(title: "時間校正", style: .plain, target: self, action: #selector(onTapTimeCalibration)),
            UIBarButtonItem(title: "位置校正", style: .plain, target: self, action: #selector(openCalibration)),
        ]

        settings = SettingsViewController.loadFromUserDefaults()
        serverConfig = ServerUploader.ServerConfig(serverIP: settings.serverIP, serverPort: settings.serverPort)

        recorder = PitchRecorder()
        recorder.setCameraId(settings.cameraRole)
        recorder.onRecordingStarted = { [weak self] idx in
            DispatchQueue.main.async {
                guard let self else { return }
                DiagnosticsData.shared.update(localRecordingIndex: .some(idx))
                // Keep the "尚未時間校正" warning up while recording — the
                // upload will still go but the server will skip triangulation,
                // and the operator needs to keep seeing why.
                if self.lastSyncAnchorTimestampS != nil {
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
            log.info("camera cycle complete session=\(payload.session_id, privacy: .public) cam=\(payload.camera_id, privacy: .public) has_clip=\(finishingClip != nil)")
            // End-of-recording feedback — haptic + system sound so the
            // operator knows the cycle finished without looking down.
            DispatchQueue.main.async {
                AudioServicesPlaySystemSound(self.endRecSoundID)
                self.endRecHaptic.notificationOccurred(.success)
            }
            let enriched = self.enrichedPayload(from: payload)
            if let finishingClip {
                finishingClip.finish { [weak self] videoURL in
                    self?.handleFinishedClip(enriched: enriched, videoURL: videoURL)
                }
            } else {
                // Mode-two (no clip recorder built) or mode-one with a
                // failed bootstrap — either way handleFinishedClip owns the
                // drain-frames-and-persist dance. In mode-two it reads the
                // detection buffer and routes through handleOnDeviceCycle;
                // in the degenerate mode-one case with no MOV it falls
                // through to a JSON-only upload.
                self.handleFinishedClip(enriched: enriched, videoURL: nil)
            }
        }

        do {
            try payloadStore.ensureDirectory()
        } catch {
            lastUploadStatusText = "暫存初始化失敗 · \(error.localizedDescription)"
        }

        uploader = ServerUploader(config: serverConfig)
        uploadQueue = PayloadUploadQueue(store: payloadStore, uploader: uploader)
        wireUploadQueueCallbacks()
        // Rehydrate whatever payloads are sitting in Documents from a
        // previous run (or a prior cycle that hit a transient network
        // error) and kick the worker. Done at viewDidLoad so the queue
        // lifecycle is decoupled from session arm/disarm — cached pitches
        // upload as soon as the server is reachable, not "next time the
        // operator arms a session".
        try? uploadQueue.reloadPending()
        uploadQueue.processNextIfNeeded()

        healthMonitor = ServerHealthMonitor(
            uploader: uploader,
            cameraId: settings.cameraRole,
            baseIntervalS: settings.pollInterval
        )
        wireHealthMonitorCallbacks()

        setupUI()
        setupPreviewAndCapture()
        setupAudioCapture()
        setupDisplayLink()
        healthMonitor.start()
        updatePreviewToggleTitle()
        updateUIForState()
    }

    @objc private func openCalibration() {
        let vc = CalibrationChooserViewController()
        let nav = UINavigationController(rootViewController: vc)
        nav.modalPresentationStyle = .fullScreen
        present(nav, animated: true)
    }

    @objc private func togglePreviewPark() {
        let newValue = !settings.parkCameraInStandby
        SettingsViewController.setParkCameraInStandby(newValue)
        // Re-diff with the rest of settings-driven wiring untouched. The
        // park branch inside applyUpdatedSettings handles start/stop when
        // state == .standby; other states are no-ops.
        applyUpdatedSettings()
    }

    /// Sync the nav-bar button title to `settings.parkCameraInStandby`.
    /// Title shows the **current** mode so a tap reads as "flip this".
    private func updatePreviewToggleTitle() {
        previewToggleItem.title = settings.parkCameraInStandby
            ? "預覽：關"
            : "預覽：開"
    }

    @objc private func openSettings() {
        let vc = SettingsViewController()
        vc.cameraVC = self
        vc.onDismiss = { [weak self] in
            self?.applyUpdatedSettings()
        }
        let nav = UINavigationController(rootViewController: vc)
        nav.modalPresentationStyle = .formSheet
        present(nav, animated: true)
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        applyUpdatedSettings()
        updateUIForState()
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        // Default: capture session stays OFF in standby — the phone was
        // heating up from an idle 60 fps preview. Session is spun up on-
        // demand only by `enterRecordingMode` (arm) or `startTimeSync`
        // (manual 時間校正). Settings → Camera → "Park in STANDBY" can flip
        // this off; in that case we keep the preview running at the idle
        // fps so the operator gets a continuous framing aid.
        if !settings.parkCameraInStandby && state == .standby {
            startCapture(at: standbyFps)
        }
        healthMonitor.start()
        displayLink?.isPaused = false
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        healthMonitor.stop()
        displayLink?.isPaused = true
        if state == .timeSyncWaiting {
            cancelTimeSync()
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
        if lastSyncAnchorTimestampS == nil {
            warningLabel.text = "尚未時間校正，將無法三角化"
            warningLabel.textColor = DesignTokens.Colors.ink
            warningLabel.isHidden = false
            log.warning("arm without time sync anchor — server will skip triangulation")
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
        if settings.parkCameraInStandby {
            stopCapture()
        } else {
            // Keep the preview live — drop fps back to idle so the sensor
            // stops running at 240.
            switchCaptureFps(standbyFps)
        }
        updateUIForState()
    }

    // Pre/post-roll margins around the detected ball window. Two-camera
    // trim ranges rarely align to the same frame (parallax + occlusion
    // differences), so we pad the window on both ends — 300 ms absorbs up
    // to ~250 ms of A/B disagreement while still shrinking the upload to
    // under 2 s in the common case.
    private static let trimPreRollS: Double = 0.3
    private static let trimPostRollS: Double = 0.3
    /// Hard ceiling on the trimmed clip duration, measured from the first
    /// detection (minus pre-roll). Covers a realistic pitch + follow-through
    /// without blowing the upload budget.
    private static let trimMaxDurationS: Double = 1.5

    /// Cycle-complete router. Branches on the effective capture mode:
    ///   - `onDevice` → attach the drained frames to the payload, delete
    ///     the (unused) MOV, and upload frames-only.
    ///   - `cameraOnly` → use detection timestamps as trim oracle and ship
    ///     the trimmed MOV; `frames` stays empty on the wire because
    ///     server detection is authoritative.
    ///   - `dual` → same as `cameraOnly` (trim the MOV using on-device
    ///     detection as oracle, upload the clip) **plus** attach the
    ///     iOS-end frame list as `frames_on_device` so the server keeps
    ///     both detection streams for side-by-side comparison.
    ///
    /// `currentCaptureMode` is read once at cycle-complete — if the
    /// dashboard flips the toggle mid-recording the session still
    /// finishes in the mode it armed with (that's what Session.mode
    /// guarantees on the server side).
    private func handleFinishedClip(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?
    ) {
        let frames = drainDetectedFrames()
        let mode = currentCaptureMode
        log.info("cycle complete session=\(enriched.session_id, privacy: .public) mode=\(mode.rawValue, privacy: .public) frames=\(frames.count) ball_frames=\(frames.filter { $0.ball_detected }.count) has_video=\(videoURL != nil)")

        if mode == .onDevice {
            handleOnDeviceCycle(enriched: enriched, videoURL: videoURL, frames: frames)
            return
        }
        // Dual carries frames_on_device on top of the MOV path; camera_only
        // leaves frames_on_device empty. Thread the decision via the
        // enriched payload so handleCameraOnlyCycle's trim logic stays
        // agnostic.
        let payloadForUpload: ServerUploader.PitchPayload
        if mode == .dual {
            payloadForUpload = enriched.withFramesOnDevice(frames)
        } else {
            payloadForUpload = enriched
        }
        handleCameraOnlyCycle(enriched: payloadForUpload, videoURL: videoURL, frames: frames)
    }

    /// Mode-two: attach the frame list to the payload, delete any MOV
    /// still sitting in tmp, and ship a frames-only payload (no video
    /// part, bandwidth is a few KB).
    private func handleOnDeviceCycle(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?,
        frames: [ServerUploader.FramePayload]
    ) {
        if let videoURL {
            try? FileManager.default.removeItem(at: videoURL)
        }
        let updated = enriched.withFrames(frames)
        persistCompletedCycle(updated, videoURL: nil)
    }

    /// Mode-one: detection results are a trim oracle only; server re-runs
    /// authoritative detection on the received MOV. Uses the timestamps of
    /// `ball_detected == true` frames to pick the trim window, falling
    /// back to uploading the full MOV when no ball was seen or trim fails.
    private func handleCameraOnlyCycle(
        enriched: ServerUploader.PitchPayload,
        videoURL: URL?,
        frames: [ServerUploader.FramePayload]
    ) {
        guard let videoURL else {
            persistCompletedCycle(enriched, videoURL: nil)
            return
        }

        let ballTimestamps = frames.compactMap { $0.ball_detected ? $0.timestamp_s : nil }
        let originalStart = enriched.video_start_pts_s

        guard !ballTimestamps.isEmpty else {
            log.info("cycle trim skip reason=no_ball_detected session=\(enriched.session_id, privacy: .public)")
            persistCompletedCycle(enriched, videoURL: videoURL)
            return
        }

        let first = ballTimestamps.min() ?? originalStart
        let last = ballTimestamps.max() ?? first
        let desiredStartAbs = max(first - Self.trimPreRollS, originalStart)
        let desiredEndAbs = min(last + Self.trimPostRollS, desiredStartAbs + Self.trimMaxDurationS)
        let startOffsetInMovS = desiredStartAbs - originalStart
        let durationS = max(desiredEndAbs - desiredStartAbs, 0)

        guard durationS > 0 else {
            log.warning("cycle trim degenerate range — uploading full clip session=\(enriched.session_id, privacy: .public)")
            persistCompletedCycle(enriched, videoURL: videoURL)
            return
        }

        log.info("cycle trim plan session=\(enriched.session_id, privacy: .public) ball_frames=\(ballTimestamps.count) window=[\(desiredStartAbs),\(desiredEndAbs)] offset_in_mov=\(startOffsetInMovS) dur=\(durationS)")

        let destination = payloadStore.makeTempVideoURL()
        ClipTrimmer.trim(
            source: videoURL,
            startOffsetFromMovStartS: startOffsetInMovS,
            durationS: durationS,
            destination: destination,
            originalVideoStartPtsS: originalStart
        ) { [weak self] output in
            guard let self else { return }
            guard let output else {
                log.warning("cycle trim failed — uploading full clip session=\(enriched.session_id, privacy: .public)")
                self.persistCompletedCycle(enriched, videoURL: videoURL)
                return
            }
            try? FileManager.default.removeItem(at: videoURL)
            let updated = enriched.withVideoStartPts(output.absoluteStartPtsS)
            log.info("cycle trimmed session=\(enriched.session_id, privacy: .public) abs_start=\(output.absoluteStartPtsS) dur=\(output.durationS)")
            self.persistCompletedCycle(updated, videoURL: output.url)
        }
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

    // MARK: - Time calibration (chirp anchor)

    @objc private func onTapTimeCalibration() {
        if state == .timeSyncWaiting {
            cancelTimeSync()
        } else if state == .standby {
            startTimeSync()
        }
        updateUIForState()
    }

    private func startTimeSync() {
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
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        latestChirpSnapshot = nil
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        // Already at standbyFps — if the operator asked to keep the preview,
        // just leave the session running; otherwise park it.
        if settings.parkCameraInStandby {
            stopCapture()
        }
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
        log.info("camera complete time-sync anchor_frame=\(event.anchorFrameIndex) anchor_ts=\(event.anchorTimestampS) cam=\(self.settings.cameraRole, privacy: .public)")
        timeSyncTimeoutWork?.cancel()
        timeSyncTimeoutWork = nil
        chirpDetector?.onChirpDetected = nil
        lastSyncAnchorTimestampS = event.anchorTimestampS
        // Surface the freshly-acquired anchor to the dashboard via the
        // next heartbeat so the sidebar's "time sync" dot flips green.
        healthMonitor?.updateTimeSynced(true)
        latestChirpSnapshot = nil
        state = .standby
        frameStateBox.update(state: .standby, pendingBootstrap: false, sessionId: nil)
        if settings.parkCameraInStandby {
            stopCapture()
        }
        warningLabel.isHidden = true
        lastUploadStatusText = "時間校正完成"
        updateUIForState()
    }

    // MARK: - Capture setup

    private func setupPreviewAndCapture() {
        session.beginConfiguration()

        if let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) {
            do {
                try configureCaptureFormat(
                    device,
                    targetWidth: settings.captureWidth,
                    targetHeight: settings.captureHeight,
                    targetFps: standbyFps
                )
                IntrinsicsStore.setHorizontalFov(horizontalFovRadians)

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
        preview.videoGravity = .resizeAspectFill
        preview.frame = view.bounds
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
        var selected: AVCaptureDevice.Format?
        for format in device.formats {
            let dims = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            let w = Int(dims.width)
            let h = Int(dims.height)
            let matchesRes = (w == targetWidth && h == targetHeight) || (w == targetHeight && h == targetWidth)
            let supportsFps = format.videoSupportedFrameRateRanges.contains { range in
                range.minFrameRate <= targetFps && range.maxFrameRate >= targetFps
            }
            if matchesRes && supportsFps {
                selected = format
                break
            }
        }
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
        let frameDuration = CMTime(value: 1, timescale: Int32(targetFps))
        device.activeVideoMinFrameDuration = frameDuration
        device.activeVideoMaxFrameDuration = frameDuration

        // Cap the AE exposure time to the target frame duration. Without this,
        // iOS lengthens individual exposures in low light to brighten the
        // image, which drags the effective capture rate down (a room that
        // wants 70 ms exposures drops a "240 fps" session to ~14 fps). Capped
        // AE keeps the sensor locked to the target rate and compensates with
        // ISO — noisier in dim rooms, but frame rate holds.
        let lo = selected.minExposureDuration
        let hi = selected.maxExposureDuration
        let capped: CMTime
        if CMTimeCompare(frameDuration, lo) < 0 {
            capped = lo
        } else if CMTimeCompare(frameDuration, hi) > 0 {
            capped = hi
        } else {
            capped = frameDuration
        }
        device.activeMaxExposureDuration = capped
        device.exposureMode = .continuousAutoExposure

        horizontalFovRadians = Double(device.activeFormat.videoFieldOfView) * Double.pi / 180.0
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
                    targetWidth: self.settings.captureWidth,
                    targetHeight: self.settings.captureHeight,
                    targetFps: targetFps
                )
                IntrinsicsStore.setHorizontalFov(self.horizontalFovRadians)
                // Read back the actually-applied rate so an operator can
                // tell from logs whether the sensor is honouring our
                // request. 240 fps formats typically crop the sensor ROI,
                // so `videoFieldOfView` may differ between 60 and 240 fps
                // — log it per switch so any FOV-approximation intrinsics
                // drift is visible.
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
                    targetWidth: self.settings.captureWidth,
                    targetHeight: self.settings.captureHeight,
                    targetFps: targetFps
                )
                IntrinsicsStore.setHorizontalFov(self.horizontalFovRadians)
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
                targetWidth: settings.captureWidth,
                targetHeight: settings.captureHeight,
                targetFps: targetFps
            )
            IntrinsicsStore.setHorizontalFov(horizontalFovRadians)
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

        let detector = AudioChirpDetector(threshold: Float(settings.chirpThreshold))
        output.setSampleBufferDelegate(detector, queue: detector.deliveryQueue)
        session.commitConfiguration()

        audioInput = input
        audioOutput = output
        chirpDetector = detector
    }

    // MARK: - Layout + overlays

    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        previewLayer?.frame = view.bounds
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
        guard state == .timeSyncWaiting, let detector = chirpDetector else {
            return
        }
        // Read-only access to the last snapshot; audio-queue writes may race
        // but the HUD only needs a near-instant view.
        latestChirpSnapshot = detector.lastSnapshot
        guard let snap = latestChirpSnapshot else { return }
        let statusText: String
        let color: UIColor
        if !snap.armed {
            statusText = "warming up"
            color = DesignTokens.Colors.sub
        } else if snap.triggered {
            statusText = "TRIGGER"
            color = DesignTokens.Colors.success
        } else if snap.lastPeak >= snap.threshold * 0.8 {
            statusText = "close"
            color = DesignTokens.Colors.warning
        } else {
            statusText = "listening"
            color = DesignTokens.Colors.sub
        }
        chirpDebugLabel.text = String(
            format: "peak %.2f / %.2f  buf %d  %@",
            snap.lastPeak, snap.threshold, snap.bufferFillSamples, statusText
        )
        chirpDebugLabel.textColor = color
    }

    // MARK: - Server health + upload queue wiring

    private func wireHealthMonitorCallbacks() {
        healthMonitor.onStatusChanged = { [weak self] text, _ in
            DiagnosticsData.shared.update(serverStatusText: text)
            self?.updateUIForState()
        }
        healthMonitor.onHeartbeatSuccess = { [weak self] response in
            guard let self else { return }
            // Cache the server's session id so `startRecording` can stamp
            // it onto uploads without another round-trip. Nil when idle.
            let sid = (response.session?.armed == true) ? response.session?.id : nil
            self.currentSessionId = sid
            DiagnosticsData.shared.update(sessionId: .some(sid))
            // Effective mode: snapshot from the armed session if present,
            // otherwise the dashboard's global toggle. Unknown / missing
            // fields fall back to cameraOnly for backwards compat with
            // pre-mode-split server builds.
            let modeStr = response.session?.mode ?? response.capture_mode
                ?? ServerUploader.CaptureMode.cameraOnly.rawValue
            let mode = ServerUploader.CaptureMode(rawValue: modeStr) ?? .cameraOnly
            if self.currentCaptureMode != mode {
                log.info("capture mode changed to \(mode.rawValue, privacy: .public)")
            }
            self.currentCaptureMode = mode
            DispatchQueue.main.async {
                self.modeLabel.text = "MODE · \(mode.displayLabel.uppercased())"
            }
            let cam = self.settings.cameraRole
            let cmd = response.commands?[cam]
            // Log every transition so Xcode console shows the server's
            // intent reaching iOS. A steady-state "arm"/"disarm" echoing
            // every second is logged only when the value changed — see
            // handleDashboardCommand's guard.
            if cmd != self.lastAppliedCommand {
                log.info("heartbeat reply session_armed=\(response.session?.armed ?? false) session_id=\(sid ?? "nil", privacy: .public) command=\(cmd ?? "nil", privacy: .public) cam=\(cam, privacy: .public)")
            }
            self.handleDashboardCommand(
                cmd,
                sessionArmed: response.session?.armed ?? false
            )
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
            // A successful upload arrived — clear any sticky red from a
            // previous dropped-payload banner so the green default returns.
            self.lastResultLabel.textColor = DesignTokens.Colors.ok
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
            // Surface a sticky red banner on the "Last:" row — the green
            // success line gets overwritten by the next paired pitch, but
            // a dropped payload is data loss the operator must notice.
            self.lastResultText = "上傳失敗已丟棄: \(basename) — \(detail)"
            self.lastResultLabel.textColor = DesignTokens.Colors.fail
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

    /// Dispatch the server's per-device command. Only reacts to state
    /// *transitions* (tracked via `lastAppliedCommand`) so repeated arm
    /// replies during an armed session don't re-trigger enter.
    private func handleDashboardCommand(_ command: String?, sessionArmed: Bool) {
        defer { lastAppliedCommand = command }
        guard command != lastAppliedCommand else { return }
        switch command {
        case "arm":
            applyRemoteArm()
        case "disarm":
            applyRemoteDisarm()
        default:
            // No pending command. Recording only ends via an explicit
            // disarm — never a silent fallthrough.
            break
        }
        _ = sessionArmed  // reserved for future "did the session id change?" logic
    }

    private func applyRemoteArm() {
        log.info("camera received arm command state=\(Self.stateText(self.state), privacy: .public) session=\(self.currentSessionId ?? "nil", privacy: .public) cam=\(self.settings.cameraRole, privacy: .public)")
        switch state {
        case .standby:
            enterRecordingMode()
        case .timeSyncWaiting, .recording, .uploading:
            // Active state — arm is a no-op. `.timeSyncWaiting` finishes
            // on its own and returns to standby; next heartbeat re-sends
            // the arm command and this branch flips us into recording.
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

    /// Force an immediate heartbeat probe, resetting backoff. Exposed for
    /// the Settings → Diagnostics "Test connection" action (formerly a
    /// HUD button on the main screen).
    func testServerConnection() {
        healthMonitor.resetBackoff()
        healthMonitor.probeNow()
    }

    /// Settings-dismiss callback. Re-diffs UserDefaults and reconfigures
    /// anything settings-driven. Replaces the old per-poll diffing.
    private func applyUpdatedSettings() {
        let latest = SettingsViewController.loadFromUserDefaults()
        let serverChanged = latest.serverIP != settings.serverIP
            || latest.serverPort != settings.serverPort
        let formatChanged = latest.captureWidth != settings.captureWidth
            || latest.captureHeight != settings.captureHeight
        let chirpThresholdChanged = latest.chirpThreshold != settings.chirpThreshold
        let pollIntervalChanged = latest.pollInterval != settings.pollInterval
        let cameraRoleChanged = latest.cameraRole != settings.cameraRole
        let parkChanged = latest.parkCameraInStandby != settings.parkCameraInStandby

        settings = latest

        if serverChanged {
            serverConfig = ServerUploader.ServerConfig(serverIP: latest.serverIP, serverPort: latest.serverPort)
            uploader = ServerUploader(config: serverConfig)
            healthMonitor.updateUploader(uploader)
            uploadQueue.updateUploader(uploader)
        }
        if formatChanged {
            // User picked a different capture resolution in Settings.
            // `settings` has already been updated to `latest` above, so
            // switchCaptureFps → configureCaptureFormat will look up the
            // new target dims via self.settings.
            let fps = (state == .standby || state == .timeSyncWaiting) ? standbyFps : trackingFps
            switchCaptureFps(fps)
        }
        if chirpThresholdChanged {
            chirpDetector?.setThreshold(Float(latest.chirpThreshold))
        }
        if cameraRoleChanged {
            healthMonitor.updateCameraId(latest.cameraRole)
        }
        if pollIntervalChanged {
            healthMonitor.updateBaseInterval(latest.pollInterval)
        }
        recorder?.setCameraId(latest.cameraRole)

        if serverChanged || pollIntervalChanged {
            // New endpoint or new cadence — invalidate in-flight probe, reset
            // backoff, and re-probe immediately so the HUD reflects reality.
            healthMonitor.resetBackoff()
            healthMonitor.probeNow()
        }

        // Park-mode only takes effect in standby — the active states own
        // their own capture lifecycle and we'd rather not yank the session
        // out from under a live recording or a time-sync listener.
        if parkChanged && state == .standby {
            if latest.parkCameraInStandby {
                stopCapture()
            } else {
                startCapture(at: standbyFps)
            }
        }
        if parkChanged {
            updatePreviewToggleTitle()
        }
    }

    /// Forward the heartbeat monitor's tick to the shared Diagnostics
    /// singleton so Settings → Diagnostics can render it. The main HUD
    /// itself no longer shows "last contact" — Ready card's server row
    /// answers the operator's "am I online?" question on its own.
    private func updateLastContactLabel(from date: Date?) {
        DiagnosticsData.shared.update(lastContactAt: date)
    }

    private func setupUI() {
        // Top-left state chip.
        topStatusChip.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(topStatusChip)

        // Top-right mode indicator. Populated from heartbeat replies; shows
        // the effective capture mode so the operator always knows whether
        // an arm will record+upload MOV (camera-only) or run detection on
        // device (on-device).
        modeLabel.font = DesignTokens.Fonts.mono(size: 11, weight: .medium)
        modeLabel.textColor = DesignTokens.Colors.sub
        modeLabel.textAlignment = .right
        modeLabel.translatesAutoresizingMaskIntoConstraints = false
        modeLabel.text = "MODE · CAMERA-ONLY"
        view.addSubview(modeLabel)

        // Transient banner for error / progress text. Hidden by default;
        // state-change paths set text + reveal, and a timer usually hides it.
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

        // Chirp peak / buffer debug — only visible during time-sync. This
        // is the one on-screen debug overlay we keep because the operator
        // needs it to tune chirp threshold in real time.
        chirpDebugLabel.font = DesignTokens.Fonts.mono(size: 13, weight: .medium)
        chirpDebugLabel.textColor = DesignTokens.Colors.sub
        chirpDebugLabel.numberOfLines = 0
        chirpDebugLabel.textAlignment = .center
        chirpDebugLabel.translatesAutoresizingMaskIntoConstraints = false
        chirpDebugLabel.isHidden = true
        view.addSubview(chirpDebugLabel)

        // Last successful result — hidden until the first result arrives so
        // an empty "(尚無結果)" doesn't linger as noise on cold launch.
        lastResultLabel.font = DesignTokens.Fonts.sans(size: 14, weight: .medium)
        lastResultLabel.textColor = DesignTokens.Colors.ok
        lastResultLabel.numberOfLines = 2
        lastResultLabel.translatesAutoresizingMaskIntoConstraints = false
        lastResultLabel.isHidden = true
        view.addSubview(lastResultLabel)

        // Upload status — one-liner under the Ready card. Hidden when the
        // text is "Idle" so only meaningful phases show up.
        uploadStatusLabel.font = DesignTokens.Fonts.sans(size: 13, weight: .medium)
        uploadStatusLabel.textColor = DesignTokens.Colors.sub
        uploadStatusLabel.numberOfLines = 1
        uploadStatusLabel.translatesAutoresizingMaskIntoConstraints = false
        uploadStatusLabel.isHidden = true
        view.addSubview(uploadStatusLabel)

        // The Ready card. Centered horizontally, sitting above the bottom
        // safe area so it doesn't fight the camera preview mid-frame.
        view.addSubview(readyCard)

        NSLayoutConstraint.activate([
            topStatusChip.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: DesignTokens.Spacing.m),
            topStatusChip.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),

            modeLabel.centerYAnchor.constraint(equalTo: topStatusChip.centerYAnchor),
            modeLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.m),

            warningLabel.topAnchor.constraint(equalTo: topStatusChip.bottomAnchor, constant: DesignTokens.Spacing.s),
            warningLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            warningLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),

            readyCard.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            readyCard.widthAnchor.constraint(lessThanOrEqualToConstant: 480),
            readyCard.leadingAnchor.constraint(greaterThanOrEqualTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            readyCard.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
            readyCard.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -DesignTokens.Spacing.xl),

            uploadStatusLabel.topAnchor.constraint(equalTo: readyCard.bottomAnchor, constant: DesignTokens.Spacing.s),
            uploadStatusLabel.centerXAnchor.constraint(equalTo: view.centerXAnchor),

            lastResultLabel.topAnchor.constraint(equalTo: uploadStatusLabel.bottomAnchor, constant: DesignTokens.Spacing.xs),
            lastResultLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            lastResultLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),

            chirpDebugLabel.bottomAnchor.constraint(equalTo: readyCard.topAnchor, constant: -DesignTokens.Spacing.s),
            chirpDebugLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            chirpDebugLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),
        ])

        setupStateBorder()
        setupRecIndicator()
    }

    /// Recompute ready-card gate states + hint, then repaint. Called every
    /// time `updateUIForState` fires.
    private func updateReadyCard() {
        let calibrationOK = IntrinsicsStore.hasHomography()
        let timeSyncOK = lastSyncAnchorTimestampS != nil
        let serverOK = healthMonitor?.isReachable ?? false

        let hint: String
        if !serverOK {
            hint = "無法接收開始指令：檢查 Wi-Fi 或 Settings 中的伺服器 IP"
        } else if !calibrationOK {
            hint = "本機還沒校正鏡頭位置，無法三角化"
        } else if !timeSyncOK {
            hint = "尚未時間校正，錄影將無法與另一台配對"
        } else {
            hint = "等待開始指令…（由 Dashboard 控制）"
        }

        let calibrationGate = ReadyCard.Gate(
            state: calibrationOK ? .pass : .fail,
            label: "位置校正",
            action: calibrationOK ? nil : "按這裡開始",
            onTap: calibrationOK ? nil : { [weak self] in self?.openCalibration() }
        )
        let timeSyncGate = ReadyCard.Gate(
            state: timeSyncOK ? .pass : (state == .timeSyncWaiting ? .pending : .fail),
            label: "時間校正",
            action: (timeSyncOK || state == .timeSyncWaiting) ? nil : "按這裡開始",
            onTap: (timeSyncOK || state == .timeSyncWaiting) ? nil : { [weak self] in self?.onTapTimeCalibration() }
        )
        let serverGate = ReadyCard.Gate(
            state: serverOK ? .pass : .fail,
            label: serverOK ? "伺服器已連線" : "伺服器離線",
            action: serverOK ? nil : "檢查 Wi-Fi / Settings IP",
            onTap: nil
        )
        readyCard.update(.init(
            cameraRole: settings.cameraRole,
            calibration: calibrationGate,
            timeSync: timeSyncGate,
            server: serverGate,
            hint: hint
        ))
        readyCard.isHidden = (state != .standby)
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
        // Upload status — hide the uninformative "Idle" default so the HUD
        // only surfaces meaningful phases ("暫存完成", "時間校正完成", etc).
        if lastUploadStatusText.isEmpty || lastUploadStatusText == "Idle" {
            uploadStatusLabel.isHidden = true
            uploadStatusLabel.text = nil
        } else {
            uploadStatusLabel.isHidden = false
            uploadStatusLabel.text = lastUploadStatusText
        }

        // Last result line — reveal only once a real result has landed.
        if !hasReceivedFirstResult {
            lastResultLabel.isHidden = true
        } else {
            lastResultLabel.isHidden = false
            lastResultLabel.text = lastResultText
        }

        chirpDebugLabel.isHidden = (state != .timeSyncWaiting)
        updateReadyCard()

        // State-transition-only side effects: re-running applyStateVisuals
        // on every tick resets the REC timer and rebuilds the pulse
        // animation, so edge-trigger it here. Label / colour refresh above
        // stays level-triggered.
        if lastRenderedState != state {
            applyStateVisuals()
            lastRenderedState = state
        }
    }

    /// True once at least one upload result (success or fail) has been
    /// observed, so we can start showing the `lastResultLabel`. Before then
    /// the placeholder text would just be noise on first launch.
    private var hasReceivedFirstResult: Bool {
        lastResultText != Self.initialLastResultText
    }
    private static let initialLastResultText = "(尚無結果)"

    static func stateText(_ state: AppState) -> String {
        switch state {
        case .standby: return "STANDBY"
        case .timeSyncWaiting: return "TIME_SYNC"
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
        if latestImageWidth != width || latestImageHeight != height {
            IntrinsicsStore.setImageDimensions(width: width, height: height)
        }
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

        // Only the `.recording` state cares about samples.
        guard snap.state == .recording else { return }

        // Both camera_only and dual need the MOV recorder — dual bundles
        // the MOV alongside its on-device frame list. Only on_device skips
        // ClipRecorder entirely (bandwidth savings, no clip on disk).
        let needsVideoRecorder = currentCaptureMode != .onDevice

        if needsVideoRecorder {
            // Lazy-bootstrap the ClipRecorder from the first real sample's
            // dimensions (deferred from enterRecordingMode so we can key
            // off whatever the sensor is actually delivering post-fps-
            // switch). `consumePendingBootstrap` clears the flag atomically
            // so a simultaneous main-thread push can't race us into
            // starting the writer twice.
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
        } else {
            // Mode-two: no MOV is being written. Drain the pendingBootstrap
            // flag so a later arm that flips back to camera_only doesn't
            // inherit a stale "please prepare" request (Session.mode is
            // frozen per-arm on the server, but belt+braces here).
            _ = frameStateBox.consumePendingBootstrap()
        }

        // Detection runs in both modes — same BTDetectionSession algorithm,
        // just different fates for its output (trim oracle vs. uploaded
        // payload).
        dispatchDetectionIfDue(pixelBuffer: pixelBuffer, timestampS: timestampS)

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
                anchorTimestampS: lastSyncAnchorTimestampS,
                videoStartPtsS: timestampS,
                videoFps: trackingFps
            )
        }
    }

    /// Fire off one BTDetectionSession step on `detectionQueue` if (a) the
    /// throttle window has elapsed (≥60 Hz cap) and (b) no previous
    /// detection is still running. Called from `captureOutput` — must not
    /// block.
    ///
    /// A `detectionGeneration` check inside the async closure guards against
    /// a late detection landing in the wrong cycle's buffer (enter/exit of
    /// recording bumps the generation).
    ///
    /// Every dispatched detection produces a FramePayload entry: the server's
    /// pipeline also records one entry per decoded frame (with null px/py
    /// when no ball), so we mirror that shape to keep the two sides
    /// substitutable.
    private func dispatchDetectionIfDue(pixelBuffer: CVPixelBuffer, timestampS: TimeInterval) {
        detectionStateLock.lock()
        let elapsed = timestampS - lastDetectionDispatchTimeS
        let shouldDispatch = !detectionInFlight && elapsed >= Self.detectionIntervalS
        let gen = detectionGeneration
        guard let session = detectionSession, shouldDispatch else {
            detectionStateLock.unlock()
            return
        }
        detectionInFlight = true
        lastDetectionDispatchTimeS = timestampS
        let callIndex = detectionCallIndex
        detectionCallIndex += 1
        detectionStateLock.unlock()

        detectionQueue.async { [weak self] in
            guard let self else { return }
            let detection = session.apply(pixelBuffer)
            self.detectionStateLock.lock()
            defer { self.detectionStateLock.unlock() }
            // Generation mismatch means the recording cycle we were dispatched
            // for has already ended — drop this result on the floor instead
            // of contaminating the fresh cycle's buffer.
            guard gen == self.detectionGeneration else { return }
            self.detectionInFlight = false
            let frame = ServerUploader.FramePayload(
                frame_index: callIndex,
                timestamp_s: timestampS,
                px: detection.map { Double($0.px) },
                py: detection.map { Double($0.py) },
                ball_detected: detection != nil
            )
            self.detectionFramesBuffer.append(frame)
        }
    }

    /// Bump the detection generation, rebuild the session, clear the
    /// buffer, reset the throttle. Called at both ends of a recording
    /// cycle so no stale MOG2 state or detections bleed across arms.
    private func resetBallDetectionState() {
        detectionStateLock.lock()
        detectionGeneration &+= 1
        detectionFramesBuffer.removeAll()
        detectionInFlight = false
        lastDetectionDispatchTimeS = 0
        detectionCallIndex = 0
        detectionSession = BTDetectionSession()
        detectionStateLock.unlock()
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
        DiagnosticsData.shared.update(fpsEstimate: fpsEstimate)
    }

    // MARK: - Payload enrichment

    private func enrichedPayload(from payload: ServerUploader.PitchPayload) -> ServerUploader.PitchPayload {
        let dims = IntrinsicsStore.loadImageDimensions()
        return ServerUploader.PitchPayload(
            camera_id: payload.camera_id,
            session_id: payload.session_id,
            sync_anchor_timestamp_s: payload.sync_anchor_timestamp_s,
            video_start_pts_s: payload.video_start_pts_s,
            video_fps: payload.video_fps,
            local_recording_index: payload.local_recording_index,
            intrinsics: IntrinsicsStore.loadIntrinsicsPayload(),
            homography: IntrinsicsStore.loadHomography(),
            image_width_px: dims?.width,
            image_height_px: dims?.height,
            frames: payload.frames,
            frames_on_device: payload.frames_on_device
        )
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

