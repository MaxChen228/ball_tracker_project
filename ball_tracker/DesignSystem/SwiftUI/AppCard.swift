import SwiftUI

/// Card surface. 1 px border, no shadow — PHYSICS_LAB精神.
struct AppCard<Content: View>: View {
    @Environment(\.appTheme) private var theme
    var padding: CGFloat
    var cornerRadius: CGFloat
    @ViewBuilder var content: Content

    init(
        padding: CGFloat = DesignTokens.Spacing.l,
        cornerRadius: CGFloat = DesignTokens.CornerRadius.card,
        @ViewBuilder content: () -> Content
    ) {
        self.padding = padding
        self.cornerRadius = cornerRadius
        self.content = content()
    }

    var body: some View {
        content
            .padding(padding)
            .background(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .fill(theme.palette.cardBackground)
            )
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
                    .strokeBorder(theme.palette.cardBorder, lineWidth: 1)
            )
    }
}
