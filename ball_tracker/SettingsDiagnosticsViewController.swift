import UIKit

/// Diagnostics subpage reached from Settings → 診斷. Shows the debug
/// numbers we pulled off the main HUD (FPS, last-contact, session id,
/// local recording index) plus the Test-connection action. Operators who
/// never need these see a clean camera view; developers still have the
/// info one menu deep.
///
/// Pull-style: polls `DiagnosticsData.shared` at 1 Hz rather than wiring
/// observers — overlap with the heartbeat cadence is fine for humans.
final class SettingsDiagnosticsViewController: UIViewController {
    private let stack = UIStackView()
    private let fpsLabel = UILabel()
    private let lastContactLabel = UILabel()
    private let serverLabel = UILabel()
    private let sessionLabel = UILabel()
    private let recordingIndexLabel = UILabel()
    private let testButton = UIButton(type: .system)

    /// Optional reference to the live camera VC so the Test button can
    /// trigger an actual probe. Nil-safe — the page is still useful as a
    /// read-only diagnostic when the camera VC is not in the hierarchy.
    weak var cameraVC: CameraViewController?

    private var refreshTimer: Timer?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        title = "診斷"

        stack.axis = .vertical
        stack.spacing = DesignTokens.Spacing.m
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.layoutMargins = UIEdgeInsets(
            top: DesignTokens.Spacing.l,
            left: DesignTokens.Spacing.l,
            bottom: DesignTokens.Spacing.l,
            right: DesignTokens.Spacing.l
        )
        stack.isLayoutMarginsRelativeArrangement = true
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])

        stack.addArrangedSubview(row(key: "FPS", value: fpsLabel))
        stack.addArrangedSubview(row(key: "Server", value: serverLabel))
        stack.addArrangedSubview(row(key: "Last contact", value: lastContactLabel))
        stack.addArrangedSubview(row(key: "Session", value: sessionLabel))
        stack.addArrangedSubview(row(key: "Recording idx", value: recordingIndexLabel))

        testButton.setTitle("Test 伺服器連線", for: .normal)
        testButton.titleLabel?.font = DesignTokens.Fonts.sans(size: 16, weight: .semibold)
        testButton.addTarget(self, action: #selector(onTapTest), for: .touchUpInside)
        stack.addArrangedSubview(testButton)

        refresh()
    }

    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        refreshTimer?.invalidate()
        let timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        timer.tolerance = 0.2
        refreshTimer = timer
    }

    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    private func row(key: String, value: UILabel) -> UIStackView {
        let k = UILabel()
        k.text = key
        k.font = DesignTokens.Fonts.sans(size: 13, weight: .medium)
        k.textColor = .secondaryLabel
        k.widthAnchor.constraint(equalToConstant: 120).isActive = true

        value.font = DesignTokens.Fonts.mono(size: 14, weight: .regular)
        value.textColor = .label
        value.numberOfLines = 1
        value.text = "—"

        let stack = UIStackView(arrangedSubviews: [k, value])
        stack.axis = .horizontal
        stack.spacing = DesignTokens.Spacing.s
        return stack
    }

    private func refresh() {
        let d = DiagnosticsData.shared
        fpsLabel.text = String(format: "%.1f", d.fpsEstimate)
        serverLabel.text = d.serverStatusText
        lastContactLabel.text = Self.relativeContact(d.lastContactAt)
        sessionLabel.text = d.sessionId ?? "—"
        if let idx = d.localRecordingIndex {
            recordingIndexLabel.text = String(idx)
        } else {
            recordingIndexLabel.text = "—"
        }
    }

    private static func relativeContact(_ date: Date?) -> String {
        guard let date else { return "—" }
        let s = Int(Date().timeIntervalSince(date))
        if s < 60 { return "\(s) 秒前" }
        if s < 3600 { return "\(s / 60) 分 \(s % 60) 秒前" }
        return "\(s / 3600) 小時前"
    }

    @objc private func onTapTest() {
        cameraVC?.testServerConnection()
    }
}
