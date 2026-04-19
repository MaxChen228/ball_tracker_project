import SwiftUI

/// Status pill. Capsule with 1 px tone-colored border + 8% tone fill.
struct AppTag: View {
    enum Tone {
        case neutral, success, warning, destructive, cameraA, cameraB, accent
    }

    let text: String
    let tone: Tone
    @Environment(\.appTheme) private var theme

    private var color: Color {
        switch tone {
        case .neutral: return theme.palette.sub
        case .success: return theme.palette.success
        case .warning: return theme.palette.warning
        case .destructive: return theme.palette.destructive
        case .cameraA, .accent: return theme.palette.accent
        case .cameraB: return theme.palette.cameraB
        }
    }

    var body: some View {
        Text(text.uppercased())
            .font(DesignTokens.Swift.caption2)
            .tracking(DesignTokens.Tracking.label)
            .foregroundStyle(color)
            .padding(.horizontal, DesignTokens.Spacing.m)
            .padding(.vertical, DesignTokens.Spacing.xs)
            .background(Capsule().fill(color.opacity(0.10)))
            .overlay(Capsule().strokeBorder(color.opacity(0.45), lineWidth: 1))
    }
}
