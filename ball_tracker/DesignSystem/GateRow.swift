import UIKit

/// One "gate" row in the Ready card — a single yes/no precondition that
/// needs to be green before the operator can shoot.
///
/// - `.pass` → shows `✓` + label in success (灰橄欖) color
/// - `.pending` → shows `…` + label in warning (琥珀) color
/// - `.fail` → shows `✗` + label in destructive (煙玫瑰) color; if `action`
///   is provided, the row becomes tappable and the label reads `label · action`
final class GateRow: UIControl {
    enum State {
        case pass
        case pending
        case fail
    }

    private let glyphLabel = UILabel()
    private let textLabel = UILabel()
    private var tapAction: (() -> Void)?
    private var highlightColor: UIColor = .clear

    init() {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        layer.cornerRadius = DesignTokens.CornerRadius.chipSmall

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
            glyphLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: DesignTokens.Spacing.xs),
            glyphLabel.centerYAnchor.constraint(equalTo: centerYAnchor),
            glyphLabel.widthAnchor.constraint(equalToConstant: 20),

            textLabel.leadingAnchor.constraint(equalTo: glyphLabel.trailingAnchor, constant: DesignTokens.Spacing.s),
            textLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -DesignTokens.Spacing.xs),
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
            color = DesignTokens.Colors.success
        case .pending:
            glyph = "…"
            color = DesignTokens.Colors.warning
        case .fail:
            glyph = "✗"
            color = DesignTokens.Colors.destructive
        }
        glyphLabel.text = glyph
        glyphLabel.textColor = color
        highlightColor = color.withAlphaComponent(0.08)
        if let action {
            textLabel.text = "\(label) · \(action)"
            textLabel.textColor = color
        } else {
            textLabel.text = label
            textLabel.textColor = state == .pass ? DesignTokens.Colors.ink : DesignTokens.Colors.sub
        }
    }

    override var isHighlighted: Bool {
        didSet {
            guard oldValue != isHighlighted else { return }
            UIView.animate(withDuration: DesignTokens.Motion.quick) {
                self.backgroundColor = self.isHighlighted ? self.highlightColor : .clear
            }
        }
    }

    @objc private func onTap() {
        tapAction?()
    }
}
