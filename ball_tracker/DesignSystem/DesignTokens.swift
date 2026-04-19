import UIKit
import SwiftUI

/// Design tokens — single source of truth across the app.
///
/// Hybrid language: KG 莫蘭迪 palette + server PHYSICS_LAB density & 1 px border
/// philosophy (shadows are avoided). iOS system fonts throughout (SF + SF Mono
/// + SF Serif) so no third-party font bundling.
///
/// UIKit-facing values are the authoritative storage (`UIColor` / `UIFont`).
/// SwiftUI callers consume them via `Color(uiColor:)` / `Font(uiFont)` or the
/// convenience accessors on `DesignTokens.Swift`.
enum DesignTokens {

    // MARK: - Colors

    enum Colors {
        // --- Surface ----------------------------------------------------
        /// Page / default screen background.
        static let pageBackground = UIColor(red: 0.954, green: 0.952, blue: 0.947, alpha: 1.0)
        /// Sheet / list stage background (slightly lighter than page).
        static let stageBackground = UIColor(red: 0.972, green: 0.970, blue: 0.964, alpha: 1.0)
        /// Card surface (lightest cream).
        static let cardBackground = UIColor(red: 0.989, green: 0.987, blue: 0.982, alpha: 1.0)
        /// Translucent cream for HUD chips + cards that sit on top of the
        /// live camera preview. Swap alpha back to `0.55` on `UIColor.black`
        /// if outdoor readability regresses — one-line rollback.
        static let hudSurface = cardBackground.withAlphaComponent(0.88)

        // --- Lines ------------------------------------------------------
        /// 1 px border on cards / chips (replaces shadow).
        static let cardBorder = UIColor.black.withAlphaComponent(0.048)
        /// 1 px divider inside cards between sections.
        static let divider = UIColor.black.withAlphaComponent(0.05)

        // --- Text -------------------------------------------------------
        /// Primary body text — near-black deep warm gray.
        static let ink = UIColor(red: 0.19, green: 0.19, blue: 0.18, alpha: 1.0)
        /// Secondary caption text.
        static let sub = UIColor(red: 0.43, green: 0.43, blue: 0.42, alpha: 1.0)
        /// Tertiary / decorative text.
        static let tertiary = UIColor(red: 0.44, green: 0.44, blue: 0.42, alpha: 1.0)

        // --- Semantic ---------------------------------------------------
        /// Interactive accent + Camera A axis. KG 莫蘭迪 灰藍.
        static let accent = UIColor(hue: 215.0/360.0, saturation: 0.28, brightness: 0.66, alpha: 1.0)
        /// Camera B axis — muted chestnut (aligned with server dashboard
        /// `--dual`, morandi-washed).
        static let cameraB = UIColor(hue: 22.0/360.0, saturation: 0.26, brightness: 0.62, alpha: 1.0)
        /// Success / Ready.
        static let success = UIColor(hue: 152.0/360.0, saturation: 0.20, brightness: 0.60, alpha: 1.0)
        /// Warning / Pending / time-sync-waiting.
        static let warning = UIColor(hue: 36.0/360.0, saturation: 0.80, brightness: 0.80, alpha: 1.0)
        /// Destructive / error / REC indicator.
        static let destructive = UIColor(hue: 355.0/360.0, saturation: 0.26, brightness: 0.66, alpha: 1.0)
        /// SwiftUI `.tint()` base.
        static let tint = UIColor(hue: 215.0/360.0, saturation: 0.16, brightness: 0.52, alpha: 1.0)

        // --- Legacy aliases (existing call-sites) -----------------------
        // Kept so HUD / chip code keeps compiling while the palette flips
        // from dark-HUD to light-HUD in a single place.
        static let ok = success
        static let pending = warning
        static let fail = destructive
        static let surface = hudSurface
        static let border = cardBorder
    }

    // MARK: - Typography

    enum Fonts {
        /// SF Serif (system Serif design) — for display / hero headers.
        /// Falls back to plain system font if `.serif` descriptor is nil.
        static func serif(size: CGFloat, weight: UIFont.Weight = .regular) -> UIFont {
            let base = UIFont.systemFont(ofSize: size, weight: weight)
            if let descriptor = base.fontDescriptor.withDesign(.serif) {
                return UIFont(descriptor: descriptor, size: size)
            }
            return base
        }

        /// SF (default system) — body / labels.
        static func sans(size: CGFloat, weight: UIFont.Weight = .regular) -> UIFont {
            UIFont.systemFont(ofSize: size, weight: weight)
        }

        /// SF Mono — for timers, fps, hex ids, metric values.
        static func mono(size: CGFloat, weight: UIFont.Weight = .medium) -> UIFont {
            UIFont.monospacedDigitSystemFont(ofSize: size, weight: weight)
        }

        // --- Scale presets (kg typography cascade, iOS-system backed) ---

        static func heroDisplay(weight: UIFont.Weight = .medium) -> UIFont { serif(size: 34, weight: weight) }
        static func h1(weight: UIFont.Weight = .medium) -> UIFont { serif(size: 28, weight: weight) }
        static func h2(weight: UIFont.Weight = .medium) -> UIFont { serif(size: 22, weight: weight) }
        static func body(weight: UIFont.Weight = .regular) -> UIFont { sans(size: 17, weight: weight) }
        static func subhead(weight: UIFont.Weight = .regular) -> UIFont { sans(size: 15, weight: weight) }
        static func caption(weight: UIFont.Weight = .regular) -> UIFont { sans(size: 12, weight: weight) }
        static func caption2(weight: UIFont.Weight = .medium) -> UIFont { sans(size: 11, weight: weight) }
        /// 9 pt mono — PHYSICS_LAB-style metadata labels. Pair with tracking.
        static func caption3(weight: UIFont.Weight = .regular) -> UIFont { mono(size: 9, weight: weight) }
    }

    // MARK: - Spacing (named scale, 4 pt base)

    enum Spacing {
        static let micro: CGFloat = 2
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        /// Page horizontal padding (kg --page-h-padding).
        static let pageH: CGFloat = 20
        /// Keep existing value 24 so UIKit call-sites (ReadyCard bottom,
        /// CalibrationChooser top) don't shift.
        static let xl: CGFloat = 24
        static let xxl: CGFloat = 32
        static let huge: CGFloat = 48
    }

    // MARK: - Corner radii

    enum CornerRadius {
        /// PHYSICS_LAB button / timeline input.
        static let chipSmall: CGFloat = 4
        /// KG chip radius.
        static let chip: CGFloat = 8
        /// KG card radius.
        static let card: CGFloat = 12
        /// Large overlay / sheet radius.
        static let sheet: CGFloat = 16
    }

    // MARK: - Motion

    enum Motion {
        /// Quick UI state swap (chip tone flip, toggle).
        static let quick: TimeInterval = 0.15
        /// Standard fade / translate.
        static let standard: TimeInterval = 0.18
        /// Spring-ish press feedback on buttons.
        static let buttonSpring: TimeInterval = 0.35
    }

    // MARK: - Tracking (letter-spacing)

    enum Tracking {
        /// Body — no tracking.
        static let none: CGFloat = 0
        /// Small uppercase label (card titles, section dividers).
        static let label: CGFloat = 0.8
        /// Monospace 9 pt metadata — wide tracking for PHYSICS_LAB density.
        static let metadata: CGFloat = 1.2
    }

    // MARK: - SwiftUI projection

    /// SwiftUI-side accessors. Values are computed from the UIKit-side
    /// constants so the two stay in lock-step.
    enum Swift {
        // Surface
        static var pageBackground: Color { Color(uiColor: Colors.pageBackground) }
        static var stageBackground: Color { Color(uiColor: Colors.stageBackground) }
        static var cardBackground: Color { Color(uiColor: Colors.cardBackground) }
        static var hudSurface: Color { Color(uiColor: Colors.hudSurface) }
        // Lines
        static var cardBorder: Color { Color(uiColor: Colors.cardBorder) }
        static var divider: Color { Color(uiColor: Colors.divider) }
        // Text
        static var ink: Color { Color(uiColor: Colors.ink) }
        static var sub: Color { Color(uiColor: Colors.sub) }
        static var tertiary: Color { Color(uiColor: Colors.tertiary) }
        // Semantic
        static var accent: Color { Color(uiColor: Colors.accent) }
        static var cameraB: Color { Color(uiColor: Colors.cameraB) }
        static var success: Color { Color(uiColor: Colors.success) }
        static var warning: Color { Color(uiColor: Colors.warning) }
        static var destructive: Color { Color(uiColor: Colors.destructive) }
        static var tint: Color { Color(uiColor: Colors.tint) }

        // Fonts
        static func serif(size: CGFloat, weight: Font.Weight = .regular) -> Font {
            Font.system(size: size, weight: weight, design: .serif)
        }
        static func sans(size: CGFloat, weight: Font.Weight = .regular) -> Font {
            Font.system(size: size, weight: weight, design: .default)
        }
        static func mono(size: CGFloat, weight: Font.Weight = .medium) -> Font {
            Font.system(size: size, weight: weight, design: .monospaced)
        }

        static var heroDisplay: Font { serif(size: 34, weight: .medium) }
        static var h1: Font { serif(size: 28, weight: .medium) }
        static var h2: Font { serif(size: 22, weight: .medium) }
        static var body: Font { sans(size: 17) }
        static var subhead: Font { sans(size: 15) }
        static var caption: Font { sans(size: 12) }
        static var caption2: Font { sans(size: 11, weight: .medium) }
        static var caption3: Font { mono(size: 9) }
    }
}
