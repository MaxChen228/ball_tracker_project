import UIKit

/// Pill label used for the top-of-HUD state indicator ("待機", "錄影中"…).
/// Uses the hybrid language: `hudSurface` fill for every state, 1 px tone
/// border, tone-colored uppercase label. No per-state background swap — the
/// chip's tone is carried by the border + text so it reads well over the
/// live camera preview without the old "color-blob on dark" feel.
final class StatusChip: UILabel {
    enum Style {
        case ok
        case pending
        case fail
        case neutral
    }

    private let contentInsets = UIEdgeInsets(
        top: DesignTokens.Spacing.xs,
        left: DesignTokens.Spacing.m,
        bottom: DesignTokens.Spacing.xs,
        right: DesignTokens.Spacing.m
    )

    init() {
        super.init(frame: .zero)
        font = DesignTokens.Fonts.sans(size: 16, weight: .heavy)
        textAlignment = .center
        numberOfLines = 1
        adjustsFontSizeToFitWidth = true
        minimumScaleFactor = 0.7
        layer.cornerRadius = DesignTokens.CornerRadius.chip
        layer.masksToBounds = true
        layer.borderWidth = 1
        backgroundColor = DesignTokens.Colors.hudSurface
        setStyle(.neutral)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func setStyle(_ style: Style) {
        let tone: UIColor
        switch style {
        case .ok:
            tone = DesignTokens.Colors.success
        case .pending:
            tone = DesignTokens.Colors.warning
        case .fail:
            tone = DesignTokens.Colors.destructive
        case .neutral:
            tone = DesignTokens.Colors.sub
        }
        textColor = tone
        layer.borderColor = tone.withAlphaComponent(0.45).cgColor
    }

    override func drawText(in rect: CGRect) {
        super.drawText(in: rect.inset(by: contentInsets))
    }

    override var intrinsicContentSize: CGSize {
        let s = super.intrinsicContentSize
        return CGSize(
            width: s.width + contentInsets.left + contentInsets.right,
            height: s.height + contentInsets.top + contentInsets.bottom
        )
    }
}
