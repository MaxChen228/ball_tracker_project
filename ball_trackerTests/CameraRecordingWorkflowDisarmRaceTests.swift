import XCTest
@testable import ball_tracker

/// Race-focused coverage for `CameraRecordingWorkflow.handleRemoteDisarm`.
///
/// The handler enqueues work onto the workflow's `processingQueue`; these
/// tests build the workflow with a serial test queue and barrier-wait via
/// `processingQueue.sync {}` so assertions observe the settled state.
final class CameraRecordingWorkflowDisarmRaceTests: XCTestCase {

    // MARK: helpers

    /// Records every transition the workflow emits so we can assert on the
    /// final state and the sequence leading into it.
    final class TransitionRecorder {
        private let lock = NSLock()
        private var events: [(CameraViewController.AppState, Bool, String?)] = []

        func record(_ state: CameraViewController.AppState, _ recording: Bool, _ sid: String?) {
            lock.lock()
            events.append((state, recording, sid))
            lock.unlock()
        }

        var all: [(CameraViewController.AppState, Bool, String?)] {
            lock.lock()
            defer { lock.unlock() }
            return events
        }

        var finalState: CameraViewController.AppState? { all.last?.0 }
    }

    private func makeWorkflow(
        processingQueue: DispatchQueue,
        recorder: TransitionRecorder = TransitionRecorder()
    ) -> (CameraRecordingWorkflow, TransitionRecorder) {
        let deps = CameraRecordingWorkflow.Dependencies(
            getCameraRole: { "A" },
            getCurrentSessionPaths: { [.live] },
            getSyncId: { nil },
            getSyncAnchorTimestampS: { 1.0 },
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
            clearRecoveredAnchor: {},
            dispatchLiveCycleEnd: { _, _ in },
            showErrorBanner: { _ in },
            hideBanner: {},
            setStatusText: { _ in },
            transitionState: { state, recording, sid in
                recorder.record(state, recording, sid)
            },
            reconcileStandbyCaptureState: {},
            refreshUI: {}
        )
        let uploader = ServerUploader(config: .init(serverIP: "127.0.0.1", serverPort: 1))
        let workflow = CameraRecordingWorkflow(
            uploader: uploader,
            trackingFps: 240,
            processingQueue: processingQueue,
            dependencies: deps
        )
        return (workflow, recorder)
    }

    /// Drain the workflow's processingQueue + main queue so every async
    /// hop in handleRemoteDisarm / handleCycleComplete settles before
    /// assertions run.
    private func drain(processingQueue: DispatchQueue) {
        processingQueue.sync {}
        // handleCycleComplete / exitRecordingToStandby bounce back to main.
        let exp = expectation(description: "main queue drain")
        DispatchQueue.main.async { exp.fulfill() }
        wait(for: [exp], timeout: 2.0)
        // Second barrier — persist path can hop back to processingQueue
        // again (clipRecorder cleanup), so drain once more.
        processingQueue.sync {}
        let exp2 = expectation(description: "main queue drain 2")
        DispatchQueue.main.async { exp2.fulfill() }
        wait(for: [exp2], timeout: 2.0)
    }

    // MARK: 1. arm → disarm without any frame → settles to standby

    func testDisarmBeforeAnyFrameFallsBackToStandby() {
        let q = DispatchQueue(label: "test.disarm.nofrm")
        let (workflow, recorder) = makeWorkflow(processingQueue: q)

        workflow.enterRecordingMode(sessionId: "s_abc01234", serverTimeSyncConfirmed: true)
        XCTAssertFalse(workflow.isRecordingActive, "no frame → startRecorderIfNeeded not called")

        // handleRemoteDisarm must see `active == false` and take the
        // warning branch (clipRecorder.cancel, jump to standby).
        workflow.handleRemoteDisarm(currentSessionId: "s_abc01234", currentState: .recording)
        drain(processingQueue: q)

        XCTAssertFalse(workflow.isRecordingActive)
        XCTAssertEqual(recorder.finalState, .standby,
                       "disarm-before-frame must end in .standby, not stall in .recording")
    }

    // MARK: 2. arm → startRecorderIfNeeded (first-frame proxy) → disarm →
    //         forceFinish runs and cycle completes

    func testDisarmAfterFirstFrameFinishesCycle() {
        let q = DispatchQueue(label: "test.disarm.afterfrm")
        let (workflow, recorder) = makeWorkflow(processingQueue: q)

        workflow.enterRecordingMode(sessionId: "s_deadbeef", serverTimeSyncConfirmed: true)

        // Simulate the captureOutput path that would bootstrap the clip
        // recorder and start the per-cycle bookkeeping. `bootstrapClipRecorder`
        // is a no-op at the workflow level unless `prepare` succeeds — we
        // skip it here (the test queue has no AVAssetWriter surface) and
        // just invoke `startRecorderIfNeeded`, which is what the capture
        // path does after the clip is ready.
        var completionCount = 0
        workflow.onCycleCompleted = { completionCount += 1 }

        workflow.startRecorderIfNeeded(sessionId: "s_deadbeef", timestampS: 0.125)
        XCTAssertTrue(workflow.isRecordingActive)

        workflow.handleRemoteDisarm(currentSessionId: "s_deadbeef", currentState: .recording)
        drain(processingQueue: q)

        XCTAssertFalse(workflow.isRecordingActive,
                       "forceFinish flips isRecording false")
        XCTAssertEqual(completionCount, 1,
                       "forceFinish path must emit exactly one cycle completion")
        XCTAssertEqual(recorder.finalState, .standby)
    }

    // MARK: 3. Rapid arm → disarm → arm → disarm convergence

    func testRapidArmDisarmConvergesToStandby() {
        let q = DispatchQueue(label: "test.disarm.rapid")
        let (workflow, recorder) = makeWorkflow(processingQueue: q)

        for i in 0..<5 {
            workflow.enterRecordingMode(sessionId: "s_loop\(String(format: "%04d", i))", serverTimeSyncConfirmed: true)
            workflow.handleRemoteDisarm(currentSessionId: "s_loop\(String(format: "%04d", i))", currentState: .recording)
            drain(processingQueue: q)
        }
        XCTAssertFalse(workflow.isRecordingActive)
        XCTAssertEqual(recorder.finalState, .standby,
                       "5 rapid arm/disarm cycles must still end in .standby")
    }

    // MARK: 4. Double disarm is safe

    func testDoubleDisarmIsSafe() {
        let q = DispatchQueue(label: "test.disarm.double")
        let (workflow, recorder) = makeWorkflow(processingQueue: q)

        workflow.enterRecordingMode(sessionId: "s_double00", serverTimeSyncConfirmed: true)
        workflow.startRecorderIfNeeded(sessionId: "s_double00", timestampS: 0.0)

        var completionCount = 0
        workflow.onCycleCompleted = { completionCount += 1 }

        workflow.handleRemoteDisarm(currentSessionId: "s_double00", currentState: .recording)
        workflow.handleRemoteDisarm(currentSessionId: "s_double00", currentState: .recording)
        drain(processingQueue: q)

        XCTAssertFalse(workflow.isRecordingActive)
        XCTAssertEqual(completionCount, 1,
                       "Second disarm sees isRecording=false and must not emit a second cycle")
        XCTAssertEqual(recorder.finalState, .standby)
    }

}
