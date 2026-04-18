import UIKit

/// Design tokens for the camera HUD + calibration screens. Single source of
/// truth for colors, fonts, spacing. All new UI code pulls from here — no
/// inline hex / `.systemRed` / ad-hoc `UIFont.systemFont` call sites.
///
/// Intentionally small: this project has ~3 screens worth of UI. Adding one
/// more primitive should only happen when the same literal shows up in two+
/// unrelated call sites.
enum DesignTokens {
    enum Colors {
        /// Success / Ready green. Muted so it sits comfortably over the live
        /// camera preview without grabbing attention away from the ball.
        static let ok = UIColor(red: 0x3D / 255.0, green: 0x7B / 255.0, blue: 0x5F / 255.0, alpha: 1.0)
        /// Pending / in-progress amber.
        static let pending = UIColor(red: 0xD4 / 255.0, green: 0x9A / 255.0, blue: 0x1F / 255.0, alpha: 1.0)
        /// Failure / destructive. Aliased to the system red so it still
        /// plays nicely with accessibility filters.
        static let fail = UIColor.systemRed
        /// Primary body text on dark HUD surface.
        static let ink = UIColor.white
        /// Secondary / caption text.
        static let sub = UIColor(white: 1.0, alpha: 0.65)
        /// Translucent black chip background over the live preview.
        static let surface = UIColor.black.withAlphaComponent(0.55)
        /// Hair-line border used on cards + rows. Replaces shadows so we
        /// don't fight the PHYSICS_LAB design system server-side.
        static let border = UIColor(white: 1.0, alpha: 0.18)
    }

    enum Fonts {
        /// Monospaced — use for timers, fps, hex ids, any numeric run where
        /// line-length should stay steady as the digit changes.
        static func mono(size: CGFloat, weight: UIFont.Weight = .medium) -> UIFont {
            UIFont.monospacedDigitSystemFont(ofSize: size, weight: weight)
        }
        /// Sans — use for prose, labels, status text.
        static func sans(size: CGFloat, weight: UIFont.Weight = .regular) -> UIFont {
            UIFont.systemFont(ofSize: size, weight: weight)
        }
    }

    /// Spacing scale. Base unit 4 pt. Use named steps — raw 4/8/12 literals
    /// in new code should be replaced with these.
    enum Spacing {
        static let xs: CGFloat = 4
        static let s: CGFloat = 8
        static let m: CGFloat = 12
        static let l: CGFloat = 16
        static let xl: CGFloat = 24
    }

    enum CornerRadius {
        static let chip: CGFloat = 4
        static let card: CGFloat = 8
    }
}
