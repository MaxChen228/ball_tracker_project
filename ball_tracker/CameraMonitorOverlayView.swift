import UIKit

final class CameraMonitorOverlayView {
    let topStatusChip = StatusChip()
    let controlPanel = UIView()
    let roleControl = UISegmentedControl(items: ["A", "B"])
    let connectionLabel = UILabel()
    let previewLabel = UILabel()
    let warningLabel = UILabel()
    let stateBorderLayer = CAShapeLayer()
    let recIndicator = UIView()

    private let recDotView = UIView()
    private let recTimerLabel = UILabel()
    private var recTimer: Timer?
    private var recStartTime: CFTimeInterval = 0

    var onRoleChanged: (() -> Void)?

    func install(in view: UIView) {
        topStatusChip.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(topStatusChip)

        controlPanel.translatesAutoresizingMaskIntoConstraints = false
        controlPanel.backgroundColor = DesignTokens.Colors.hudSurface
        controlPanel.layer.cornerRadius = DesignTokens.CornerRadius.card
        controlPanel.layer.borderWidth = 1
        controlPanel.layer.borderColor = DesignTokens.Colors.cardBorder.cgColor
        view.addSubview(controlPanel)

        let roleLabel = makePanelLabel("ROLE")

        roleControl.translatesAutoresizingMaskIntoConstraints = false
        roleControl.selectedSegmentTintColor = DesignTokens.Colors.accent
        roleControl.setTitleTextAttributes([.foregroundColor: DesignTokens.Colors.ink], for: .normal)
        roleControl.setTitleTextAttributes([.foregroundColor: DesignTokens.Colors.cardBackground], for: .selected)
        roleControl.addTarget(self, action: #selector(handleRoleChanged), for: .valueChanged)

        connectionLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        connectionLabel.textColor = DesignTokens.Colors.ink

        previewLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        previewLabel.textColor = DesignTokens.Colors.sub

        let roleRow = UIStackView(arrangedSubviews: [roleLabel, roleControl])
        roleRow.axis = .horizontal
        roleRow.alignment = .center
        roleRow.spacing = DesignTokens.Spacing.s

        let statusRow = UIStackView(arrangedSubviews: [connectionLabel, previewLabel])
        statusRow.axis = .vertical
        statusRow.alignment = .leading
        statusRow.spacing = DesignTokens.Spacing.xs

        let root = UIStackView(arrangedSubviews: [roleRow, statusRow])
        root.axis = .vertical
        root.spacing = DesignTokens.Spacing.s
        root.translatesAutoresizingMaskIntoConstraints = false
        controlPanel.addSubview(root)

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

        stateBorderLayer.fillColor = UIColor.clear.cgColor
        stateBorderLayer.strokeColor = UIColor.clear.cgColor
        stateBorderLayer.lineWidth = 0
        view.layer.addSublayer(stateBorderLayer)

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
            topStatusChip.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: DesignTokens.Spacing.m),
            topStatusChip.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),

            controlPanel.topAnchor.constraint(equalTo: topStatusChip.bottomAnchor, constant: DesignTokens.Spacing.s),
            controlPanel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.m),
            controlPanel.trailingAnchor.constraint(lessThanOrEqualTo: view.trailingAnchor, constant: -DesignTokens.Spacing.xl),

            roleLabel.widthAnchor.constraint(equalToConstant: 52),
            roleControl.widthAnchor.constraint(equalToConstant: 120),

            root.topAnchor.constraint(equalTo: controlPanel.topAnchor, constant: DesignTokens.Spacing.m),
            root.leadingAnchor.constraint(equalTo: controlPanel.leadingAnchor, constant: DesignTokens.Spacing.m),
            root.trailingAnchor.constraint(equalTo: controlPanel.trailingAnchor, constant: -DesignTokens.Spacing.m),
            root.bottomAnchor.constraint(equalTo: controlPanel.bottomAnchor, constant: -DesignTokens.Spacing.m),

            warningLabel.topAnchor.constraint(equalTo: controlPanel.bottomAnchor, constant: DesignTokens.Spacing.s),
            warningLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: DesignTokens.Spacing.l),
            warningLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -DesignTokens.Spacing.l),

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

    func syncRole(cameraRole: String) {
        roleControl.selectedSegmentIndex = cameraRole == "B" ? 1 : 0
    }

    var selectedCameraRole: String {
        roleControl.selectedSegmentIndex == 1 ? "B" : "A"
    }

    func setRecordingActive(_ isActive: Bool) {
        if isActive {
            recIndicator.isHidden = false
            startRecTimer()
        } else {
            recIndicator.isHidden = true
            stopRecTimer()
        }
    }

    func updateBorderPath(for bounds: CGRect) {
        stateBorderLayer.frame = bounds
        stateBorderLayer.path = UIBezierPath(rect: bounds).cgPath
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
            self.recDotView.alpha = (Int(elapsed * 2) % 2 == 0) ? 1.0 : 0.25
        }
    }

    private func stopRecTimer() {
        recTimer?.invalidate()
        recTimer = nil
        recDotView.alpha = 1.0
    }

    @objc
    private func handleRoleChanged() {
        onRoleChanged?()
    }

    private func makePanelLabel(_ text: String) -> UILabel {
        let label = UILabel()
        label.font = DesignTokens.Fonts.mono(size: 12, weight: .bold)
        label.textColor = DesignTokens.Colors.sub
        label.text = text
        return label
    }
}
