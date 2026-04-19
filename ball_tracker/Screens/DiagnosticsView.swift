import SwiftUI

/// Diagnostics subpage content. Pulls from `DiagnosticsData.shared` at 1 Hz
/// via `TimelineView.periodic` — matches the prior `Timer`-driven refresh
/// without handing off the VM to a property wrapper.
struct DiagnosticsView: View {
    let onTest: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1.0)) { context in
            ScrollView {
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.xl) {
                    AppSection("Runtime") {
                        AppCard {
                            VStack(alignment: .leading, spacing: DesignTokens.Spacing.m) {
                                let data = DiagnosticsData.shared
                                row("FPS", value: String(format: "%.1f", data.fpsEstimate))
                                row("Server", value: data.serverStatusText)
                                row("Last contact", value: Self.relativeContact(data.lastContactAt, at: context.date))
                                row("Session", value: data.sessionId ?? "—")
                                row("Recording idx", value: data.localRecordingIndex.map(String.init) ?? "—")
                            }
                        }
                    }

                    Button(action: onTest) {
                        Text("Test 伺服器連線")
                    }
                    .buttonStyle(.appPrimary)
                }
                .padding(.horizontal, DesignTokens.Spacing.pageH)
                .padding(.vertical, DesignTokens.Spacing.l)
            }
            .background(theme.palette.pageBackground.ignoresSafeArea())
        }
    }

    @ViewBuilder
    private func row(_ key: String, value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: DesignTokens.Spacing.s) {
            Text(key)
                .font(DesignTokens.Swift.caption.weight(.medium))
                .foregroundStyle(theme.palette.sub)
                .frame(width: 120, alignment: .leading)
            Text(value)
                .font(DesignTokens.Swift.mono(size: 14))
                .foregroundStyle(theme.palette.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private static func relativeContact(_ date: Date?, at now: Date) -> String {
        guard let date else { return "—" }
        let s = Int(now.timeIntervalSince(date))
        if s < 60 { return "\(s) 秒前" }
        if s < 3600 { return "\(s / 60) 分 \(s % 60) 秒前" }
        return "\(s / 3600) 小時前"
    }
}
