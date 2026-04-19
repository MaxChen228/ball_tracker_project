import SwiftUI

/// SwiftUI environment wrapper for the ball_tracker design system.
///
/// Only `.light` is implemented. Dark / sepia are scaffolded by the `Palette`
/// struct so adding them later is a matter of filling in values and wiring a
/// picker — no component-level changes needed.
struct AppTheme: Equatable {
    struct Palette: Equatable {
        // Surface
        let pageBackground: Color
        let stageBackground: Color
        let cardBackground: Color
        // Lines
        let cardBorder: Color
        let divider: Color
        // Text
        let ink: Color
        let sub: Color
        let tertiary: Color
        // Semantic
        let accent: Color
        let cameraB: Color
        let success: Color
        let warning: Color
        let destructive: Color
        let tint: Color
    }

    let palette: Palette

    static let light = AppTheme(palette: .init(
        pageBackground: DesignTokens.Swift.pageBackground,
        stageBackground: DesignTokens.Swift.stageBackground,
        cardBackground: DesignTokens.Swift.cardBackground,
        cardBorder: DesignTokens.Swift.cardBorder,
        divider: DesignTokens.Swift.divider,
        ink: DesignTokens.Swift.ink,
        sub: DesignTokens.Swift.sub,
        tertiary: DesignTokens.Swift.tertiary,
        accent: DesignTokens.Swift.accent,
        cameraB: DesignTokens.Swift.cameraB,
        success: DesignTokens.Swift.success,
        warning: DesignTokens.Swift.warning,
        destructive: DesignTokens.Swift.destructive,
        tint: DesignTokens.Swift.tint
    ))
}

private struct AppThemeEnvironmentKey: EnvironmentKey {
    static let defaultValue = AppTheme.light
}

extension EnvironmentValues {
    var appTheme: AppTheme {
        get { self[AppThemeEnvironmentKey.self] }
        set { self[AppThemeEnvironmentKey.self] = newValue }
    }
}

extension View {
    /// Inject a theme into the view tree. Pair with `.tint(theme.palette.tint)`
    /// at the root if you want system affordances (TextField caret, Toggle
    /// fill) to pick up the accent.
    func appTheme(_ theme: AppTheme) -> some View {
        environment(\.appTheme, theme)
            .tint(theme.palette.tint)
    }
}
