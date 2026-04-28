import XCTest
@testable import ball_tracker

/// Unit tests for the iOS-side `BallDetectionEngine` abstraction.
///
/// Mirrors the server-side `test_detection_engine.py` identity gates so
/// engine identifiers can't drift out of lock-step between the two
/// implementations. The C++ detection internals are covered by the
/// parity tests on the server side; here we just guard the Swift seams.
final class BallDetectionEngineTests: XCTestCase {

    func testHSVEngineHasStableIdentifier() {
        // Identifier is stamped onto every WS frame payload and persisted
        // on disk. Bumping it without a coordinated server-side change
        // breaks reproducibility of historical pitches.
        let engine = HSVDetectionEngine()
        XCTAssertEqual(engine.name, "hsv@ios.1.0")
        XCTAssertEqual(engine.name, BallDetectionEngineID.hsvIOS)
    }

    func testHSVEngineConformsToProtocol() {
        // Static-ish protocol-conformance check — if HSVDetectionEngine
        // ever stops conforming, this stops compiling.
        let engine: BallDetectionEngine = HSVDetectionEngine()
        XCTAssertEqual(engine.name, BallDetectionEngineID.hsvIOS)
    }

    func testFramePayloadCarriesEngineName() {
        // The wire shape MUST surface `engine`; server-side device_ws
        // requires it on every frame message under lockstep iOS+server.
        let frame = ServerUploader.FramePayload(
            frame_index: 0,
            timestamp_s: 0.0,
            candidates: [],
            engine: BallDetectionEngineID.hsvIOS
        )
        XCTAssertEqual(frame.engine, "hsv@ios.1.0")
    }
}
