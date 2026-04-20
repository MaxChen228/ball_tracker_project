import SwiftUI

/// Diagnostics subpage content. Pulls from `DiagnosticsData.shared` at 1 Hz
/// via `TimelineView.periodic` — matches the prior `Timer`-driven refresh
/// without handing off the VM to a property wrapper.
struct DiagnosticsView: View {
    let onTest: () -> Void

    @Environment(\.appTheme) private var theme
    private var data: DiagnosticsData { DiagnosticsData.shared }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1.0)) { context in
            content(at: context.date)
        }
    }

    @ViewBuilder
    private func content(at now: Date) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.xl) {
                AppSection("Runtime") {
                    AppCard {
                        VStack(alignment: .leading, spacing: DesignTokens.Spacing.m) {
                            row("FPS", value: String(format: "%.1f", data.fpsEstimate))
                            row("Server", value: data.serverStatusText)
                            row("Last contact", value: Self.relativeContact(data.lastContactAt, at: now))
                            row("Session", value: data.sessionId ?? "—")
                            row("Recording idx", value: data.localRecordingIndex.map(String.init) ?? "—")
                            row("Exposure cap", value: data.trackingExposureCapLabel)
                        }
                    }
                }

                AppSection("240 FPS Experiment") {
                    AppCard {
                        VStack(alignment: .leading, spacing: DesignTokens.Spacing.m) {
                            Text("Tracking 曝光上限")
                                .font(DesignTokens.Swift.body.weight(.semibold))
                                .foregroundStyle(theme.palette.ink)
                            Text("只影響高速錄影路線；用來比較 motion blur 與亮度噪點的取捨。")
                                .font(DesignTokens.Swift.caption)
                                .foregroundStyle(theme.palette.sub)
                            row("Source", value: "Dashboard / heartbeat")
                            row("Effective", value: data.trackingExposureCapLabel)
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
