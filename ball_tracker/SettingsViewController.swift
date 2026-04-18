import UIKit
import UniformTypeIdentifiers

final class SettingsViewController: UIViewController {
    struct Settings {
        var serverIP: String
        var serverPort: Int
        var cameraRole: String          // "A" or "B"

        /// Matched-filter peak threshold for the AudioChirpDetector. Tune
        /// down if the chirp only flashes orange ("close") in HUD; tune up
        /// if it false-triggers on ambient noise. Range roughly 0.05–0.50.
        var chirpThreshold: Double

        /// Base cadence (seconds) for the `/heartbeat` loop when the
        /// server is reachable. Clamped to [1, 60] on save.
        var pollInterval: Double

        // Resolution is system-wide fixed at 1920×1080; fields are kept so
        // downstream callers (CameraViewController.selectFormat) can stay
        // parameterised, but Settings always writes/loads 1920/1080.
        var captureWidth: Int
        var captureHeight: Int

        /// Park the AVCaptureSession (stopRunning) while the phone is in
        /// `.standby`. Default true — saves power and prevents the sensor
        /// from heating up during long idle waits. Turn off for setup /
        /// framing where the operator wants a continuous live preview and
        /// is willing to trade battery + heat for it.
        var parkCameraInStandby: Bool

        // Manual intrinsics override (e.g. from a ChArUco calibration run).
        // When enabled, these values are written to the shared fx/fz/cx/cy
        // UserDefaults keys and Calibration view will NOT overwrite them.
        var manualIntrinsicsEnabled: Bool
        var manualFx: Double
        var manualFy: Double            // stored as intrinsic_fz
        var manualCx: Double
        var manualCy: Double
        // OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3]. Empty means
        // no distortion is persisted and the payload omits the field.
        var manualDistortion: [Double]

        // Display-only metadata recorded at import/save time so the summary
        // card can show "ChArUco · 1080p · RMS 1.07 px · 3 min ago" without
        // hitting the filesystem. 0 / nil means "unknown" (e.g. manually typed).
        var intrinsicsCalibratedWidth: Int
        var intrinsicsCalibratedHeight: Int
        var intrinsicsRms: Double
        var intrinsicsCalibratedAt: Date?
    }

    /// Decoded from `calibrate_intrinsics.py --out *.json`.
    private struct CharucoIntrinsicsJSON: Decodable {
        let fx: Double
        let fy: Double
        let cx: Double
        let cy: Double
        let image_width: Int
        let image_height: Int
        let rms_reprojection_error_px: Double?
        let distortion_coeffs: [Double]
    }

    private static let keyServerIP = "server_ip"
    private static let keyServerPort = "server_port"
    private static let keyCameraRole = "camera_role"

    private static let keyChirpThreshold = "chirp_threshold"

    private static let keyPollInterval = "poll_interval_s"

    private static let keyCaptureWidth = "capture_width"
    private static let keyCaptureHeight = "capture_height"
    private static let keyParkCameraInStandby = "park_camera_in_standby"

    private static let keyManualIntrinsicsEnabled = "manual_intrinsics_enabled"
    static let keyIntrinsicsSource = "intrinsics_source"  // "manual" | "fov"
    private static let keyIntrinsicFx = "intrinsic_fx"
    private static let keyIntrinsicFz = "intrinsic_fz"
    private static let keyIntrinsicCx = "intrinsic_cx"
    private static let keyIntrinsicCy = "intrinsic_cy"
    private static let keyIntrinsicDistortion = "intrinsic_distortion"

    // New metadata keys (display-only; triangulation doesn't read them).
    private static let keyIntrinsicsCalibratedW = "intrinsic_calibrated_w"
    private static let keyIntrinsicsCalibratedH = "intrinsic_calibrated_h"
    private static let keyIntrinsicsRms = "intrinsic_rms"
    private static let keyIntrinsicsCalibratedAt = "intrinsic_calibrated_at"

    private let scrollView = UIScrollView()
    private let contentStack = UIStackView()

    // Status summary
    private let statusCard = UIView()
    private let statusServerLabel = UILabel()
    private let statusRoleLabel = UILabel()
    private let statusIntrinsicsLabel = UILabel()

    // Fields
    private let serverIPField = UITextField()
    private let serverPortField = UITextField()
    private let cameraRoleControl = UISegmentedControl(items: ["A · 1B 側", "B · 3B 側"])
    private let parkCameraSwitch = UISwitch()

    private let chirpThresholdField = UITextField()
    private let pollIntervalField = UITextField()


    // Capture resolution is hard-wired to 1920×1080 (16:9) across the whole
    // system — see `captureWidthFixed` / `captureHeightFixed`. Added after
    // repeatedly hitting "intrinsics baked at 720p, pipeline running 1080p"
    // drift. If a future pipeline ever needs 720p again, reintroduce a
    // segmented control and the `resolutionChanged` logic the prior revision
    // had (git blame this file for the full prompt-driven flow).
    private static let captureWidthFixed = 1920
    private static let captureHeightFixed = 1080

    private let manualIntrinsicsSwitch = UISwitch()
    private let importIntrinsicsButton = UIButton(type: .system)
    private let manualFxField = UITextField()
    private let manualFyField = UITextField()
    private let manualCxField = UITextField()
    private let manualCyField = UITextField()
    private let manualDistortionField = UITextField()

    // Pending import metadata; written to UserDefaults only on Save.
    private var pendingImportMeta: (w: Int, h: Int, rms: Double)?

    var onDismiss: (() -> Void)?

    static func loadFromUserDefaults() -> Settings {
        let d = UserDefaults.standard

        func intOrDefault(_ key: String, defaultValue: Int) -> Int {
            if d.object(forKey: key) == nil { return defaultValue }
            return d.integer(forKey: key)
        }

        func doubleOrDefault(_ key: String, defaultValue: Double) -> Double {
            if d.object(forKey: key) == nil { return defaultValue }
            return d.double(forKey: key)
        }

        let serverIP = d.string(forKey: keyServerIP) ?? "192.168.1.100"
        let serverPort = intOrDefault(keyServerPort, defaultValue: 8765)
        let cameraRole = d.string(forKey: keyCameraRole) ?? "A"

        let chirpThreshold = doubleOrDefault(keyChirpThreshold, defaultValue: 0.18)
        let pollInterval = doubleOrDefault(keyPollInterval, defaultValue: 1.0)
        // Default true (park) — matches the energy-saving behaviour that
        // shipped before this toggle existed. Legacy installs without the
        // key land on the same behaviour they had yesterday.
        let parkCameraInStandby = d.object(forKey: keyParkCameraInStandby) as? Bool ?? true

        // Capture resolution is system-wide fixed at 1920×1080 (see
        // `captureWidthFixed` / `captureHeightFixed`). Any stale UserDefaults
        // values from prior 720p runs are ignored on load. Capture FPS is
        // adaptive (60 idle / 240 tracking) and owned by CameraViewController —
        // it is no longer a user-visible setting.
        let captureWidth = captureWidthFixed
        let captureHeight = captureHeightFixed

        let manualEnabled = d.bool(forKey: keyManualIntrinsicsEnabled)
        let manualFx = manualEnabled ? doubleOrDefault(keyIntrinsicFx, defaultValue: 0) : 0
        let manualFy = manualEnabled ? doubleOrDefault(keyIntrinsicFz, defaultValue: 0) : 0
        let manualCx = manualEnabled ? doubleOrDefault(keyIntrinsicCx, defaultValue: 0) : 0
        let manualCy = manualEnabled ? doubleOrDefault(keyIntrinsicCy, defaultValue: 0) : 0
        let manualDistortion: [Double]
        if manualEnabled, let arr = d.array(forKey: keyIntrinsicDistortion) as? [Double], arr.count == 5 {
            manualDistortion = arr
        } else {
            manualDistortion = []
        }

        let calW = intOrDefault(keyIntrinsicsCalibratedW, defaultValue: 0)
        let calH = intOrDefault(keyIntrinsicsCalibratedH, defaultValue: 0)
        let rms = doubleOrDefault(keyIntrinsicsRms, defaultValue: 0)
        let calAtTs = doubleOrDefault(keyIntrinsicsCalibratedAt, defaultValue: 0)
        let calAt: Date? = calAtTs > 0 ? Date(timeIntervalSince1970: calAtTs) : nil

        return Settings(
            serverIP: serverIP,
            serverPort: serverPort,
            cameraRole: cameraRole,
            chirpThreshold: chirpThreshold,
            pollInterval: pollInterval,
            captureWidth: captureWidth,
            captureHeight: captureHeight,
            parkCameraInStandby: parkCameraInStandby,
            manualIntrinsicsEnabled: manualEnabled,
            manualFx: manualFx,
            manualFy: manualFy,
            manualCx: manualCx,
            manualCy: manualCy,
            manualDistortion: manualDistortion,
            intrinsicsCalibratedWidth: calW,
            intrinsicsCalibratedHeight: calH,
            intrinsicsRms: rms,
            intrinsicsCalibratedAt: calAt
        )
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemGroupedBackground
        title = "Settings"

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "Cancel",
            style: .plain,
            target: self,
            action: #selector(cancelTapped)
        )
        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "Save",
            style: .done,
            target: self,
            action: #selector(saveTapped)
        )
        // Block interactive swipe-dismiss so changes can't silently drop.
        isModalInPresentation = true

        setupUI()
        let current = Self.loadFromUserDefaults()
        populateFields(from: current)
        updateStatusSummary(from: current)
        updateManualFieldsEnabled(animated: false)

        let tap = UITapGestureRecognizer(target: self, action: #selector(dismissKeyboard))
        tap.cancelsTouchesInView = false
        view.addGestureRecognizer(tap)
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        onDismiss?()
    }

    @objc private func cancelTapped() {
        dismiss(animated: true)
    }

    @objc private func dismissKeyboard() {
        view.endEditing(true)
    }

    @objc private func saveTapped() {
        dismissKeyboard()
        if let problem = validate() {
            let alert = UIAlertController(title: "無法儲存", message: problem, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
            return
        }

        let current = Self.loadFromUserDefaults()

        // Resolve intrinsics metadata: import overrides, else keep existing,
        // else default to current resolution (manual typing at this resolution).
        let meta: (w: Int, h: Int, rms: Double, at: Date?)
        if let p = pendingImportMeta {
            meta = (p.w, p.h, p.rms, Date())
        } else if manualIntrinsicsSwitch.isOn {
            if current.intrinsicsCalibratedWidth > 0 {
                meta = (current.intrinsicsCalibratedWidth,
                        current.intrinsicsCalibratedHeight,
                        current.intrinsicsRms,
                        current.intrinsicsCalibratedAt)
            } else {
                meta = (Self.captureWidthFixed, Self.captureHeightFixed, 0, Date())
            }
        } else {
            meta = (0, 0, 0, nil)
        }

        let settings = Settings(
            serverIP: normalizeServerIP(serverIPField.text, fallback: current.serverIP),
            serverPort: intValue(serverPortField.text, fallback: current.serverPort),
            cameraRole: cameraRoleControl.selectedSegmentIndex == 1 ? "B" : "A",
            chirpThreshold: doubleValue(chirpThresholdField.text, fallback: current.chirpThreshold),
            pollInterval: min(60.0, max(1.0, doubleValue(pollIntervalField.text, fallback: current.pollInterval))),
            captureWidth: Self.captureWidthFixed,
            captureHeight: Self.captureHeightFixed,
            parkCameraInStandby: parkCameraSwitch.isOn,
            manualIntrinsicsEnabled: manualIntrinsicsSwitch.isOn,
            manualFx: doubleValue(manualFxField.text, fallback: current.manualFx),
            manualFy: doubleValue(manualFyField.text, fallback: current.manualFy),
            manualCx: doubleValue(manualCxField.text, fallback: current.manualCx),
            manualCy: doubleValue(manualCyField.text, fallback: current.manualCy),
            manualDistortion: parseDistortion(manualDistortionField.text) ?? current.manualDistortion,
            intrinsicsCalibratedWidth: meta.w,
            intrinsicsCalibratedHeight: meta.h,
            intrinsicsRms: meta.rms,
            intrinsicsCalibratedAt: meta.at
        )

        Self.saveToUserDefaults(settings)
        dismiss(animated: true)
    }

    // MARK: - Validation

    private func validate() -> String? {
        let port = intValue(serverPortField.text, fallback: -1)
        if port < 1 || port > 65535 { return "Server port 需介於 1–65535" }

        let poll = doubleValue(pollIntervalField.text, fallback: 1.0)
        if poll < 1 || poll > 60 { return "Heartbeat interval 需介於 1–60 秒" }

        let ct = doubleValue(chirpThresholdField.text, fallback: 0)
        if ct <= 0 || ct > 1 { return "Chirp threshold 需介於 0–1（典型 0.15–0.35）" }

        if manualIntrinsicsSwitch.isOn {
            let fx = doubleValue(manualFxField.text, fallback: -1)
            let fy = doubleValue(manualFyField.text, fallback: -1)
            let cx = doubleValue(manualCxField.text, fallback: -1)
            let cy = doubleValue(manualCyField.text, fallback: -1)
            if fx < 100 { return "fx 不合理（應 > 100 px）" }
            if fy < 100 { return "fy 不合理（應 > 100 px）" }
            if cx <= 0 { return "cx 不合理（需 > 0）" }
            if cy <= 0 { return "cy 不合理（需 > 0）" }

            if let raw = manualDistortionField.text?.trimmingCharacters(in: .whitespacesAndNewlines),
               !raw.isEmpty {
                let parts = raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
                if parts.count != 5 { return "distortion 需 5 個以逗號分隔的數字（k1,k2,p1,p2,k3）" }
                if parts.contains(where: { Double($0) == nil }) {
                    return "distortion 含非數字元素"
                }
            }
        }
        return nil
    }

    // MARK: - UI

    private func setupUI() {
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        contentStack.axis = .vertical
        contentStack.spacing = 20
        contentStack.layoutMargins = UIEdgeInsets(top: 16, left: 16, bottom: 32, right: 16)
        contentStack.isLayoutMarginsRelativeArrangement = true

        view.addSubview(scrollView)
        scrollView.addSubview(contentStack)

        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),

            contentStack.topAnchor.constraint(equalTo: scrollView.contentLayoutGuide.topAnchor),
            contentStack.leadingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.leadingAnchor),
            contentStack.trailingAnchor.constraint(equalTo: scrollView.contentLayoutGuide.trailingAnchor),
            contentStack.bottomAnchor.constraint(equalTo: scrollView.contentLayoutGuide.bottomAnchor),
            contentStack.widthAnchor.constraint(equalTo: scrollView.frameLayoutGuide.widthAnchor),
        ])

        configureTextField(serverIPField, placeholder: "192.168.1.100", keyboard: .numbersAndPunctuation)
        configureTextField(serverPortField, placeholder: "8765", keyboard: .numberPad)
        configureTextField(chirpThresholdField, placeholder: "0.18", keyboard: .decimalPad)
        configureTextField(pollIntervalField, placeholder: "1", keyboard: .decimalPad)
        configureTextField(manualFxField, placeholder: "e.g. 1371.5", keyboard: .decimalPad)
        configureTextField(manualFyField, placeholder: "e.g. 1378.0", keyboard: .decimalPad)
        configureTextField(manualCxField, placeholder: "e.g. 961.9", keyboard: .decimalPad)
        configureTextField(manualCyField, placeholder: "e.g. 536.9", keyboard: .decimalPad)
        configureTextField(manualDistortionField, placeholder: "k1,k2,p1,p2,k3", keyboard: .numbersAndPunctuation)

        manualIntrinsicsSwitch.addTarget(self, action: #selector(manualToggleChanged), for: .valueChanged)

        importIntrinsicsButton.setTitle("Import ChArUco JSON…", for: .normal)
        importIntrinsicsButton.titleLabel?.font = .systemFont(ofSize: 15, weight: .medium)
        importIntrinsicsButton.contentHorizontalAlignment = .leading
        importIntrinsicsButton.addTarget(self, action: #selector(importIntrinsicsTapped), for: .touchUpInside)

        // Status summary card
        buildStatusCard()
        contentStack.addArrangedSubview(statusCard)

        contentStack.addArrangedSubview(sectionBlock(
            title: "Server",
            rows: [
                fieldRow(label: "IP", field: serverIPField),
                fieldRow(label: "Port", field: serverPortField),
                fieldRow(label: "Heartbeat (s)", field: pollIntervalField),
            ],
            footer: "IP 支援貼整串 URL（如 http://x.x.x.x:8765/status），會自動抽出主機。"
        ))

        contentStack.addArrangedSubview(sectionBlock(
            title: "Camera",
            rows: [
                controlRow(label: "Role", control: cameraRoleControl),
                controlRow(label: "Park in STANDBY", control: parkCameraSwitch),
            ],
            footer: "解析度系統固定 1080p。FPS 自動切換：待機 60、錄影 240（曝光上限鎖定，暗室會噪聲化但不掉幀）。\nPark 開啟（預設）時 STANDBY 會停住 capture session 避免過熱；關閉則保留即時預覽，方便設置取景但會持續耗電發熱。"
        ))

        contentStack.addArrangedSubview(sectionBlock(
            title: "Audio Sync",
            rows: [
                fieldRow(label: "Chirp Threshold", field: chirpThresholdField),
            ],
            footer: "HUD 閃橘色（接近）但不觸發 → 降低；環境噪音誤觸 → 提高。預設 0.18。"
        ))

        contentStack.addArrangedSubview(sectionBlock(
            title: "Camera Intrinsics",
            rows: [
                controlRow(label: "Use ChArUco values", control: manualIntrinsicsSwitch),
                singleView(importIntrinsicsButton),
                fieldRow(label: "fx", field: manualFxField),
                fieldRow(label: "fy", field: manualFyField),
                fieldRow(label: "cx", field: manualCxField),
                fieldRow(label: "cy", field: manualCyField),
                fieldRow(label: "distortion", field: manualDistortionField),
            ],
            footer: "匯入 calibrate_intrinsics.py 輸出的 JSON，自動縮放到 1080p。關閉開關時改用 FOV 近似。"
        ))
    }

    private func buildStatusCard() {
        statusCard.backgroundColor = .secondarySystemGroupedBackground
        statusCard.layer.cornerRadius = 12
        statusCard.translatesAutoresizingMaskIntoConstraints = false

        let title = UILabel()
        title.text = "Status"
        title.font = .systemFont(ofSize: 13, weight: .semibold)
        title.textColor = .secondaryLabel

        statusServerLabel.font = .monospacedSystemFont(ofSize: 14, weight: .regular)
        statusRoleLabel.font = .systemFont(ofSize: 14)
        statusIntrinsicsLabel.font = .systemFont(ofSize: 14)

        let rows = UIStackView(arrangedSubviews: [
            statusRow(key: "Server", value: statusServerLabel),
            statusRow(key: "Role", value: statusRoleLabel),
            statusRow(key: "Intrinsics", value: statusIntrinsicsLabel),
        ])
        rows.axis = .vertical
        rows.spacing = 6

        let container = UIStackView(arrangedSubviews: [title, rows])
        container.axis = .vertical
        container.spacing = 8
        container.translatesAutoresizingMaskIntoConstraints = false
        statusCard.addSubview(container)
        NSLayoutConstraint.activate([
            container.topAnchor.constraint(equalTo: statusCard.topAnchor, constant: 12),
            container.leadingAnchor.constraint(equalTo: statusCard.leadingAnchor, constant: 14),
            container.trailingAnchor.constraint(equalTo: statusCard.trailingAnchor, constant: -14),
            container.bottomAnchor.constraint(equalTo: statusCard.bottomAnchor, constant: -12),
        ])
    }

    private func statusRow(key: String, value: UILabel) -> UIStackView {
        let k = UILabel()
        k.text = key
        k.font = .systemFont(ofSize: 13, weight: .medium)
        k.textColor = .secondaryLabel
        k.widthAnchor.constraint(equalToConstant: 88).isActive = true
        let stack = UIStackView(arrangedSubviews: [k, value])
        stack.axis = .horizontal
        stack.spacing = 8
        return stack
    }

    private func populateFields(from settings: Settings) {
        serverIPField.text = settings.serverIP
        serverPortField.text = String(settings.serverPort)
        cameraRoleControl.selectedSegmentIndex = settings.cameraRole == "B" ? 1 : 0
        parkCameraSwitch.isOn = settings.parkCameraInStandby
        chirpThresholdField.text = String(settings.chirpThreshold)
        pollIntervalField.text = String(settings.pollInterval)

        manualIntrinsicsSwitch.isOn = settings.manualIntrinsicsEnabled
        manualFxField.text = settings.manualFx > 0 ? String(settings.manualFx) : ""
        manualFyField.text = settings.manualFy > 0 ? String(settings.manualFy) : ""
        manualCxField.text = settings.manualCx > 0 ? String(settings.manualCx) : ""
        manualCyField.text = settings.manualCy > 0 ? String(settings.manualCy) : ""
        manualDistortionField.text = settings.manualDistortion.isEmpty
            ? ""
            : settings.manualDistortion.map { String($0) }.joined(separator: ",")
    }

    // MARK: - Status summary

    private func updateStatusSummary(from s: Settings) {
        statusServerLabel.text = "\(s.serverIP):\(s.serverPort)"
        statusRoleLabel.text = s.cameraRole == "A" ? "A · 1B 側" : "B · 3B 側"
        statusIntrinsicsLabel.text = intrinsicsSummary(s)
    }

    private func intrinsicsSummary(_ s: Settings) -> String {
        guard s.manualIntrinsicsEnabled && s.manualFx > 0 else {
            return "FOV approximation（未校正）"
        }
        var parts = ["Manual"]
        if s.intrinsicsCalibratedHeight > 0 {
            parts.append("\(s.intrinsicsCalibratedHeight)p")
        }
        if s.intrinsicsRms > 0 {
            parts.append(String(format: "RMS %.2f px", s.intrinsicsRms))
        }
        if let at = s.intrinsicsCalibratedAt {
            let fmt = RelativeDateTimeFormatter()
            fmt.unitsStyle = .abbreviated
            parts.append(fmt.localizedString(for: at, relativeTo: Date()))
        }
        return parts.joined(separator: " · ")
    }

    // MARK: - Live interactions

    @objc private func manualToggleChanged() {
        updateManualFieldsEnabled(animated: true)
        updateLiveStatusPreview()
    }

    @objc private func importIntrinsicsTapped() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.json])
        picker.delegate = self
        picker.allowsMultipleSelection = false
        picker.shouldShowFileExtensions = true
        present(picker, animated: true)
    }

    private func updateManualFieldsEnabled(animated: Bool) {
        let on = manualIntrinsicsSwitch.isOn
        let alpha: CGFloat = on ? 1.0 : 0.35
        let fields: [UITextField] = [manualFxField, manualFyField, manualCxField, manualCyField, manualDistortionField]
        let change = {
            fields.forEach { $0.isEnabled = on; $0.alpha = alpha }
            self.importIntrinsicsButton.isEnabled = on
            self.importIntrinsicsButton.alpha = alpha
        }
        if animated { UIView.animate(withDuration: 0.2, animations: change) } else { change() }
    }

    /// Rebuilds the Intrinsics status line from live field values (no persistence).
    /// Called after toggle / import / resolution change so the card stays truthful.
    private func updateLiveStatusPreview() {
        let fx = Double(manualFxField.text ?? "") ?? 0
        guard manualIntrinsicsSwitch.isOn, fx > 0 else {
            statusIntrinsicsLabel.text = "FOV approximation（未校正）"
            return
        }
        var parts = [pendingImportMeta != nil ? "ChArUco" : "Manual"]
        parts.append("\(Self.captureHeightFixed)p")
        if let rms = pendingImportMeta?.rms, rms > 0 {
            parts.append(String(format: "RMS %.2f px", rms))
        }
        statusIntrinsicsLabel.text = parts.joined(separator: " · ")
    }

    // MARK: - Intrinsics math

    private func clearManualIntrinsics() {
        manualFxField.text = ""
        manualFyField.text = ""
        manualCxField.text = ""
        manualCyField.text = ""
        manualDistortionField.text = ""
    }

    /// Converts intrinsics from (fromW×fromH) to (toW×toH). Handles pure scale when
    /// aspect ratios match, and a 4:3 → 16:9 center-crop-then-scale (the ChArUco
    /// calibration JSON is typically 4032×3024 while the live pipeline is 16:9).
    /// Returns nil if the aspect transform is unsupported (e.g. 16:9 → 4:3).
    private func scaleIntrinsics(
        fx: Double, fy: Double, cx: Double, cy: Double,
        fromW: Int, fromH: Int, toW: Int, toH: Int
    ) -> (fx: Double, fy: Double, cx: Double, cy: Double)? {
        let fromAspect = Double(fromW) / Double(fromH)
        let toAspect = Double(toW) / Double(toH)
        let eps = 0.01

        let effW = fromW
        var effH = fromH
        let adjCx = cx
        var adjCy = cy

        if abs(fromAspect - toAspect) > eps {
            if fromAspect < toAspect {
                // Source is taller (e.g. 4:3). Crop top/bottom to match target aspect.
                let newH = Int((Double(fromW) / toAspect).rounded())
                let crop = (fromH - newH) / 2
                effH = newH
                adjCy = cy - Double(crop)
            } else {
                // Source is wider than target (rare for this app). Not supported.
                return nil
            }
        }

        let sx = Double(toW) / Double(effW)
        let sy = Double(toH) / Double(effH)
        return (fx * sx, fy * sy, adjCx * sx, adjCy * sy)
    }

    // MARK: - UI helpers

    private func configureTextField(_ field: UITextField, placeholder: String, keyboard: UIKeyboardType) {
        field.borderStyle = .roundedRect
        field.placeholder = placeholder
        field.keyboardType = keyboard
        field.autocorrectionType = .no
        field.autocapitalizationType = .none
    }

    private func sectionBlock(title: String, rows: [UIView], footer: String?) -> UIView {
        let container = UIView()
        container.translatesAutoresizingMaskIntoConstraints = false

        let card = UIView()
        card.backgroundColor = .secondarySystemGroupedBackground
        card.layer.cornerRadius = 12
        card.translatesAutoresizingMaskIntoConstraints = false

        let titleLabel = UILabel()
        titleLabel.text = title
        titleLabel.font = .systemFont(ofSize: 13, weight: .semibold)
        titleLabel.textColor = .secondaryLabel
        titleLabel.translatesAutoresizingMaskIntoConstraints = false

        let rowStack = UIStackView(arrangedSubviews: rows)
        rowStack.axis = .vertical
        rowStack.spacing = 10
        rowStack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(rowStack)
        NSLayoutConstraint.activate([
            rowStack.topAnchor.constraint(equalTo: card.topAnchor, constant: 12),
            rowStack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 14),
            rowStack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -14),
            rowStack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -12),
        ])

        container.addSubview(titleLabel)
        container.addSubview(card)

        var constraints: [NSLayoutConstraint] = [
            titleLabel.topAnchor.constraint(equalTo: container.topAnchor),
            titleLabel.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 4),
            titleLabel.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -4),
            card.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 6),
            card.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            card.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ]

        if let footer {
            let footerLabel = UILabel()
            footerLabel.text = footer
            footerLabel.font = .systemFont(ofSize: 12)
            footerLabel.textColor = .secondaryLabel
            footerLabel.numberOfLines = 0
            footerLabel.translatesAutoresizingMaskIntoConstraints = false
            container.addSubview(footerLabel)
            constraints += [
                footerLabel.topAnchor.constraint(equalTo: card.bottomAnchor, constant: 6),
                footerLabel.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 4),
                footerLabel.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -4),
                footerLabel.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            ]
        } else {
            constraints.append(card.bottomAnchor.constraint(equalTo: container.bottomAnchor))
        }

        NSLayoutConstraint.activate(constraints)
        return container
    }

    private func fieldRow(label text: String, field: UITextField) -> UIView {
        let label = UILabel()
        label.text = text
        label.font = .systemFont(ofSize: 15, weight: .medium)
        label.widthAnchor.constraint(equalToConstant: 110).isActive = true
        label.setContentHuggingPriority(.required, for: .horizontal)
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        let stack = UIStackView(arrangedSubviews: [label, field])
        stack.axis = .horizontal
        stack.spacing = 12
        stack.alignment = .center
        return stack
    }

    private func controlRow(label text: String, control: UIControl) -> UIView {
        let label = UILabel()
        label.text = text
        label.font = .systemFont(ofSize: 15, weight: .medium)
        label.widthAnchor.constraint(equalToConstant: 110).isActive = true
        label.setContentHuggingPriority(.required, for: .horizontal)

        let stack = UIStackView(arrangedSubviews: [label, control])
        stack.axis = .horizontal
        stack.spacing = 12
        stack.alignment = .center
        return stack
    }

    private func singleView(_ view: UIView) -> UIView {
        let wrap = UIStackView(arrangedSubviews: [view])
        wrap.axis = .horizontal
        return wrap
    }

    // MARK: - Parsing

    private func intValue(_ text: String?, fallback: Int) -> Int {
        guard let text, let value = Int(text) else { return fallback }
        return value
    }

    private func doubleValue(_ text: String?, fallback: Double) -> Double {
        guard let text, let value = Double(text) else { return fallback }
        return value
    }

    /// Parses a comma-separated 5-tuple of doubles. Returns nil for empty or
    /// malformed input; callers fall back to the existing persisted value.
    private func parseDistortion(_ text: String?) -> [Double]? {
        guard let raw = text?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return []  // explicit empty → clear
        }
        let parts = raw.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
        guard parts.count == 5 else { return nil }
        let values = parts.compactMap { Double($0) }
        return values.count == 5 ? values : nil
    }

    /// Accept pasted URLs like `http://10.2.248.10:8765/status` and keep only the host.
    private func normalizeServerIP(_ text: String?, fallback: String) -> String {
        guard var s = text?.trimmingCharacters(in: .whitespacesAndNewlines), !s.isEmpty else {
            return fallback
        }
        if let schemeRange = s.range(of: "://") {
            s = String(s[schemeRange.upperBound...])
        }
        if let slash = s.firstIndex(of: "/") {
            s = String(s[..<slash])
        }
        if let colon = s.firstIndex(of: ":") {
            s = String(s[..<colon])
        }
        s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? fallback : s
    }

    // MARK: - Persistence

    static func saveToUserDefaults(_ settings: Settings) {
        let d = UserDefaults.standard
        d.set(settings.serverIP, forKey: keyServerIP)
        d.set(settings.serverPort, forKey: keyServerPort)
        d.set(settings.cameraRole, forKey: keyCameraRole)
        d.set(settings.chirpThreshold, forKey: keyChirpThreshold)
        d.set(settings.pollInterval, forKey: keyPollInterval)
        d.set(settings.captureWidth, forKey: keyCaptureWidth)
        d.set(settings.captureHeight, forKey: keyCaptureHeight)
        d.set(settings.parkCameraInStandby, forKey: keyParkCameraInStandby)

        d.set(settings.manualIntrinsicsEnabled, forKey: keyManualIntrinsicsEnabled)
        if settings.manualIntrinsicsEnabled
            && settings.manualFx > 0 && settings.manualFy > 0
            && settings.manualCx > 0 && settings.manualCy > 0 {
            d.set(settings.manualFx, forKey: keyIntrinsicFx)
            d.set(settings.manualFy, forKey: keyIntrinsicFz)  // fz ≡ fy (CLAUDE.md)
            d.set(settings.manualCx, forKey: keyIntrinsicCx)
            d.set(settings.manualCy, forKey: keyIntrinsicCy)
            d.set("manual", forKey: keyIntrinsicsSource)
            if settings.manualDistortion.count == 5 {
                d.set(settings.manualDistortion, forKey: keyIntrinsicDistortion)
            } else {
                d.removeObject(forKey: keyIntrinsicDistortion)
            }
            d.set(settings.intrinsicsCalibratedWidth, forKey: keyIntrinsicsCalibratedW)
            d.set(settings.intrinsicsCalibratedHeight, forKey: keyIntrinsicsCalibratedH)
            d.set(settings.intrinsicsRms, forKey: keyIntrinsicsRms)
            d.set(settings.intrinsicsCalibratedAt?.timeIntervalSince1970 ?? 0, forKey: keyIntrinsicsCalibratedAt)
        } else {
            d.set("fov", forKey: keyIntrinsicsSource)
            d.removeObject(forKey: keyIntrinsicDistortion)
            d.removeObject(forKey: keyIntrinsicsCalibratedW)
            d.removeObject(forKey: keyIntrinsicsCalibratedH)
            d.removeObject(forKey: keyIntrinsicsRms)
            d.removeObject(forKey: keyIntrinsicsCalibratedAt)
        }
    }
}

// MARK: - Document picker (ChArUco JSON import)

extension SettingsViewController: UIDocumentPickerDelegate {
    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        guard let url = urls.first else { return }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }

        do {
            let data = try Data(contentsOf: url)
            let payload = try JSONDecoder().decode(CharucoIntrinsicsJSON.self, from: data)
            applyImportedIntrinsics(payload)
        } catch {
            let alert = UIAlertController(
                title: "匯入失敗",
                message: "無法解析 JSON：\(error.localizedDescription)",
                preferredStyle: .alert
            )
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
        }
    }

    private func applyImportedIntrinsics(_ p: CharucoIntrinsicsJSON) {
        let targetW = Self.captureWidthFixed
        let targetH = Self.captureHeightFixed

        guard let baked = scaleIntrinsics(
            fx: p.fx, fy: p.fy, cx: p.cx, cy: p.cy,
            fromW: p.image_width, fromH: p.image_height,
            toW: targetW, toH: targetH
        ) else {
            let alert = UIAlertController(
                title: "不支援的 aspect",
                message: "JSON 為 \(p.image_width)×\(p.image_height)，無法轉換到 \(targetW)×\(targetH)。",
                preferredStyle: .alert
            )
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
            return
        }

        manualIntrinsicsSwitch.setOn(true, animated: true)
        updateManualFieldsEnabled(animated: true)

        manualFxField.text = String(format: "%.2f", baked.fx)
        manualFyField.text = String(format: "%.2f", baked.fy)
        manualCxField.text = String(format: "%.2f", baked.cx)
        manualCyField.text = String(format: "%.2f", baked.cy)
        if p.distortion_coeffs.count == 5 {
            manualDistortionField.text = p.distortion_coeffs
                .map { String(format: "%.6f", $0) }
                .joined(separator: ",")
        }

        pendingImportMeta = (targetW, targetH, p.rms_reprojection_error_px ?? 0)
        updateLiveStatusPreview()
    }
}
