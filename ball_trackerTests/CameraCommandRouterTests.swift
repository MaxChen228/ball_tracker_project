import XCTest
@testable import ball_tracker

/// Coverage for `CameraCommandRouter` WS message handling — specifically
/// the `settings` hot-reload path (chirp_threshold / heartbeat_interval /
/// hsv_range / capture_height_px) plus arm/disarm/sync_command routing.
///
/// These paths had zero tests; a regression where a `settings` field
/// failed to fan out would silently break field tuning.
final class CameraCommandRouterTests: XCTestCase {

    /// Mutable record-of-calls for every dependency closure. All accesses
    /// guarded by `lock` because `CameraCommandRouter.handle(message:)`
    /// dispatches several paths onto `DispatchQueue.main.async`.
    final class Fixture {
        private let lock = NSLock()

        var state: CameraViewController.AppState = .standby
        var cameraRole: String = "A"
        var sessionPaths: Set<ServerUploader.DetectionPath> = [.live]
        var captureHeight: Int = 1080
        var previewRequested: Bool = false
        var calCaptureState: CalCaptureState = .idle
        var armCalibrationCaptureResult: Bool = true
        private(set) var armCalibrationCaptureCount = 0

        private(set) var setSessionIdCalls: [String?] = []
        private(set) var setSessionPathsCalls: [Set<ServerUploader.DetectionPath>] = []
        private(set) var refreshModeLabelCount = 0
        private(set) var startTimeSyncCalls: [String] = []
        private(set) var applyMutualSyncCalls: [(String, [Double], Double)] = []
        private(set) var applyRemoteArmCount = 0
        private(set) var applyRemoteDisarmCount = 0
        private(set) var updateTimeSyncServerCalls: [(Bool, String?)] = []
        private(set) var chirpThresholdPushes: [Double] = []
        private(set) var heartbeatIntervalPushes: [Double] = []
        private(set) var hsvPushes: [ServerUploader.HSVRangePayload] = []
        private(set) var shapeGatePushes: [ServerUploader.ShapeGatePayload] = []
        private(set) var exposureCapPushes: [String] = []
        private(set) var captureHeightApplied: [Int] = []
        private(set) var ensurePreviewUploaderCount = 0
        private(set) var resetPreviewUploaderCount = 0
        private(set) var startStandbyCaptureCount = 0
        private(set) var stopCaptureCount = 0

        func guarded<T>(_ block: () -> T) -> T {
            lock.lock(); defer { lock.unlock() }
            return block()
        }

        func record<T>(_ block: () -> T) -> T { guarded(block) }

        func dependencies(
            healthMonitor: HeartbeatScheduler
        ) -> CameraCommandRouter.Dependencies {
            return CameraCommandRouter.Dependencies(
                getState: { self.guarded { self.state } },
                getCameraRole: { self.guarded { self.cameraRole } },
                healthMonitor: healthMonitor,
                getCurrentSessionPaths: { self.guarded { self.sessionPaths } },
                setCurrentSessionId: { sid in
                    self.guarded { self.setSessionIdCalls.append(sid) }
                },
                setCurrentSessionPaths: { paths in
                    self.guarded {
                        self.setSessionPathsCalls.append(paths)
                        self.sessionPaths = paths
                    }
                },
                refreshModeLabel: {
                    self.guarded { self.refreshModeLabelCount += 1 }
                },
                startTimeSync: { id in
                    self.guarded { self.startTimeSyncCalls.append(id) }
                },
                applyMutualSync: { id, emits, dur in
                    self.guarded { self.applyMutualSyncCalls.append((id, emits, dur)) }
                },
                applyRemoteArm: {
                    self.guarded { self.applyRemoteArmCount += 1 }
                },
                applyRemoteDisarm: {
                    self.guarded { self.applyRemoteDisarmCount += 1 }
                },
                updateTimeSyncServerState: { confirmed, syncId in
                    self.guarded { self.updateTimeSyncServerCalls.append((confirmed, syncId)) }
                },
                chirpThresholdDidPush: { val in
                    self.guarded { self.chirpThresholdPushes.append(val) }
                },
                heartbeatIntervalDidPush: { val in
                    self.guarded { self.heartbeatIntervalPushes.append(val) }
                },
                hsvRangeDidPush: { hsv in
                    self.guarded { self.hsvPushes.append(hsv) }
                },
                shapeGateDidPush: { sg in
                    self.guarded { self.shapeGatePushes.append(sg) }
                },
                handleTrackingExposureCap: { cap in
                    self.guarded { self.exposureCapPushes.append(cap) }
                },
                currentCaptureHeight: { self.guarded { self.captureHeight } },
                applyServerCaptureHeight: { h in
                    self.guarded {
                        self.captureHeightApplied.append(h)
                        self.captureHeight = h
                    }
                },
                isPreviewRequested: { self.guarded { self.previewRequested } },
                setPreviewRequested: { req in
                    self.guarded { self.previewRequested = req }
                },
                ensurePreviewUploader: {
                    self.guarded { self.ensurePreviewUploaderCount += 1 }
                },
                resetPreviewUploader: {
                    self.guarded { self.resetPreviewUploaderCount += 1 }
                },
                startStandbyCapture: {
                    self.guarded { self.startStandbyCaptureCount += 1 }
                },
                stopCapture: {
                    self.guarded { self.stopCaptureCount += 1 }
                },
                getCalCaptureState: {
                    self.guarded { self.calCaptureState }
                },
                armCalibrationCapture: {
                    self.guarded {
                        self.armCalibrationCaptureCount += 1
                        return self.armCalibrationCaptureResult
                    }
                }
            )
        }
    }

    private func makeRouter(_ fixture: Fixture = Fixture())
        -> (CameraCommandRouter, Fixture, HeartbeatScheduler)
    {
        let hb = HeartbeatScheduler(baseIntervalS: 1.0)
        let router = CameraCommandRouter(dependencies: fixture.dependencies(healthMonitor: hb))
        return (router, fixture, hb)
    }

    /// Pump the main runloop until all `DispatchQueue.main.async` blocks
    /// the router scheduled have drained.
    private func drainMain() {
        let exp = expectation(description: "main drain")
        DispatchQueue.main.async { exp.fulfill() }
        wait(for: [exp], timeout: 1.0)
    }

    /// Server lockstep contract (server/main.py:510): every `settings` push
    /// includes `device_time_synced`, `preview_requested`, and
    /// `calibration_frame_requested`. Missing any of those triggers an
    /// atomic-drop in the router. Tests covering optional fields must
    /// include the required scaffolding so they exercise the field under
    /// test, not the drop guard.
    private func settingsMessage(
        deviceTimeSynced: Bool = false,
        deviceTimeSyncId: String? = nil,
        previewRequested: Bool = false,
        calibrationFrameRequested: Bool = false,
        extra: [String: Any] = [:]
    ) -> [String: Any] {
        var msg: [String: Any] = [
            "type": "settings",
            "device_time_synced": deviceTimeSynced,
            "preview_requested": previewRequested,
            "calibration_frame_requested": calibrationFrameRequested
        ]
        if let deviceTimeSyncId { msg["device_time_sync_id"] = deviceTimeSyncId }
        for (k, v) in extra { msg[k] = v }
        return msg
    }

    // MARK: settings · chirp_threshold

    func testSettingsPushesChirpThreshold() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: settingsMessage(extra: ["chirp_detect_threshold": 0.42]))
        drainMain()
        XCTAssertEqual(fixture.chirpThresholdPushes, [0.42])
    }

    // MARK: settings · heartbeat_interval

    func testSettingsPushesHeartbeatInterval() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: settingsMessage(extra: ["heartbeat_interval_s": 2.5]))
        drainMain()
        XCTAssertEqual(fixture.heartbeatIntervalPushes, [2.5])
    }

    // MARK: settings · hsv_range (all 6 fields required)

    func testSettingsPushesHSVRangeOnlyWhenComplete() {
        let (router, fixture, _) = makeRouter()
        // Partial dict → ignored.
        router.handle(message: settingsMessage(extra: [
            "hsv_range": ["h_min": 20, "h_max": 60]
        ]))
        drainMain()
        XCTAssertTrue(fixture.hsvPushes.isEmpty, "Incomplete hsv_range must be ignored")

        // Full dict → pushed.
        router.handle(message: settingsMessage(extra: [
            "hsv_range": [
                "h_min": 25, "h_max": 55,
                "s_min": 90, "s_max": 255,
                "v_min": 90, "v_max": 255
            ]
        ]))
        drainMain()
        XCTAssertEqual(fixture.hsvPushes.count, 1)
        XCTAssertEqual(fixture.hsvPushes.first, ServerUploader.HSVRangePayload.tennis)
    }

    // MARK: settings · capture_height_px (only applied in .standby + only if changed)

    func testCaptureHeightPushAppliedInStandby() {
        let (router, fixture, _) = makeRouter()
        fixture.state = .standby
        fixture.captureHeight = 1080

        router.handle(message: settingsMessage(extra: ["capture_height_px": 720]))
        drainMain()
        XCTAssertEqual(fixture.captureHeightApplied, [720])
    }

    func testCaptureHeightPushIgnoredWhenUnchanged() {
        let (router, fixture, _) = makeRouter()
        fixture.state = .standby
        fixture.captureHeight = 1080

        router.handle(message: settingsMessage(extra: ["capture_height_px": 1080]))
        drainMain()
        XCTAssertTrue(fixture.captureHeightApplied.isEmpty,
                      "Same-value capture_height_px must not trigger apply")
    }

    func testCaptureHeightPushDeferredWhenRecording() {
        let (router, fixture, _) = makeRouter()
        fixture.state = .recording
        fixture.captureHeight = 1080

        router.handle(message: settingsMessage(extra: ["capture_height_px": 720]))
        drainMain()
        XCTAssertTrue(fixture.captureHeightApplied.isEmpty,
                      "capture_height must not swap mid-recording (format reconfig would kill in-flight MOV)")
    }

    // MARK: settings · exposure cap + sync state + paths fan-out

    func testSettingsFansOutExposureCapAndSyncState() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: settingsMessage(
            deviceTimeSynced: true,
            deviceTimeSyncId: "chirp_abc",
            extra: [
                "tracking_exposure_cap": "120fps",
                "paths": ["live"]
            ]
        ))
        drainMain()
        XCTAssertEqual(fixture.exposureCapPushes, ["120fps"])
        XCTAssertEqual(fixture.updateTimeSyncServerCalls.count, 1)
        XCTAssertEqual(fixture.updateTimeSyncServerCalls.first?.0, true)
        XCTAssertEqual(fixture.updateTimeSyncServerCalls.first?.1, "chirp_abc")
        XCTAssertEqual(fixture.setSessionPathsCalls, [[.live]])
    }

    // MARK: settings · preview + calibration frame toggles

    func testPreviewToggleOnStartsStandbyCapture() {
        let (router, fixture, _) = makeRouter()
        fixture.state = .standby
        fixture.previewRequested = false

        router.handle(message: settingsMessage(
            deviceTimeSynced: true,
            previewRequested: true,
            calibrationFrameRequested: false
        ))
        drainMain()
        XCTAssertTrue(fixture.previewRequested)
        XCTAssertEqual(fixture.ensurePreviewUploaderCount, 1)
        XCTAssertEqual(fixture.startStandbyCaptureCount, 1)
    }

    func testPreviewToggleOffInStandbyStopsCapture() {
        let (router, fixture, _) = makeRouter()
        fixture.state = .standby
        fixture.previewRequested = true

        router.handle(message: settingsMessage(
            deviceTimeSynced: true,
            previewRequested: false,
            calibrationFrameRequested: false
        ))
        drainMain()
        XCTAssertFalse(fixture.previewRequested)
        XCTAssertEqual(fixture.resetPreviewUploaderCount, 1)
        XCTAssertEqual(fixture.stopCaptureCount, 1)
    }

    /// Regression: a `settings` push missing any required scaffolding
    /// field (here: `device_time_synced`) must atomic-drop — no
    /// paths/exposure/hsv side effect leaks through before the bail.
    func testSettingsDropsWhenRequiredFieldMissing() {
        let (router, fixture, _) = makeRouter()
        // Intentionally raw dict (no helper) — drop `device_time_synced`.
        router.handle(message: [
            "type": "settings",
            "preview_requested": false,
            "calibration_frame_requested": false,
            "paths": ["live", "server_post"],
            "tracking_exposure_cap": "120fps",
            "hsv_range": [
                "h_min": 25, "h_max": 55,
                "s_min": 90, "s_max": 255,
                "v_min": 90, "v_max": 255
            ]
        ])
        drainMain()
        XCTAssertTrue(fixture.setSessionPathsCalls.isEmpty,
                      "atomic drop: paths must not be applied")
        XCTAssertTrue(fixture.exposureCapPushes.isEmpty,
                      "atomic drop: exposure cap must not be applied")
        XCTAssertTrue(fixture.hsvPushes.isEmpty,
                      "atomic drop: hsv must not be applied")
        XCTAssertTrue(fixture.updateTimeSyncServerCalls.isEmpty,
                      "atomic drop: time-sync state must not be applied")
    }

    // MARK: arm / disarm / sync routing

    func testArmMessageSetsSessionAndFiresRemoteArm() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: [
            "type": "arm",
            "sid": "s_arm00001",
            "paths": ["live", "server_post"],
            "tracking_exposure_cap": "240fps"
        ])
        drainMain()
        XCTAssertEqual(fixture.setSessionIdCalls, ["s_arm00001"])
        XCTAssertEqual(fixture.setSessionPathsCalls.first,
                       Set([ServerUploader.DetectionPath.live, .serverPost]))
        XCTAssertEqual(fixture.exposureCapPushes, ["240fps"])
        XCTAssertEqual(fixture.applyRemoteArmCount, 1)
        XCTAssertEqual(fixture.refreshModeLabelCount, 1)
    }

    func testDisarmMessageFiresRemoteDisarm() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: ["type": "disarm"])
        drainMain()
        XCTAssertEqual(fixture.applyRemoteDisarmCount, 1)
    }

    func testSyncCommandStartOnlyFiresInStandby() {
        let (router, fixture, _) = makeRouter()
        fixture.state = .recording
        router.handle(message: [
            "type": "sync_command",
            "command": "start",
            "sync_command_id": "sc_busy"
        ])
        drainMain()
        XCTAssertTrue(fixture.startTimeSyncCalls.isEmpty,
                      "sync_command must be ignored while not in .standby")

        fixture.state = .standby
        router.handle(message: [
            "type": "sync_command",
            "command": "start",
            "sync_command_id": "sc_idle"
        ])
        drainMain()
        XCTAssertEqual(fixture.startTimeSyncCalls, ["sc_idle"])
    }

    /// Server lockstep: `emit_at_s` and `record_duration_s` are required
    /// (server/main.py emits both unconditionally). Missing either one
    /// must drop the whole sync_run, never silently fall back to a
    /// hard-coded local default.
    func testSyncRunDropsWhenRequiredFieldsMissing() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: [
            "type": "sync_run",
            "sync_id": "mutual_x"
            // emit_at_s + record_duration_s missing on purpose
        ])
        drainMain()
        XCTAssertTrue(fixture.applyMutualSyncCalls.isEmpty,
                      "sync_run must atomic-drop when required fields are absent")
    }

    func testSyncRunAppliesMutualSyncWithRequiredFields() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: [
            "type": "sync_run",
            "sync_id": "mutual_y",
            "emit_at_s": [0.5, 1.5],
            "record_duration_s": 4.0
        ])
        drainMain()
        XCTAssertEqual(fixture.applyMutualSyncCalls.count, 1)
        let call = fixture.applyMutualSyncCalls.first
        XCTAssertEqual(call?.0, "mutual_y")
        XCTAssertEqual(call?.1, [0.5, 1.5])
        XCTAssertEqual(call?.2, 4.0)
    }

    // MARK: paths parser — empty / garbage tolerant

    func testSettingsPathsParserIgnoresGarbage() {
        let (router, fixture, _) = makeRouter()
        router.handle(message: settingsMessage(extra: ["paths": ["bogus", "also_bogus"]]))
        drainMain()
        XCTAssertTrue(fixture.setSessionPathsCalls.isEmpty,
                      "Unparseable paths must not clobber session paths")

        router.handle(message: settingsMessage(extra: ["paths": ["live", "???"]]))
        drainMain()
        XCTAssertEqual(fixture.setSessionPathsCalls.last, [.live])
    }
}
