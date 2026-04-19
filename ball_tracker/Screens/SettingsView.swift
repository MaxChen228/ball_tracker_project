import SwiftUI

// MARK: - View model

/// Backing state for `SettingsView`. Strings for every field so the SwiftUI
/// `TextField`s can two-way-bind — the UIKit container runs `validate()` and
/// `save()` at commit time, which is the only point we coerce back to Int /
/// Double.
@Observable
final class SettingsViewModel {
    // Server
    var serverIP: String = "192.168.1.100"
    var serverPort: String = "8765"
    var pollInterval: String = "1"

    // Camera
    var cameraRoleIndex: Int = 0  // 0 == A, 1 == B
    /// Recording-resolution picker. Stored as `height` so the SwiftUI
    /// Picker can use `Int` tags directly; width is recovered at save
    /// time via `SettingsViewController.captureResolutions`.
    var captureHeight: Int = SettingsViewController.captureHeightFixed

    // Audio
    var chirpThreshold: String = "0.18"

    // Intrinsics
    var manualIntrinsicsEnabled: Bool = false
    var manualFx: String = ""
    var manualFy: String = ""
    var manualCx: String = ""
    var manualCy: String = ""
    var manualDistortion: String = ""

    // Status summary (computed from current in-memory state + pending imports)
    var statusServer: String = ""
    var statusRole: String = ""
    var statusIntrinsics: String = "FOV approximation（未校正）"

    private var loaded: SettingsViewController.Settings?
    private var pendingImportMeta: (w: Int, h: Int, rms: Double)?

    func load() {
        let s = SettingsViewController.loadFromUserDefaults()
        loaded = s
        serverIP = s.serverIP
        serverPort = String(s.serverPort)
        pollInterval = String(s.pollInterval)
        cameraRoleIndex = s.cameraRole == "B" ? 1 : 0
        captureHeight = s.captureHeight
        chirpThreshold = String(s.chirpThreshold)
        manualIntrinsicsEnabled = s.manualIntrinsicsEnabled
        manualFx = s.manualFx > 0 ? String(s.manualFx) : ""
        manualFy = s.manualFy > 0 ? String(s.manualFy) : ""
        manualCx = s.manualCx > 0 ? String(s.manualCx) : ""
        manualCy = s.manualCy > 0 ? String(s.manualCy) : ""
        manualDistortion = s.manualDistortion.isEmpty
            ? ""
            : s.manualDistortion.map { String($0) }.joined(separator: ",")
        refreshStatusSummary()
    }

    func validate() -> String? {
        guard let port = Int(serverPort), (1...65535).contains(port) else {
            return "Server port 需介於 1–65535"
        }
        guard let poll = Double(pollInterval), (1.0...60.0).contains(poll) else {
            return "Heartbeat interval 需介於 1–60 秒"
        }
        guard let ct = Double(chirpThreshold), ct > 0, ct <= 1 else {
            return "Chirp threshold 需介於 0–1（典型 0.15–0.35）"
        }
        if manualIntrinsicsEnabled {
            guard let fx = Double(manualFx), fx > 100 else { return "fx 不合理（應 > 100 px）" }
            guard let fy = Double(manualFy), fy > 100 else { return "fy 不合理（應 > 100 px）" }
            guard let cx = Double(manualCx), cx > 0 else { return "cx 不合理（需 > 0）" }
            guard let cy = Double(manualCy), cy > 0 else { return "cy 不合理（需 > 0）" }
            let distRaw = manualDistortion.trimmingCharacters(in: .whitespacesAndNewlines)
            if !distRaw.isEmpty {
                let parts = distRaw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                if parts.count != 5 { return "distortion 需 5 個以逗號分隔的數字（k1,k2,p1,p2,k3）" }
                if parts.contains(where: { Double($0) == nil }) {
                    return "distortion 含非數字元素"
                }
            }
        }
        return nil
    }

    func save() {
        let current = loaded ?? SettingsViewController.loadFromUserDefaults()
        let meta: (w: Int, h: Int, rms: Double, at: Date?)
        if let p = pendingImportMeta {
            meta = (p.w, p.h, p.rms, Date())
        } else if manualIntrinsicsEnabled {
            if current.intrinsicsCalibratedWidth > 0 {
                meta = (current.intrinsicsCalibratedWidth,
                        current.intrinsicsCalibratedHeight,
                        current.intrinsicsRms,
                        current.intrinsicsCalibratedAt)
            } else {
                meta = (SettingsViewController.captureWidthFixed,
                        SettingsViewController.captureHeightFixed, 0, Date())
            }
        } else {
            meta = (0, 0, 0, nil)
        }

        // Recover the recording resolution's width from the picker's
        // height — captureResolutions is the single source of truth for
        // the supported (width, height) pairs.
        let pickedResolution = SettingsViewController.captureResolutions
            .first { $0.height == captureHeight }
            ?? SettingsViewController.captureResolutions[0]

        let settings = SettingsViewController.Settings(
            serverIP: normalizeServerIP(serverIP, fallback: current.serverIP),
            serverPort: Int(serverPort) ?? current.serverPort,
            cameraRole: cameraRoleIndex == 1 ? "B" : "A",
            chirpThreshold: Double(chirpThreshold) ?? current.chirpThreshold,
            pollInterval: min(60, max(1, Double(pollInterval) ?? current.pollInterval)),
            captureWidth: pickedResolution.width,
            captureHeight: pickedResolution.height,
            parkCameraInStandby: current.parkCameraInStandby,
            manualIntrinsicsEnabled: manualIntrinsicsEnabled,
            manualFx: Double(manualFx) ?? current.manualFx,
            manualFy: Double(manualFy) ?? current.manualFy,
            manualCx: Double(manualCx) ?? current.manualCx,
            manualCy: Double(manualCy) ?? current.manualCy,
            manualDistortion: parseDistortion(manualDistortion) ?? current.manualDistortion,
            intrinsicsCalibratedWidth: meta.w,
            intrinsicsCalibratedHeight: meta.h,
            intrinsicsRms: meta.rms,
            intrinsicsCalibratedAt: meta.at
        )
        SettingsViewController.saveToUserDefaults(settings)
    }

    func applyImportedCharuco(data: Data) throws {
        struct Payload: Decodable {
            let fx: Double; let fy: Double; let cx: Double; let cy: Double
            let image_width: Int; let image_height: Int
            let rms_reprojection_error_px: Double?
            let distortion_coeffs: [Double]
        }
        let p = try JSONDecoder().decode(Payload.self, from: data)
        let targetW = SettingsViewController.captureWidthFixed
        let targetH = SettingsViewController.captureHeightFixed
        guard let baked = Self.scaleIntrinsics(
            fx: p.fx, fy: p.fy, cx: p.cx, cy: p.cy,
            fromW: p.image_width, fromH: p.image_height,
            toW: targetW, toH: targetH
        ) else {
            throw ImportError.unsupportedAspect(w: p.image_width, h: p.image_height, targetW: targetW, targetH: targetH)
        }
        manualIntrinsicsEnabled = true
        manualFx = String(format: "%.2f", baked.fx)
        manualFy = String(format: "%.2f", baked.fy)
        manualCx = String(format: "%.2f", baked.cx)
        manualCy = String(format: "%.2f", baked.cy)
        if p.distortion_coeffs.count == 5 {
            manualDistortion = p.distortion_coeffs.map { String(format: "%.6f", $0) }.joined(separator: ",")
        }
        pendingImportMeta = (targetW, targetH, p.rms_reprojection_error_px ?? 0)
        refreshStatusSummary()
    }

    enum ImportError: LocalizedError {
        case unsupportedAspect(w: Int, h: Int, targetW: Int, targetH: Int)
        var errorDescription: String? {
            switch self {
            case .unsupportedAspect(let w, let h, let tw, let th):
                return "JSON 為 \(w)×\(h)，無法轉換到 \(tw)×\(th)。"
            }
        }
    }

    func refreshStatusSummary() {
        let port = Int(serverPort) ?? 0
        statusServer = "\(serverIP):\(port)"
        statusRole = cameraRoleIndex == 0 ? "A · 1B 側" : "B · 3B 側"
        if manualIntrinsicsEnabled, let fx = Double(manualFx), fx > 0 {
            var parts = [pendingImportMeta != nil ? "ChArUco" : "Manual"]
            parts.append("\(SettingsViewController.captureHeightFixed)p")
            if let rms = pendingImportMeta?.rms, rms > 0 {
                parts.append(String(format: "RMS %.2f px", rms))
            } else if let loaded, loaded.intrinsicsRms > 0 {
                parts.append(String(format: "RMS %.2f px", loaded.intrinsicsRms))
            }
            if let at = loaded?.intrinsicsCalibratedAt, pendingImportMeta == nil {
                let fmt = RelativeDateTimeFormatter()
                fmt.unitsStyle = .abbreviated
                parts.append(fmt.localizedString(for: at, relativeTo: Date()))
            }
            statusIntrinsics = parts.joined(separator: " · ")
        } else {
            statusIntrinsics = "FOV approximation（未校正）"
        }
    }

    private func parseDistortion(_ text: String) -> [Double]? {
        let raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.isEmpty { return [] }
        let parts = raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 5 else { return nil }
        let values = parts.compactMap { Double($0) }
        return values.count == 5 ? values : nil
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

    /// Scales intrinsics between source resolution and target resolution,
    /// handling 4:3 → 16:9 center-crop (ChArUco JSON is typically 4032×3024
    /// while the pipeline runs at 1080p 16:9).
    private static func scaleIntrinsics(
        fx: Double, fy: Double, cx: Double, cy: Double,
        fromW: Int, fromH: Int, toW: Int, toH: Int
    ) -> (fx: Double, fy: Double, cx: Double, cy: Double)? {
        let fromAspect = Double(fromW) / Double(fromH)
        let toAspect = Double(toW) / Double(toH)
        let eps = 0.01
        var effH = fromH
        var adjCy = cy
        if abs(fromAspect - toAspect) > eps {
            if fromAspect < toAspect {
                let newH = Int((Double(fromW) / toAspect).rounded())
                let crop = (fromH - newH) / 2
                effH = newH
                adjCy = cy - Double(crop)
            } else {
                return nil
            }
        }
        let sx = Double(toW) / Double(fromW)
        let sy = Double(toH) / Double(effH)
        return (fx * sx, fy * sy, cx * sx, adjCy * sy)
    }
}

// MARK: - View

struct SettingsView: View {
    @Bindable var viewModel: SettingsViewModel
    let onImportIntrinsics: () -> Void
    let onOpenDiagnostics: () -> Void

    @Environment(\.appTheme) private var theme

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DesignTokens.Spacing.xl) {
                statusSection
                serverSection
                cameraSection
                audioSection
                intrinsicsSection
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
        .onChange(of: viewModel.manualIntrinsicsEnabled) { _, _ in viewModel.refreshStatusSummary() }
        .onChange(of: viewModel.manualFx) { _, _ in viewModel.refreshStatusSummary() }
    }

    // MARK: Sections

    private var statusSection: some View {
        AppSection("Status") {
            AppCard {
                VStack(alignment: .leading, spacing: DesignTokens.Spacing.s) {
                    statusRow("Server", viewModel.statusServer, mono: true)
                    statusRow("Role", viewModel.statusRole)
                    statusRow("Intrinsics", viewModel.statusIntrinsics)
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
                AppFieldRow("Heartbeat (s)") {
                    plainField($viewModel.pollInterval, placeholder: "1", keyboard: .decimalPad)
                }
            }
        }
    }

    private var cameraSection: some View {
        sectionWithFooter(
            "Camera",
            footer: "校正永遠以 1080p 為基準；改錄製解析度只影響上傳大小，server 會自動縮放內參。\n部分 iPhone 只支援 1080p / 720p @240fps；選 540p 若開啟失敗請降回 720p。\nFPS 自動切換：待機 60、錄影 240（曝光上限鎖定，暗室會噪聲化但不掉幀）。\nSTANDBY 的即時預覽用主畫面右上「預覽」按鈕切換。"
        ) {
            VStack(spacing: DesignTokens.Spacing.m) {
                AppFieldRow("Role") {
                    Picker("", selection: $viewModel.cameraRoleIndex) {
                        Text("A · 1B 側").tag(0)
                        Text("B · 3B 側").tag(1)
                    }
                    .pickerStyle(.segmented)
                }
                AppFieldRow("Resolution") {
                    Picker("", selection: $viewModel.captureHeight) {
                        ForEach(SettingsViewController.captureResolutions, id: \.height) { res in
                            Text(res.label).tag(res.height)
                        }
                    }
                    .pickerStyle(.segmented)
                }
            }
        }
    }

    private var audioSection: some View {
        sectionWithFooter(
            "Audio Sync",
            footer: "HUD 閃橘色（接近）但不觸發 → 降低；環境噪音誤觸 → 提高。預設 0.18。"
        ) {
            AppFieldRow("Chirp Threshold") {
                plainField($viewModel.chirpThreshold, placeholder: "0.18", keyboard: .decimalPad)
            }
        }
    }

    private var intrinsicsSection: some View {
        sectionWithFooter(
            "Camera Intrinsics",
            footer: "匯入 calibrate_intrinsics.py 輸出的 JSON，自動縮放到 1080p。關閉開關時改用 FOV 近似。"
        ) {
            VStack(spacing: DesignTokens.Spacing.m) {
                AppFieldRow("Use ChArUco") {
                    Toggle("", isOn: $viewModel.manualIntrinsicsEnabled)
                        .labelsHidden()
                }
                Button(action: onImportIntrinsics) {
                    HStack {
                        Text("Import ChArUco JSON…")
                        Spacer()
                    }
                }
                .buttonStyle(.appGhost())
                .disabled(!viewModel.manualIntrinsicsEnabled)
                AppDivider()
                    .padding(.vertical, DesignTokens.Spacing.xs)
                Group {
                    AppFieldRow("fx") {
                        plainField($viewModel.manualFx, placeholder: "1371.5", keyboard: .decimalPad)
                    }
                    AppFieldRow("fy") {
                        plainField($viewModel.manualFy, placeholder: "1378.0", keyboard: .decimalPad)
                    }
                    AppFieldRow("cx") {
                        plainField($viewModel.manualCx, placeholder: "961.9", keyboard: .decimalPad)
                    }
                    AppFieldRow("cy") {
                        plainField($viewModel.manualCy, placeholder: "536.9", keyboard: .decimalPad)
                    }
                    AppFieldRow("distortion") {
                        plainField($viewModel.manualDistortion, placeholder: "k1,k2,p1,p2,k3", keyboard: .numbersAndPunctuation)
                    }
                }
                .disabled(!viewModel.manualIntrinsicsEnabled)
                .opacity(viewModel.manualIntrinsicsEnabled ? 1.0 : 0.35)
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
