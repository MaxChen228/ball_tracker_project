import XCTest
@testable import ball_tracker

/// Unit coverage for `CameraStateController` + `FrameStateBox`. Previously
/// zero tests — the state machine is the heart of the iOS side so any
/// regression in `transition(to:pendingBootstrap:sessionId:)` or
/// `consumePendingBootstrap` could silently desync the capture queue from
/// the main queue view of the session.
final class CameraStateControllerTests: XCTestCase {

    private func makeController() -> (CameraStateController, FrameStateBox, () -> Int) {
        let box = FrameStateBox()
        var uiRefreshCount = 0
        let controller = CameraStateController(
            frameStateBox: box,
            onStateChanged: { uiRefreshCount += 1 }
        )
        return (controller, box, { uiRefreshCount })
    }

    // MARK: basic forward transitions

    func testStandbyToTimeSyncWaitingAndBack() {
        let (ctl, box, uiCount) = makeController()
        XCTAssertEqual(ctl.currentState, .standby)

        ctl.transition(to: .timeSyncWaiting)
        XCTAssertEqual(ctl.currentState, .timeSyncWaiting)
        XCTAssertEqual(box.snapshot().state, .timeSyncWaiting)
        XCTAssertEqual(uiCount(), 1)

        ctl.transition(to: .standby)
        XCTAssertEqual(ctl.currentState, .standby)
        XCTAssertEqual(box.snapshot().state, .standby)
        XCTAssertEqual(uiCount(), 2)
    }

    func testStandbyRecordingUploadingStandbyCycle() {
        let (ctl, box, _) = makeController()

        ctl.transition(to: .recording, pendingBootstrap: true, sessionId: "s_rec01234")
        XCTAssertEqual(ctl.currentState, .recording)
        let recSnap = box.snapshot()
        XCTAssertEqual(recSnap.state, .recording)
        XCTAssertTrue(recSnap.pendingBootstrap)
        XCTAssertEqual(recSnap.sessionId, "s_rec01234")

        ctl.transition(to: .standby, sessionId: nil)
        XCTAssertEqual(ctl.currentState, .standby)
        XCTAssertNil(box.snapshot().sessionId)
    }

    // MARK: refreshUI=false skips the callback

    func testRefreshUIFalseDoesNotFireCallback() {
        let (ctl, _, uiCount) = makeController()
        ctl.transition(to: .recording, sessionId: "s_silent00", refreshUI: false)
        XCTAssertEqual(uiCount(), 0, "refreshUI: false must skip onStateChanged")
        XCTAssertEqual(ctl.currentState, .recording)
    }

    // MARK: pendingBootstrap is one-shot, consumed atomically

    func testConsumePendingBootstrapOneShot() {
        let (ctl, _, _) = makeController()
        ctl.transition(to: .recording, pendingBootstrap: true, sessionId: "s_boot0000")
        XCTAssertTrue(ctl.consumePendingBootstrap(),
                      "First consume returns true (pending)")
        XCTAssertFalse(ctl.consumePendingBootstrap(),
                       "Second consume returns false — one-shot flag")
    }

    func testConsumePendingBootstrapAcrossRecordings() {
        let (ctl, _, _) = makeController()
        ctl.transition(to: .recording, pendingBootstrap: true, sessionId: "s_a")
        _ = ctl.consumePendingBootstrap()
        // Next recording must re-arm the bootstrap flag.
        ctl.transition(to: .standby)
        ctl.transition(to: .recording, pendingBootstrap: true, sessionId: "s_b")
        XCTAssertTrue(ctl.consumePendingBootstrap())
    }

    // MARK: disarm from any state must drop back to standby

    func testDisarmFromEveryStateConvergesToStandby() {
        let cases: [CameraViewController.AppState] = [
            .standby, .timeSyncWaiting, .mutualSyncing, .recording
        ]
        for src in cases {
            let (ctl, box, _) = makeController()
            ctl.transition(to: src, sessionId: "s_from")
            XCTAssertEqual(ctl.currentState, src)

            ctl.transition(to: .standby, sessionId: nil)
            XCTAssertEqual(ctl.currentState, .standby,
                           "must return to .standby from \(src)")
            XCTAssertNil(box.snapshot().sessionId,
                         "sessionId cleared on return to standby from \(src)")
            XCTAssertFalse(box.snapshot().pendingBootstrap,
                           "pendingBootstrap cleared on return to standby from \(src)")
        }
    }

    // MARK: timeSync trigger during recording is defensively guarded at
    // the controller layer — `transition(to: .timeSyncWaiting)` itself is
    // unconditional (the caller is responsible for guarding, and the
    // `sync_command` router check is tested separately in
    // CameraCommandRouterTests.testSyncCommandStartOnlyFiresInStandby).
    //
    // This test locks in the contract: if somebody calls `transition` with
    // a mis-ordered target, the controller does NOT silently reject — it
    // flips (and the onStateChanged callback fires) so the caller gets a
    // loud visible bug rather than a phantom no-op.

    func testControllerDoesNotSwallowMisorderedTransitions() {
        let (ctl, _, uiCount) = makeController()
        ctl.transition(to: .recording, sessionId: "s_busy0000")
        XCTAssertEqual(ctl.currentState, .recording)
        XCTAssertEqual(uiCount(), 1)

        // Caller is expected to guard; controller itself is a pure mirror.
        ctl.transition(to: .timeSyncWaiting)
        XCTAssertEqual(ctl.currentState, .timeSyncWaiting,
                       "controller is a mirror — caller owns the guard")
        XCTAssertEqual(uiCount(), 2, "UI must be notified of the unexpected flip")
    }

    // MARK: FrameStateBox concurrent snapshot vs update safety

    func testFrameStateBoxConcurrentSnapshotAndUpdate() {
        let box = FrameStateBox()
        // Spawn a writer + reader on background queues for a short burst;
        // we just need to confirm there's no crash / tsan report under
        // concurrent pressure. The snapshot is always internally consistent
        // because of the lock.
        let writeQ = DispatchQueue(label: "t.write", attributes: .concurrent)
        let readQ = DispatchQueue(label: "t.read", attributes: .concurrent)
        let expect = expectation(description: "stress")
        expect.expectedFulfillmentCount = 2

        writeQ.async {
            for i in 0..<500 {
                box.update(
                    state: i.isMultiple(of: 2) ? .recording : .standby,
                    pendingBootstrap: i.isMultiple(of: 3),
                    sessionId: "s_\(i)"
                )
            }
            expect.fulfill()
        }
        readQ.async {
            for _ in 0..<500 {
                let snap = box.snapshot()
                // No assertion on specific values — we're after crash-free
                // under a concurrent reader.
                _ = snap.state
                _ = snap.sessionId
                _ = snap.pendingBootstrap
            }
            expect.fulfill()
        }
        wait(for: [expect], timeout: 5.0)
    }
}
