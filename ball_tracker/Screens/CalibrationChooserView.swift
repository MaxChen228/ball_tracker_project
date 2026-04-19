import SwiftUI

/// Entry-point screen where the operator picks auto-ArUco vs manual 5-point
/// calibration. Pure content — navigation + push into Auto / Manual VCs is
/// handled by the UIKit container `CalibrationChooserViewController`.
struct CalibrationChooserView: View {
    let onAuto: () -> Void
    let onManual: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.l) {
                Text("選擇一種校正方式")
                    .font(DesignTokens.Swift.subhead.weight(.semibold))
                    .foregroundStyle(theme.palette.sub)

                methodCard(
                    title: "自動校正（推薦）",
                    subtitle: "貼 6 張 ArUco 標記到本壘板後，對準拍照即可。",
                    buttonTitle: "使用自動校正  →",
                    action: onAuto
                )
                methodCard(
                    title: "手動校正",
                    subtitle: "拖曳 5 個點到本壘板邊緣與後點。",
                    buttonTitle: "使用手動校正  →",
                    action: onManual
                )
            }
            .padding(.horizontal, DesignTokens.Spacing.pageH)
            .padding(.vertical, DesignTokens.Spacing.xl)
        }
        .background(theme.palette.pageBackground.ignoresSafeArea())
    }

    @ViewBuilder
    private func methodCard(
        title: String,
        subtitle: String,
        buttonTitle: String,
        action: @escaping () -> Void
    ) -> some View {
        AppCard {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.s) {
                Text(title)
                    .font(DesignTokens.Swift.h2.weight(.medium))
                    .foregroundStyle(theme.palette.ink)
                Text(subtitle)
                    .font(DesignTokens.Swift.subhead)
                    .foregroundStyle(theme.palette.sub)
                    .fixedSize(horizontal: false, vertical: true)
                Button(action: action) {
                    HStack {
                        Text(buttonTitle)
                        Spacer()
                    }
                }
                .buttonStyle(.appGhost(tone: theme.palette.accent))
                .padding(.top, DesignTokens.Spacing.xs)
            }
        }
    }
}
