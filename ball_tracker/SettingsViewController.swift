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

        var flashThresholdMultiplier: Double

        var captureWidth: Int           // 1280 or 1920
        var captureHeight: Int          // 720 or 1080
        var captureFps: Int             // 60, 120, 240
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

    private static let keyFlashMultiplier = "flash_threshold_multiplier"

    private static let keyCaptureWidth = "capture_width"
    private static let keyCaptureHeight = "capture_height"
    private static let keyCaptureFps = "capture_fps"

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

    private let flashMultiplierField = UITextField()

    private let captureResolutionControl = UISegmentedControl(items: ["720p", "1080p"])
    private let captureFpsControl = UISegmentedControl(items: ["60", "120", "240"])

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

        let flashThresholdMultiplier = doubleOrDefault(keyFlashMultiplier, defaultValue: 2.5)

        let captureWidth = intOrDefault(keyCaptureWidth, defaultValue: 1920)
        let captureHeight = intOrDefault(keyCaptureHeight, defaultValue: 1080)
        let captureFps = intOrDefault(keyCaptureFps, defaultValue: 240)

        return Settings(
            serverIP: serverIP,
            serverPort: serverPort,
            cameraRole: cameraRole,
            hMin: hMin, hMax: hMax,
            sMin: sMin, sMax: sMax,
            vMin: vMin, vMax: vMax,
            flashThresholdMultiplier: flashThresholdMultiplier,
            captureWidth: captureWidth,
            captureHeight: captureHeight,
            captureFps: captureFps,
            ballDetectionEnabled: ballDetectionEnabled
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
            flashThresholdMultiplier: doubleValue(flashMultiplierField.text, fallback: current.flashThresholdMultiplier),
            captureWidth: resolution.0,
            captureHeight: resolution.1,
            captureFps: fps,
            ballDetectionEnabled: ballDetectionSwitch.isOn
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
        configureTextField(flashMultiplierField, placeholder: "2.5", keyboard: .decimalPad)

        contentStack.addArrangedSubview(sectionTitle("Server"))
        contentStack.addArrangedSubview(fieldRow(label: "Server IP", field: serverIPField))
        contentStack.addArrangedSubview(fieldRow(label: "Server Port", field: serverPortField))

        contentStack.addArrangedSubview(sectionTitle("Camera"))
        contentStack.addArrangedSubview(controlRow(label: "Camera Role", control: cameraRoleControl))

        contentStack.addArrangedSubview(sectionTitle("HSV"))
        contentStack.addArrangedSubview(fieldRow(label: "H Min", field: hMinField))
        contentStack.addArrangedSubview(fieldRow(label: "H Max", field: hMaxField))
        contentStack.addArrangedSubview(fieldRow(label: "S Min", field: sMinField))
        contentStack.addArrangedSubview(fieldRow(label: "S Max", field: sMaxField))
        contentStack.addArrangedSubview(fieldRow(label: "V Min", field: vMinField))
        contentStack.addArrangedSubview(fieldRow(label: "V Max", field: vMaxField))

        contentStack.addArrangedSubview(sectionTitle("Timing"))
        contentStack.addArrangedSubview(fieldRow(label: "Flash Threshold", field: flashMultiplierField))

        contentStack.addArrangedSubview(sectionTitle("Capture"))
        contentStack.addArrangedSubview(controlRow(label: "Resolution", control: captureResolutionControl))
        contentStack.addArrangedSubview(controlRow(label: "FPS", control: captureFpsControl))

        contentStack.addArrangedSubview(sectionTitle("Detection"))
        contentStack.addArrangedSubview(controlRow(label: "Ball Detection", control: ballDetectionSwitch))
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
        flashMultiplierField.text = String(settings.flashThresholdMultiplier)
        captureResolutionControl.selectedSegmentIndex = settings.captureHeight >= 1080 ? 1 : 0
        captureFpsControl.selectedSegmentIndex = [60, 120, 240].firstIndex(of: settings.captureFps) ?? 2
        ballDetectionSwitch.isOn = settings.ballDetectionEnabled
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

    private func nonEmpty(_ text: String?, fallback: String) -> String {
        guard let text, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return fallback }
        return text
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
        d.set(settings.flashThresholdMultiplier, forKey: keyFlashMultiplier)
        d.set(settings.captureWidth, forKey: keyCaptureWidth)
        d.set(settings.captureHeight, forKey: keyCaptureHeight)
        d.set(settings.captureFps, forKey: keyCaptureFps)
        d.set(settings.ballDetectionEnabled, forKey: keyBallDetectionEnabled)
    }
}

