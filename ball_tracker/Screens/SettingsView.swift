import SwiftUI

// MARK: - View model

/// Backing state for `SettingsView`. Strings for every field so the SwiftUI
/// `TextField`s can two-way-bind — the UIKit container runs `validate()` and
/// `save()` at commit time, which is the only point we coerce back to Int.
///
/// Phase 6: Settings is bootstrap-only. All other runtime tuning is
/// dashboard-pushed over the live WS settings + heartbeat channel.
@Observable
final class SettingsViewModel {
    // Server
    var serverIP: String = "192.168.1.100"
    var serverPort: String = "8765"

    // Camera
    var cameraRoleIndex: Int = 0  // 0 == A, 1 == B

    // Status summary (computed from current in-memory state).
    var statusServer: String = ""
    var statusRole: String = ""

    private var loaded: SettingsViewController.Settings?

    func load() {
        let s = SettingsViewController.loadFromUserDefaults()
        loaded = s
        serverIP = s.serverIP
        serverPort = String(s.serverPort)
        cameraRoleIndex = s.cameraRole == "B" ? 1 : 0
        refreshStatusSummary()
    }

    func validate() -> String? {
        guard let port = Int(serverPort), (1...65535).contains(port) else {
            return "Server port 需介於 1–65535"
        }
        return nil
    }

    func save() {
        let current = loaded ?? SettingsViewController.loadFromUserDefaults()
        let settings = SettingsViewController.Settings(
            serverIP: normalizeServerIP(serverIP, fallback: current.serverIP),
            serverPort: Int(serverPort) ?? current.serverPort,
            cameraRole: cameraRoleIndex == 1 ? "B" : "A"
        )
        SettingsViewController.saveToUserDefaults(settings)
    }

    func refreshStatusSummary() {
        let port = Int(serverPort) ?? 0
        statusServer = "\(serverIP):\(port)"
        statusRole = cameraRoleIndex == 0 ? "A · 1B 側" : "B · 3B 側"
    }

    private func normalizeServerIP(_ text: String, fallback: String) -> String {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return fallback }
        if let schemeRange = s.range(of: "://") {
            s = String(s[schemeRange.upperBound...])
        }
        if let slash = s.firstIndex(of: "/") { s = String(s[..<slash]) }
        if let colon = s.firstIndex(of: ":") { s = String(s[..<colon]) }
        s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? fallback : s
    }
}

// MARK: - View

struct SettingsView: View {
    @Bindable var viewModel: SettingsViewModel
    let onOpenDiagnostics: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.xl) {
                statusSection
                serverSection
                cameraSection
                diagnosticsSection
            }
            .padding(.horizontal, DesignTokens.Spacing.pageH)
            .padding(.vertical, DesignTokens.Spacing.l)
        }
        .background(theme.palette.pageBackground.ignoresSafeArea())
        .scrollDismissesKeyboard(.interactively)
        .onChange(of: viewModel.serverIP) { _, _ in viewModel.refreshStatusSummary() }
        .onChange(of: viewModel.serverPort) { _, _ in viewModel.refreshStatusSummary() }
        .onChange(of: viewModel.cameraRoleIndex) { _, _ in viewModel.refreshStatusSummary() }
    }

    // MARK: Sections

    private var statusSection: some View {
        AppSection("Status") {
            AppCard {
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.s) {
                    statusRow("Server", viewModel.statusServer, mono: true)
                    statusRow("Role", viewModel.statusRole)
                }
            }
        }
    }

    private var serverSection: some View {
        sectionWithFooter(
            "Server",
            footer: "IP 支援貼整串 URL（如 http://x.x.x.x:8765/status），會自動抽出主機。"
        ) {
            VStack(spacing: DesignTokens.Spacing.m) {
                AppFieldRow("IP") {
                    plainField($viewModel.serverIP, placeholder: "192.168.1.100", keyboard: .numbersAndPunctuation)
                }
                AppFieldRow("Port") {
                    plainField($viewModel.serverPort, placeholder: "8765", keyboard: .numberPad)
                }
            }
        }
    }

    private var cameraSection: some View {
        sectionWithFooter(
            "Camera",
            footer: "Role 決定這支手機在 dashboard 上以 A / B 哪個身分註冊。" +
                    "校正、HSV、chirp 門檻、heartbeat 週期等都從 dashboard 推送。"
        ) {
            AppFieldRow("Role") {
                Picker("", selection: $viewModel.cameraRoleIndex) {
                    Text("A · 1B 側").tag(0)
                    Text("B · 3B 側").tag(1)
                }
                .pickerStyle(.segmented)
            }
        }
    }

    private var diagnosticsSection: some View {
        sectionWithFooter(
            "診斷",
            footer: "FPS、last contact、session id、Test 連線。"
        ) {
            Button(action: onOpenDiagnostics) {
                HStack {
                    Text("Diagnostics")
                    Spacer()
                    Text("→").font(DesignTokens.Swift.body.weight(.semibold))
                }
            }
            .buttonStyle(.appGhost())
        }
    }

    // MARK: Helpers

    @ViewBuilder
    private func sectionWithFooter<Content: View>(
        _ title: String,
        footer: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: DesignTokens.Spacing.s) {
            AppSection(title) {
                AppCard { content() }
            }
            Text(footer)
                .font(DesignTokens.Swift.caption)
                .foregroundStyle(theme.palette.sub)
                .padding(.horizontal, DesignTokens.Spacing.xs)
        }
    }

    @ViewBuilder
    private func plainField(
        _ binding: Binding<String>,
        placeholder: String,
        keyboard: UIKeyboardType
    ) -> some View {
        TextField(placeholder, text: binding)
            .keyboardType(keyboard)
            .autocorrectionDisabled()
            .textInputAutocapitalization(.never)
            .textFieldStyle(.roundedBorder)
            .font(DesignTokens.Swift.body)
    }

    @ViewBuilder
    private func statusRow(_ key: String, _ value: String, mono: Bool = false) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: DesignTokens.Spacing.s) {
            Text(key)
                .font(DesignTokens.Swift.caption.weight(.medium))
                .foregroundStyle(theme.palette.sub)
                .frame(width: 88, alignment: .leading)
            Text(value)
                .font(mono ? DesignTokens.Swift.mono(size: 14) : DesignTokens.Swift.subhead)
                .foregroundStyle(theme.palette.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
