import SwiftUI

/// 1 px divider in theme `divider` color. Replaces SwiftUI `Divider()` so hue
/// stays on-palette.
struct AppDivider: View {
    @Environment(\.appTheme) private var theme

    var body: some View {
        Rectangle()
            .fill(theme.palette.divider)
            .frame(height: 1)
    }
}
