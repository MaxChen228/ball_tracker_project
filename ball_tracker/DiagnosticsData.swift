import Foundation

/// Process-wide shared diagnostics data. `CameraViewController` is the sole
/// writer (every `updateUIForState` tick), `SettingsDiagnosticsViewController`
/// is the sole reader — driven by a timer on that VC. This replaces the three
/// on-HUD debug labels (FPS / last contact / Test) we removed from the main
/// screen; operators who need them still have access via Settings.
///
/// No observation / Combine / NotificationCenter — the diagnostics screen
/// polls at 1 Hz which is plenty for human debugging and dodges concurrency.
final class DiagnosticsData {
    static let shared = DiagnosticsData()
    private init() {}

    private let lock = NSLock()
    private var _fpsEstimate: Double = 0
    private var _lastContactAt: Date?
    private var _sessionId: String?
    private var _localRecordingIndex: Int?
    private var _serverStatusText: String = "unknown"

    var fpsEstimate: Double { get { lock.lock(); defer { lock.unlock() }; return _fpsEstimate } }
    var lastContactAt: Date? { get { lock.lock(); defer { lock.unlock() }; return _lastContactAt } }
    var sessionId: String? { get { lock.lock(); defer { lock.unlock() }; return _sessionId } }
    var localRecordingIndex: Int? { get { lock.lock(); defer { lock.unlock() }; return _localRecordingIndex } }
    var serverStatusText: String { get { lock.lock(); defer { lock.unlock() }; return _serverStatusText } }

    func update(
        fpsEstimate: Double? = nil,
        lastContactAt: Date?? = nil,
        sessionId: String?? = nil,
        localRecordingIndex: Int?? = nil,
        serverStatusText: String? = nil
    ) {
        lock.lock()
        defer { lock.unlock() }
        if let v = fpsEstimate { _fpsEstimate = v }
        if let v = lastContactAt { _lastContactAt = v }
        if let v = sessionId { _sessionId = v }
        if let v = localRecordingIndex { _localRecordingIndex = v }
        if let v = serverStatusText { _serverStatusText = v }
    }
}
