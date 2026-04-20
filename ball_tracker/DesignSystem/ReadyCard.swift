import UIKit

/// The standby-state "Ready card". Shows whether this phone is ready to
/// record: time-sync / server reachability, plus the next action the
/// operator should take. Hidden while recording. Phase 6: 位置校正 gate
/// dropped — calibration is dashboard-owned and iOS has no way to query
/// the server-side state.
final class ReadyCard: UIView {
    /// Single-row inputs to the card. Built by the caller once per tick.
    struct Gate {
        let state: GateRow.State
        let label: String
        let action: String?
        let onTap: (() -> Void)?
    }

    struct Model {
        let cameraRole: String
        let timeSync: Gate
        let server: Gate
        let hint: String
    }

    private let headerLabel = UILabel()
    private let stateLabel = UILabel()
    private let timeSyncRow = GateRow()
    private let serverRow = GateRow()
    private let hintLabel = UILabel()

    init() {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        backgroundColor = DesignTokens.Colors.surface
        layer.cornerRadius = DesignTokens.CornerRadius.card
        layer.borderWidth = 1
        layer.borderColor = DesignTokens.Colors.border.cgColor
        layoutMargins = UIEdgeInsets(
            top: DesignTokens.Spacing.m,
            left: DesignTokens.Spacing.l,
            bottom: DesignTokens.Spacing.m,
            right: DesignTokens.Spacing.l
        )

        headerLabel.font = DesignTokens.Fonts.sans(size: 13, weight: .semibold)
        headerLabel.textColor = DesignTokens.Colors.sub

        stateLabel.font = DesignTokens.Fonts.mono(size: 26, weight: .heavy)
        stateLabel.textColor = DesignTokens.Colors.ink

        hintLabel.font = DesignTokens.Fonts.sans(size: 14, weight: .medium)
        hintLabel.textColor = DesignTokens.Colors.sub
        hintLabel.numberOfLines = 2

        let headerStack = UIStackView(arrangedSubviews: [headerLabel, stateLabel])
        headerStack.axis = .vertical
        headerStack.spacing = DesignTokens.Spacing.xs

        let gateStack = UIStackView(arrangedSubviews: [timeSyncRow, serverRow])
        gateStack.axis = .vertical
        gateStack.spacing = DesignTokens.Spacing.m

        let rootStack = UIStackView(arrangedSubviews: [headerStack, gateStack, hintLabel])
        rootStack.axis = .vertical
        rootStack.spacing = DesignTokens.Spacing.m
        rootStack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(rootStack)

        NSLayoutConstraint.activate([
            rootStack.topAnchor.constraint(equalTo: layoutMarginsGuide.topAnchor),
            rootStack.leadingAnchor.constraint(equalTo: layoutMarginsGuide.leadingAnchor),
            rootStack.trailingAnchor.constraint(equalTo: layoutMarginsGuide.trailingAnchor),
            rootStack.bottomAnchor.constraint(equalTo: layoutMarginsGuide.bottomAnchor),
        ])
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func update(_ model: Model) {
        let ready = model.timeSync.state == .pass
            && model.server.state == .pass
        headerLabel.text = "Cam \(model.cameraRole)"
        stateLabel.text = ready ? "READY" : "尚未就緒"
        stateLabel.textColor = ready ? DesignTokens.Colors.success : DesignTokens.Colors.warning
        hintLabel.text = model.hint
        hintLabel.isHidden = model.hint.isEmpty
        apply(model.timeSync, to: timeSyncRow)
        apply(model.server, to: serverRow)
    }

    private func apply(_ gate: Gate, to row: GateRow) {
        row.configure(state: gate.state, label: gate.label, action: gate.action, onTap: gate.onTap)
    }
}
