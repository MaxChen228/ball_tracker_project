import SwiftUI

/// Form row: fixed-width label on the left, control on the right.
/// Replaces the hand-rolled `fieldRow`/`controlRow` helpers in SettingsVC.
struct AppFieldRow<Trailing: View>: View {
    @Environment(\.appTheme) private var theme
    let label: String
    var labelWidth: CGFloat
    @ViewBuilder var trailing: Trailing

    init(
        _ label: String,
        labelWidth: CGFloat = 110,
        @ViewBuilder trailing: () -> Trailing
    ) {
        self.label = label
        self.labelWidth = labelWidth
        self.trailing = trailing()
    }

    var body: some View {
        HStack(spacing: DesignTokens.Spacing.m) {
            Text(label)
                .font(DesignTokens.Swift.body)
                .foregroundStyle(theme.palette.sub)
                .frame(width: labelWidth, alignment: .leading)
            trailing
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
