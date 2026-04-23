import UIKit

/// Full-width top bar overlay.
/// Row 1: [A][B]  ·  ● STATUS  ·  [狀態chip]
/// Row 2: 192.168.50.xxx  (tappable, never truncated)
final class CameraMonitorOverlayView {

    // MARK: - Public views (referenced by CameraStatusPresenter)

    let topStatusChip = StatusChip()
    /// Text is set by CameraStatusPresenter; LED + truncated prefix stripped here.
    let connectionLabel = UILabel()
    /// Kept for API compatibility; hidden — preview state is implicit.
    let previewLabel = UILabel()
    let warningLabel = UILabel()
    let stateBorderLayer = CAShapeLayer()
    let recIndicator = UIView()

    // MARK: - Callbacks

    var onRoleChanged: (() -> Void)?
    var onIPTapped: (() -> Void)?

    // MARK: - Private

    private let topBar = UIView()
    private let roleButtonA = _RoleButton(title: "A")
    private let roleButtonB = _RoleButton(title: "B")
    private let ipValueLabel = UILabel()
    private let linkLED = UIView()
    private let statusTextLabel = UILabel()  // mirrors connectionLabel text
    private let recDotView = UIView()
    private let recTimerLabel = UILabel()
    private var recTimer: Timer?
    private var recStartTime: CFTimeInterval = 0
    private var _selectedRole: String = "A"

    // MARK: - Install

    func install(in view: UIView) {
        // ── Top bar container ────────────────────────────────────────────
        topBar.translatesAutoresizingMaskIntoConstraints = false
        topBar.backgroundColor = DesignTokens.Colors.hudSurface
        view.addSubview(topBar)

        let bottomBorder = UIView()
        bottomBorder.translatesAutoresizingMaskIntoConstraints = false
        bottomBorder.backgroundColor = DesignTokens.Colors.cardBorder
        topBar.addSubview(bottomBorder)

        // ── Row 1: role  ·  LED+status  ·  chip ─────────────────────────
        [roleButtonA, roleButtonB].forEach {
            $0.addTarget(self, action: #selector(handleRoleButton(_:)), for: .touchUpInside)
        }
        let roleStack = UIStackView(arrangedSubviews: [roleButtonA, roleButtonB])
        roleStack.axis = .horizontal
        roleStack.spacing = 4
        roleStack.alignment = .center

        // LED
        linkLED.translatesAutoresizingMaskIntoConstraints = false
        linkLED.layer.cornerRadius = 5
        linkLED.backgroundColor = DesignTokens.Colors.destructive
        NSLayoutConstraint.activate([
            linkLED.widthAnchor.constraint(equalToConstant: 10),
            linkLED.heightAnchor.constraint(equalToConstant: 10),
        ])

        // Status text — strips the "LINK · " prefix set by presenter
        statusTextLabel.font = DesignTokens.Fonts.mono(size: 13, weight: .medium)
        statusTextLabel.textColor = DesignTokens.Colors.ink

        let ledGroup = UIStackView(arrangedSubviews: [linkLED, statusTextLabel])
        ledGroup.axis = .horizontal
        ledGroup.spacing = 6
        ledGroup.alignment = .center

        topStatusChip.translatesAutoresizingMaskIntoConstraints = false

        let row1 = UIStackView(arrangedSubviews: [
            roleStack,
            UIView(),   // flexible spacer
            ledGroup,
            topStatusChip,
        ])
        row1.axis = .horizontal
        row1.alignment = .center
        row1.spacing = 12

        // ── Row 2: IP (full width, tappable) ────────────────────────────
        ipValueLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .regular)
        ipValueLabel.textColor = DesignTokens.Colors.sub
        ipValueLabel.text = "—"
        ipValueLabel.numberOfLines = 1
        ipValueLabel.lineBreakMode = .byClipping

        let row2 = UIStackView(arrangedSubviews: [ipValueLabel])
        row2.axis = .horizontal
        let ipTap = UITapGestureRecognizer(target: self, action: #selector(handleIPTapped))
        row2.addGestureRecognizer(ipTap)
        row2.isUserInteractionEnabled = true

        // ── Root vertical stack ──────────────────────────────────────────
        let rootStack = UIStackView(arrangedSubviews: [row1, row2])
        rootStack.axis = .vertical
        rootStack.spacing = 4
        rootStack.translatesAutoresizingMaskIntoConstraints = false
        topBar.addSubview(rootStack)

        // ── Warning banner ───────────────────────────────────────────────
        warningLabel.font = DesignTokens.Fonts.mono(size: 12, weight: .medium)
        warningLabel.textColor = DesignTokens.Colors.ink
        warningLabel.backgroundColor = DesignTokens.Colors.warning.withAlphaComponent(0.92)
        warningLabel.layer.cornerRadius = DesignTokens.CornerRadius.chipSmall
        warningLabel.layer.masksToBounds = true
        warningLabel.textAlignment = .center
        warningLabel.numberOfLines = 0
        warningLabel.translatesAutoresizingMaskIntoConstraints = false
        warningLabel.isHidden = true
        view.addSubview(warningLabel)

        // ── State border ─────────────────────────────────────────────────
        stateBorderLayer.fillColor = UIColor.clear.cgColor
        stateBorderLayer.strokeColor = UIColor.clear.cgColor
        stateBorderLayer.lineWidth = 0
        view.layer.addSublayer(stateBorderLayer)

        // ── Rec indicator ────────────────────────────────────────────────
        recIndicator.translatesAutoresizingMaskIntoConstraints = false
        recIndicator.isHidden = true
        recDotView.backgroundColor = DesignTokens.Colors.destructive
        recDotView.layer.cornerRadius = 5
        recDotView.translatesAutoresizingMaskIntoConstraints = false
        recTimerLabel.text = "REC 0.0s"
        recTimerLabel.textColor = DesignTokens.Colors.destructive
        recTimerLabel.font = DesignTokens.Fonts.mono(size: 13, weight: .medium)
        recTimerLabel.translatesAutoresizingMaskIntoConstraints = false
        recIndicator.addSubview(recDotView)
        recIndicator.addSubview(recTimerLabel)
        view.addSubview(recIndicator)

        // ── Constraints ──────────────────────────────────────────────────
        let hPad: CGFloat = DesignTokens.Spacing.l
        let vPad: CGFloat = DesignTokens.Spacing.s

        NSLayoutConstraint.activate([
            topBar.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            topBar.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            topBar.trailingAnchor.constraint(equalTo: view.trailingAnchor),

            bottomBorder.leadingAnchor.constraint(equalTo: topBar.leadingAnchor),
            bottomBorder.trailingAnchor.constraint(equalTo: topBar.trailingAnchor),
            bottomBorder.bottomAnchor.constraint(equalTo: topBar.bottomAnchor),
            bottomBorder.heightAnchor.constraint(equalToConstant: 1),

            rootStack.topAnchor.constraint(equalTo: topBar.topAnchor, constant: vPad),
            rootStack.leadingAnchor.constraint(equalTo: topBar.leadingAnchor, constant: hPad),
            rootStack.trailingAnchor.constraint(equalTo: topBar.trailingAnchor, constant: -hPad),
            rootStack.bottomAnchor.constraint(equalTo: topBar.bottomAnchor, constant: -vPad),

            warningLabel.topAnchor.constraint(equalTo: topBar.bottomAnchor, constant: DesignTokens.Spacing.s),
            warningLabel.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: hPad),
            warningLabel.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -hPad),

            recIndicator.topAnchor.constraint(equalTo: topBar.bottomAnchor, constant: DesignTokens.Spacing.s),
            recIndicator.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -hPad),

            recDotView.leadingAnchor.constraint(equalTo: recIndicator.leadingAnchor),
            recDotView.centerYAnchor.constraint(equalTo: recIndicator.centerYAnchor),
            recDotView.widthAnchor.constraint(equalToConstant: 10),
            recDotView.heightAnchor.constraint(equalToConstant: 10),

            recTimerLabel.leadingAnchor.constraint(equalTo: recDotView.trailingAnchor, constant: 6),
            recTimerLabel.trailingAnchor.constraint(equalTo: recIndicator.trailingAnchor),
            recTimerLabel.topAnchor.constraint(equalTo: recIndicator.topAnchor),
            recTimerLabel.bottomAnchor.constraint(equalTo: recIndicator.bottomAnchor),
        ])

        // previewLabel hidden — kept for presenter API compat
        previewLabel.isHidden = true
        // connectionLabel hidden — we mirror its text to statusTextLabel
        connectionLabel.isHidden = true
    }

    // MARK: - Public API

    func syncRole(cameraRole: String) {
        _selectedRole = cameraRole
        roleButtonA.isSelected = (cameraRole == "A")
        roleButtonB.isSelected = (cameraRole == "B")
    }

    var selectedCameraRole: String { _selectedRole }

    func syncIP(_ ip: String) {
        ipValueLabel.text = ip
    }

    func syncStatus(_ text: String) {
        // Strip "LINK · " prefix if present — only the state word shown next to LED
        let display = text.hasPrefix("LINK · ") ? String(text.dropFirst(7)) : text
        statusTextLabel.text = display
    }

    func syncConnection(reachable: Bool) {
        linkLED.backgroundColor = reachable ? DesignTokens.Colors.success : DesignTokens.Colors.destructive
        linkLED.layer.shadowColor = reachable ? DesignTokens.Colors.success.cgColor : UIColor.clear.cgColor
        linkLED.layer.shadowRadius = reachable ? 4 : 0
        linkLED.layer.shadowOpacity = reachable ? 0.8 : 0
        linkLED.layer.shadowOffset = .zero
    }

    func updateBorderPath(for bounds: CGRect) {
        stateBorderLayer.frame = bounds
        stateBorderLayer.path = UIBezierPath(rect: bounds).cgPath
    }

    func setRecordingActive(_ isActive: Bool) {
        if isActive { recIndicator.isHidden = false; _startRecTimer() }
        else         { recIndicator.isHidden = true;  _stopRecTimer()  }
    }

    // MARK: - Private

    @objc private func handleRoleButton(_ sender: _RoleButton) {
        syncRole(cameraRole: sender === roleButtonA ? "A" : "B")
        onRoleChanged?()
    }

    @objc private func handleIPTapped() { onIPTapped?() }

    private func _startRecTimer() {
        recStartTime = CACurrentMediaTime()
        recTimer?.invalidate()
        recTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            let e = CACurrentMediaTime() - self.recStartTime
            self.recTimerLabel.text = String(format: "REC %.1fs", e)
            self.recDotView.alpha = (Int(e * 2) % 2 == 0) ? 1.0 : 0.3
        }
    }

    private func _stopRecTimer() {
        recTimer?.invalidate()
        recTimer = nil
        recDotView.alpha = 1.0
    }
}

// MARK: - Role button

private final class _RoleButton: UIControl {
    override var isSelected: Bool  { didSet { _refresh() } }
    override var isHighlighted: Bool { didSet { alpha = isHighlighted ? 0.55 : 1.0 } }

    private let label = UILabel()

    init(title: String) {
        super.init(frame: .zero)
        label.text = title
        label.font = DesignTokens.Fonts.mono(size: 14, weight: .medium)
        label.textAlignment = .center
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        layer.cornerRadius = DesignTokens.CornerRadius.chipSmall
        layer.borderWidth = 1
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: centerXAnchor),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            widthAnchor.constraint(equalToConstant: 34),
            heightAnchor.constraint(equalToConstant: 26),
        ])
        _refresh()
    }

    required init?(coder: NSCoder) { fatalError() }

    private func _refresh() {
        backgroundColor = isSelected ? DesignTokens.Colors.accent : .clear
        layer.borderColor = isSelected
            ? DesignTokens.Colors.accent.cgColor
            : DesignTokens.Colors.cardBorder.cgColor
        label.textColor = isSelected ? DesignTokens.Colors.cardBackground : DesignTokens.Colors.sub
    }
}
