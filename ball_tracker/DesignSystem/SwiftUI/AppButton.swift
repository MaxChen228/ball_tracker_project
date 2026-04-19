import SwiftUI

// MARK: - Primary

/// Primary CTA: ink-filled pill, cream text. Aligns with server `.btn.primary`.
struct AppPrimaryButtonStyle: ButtonStyle {
    @Environment(\.appTheme) private var theme

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DesignTokens.Swift.body.weight(.medium))
            .foregroundStyle(theme.palette.cardBackground)
            .padding(.vertical, DesignTokens.Spacing.m)
            .padding(.horizontal, DesignTokens.Spacing.l)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.CornerRadius.chip, style: .continuous)
                    .fill(theme.palette.ink.opacity(configuration.isPressed ? 0.82 : 1.0))
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1.0)
            .animation(.easeOut(duration: DesignTokens.Motion.quick), value: configuration.isPressed)
    }
}

// MARK: - Secondary

/// Outline button: transparent bg, 1 px border, ink text.
struct AppSecondaryButtonStyle: ButtonStyle {
    @Environment(\.appTheme) private var theme

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DesignTokens.Swift.body.weight(.medium))
            .foregroundStyle(theme.palette.ink)
            .padding(.vertical, DesignTokens.Spacing.m)
            .padding(.horizontal, DesignTokens.Spacing.l)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.CornerRadius.chip, style: .continuous)
                    .strokeBorder(theme.palette.cardBorder, lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.7 : 1.0)
            .animation(.easeOut(duration: DesignTokens.Motion.quick), value: configuration.isPressed)
    }
}

// MARK: - Destructive

/// Outline in destructive tone, for `Delete` / `Cancel` CTAs.
struct AppDestructiveButtonStyle: ButtonStyle {
    @Environment(\.appTheme) private var theme

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DesignTokens.Swift.body.weight(.medium))
            .foregroundStyle(theme.palette.destructive)
            .padding(.vertical, DesignTokens.Spacing.m)
            .padding(.horizontal, DesignTokens.Spacing.l)
            .frame(maxWidth: .infinity)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.CornerRadius.chip, style: .continuous)
                    .strokeBorder(theme.palette.destructive.opacity(0.55), lineWidth: 1)
            )
            .opacity(configuration.isPressed ? 0.7 : 1.0)
            .animation(.easeOut(duration: DesignTokens.Motion.quick), value: configuration.isPressed)
    }
}

// MARK: - Ghost

/// No background except a faint fill while pressed. Aligns with kg
/// `GhostButtonStyle`.
struct AppGhostButtonStyle: ButtonStyle {
    @Environment(\.appTheme) private var theme
    var tone: Color?

    func makeBody(configuration: Configuration) -> some View {
        let effectiveTone = tone ?? theme.palette.accent
        configuration.label
            .font(DesignTokens.Swift.body.weight(.medium))
            .foregroundStyle(effectiveTone.opacity(configuration.isPressed ? 0.7 : 1.0))
            .padding(.vertical, DesignTokens.Spacing.s)
            .padding(.horizontal, DesignTokens.Spacing.m)
            .background(
                Capsule().fill(effectiveTone.opacity(configuration.isPressed ? 0.12 : 0))
            )
            .animation(.easeOut(duration: DesignTokens.Motion.quick), value: configuration.isPressed)
    }
}

// MARK: - Shorthand

extension ButtonStyle where Self == AppPrimaryButtonStyle {
    static var appPrimary: AppPrimaryButtonStyle { AppPrimaryButtonStyle() }
}

extension ButtonStyle where Self == AppSecondaryButtonStyle {
    static var appSecondary: AppSecondaryButtonStyle { AppSecondaryButtonStyle() }
}

extension ButtonStyle where Self == AppDestructiveButtonStyle {
    static var appDestructive: AppDestructiveButtonStyle { AppDestructiveButtonStyle() }
}

extension ButtonStyle where Self == AppGhostButtonStyle {
    static func appGhost(tone: Color? = nil) -> AppGhostButtonStyle {
        AppGhostButtonStyle(tone: tone)
    }
}
