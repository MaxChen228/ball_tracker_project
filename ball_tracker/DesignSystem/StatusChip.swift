import UIKit

/// Colored pill label used for the top-of-HUD state indicator ("待機",
/// "錄影中", etc). Replaces the older `PaddedLabel` — same intrinsic
/// padding trick but with a `setStyle` API that pulls from DesignTokens
/// so a rename / palette change flips every chip in one place.
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
        font = DesignTokens.Fonts.sans(size: 18, weight: .heavy)
        textAlignment = .center
        numberOfLines = 1
        adjustsFontSizeToFitWidth = true
        minimumScaleFactor = 0.7
        layer.cornerRadius = DesignTokens.CornerRadius.chip
        layer.masksToBounds = true
        setStyle(.neutral)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func setStyle(_ style: Style) {
        switch style {
        case .ok:
            backgroundColor = DesignTokens.Colors.ok
            textColor = .white
        case .pending:
            backgroundColor = DesignTokens.Colors.pending
            textColor = .black
        case .fail:
            backgroundColor = DesignTokens.Colors.fail
            textColor = .white
        case .neutral:
            backgroundColor = UIColor(white: 0.25, alpha: 0.9)
            textColor = DesignTokens.Colors.ink
        }
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
