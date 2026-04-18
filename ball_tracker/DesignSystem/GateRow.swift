import UIKit

/// One "gate" row in the Ready card — a single yes/no precondition that
/// needs to be green before the operator can shoot.
///
/// - `.pass` → shows `✓` + label in the ok color
/// - `.pending` → shows `…` + label in the pending color
/// - `.fail` → shows `✗` + label in fail color; if `action` is provided,
///   the row becomes tappable and the label reads `label · action`
final class GateRow: UIControl {
    enum State {
        case pass
        case pending
        case fail
    }

    private let glyphLabel = UILabel()
    private let textLabel = UILabel()
    private var tapAction: (() -> Void)?

    init() {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false

        glyphLabel.font = DesignTokens.Fonts.mono(size: 16, weight: .bold)
        glyphLabel.textAlignment = .center
        glyphLabel.translatesAutoresizingMaskIntoConstraints = false

        textLabel.font = DesignTokens.Fonts.sans(size: 15, weight: .medium)
        textLabel.numberOfLines = 1
        textLabel.adjustsFontSizeToFitWidth = true
        textLabel.minimumScaleFactor = 0.75
        textLabel.translatesAutoresizingMaskIntoConstraints = false

        addSubview(glyphLabel)
        addSubview(textLabel)

        NSLayoutConstraint.activate([
            glyphLabel.leadingAnchor.constraint(equalTo: leadingAnchor),
            glyphLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
            glyphLabel.widthAnchor.constraint(equalToConstant: 20),

            textLabel.leadingAnchor.constraint(equalTo: glyphLabel.trailingAnchor, constant: DesignTokens.Spacing.s),
            textLabel.trailingAnchor.constraint(equalTo: trailingAnchor),
            textLabel.topAnchor.constraint(equalTo: topAnchor, constant: DesignTokens.Spacing.xs),
            textLabel.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -DesignTokens.Spacing.xs),
        ])

        addTarget(self, action: #selector(onTap), for: .touchUpInside)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// - label: primary sentence (e.g. "位置校正", "時間校正")
    /// - action: optional call to action shown after `·` on fail rows
    ///           (e.g. "按這裡開始"). Enables tap handling.
    func configure(
        state: State,
        label: String,
        action: String? = nil,
        onTap: (() -> Void)? = nil
    ) {
        tapAction = onTap
        isUserInteractionEnabled = onTap != nil
        let glyph: String
        let color: UIColor
        switch state {
        case .pass:
            glyph = "✓"
            color = DesignTokens.Colors.ok
        case .pending:
            glyph = "…"
            color = DesignTokens.Colors.pending
        case .fail:
            glyph = "✗"
            color = DesignTokens.Colors.fail
        }
        glyphLabel.text = glyph
        glyphLabel.textColor = color
        if let action {
            textLabel.text = "\(label) · \(action)"
            textLabel.textColor = DesignTokens.Colors.ok
        } else {
            textLabel.text = label
            textLabel.textColor = state == .pass ? DesignTokens.Colors.ink : DesignTokens.Colors.sub
        }
    }

    @objc private func onTap() {
        tapAction?()
    }
}
