import UIKit

/// PHYSICS_LAB-style status chip — 10 pt mono uppercase, 1 px tone border,
/// `hudSurface` fill. Matches web dashboard `.chip` / `.calibrated` patterns.
final class StatusChip: UILabel {
    enum Style {
        case ok
        case pending
        case fail
        case neutral
    }

    private let contentInsets = UIEdgeInsets(top: 3, left: 8, bottom: 3, right: 8)

    init() {
        super.init(frame: .zero)
        font = DesignTokens.Fonts.mono(size: 11, weight: .medium)
        textAlignment = .center
        numberOfLines = 1
        adjustsFontSizeToFitWidth = true
        minimumScaleFactor = 0.8
        layer.cornerRadius = DesignTokens.CornerRadius.chipSmall
        layer.masksToBounds = true
        layer.borderWidth = 1
        backgroundColor = DesignTokens.Colors.hudSurface
        setStyle(.neutral)
    }

    required init?(coder: NSCoder) { fatalError() }

    func setStyle(_ style: Style) {
        let tone: UIColor
        switch style {
        case .ok:      tone = DesignTokens.Colors.success
        case .pending: tone = DesignTokens.Colors.warning
        case .fail:    tone = DesignTokens.Colors.destructive
        case .neutral: tone = DesignTokens.Colors.sub
        }
        textColor = tone
        layer.borderColor = tone.withAlphaComponent(0.5).cgColor
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
