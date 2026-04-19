import SwiftUI

/// SwiftUI parallel of the UIKit `GateRow`. Shows ✓ / … / ✗ + label; optional
/// tap action promotes the row to a ghost button.
struct AppGateRowView: View {
    enum State { case pass, pending, fail }

    let text: String
    let state: State
    var action: (() -> Void)?

    @Environment(\.appTheme) private var theme

    private var glyph: String {
        switch state {
        case .pass: return "✓"
        case .pending: return "…"
        case .fail: return "✗"
        }
    }

    private var toneColor: Color {
        switch state {
        case .pass: return theme.palette.success
        case .pending: return theme.palette.warning
        case .fail: return theme.palette.destructive
        }
    }

    private var textColor: Color {
        switch state {
        case .pass: return theme.palette.success
        case .pending: return theme.palette.ink
        case .fail: return theme.palette.sub
        }
    }

    var body: some View {
        let row = HStack(spacing: DesignTokens.Spacing.s) {
            Text(glyph)
                .font(DesignTokens.Swift.mono(size: 16, weight: .bold))
                .foregroundStyle(toneColor)
                .frame(width: 20, alignment: .center)
            Text(text)
                .font(DesignTokens.Swift.subhead.weight(.medium))
                .foregroundStyle(textColor)
            Spacer(minLength: 0)
        }
        .padding(.vertical, DesignTokens.Spacing.xs)
        .contentShape(Rectangle())

        if let action = action, state != .pass {
            Button(action: action) { row }
                .buttonStyle(.appGhost(tone: toneColor))
        } else {
            row
        }
    }
}
