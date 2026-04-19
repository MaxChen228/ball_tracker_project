import SwiftUI

/// Form section block. Top uppercase mono caption + content stack.
/// Aligns with server `.card-title` pattern.
struct AppSection<Content: View>: View {
    @Environment(\.appTheme) private var theme
    let title: String?
    var spacing: CGFloat
    @ViewBuilder var content: Content

    init(
        _ title: String? = nil,
        spacing: CGFloat = DesignTokens.Spacing.s,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.spacing = spacing
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: spacing) {
            if let title = title {
                Text(title.uppercased())
                    .font(DesignTokens.Swift.caption2)
                    .tracking(DesignTokens.Tracking.label)
                    .foregroundStyle(theme.palette.sub)
                    .padding(.bottom, DesignTokens.Spacing.xs)
            }
            content
        }
    }
}
