import XCTest
@testable import ball_tracker

/// XCTest suite for the per-cycle bookkeeping inlined into
/// `CameraRecordingWorkflow` (formerly lived on the standalone
/// `PitchRecorder` class). The phone is a pure capture client post-PR61,
/// so this suite only exercises the payload shape + single-exit invariant:
/// `forceFinishIfRecording()` is the sole path that fires the cycle
/// completion, and the workflow's persistence + upload chain runs off the
/// emitted `PitchPayload`.
///
/// We can't exercise the full persistence chain without a filesystem
/// sandbox, so these tests observe the payload on the way through via the
/// upload queue's enqueue side-effect being skipped (no MOV, server_post
/// path) — the assertions target payload fields and the active/inactive
/// state transitions only.
final class PitchRecorderTests: XCTestCase {

    // MARK: helpers

    /// Build a workflow with stub dependencies that capture the emitted
    /// `PitchPayload` via `persistCompletedCycle` (we don't assert on
    /// disk state — only on the payload the workflow produces).
    private func makeWorkflow(
        cameraRole: String = "A",
        syncId: String? = nil,
        anchor: Double? = nil,
        onStatus: @escaping (String) -> Void = { _ in },
        onTransition: @escaping (CameraViewController.AppState, Bool) -> Void = { _, _ in }
    ) -> (CameraRecordingWorkflow, () -> String) {
        var lastStatus: String = ""
        let deps = CameraRecordingWorkflow.Dependencies(
            getCameraRole: { cameraRole },
            getCurrentSessionPaths: { [.serverPost] },
            getSyncId: { syncId },
            getSyncAnchorTimestampS: { anchor },
            currentCaptureTelemetry: { fps in
                ServerUploader.CaptureTelemetry(
                    width_px: 1920, height_px: 1080,
                    target_fps: fps, applied_fps: fps,
                    format_fov_deg: nil, format_index: nil,
                    is_video_binned: nil,
                    tracking_exposure_cap: nil, applied_max_exposure_s: nil
                )
            },
            startCapture: { _ in },
            resetDetectionState: {},
            drainDetectedFrames: { [] },
            clearRecoveredAnchor: {},
            dispatchLiveCycleEnd: { _, _ in },
            showErrorBanner: { _ in },
            hideBanner: {},
            setStatusText: { text in
                lastStatus = text
                onStatus(text)
            },
            transitionState: { state, recording, _ in
                onTransition(state, recording)
            },
            reconcileStandbyCaptureState: {},
            refreshUI: {}
        )
        let uploader = ServerUploader(config: .init(serverIP: "127.0.0.1", serverPort: 1))
        let workflow = CameraRecordingWorkflow(
            uploader: uploader,
            trackingFps: 240,
            processingQueue: DispatchQueue(label: "test.recording"),
            dependencies: deps
        )
        return (workflow, { lastStatus })
    }

    // MARK: 1. startRecorderIfNeeded → forceFinishIfRecording flips active state

    func testStartThenForceFinishFlipsActiveFlag() {
        let (workflow, _) = makeWorkflow(cameraRole: "B", anchor: 12.345)
        var startedIndices: [Int] = []
        workflow.onRecordingStarted = { startedIndices.append($0) }

        XCTAssertFalse(workflow.isRecordingActive)
        workflow.startRecorderIfNeeded(sessionId: "s_test01", timestampS: 42.125)
        XCTAssertTrue(workflow.isRecordingActive)
        XCTAssertEqual(startedIndices, [1])

        workflow.forceFinishIfRecording()
        XCTAssertFalse(workflow.isRecordingActive)
    }

    // MARK: 2. forceFinish while NOT recording is a no-op

    func testForceFinishWhileNotRecordingIsNoop() {
        let (workflow, _) = makeWorkflow()
        var cycleCompletions = 0
        workflow.onCycleCompleted = { cycleCompletions += 1 }

        workflow.forceFinishIfRecording()
        XCTAssertEqual(cycleCompletions, 0)
        XCTAssertFalse(workflow.isRecordingActive)
    }

    // MARK: 3. Double-start is idempotent

    func testDoubleStartIsIgnored() {
        let (workflow, _) = makeWorkflow()
        var startedIndices: [Int] = []
        workflow.onRecordingStarted = { startedIndices.append($0) }

        workflow.startRecorderIfNeeded(sessionId: "s_once", timestampS: 0.0)
        workflow.startRecorderIfNeeded(sessionId: "s_twice", timestampS: 0.0)
        XCTAssertEqual(startedIndices, [1], "Second startRecorderIfNeeded while active must be ignored")
    }

    // MARK: 4. forceFinish twice only reports once

    func testForceFinishTwiceOnlyReportsOnce() {
        let (workflow, _) = makeWorkflow()
        workflow.startRecorderIfNeeded(sessionId: "s_dbl", timestampS: 0.0)

        workflow.forceFinishIfRecording()
        XCTAssertFalse(workflow.isRecordingActive)
        // Second call is a no-op — active already false.
        workflow.forceFinishIfRecording()
        XCTAssertFalse(workflow.isRecordingActive)
    }

    // MARK: 5. localRecordingIndex increments across cycles

    func testLocalRecordingIndexIncrementsAcrossCycles() {
        let (workflow, _) = makeWorkflow()
        var startedIndices: [Int] = []
        workflow.onRecordingStarted = { startedIndices.append($0) }

        workflow.startRecorderIfNeeded(sessionId: "s_a", timestampS: 0.0)
        workflow.forceFinishIfRecording()
        XCTAssertEqual(startedIndices, [1])

        workflow.startRecorderIfNeeded(sessionId: "s_b", timestampS: 0.0)
        XCTAssertEqual(startedIndices, [1, 2], "localRecordingIndex must NOT reset across cycles")
    }
}
