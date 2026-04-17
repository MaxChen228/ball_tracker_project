import UIKit

final class SettingsViewController: UIViewController {
    struct Settings {
        var serverIP: String
        var serverPort: Int
        var cameraRole: String          // "A" or "B"

        var hMin: Int
        var hMax: Int
        var sMin: Int
        var sMax: Int
        var vMin: Int
        var vMax: Int

        /// Matched-filter peak threshold for the AudioChirpDetector. Tune
        /// down if the chirp only flashes orange ("close") in HUD; tune up
        /// if it false-triggers on ambient noise. Range roughly 0.05–0.50.
        var chirpThreshold: Double

        /// Base cadence (seconds) for the /status health probe when the
        /// server is reachable. On failure the probe backs off exponentially
        /// up to a hard cap; success resets to this base. Clamped to
        /// [2, 300] on save.
        var pollInterval: Double

        var captureWidth: Int           // 1280 or 1920
        var captureHeight: Int          // 720 or 1080
        var captureFps: Int             // 60, 120, 240

        // Manual intrinsics override (e.g. from a ChArUco calibration run).
        // When enabled, these values are written to the shared fx/fz/cx/cy
        // UserDefaults keys and Calibration view will NOT overwrite them from
        // the AVCapture FOV approximation.
        var manualIntrinsicsEnabled: Bool
        var manualFx: Double
        var manualFy: Double            // stored as intrinsic_fz (Swift naming collision, see CLAUDE.md)
        var manualCx: Double
        var manualCy: Double
        // OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3]. Empty means
        // no distortion is persisted and the payload omits the field.
        var manualDistortion: [Double]
    }

    private static let keyServerIP = "server_ip"
    private static let keyServerPort = "server_port"
    private static let keyCameraRole = "camera_role"

    private static let keyHMin = "h_min"
    private static let keyHMax = "h_max"
    private static let keySMin = "s_min"
    private static let keySMax = "s_max"
    private static let keyVMin = "v_min"
    private static let keyVMax = "v_max"

    private static let keyChirpThreshold = "chirp_threshold"

    private static let keyPollInterval = "poll_interval_s"

    private static let keyCaptureWidth = "capture_width"
    private static let keyCaptureHeight = "capture_height"
    private static let keyCaptureFps = "capture_fps"

    // Manual intrinsics override. If enabled, these values get written to the
    // shared keys (`intrinsic_fx`, `intrinsic_fz`, `intrinsic_cx`, `intrinsic_cy`)
    // that BallDetector / ServerUploader already read.
    private static let keyManualIntrinsicsEnabled = "manual_intrinsics_enabled"
    static let keyIntrinsicsSource = "intrinsics_source"  // "manual" | "fov"
    private static let keyIntrinsicFx = "intrinsic_fx"
    private static let keyIntrinsicFz = "intrinsic_fz"
    private static let keyIntrinsicCx = "intrinsic_cx"
    private static let keyIntrinsicCy = "intrinsic_cy"
    private static let keyIntrinsicDistortion = "intrinsic_distortion"

    private let scrollView = UIScrollView()
    private let contentStack = UIStackView()

    private let serverIPField = UITextField()
    private let serverPortField = UITextField()
    private let cameraRoleControl = UISegmentedControl(items: ["A", "B"])

    private let hMinField = UITextField()
    private let hMaxField = UITextField()
    private let sMinField = UITextField()
    private let sMaxField = UITextField()
    private let vMinField = UITextField()
    private let vMaxField = UITextField()

    private let chirpThresholdField = UITextField()

    private let pollIntervalField = UITextField()

    private let captureResolutionControl = UISegmentedControl(items: ["720p", "1080p"])
    private let captureFpsControl = UISegmentedControl(items: ["60", "120", "240"])

    private let manualIntrinsicsSwitch = UISwitch()
    private let manualFxField = UITextField()
    private let manualFyField = UITextField()
    private let manualCxField = UITextField()
    private let manualCyField = UITextField()
    private let manualDistortionField = UITextField()

    /// Invoked once in `viewDidDisappear`, regardless of whether the user
    /// dismissed via Close, Save, or an interactive swipe. The presenting
    /// camera view uses this to re-diff UserDefaults and reconfigure
    /// anything settings-driven (server URL, capture format, chirp
    /// threshold, poll cadence) without needing viewWillAppear.
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

        let hMin = intOrDefault(keyHMin, defaultValue: 100)
        let hMax = intOrDefault(keyHMax, defaultValue: 130)
        let sMin = intOrDefault(keySMin, defaultValue: 140)
        let sMax = intOrDefault(keySMax, defaultValue: 255)
        let vMin = intOrDefault(keyVMin, defaultValue: 40)
        let vMax = intOrDefault(keyVMax, defaultValue: 255)

        let chirpThreshold = doubleOrDefault(keyChirpThreshold, defaultValue: 0.18)

        let pollInterval = doubleOrDefault(keyPollInterval, defaultValue: 10.0)

        let captureWidth = intOrDefault(keyCaptureWidth, defaultValue: 1920)
        let captureHeight = intOrDefault(keyCaptureHeight, defaultValue: 1080)
        let captureFps = intOrDefault(keyCaptureFps, defaultValue: 240)

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

        return Settings(
            serverIP: serverIP,
            serverPort: serverPort,
            cameraRole: cameraRole,
            hMin: hMin, hMax: hMax,
            sMin: sMin, sMax: sMax,
            vMin: vMin, vMax: vMax,
            chirpThreshold: chirpThreshold,
            pollInterval: pollInterval,
            captureWidth: captureWidth,
            captureHeight: captureHeight,
            captureFps: captureFps,
            manualIntrinsicsEnabled: manualEnabled,
            manualFx: manualFx,
            manualFy: manualFy,
            manualCx: manualCx,
            manualCy: manualCy,
            manualDistortion: manualDistortion
        )
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        title = "Settings"

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "Close",
            style: .plain,
            target: self,
            action: #selector(closeTapped)
        )
        navigationItem.rightBarButtonItem = UIBarButtonItem(
            title: "Save",
            style: .done,
            target: self,
            action: #selector(saveTapped)
        )

        setupUI()
        populateFields(from: Self.loadFromUserDefaults())

        let tap = UITapGestureRecognizer(target: self, action: #selector(dismissKeyboard))
        tap.cancelsTouchesInView = false
        view.addGestureRecognizer(tap)
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        onDismiss?()
    }

    @objc private func closeTapped() {
        dismiss(animated: true)
    }

    @objc private func dismissKeyboard() {
        view.endEditing(true)
    }

    @objc private func saveTapped() {
        let current = Self.loadFromUserDefaults()
        let resolution = captureResolutionControl.selectedSegmentIndex == 1 ? (1920, 1080) : (1280, 720)
        let fpsOptions = [60, 120, 240]
        let fps = fpsOptions[min(max(0, captureFpsControl.selectedSegmentIndex), fpsOptions.count - 1)]

        let settings = Settings(
            serverIP: normalizeServerIP(serverIPField.text, fallback: current.serverIP),
            serverPort: intValue(serverPortField.text, fallback: current.serverPort),
            cameraRole: cameraRoleControl.selectedSegmentIndex == 1 ? "B" : "A",
            hMin: intValue(hMinField.text, fallback: current.hMin),
            hMax: intValue(hMaxField.text, fallback: current.hMax),
            sMin: intValue(sMinField.text, fallback: current.sMin),
            sMax: intValue(sMaxField.text, fallback: current.sMax),
            vMin: intValue(vMinField.text, fallback: current.vMin),
            vMax: intValue(vMaxField.text, fallback: current.vMax),
            chirpThreshold: doubleValue(chirpThresholdField.text, fallback: current.chirpThreshold),
            pollInterval: min(300.0, max(2.0, doubleValue(pollIntervalField.text, fallback: current.pollInterval))),
            captureWidth: resolution.0,
            captureHeight: resolution.1,
            captureFps: fps,
            manualIntrinsicsEnabled: manualIntrinsicsSwitch.isOn,
            manualFx: doubleValue(manualFxField.text, fallback: current.manualFx),
            manualFy: doubleValue(manualFyField.text, fallback: current.manualFy),
            manualCx: doubleValue(manualCxField.text, fallback: current.manualCx),
            manualCy: doubleValue(manualCyField.text, fallback: current.manualCy),
            manualDistortion: parseDistortion(manualDistortionField.text) ?? current.manualDistortion
        )

        Self.saveToUserDefaults(settings)
        dismiss(animated: true)
    }

    private func setupUI() {
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        contentStack.axis = .vertical
        contentStack.spacing = 16
        contentStack.layoutMargins = UIEdgeInsets(top: 20, left: 16, bottom: 24, right: 16)
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
        configureTextField(hMinField, placeholder: "100", keyboard: .numberPad)
        configureTextField(hMaxField, placeholder: "130", keyboard: .numberPad)
        configureTextField(sMinField, placeholder: "140", keyboard: .numberPad)
        configureTextField(sMaxField, placeholder: "255", keyboard: .numberPad)
        configureTextField(vMinField, placeholder: "40", keyboard: .numberPad)
        configureTextField(vMaxField, placeholder: "255", keyboard: .numberPad)
        configureTextField(chirpThresholdField, placeholder: "0.18", keyboard: .decimalPad)
        configureTextField(pollIntervalField, placeholder: "10", keyboard: .decimalPad)
        configureTextField(manualFxField, placeholder: "fx (e.g. 1600)", keyboard: .decimalPad)
        configureTextField(manualFyField, placeholder: "fy (e.g. 1600)", keyboard: .decimalPad)
        configureTextField(manualCxField, placeholder: "cx (e.g. 960)", keyboard: .decimalPad)
        configureTextField(manualCyField, placeholder: "cy (e.g. 540)", keyboard: .decimalPad)
        configureTextField(manualDistortionField, placeholder: "k1,k2,p1,p2,k3", keyboard: .numbersAndPunctuation)

        contentStack.addArrangedSubview(sectionTitle("Server"))
        contentStack.addArrangedSubview(fieldRow(label: "Server IP", field: serverIPField))
        contentStack.addArrangedSubview(fieldRow(label: "Server Port", field: serverPortField))
        contentStack.addArrangedSubview(fieldRow(label: "Poll Interval (s)", field: pollIntervalField))

        contentStack.addArrangedSubview(sectionTitle("Camera"))
        contentStack.addArrangedSubview(controlRow(label: "Camera Role", control: cameraRoleControl))

        contentStack.addArrangedSubview(sectionTitle("HSV"))
        contentStack.addArrangedSubview(fieldRow(label: "H Min", field: hMinField))
        contentStack.addArrangedSubview(fieldRow(label: "H Max", field: hMaxField))
        contentStack.addArrangedSubview(fieldRow(label: "S Min", field: sMinField))
        contentStack.addArrangedSubview(fieldRow(label: "S Max", field: sMaxField))
        contentStack.addArrangedSubview(fieldRow(label: "V Min", field: vMinField))
        contentStack.addArrangedSubview(fieldRow(label: "V Max", field: vMaxField))

        contentStack.addArrangedSubview(sectionTitle("Sync"))
        contentStack.addArrangedSubview(fieldRow(label: "Chirp Threshold", field: chirpThresholdField))

        contentStack.addArrangedSubview(sectionTitle("Capture"))
        contentStack.addArrangedSubview(controlRow(label: "Resolution", control: captureResolutionControl))
        contentStack.addArrangedSubview(controlRow(label: "FPS", control: captureFpsControl))

        contentStack.addArrangedSubview(sectionTitle("Intrinsics (override)"))
        contentStack.addArrangedSubview(controlRow(label: "Use ChArUco values", control: manualIntrinsicsSwitch))
        contentStack.addArrangedSubview(fieldRow(label: "fx", field: manualFxField))
        contentStack.addArrangedSubview(fieldRow(label: "fy", field: manualFyField))
        contentStack.addArrangedSubview(fieldRow(label: "cx", field: manualCxField))
        contentStack.addArrangedSubview(fieldRow(label: "cy", field: manualCyField))
        contentStack.addArrangedSubview(fieldRow(label: "distortion", field: manualDistortionField))
    }

    private func populateFields(from settings: Settings) {
        serverIPField.text = settings.serverIP
        serverPortField.text = String(settings.serverPort)
        cameraRoleControl.selectedSegmentIndex = settings.cameraRole == "B" ? 1 : 0
        hMinField.text = String(settings.hMin)
        hMaxField.text = String(settings.hMax)
        sMinField.text = String(settings.sMin)
        sMaxField.text = String(settings.sMax)
        vMinField.text = String(settings.vMin)
        vMaxField.text = String(settings.vMax)
        chirpThresholdField.text = String(settings.chirpThreshold)
        pollIntervalField.text = String(settings.pollInterval)
        captureResolutionControl.selectedSegmentIndex = settings.captureHeight >= 1080 ? 1 : 0
        captureFpsControl.selectedSegmentIndex = [60, 120, 240].firstIndex(of: settings.captureFps) ?? 2

        manualIntrinsicsSwitch.isOn = settings.manualIntrinsicsEnabled
        manualFxField.text = settings.manualFx > 0 ? String(settings.manualFx) : ""
        manualFyField.text = settings.manualFy > 0 ? String(settings.manualFy) : ""
        manualCxField.text = settings.manualCx > 0 ? String(settings.manualCx) : ""
        manualCyField.text = settings.manualCy > 0 ? String(settings.manualCy) : ""
        manualDistortionField.text = settings.manualDistortion.isEmpty
            ? ""
            : settings.manualDistortion.map { String($0) }.joined(separator: ",")
    }

    private func configureTextField(_ field: UITextField, placeholder: String, keyboard: UIKeyboardType) {
        field.borderStyle = .roundedRect
        field.placeholder = placeholder
        field.keyboardType = keyboard
        field.autocorrectionType = .no
        field.autocapitalizationType = .none
    }

    private func sectionTitle(_ text: String) -> UILabel {
        let label = UILabel()
        label.text = text
        label.font = .systemFont(ofSize: 18, weight: .bold)
        return label
    }

    private func fieldRow(label text: String, field: UITextField) -> UIView {
        let label = UILabel()
        label.text = text
        label.font = .systemFont(ofSize: 15, weight: .medium)
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
        label.setContentHuggingPriority(.required, for: .horizontal)

        let stack = UIStackView(arrangedSubviews: [label, control])
        stack.axis = .horizontal
        stack.spacing = 12
        stack.alignment = .center
        return stack
    }

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

    static func saveToUserDefaults(_ settings: Settings) {
        let d = UserDefaults.standard
        d.set(settings.serverIP, forKey: keyServerIP)
        d.set(settings.serverPort, forKey: keyServerPort)
        d.set(settings.cameraRole, forKey: keyCameraRole)
        d.set(settings.hMin, forKey: keyHMin)
        d.set(settings.hMax, forKey: keyHMax)
        d.set(settings.sMin, forKey: keySMin)
        d.set(settings.sMax, forKey: keySMax)
        d.set(settings.vMin, forKey: keyVMin)
        d.set(settings.vMax, forKey: keyVMax)
        d.set(settings.chirpThreshold, forKey: keyChirpThreshold)
        d.set(settings.pollInterval, forKey: keyPollInterval)
        d.set(settings.captureWidth, forKey: keyCaptureWidth)
        d.set(settings.captureHeight, forKey: keyCaptureHeight)
        d.set(settings.captureFps, forKey: keyCaptureFps)

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
        } else {
            d.set("fov", forKey: keyIntrinsicsSource)
            d.removeObject(forKey: keyIntrinsicDistortion)
        }
    }
}
