import UIKit
import SwiftUI
import UniformTypeIdentifiers

/// Thin UIKit container for the settings screen — owns the navigation bar
/// (Save / Cancel), the document picker for importing ChArUco JSON, and the
/// push to the diagnostics subpage. The form content itself is SwiftUI
/// (`SettingsView` + `SettingsViewModel`).
final class SettingsViewController: UIViewController {

    // MARK: - Data model

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

        var captureWidth: Int
        var captureHeight: Int

        /// Park the AVCaptureSession (stopRunning) while the phone is in
        /// `.standby`. Default true — saves power and prevents the sensor
        /// from heating up during long idle waits. Turn off for setup /
        /// framing where the operator wants a continuous live preview.
        var parkCameraInStandby: Bool

        // Manual intrinsics override (e.g. from a ChArUco calibration run).
        var manualIntrinsicsEnabled: Bool
        var manualFx: Double
        var manualFy: Double            // stored as intrinsic_fz
        var manualCx: Double
        var manualCy: Double
        // OpenCV 5-coefficient distortion [k1, k2, p1, p2, k3].
        var manualDistortion: [Double]

        // Display-only metadata recorded at import/save time.
        var intrinsicsCalibratedWidth: Int
        var intrinsicsCalibratedHeight: Int
        var intrinsicsRms: Double
        var intrinsicsCalibratedAt: Date?
    }

    // MARK: - UserDefaults keys

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

    private static let keyIntrinsicsCalibratedW = "intrinsic_calibrated_w"
    private static let keyIntrinsicsCalibratedH = "intrinsic_calibrated_h"
    private static let keyIntrinsicsRms = "intrinsic_rms"
    private static let keyIntrinsicsCalibratedAt = "intrinsic_calibrated_at"

    // Calibration always bakes intrinsics at 1080p — every downstream math
    // (HSV detection, intrinsics scaling, ChArUco import target) treats
    // these as the reference resolution. The capture format itself can
    // drop to 720p / 540p via the Settings picker, and the server rescales
    // intrinsics + homography per-pitch through
    // `pairing.scale_pitch_to_video_dims`.
    static let captureWidthFixed = 1920
    static let captureHeightFixed = 1080

    /// Recording-resolution options shown in the SwiftUI Settings picker.
    /// All 16:9. `CameraViewController.configureCaptureFormat` searches for
    /// an AVCaptureDevice format matching (width, height) at 240 fps; 540p
    /// isn't supported on every iPhone — the picker still offers it but
    /// the format selection will fail loudly if the rig can't honour it.
    struct CaptureResolution: Hashable {
        let label: String
        let width: Int
        let height: Int
    }
    static let captureResolutions: [CaptureResolution] = [
        CaptureResolution(label: "1080p", width: 1920, height: 1080),
        CaptureResolution(label: "720p", width: 1280, height: 720),
        CaptureResolution(label: "540p", width: 960, height: 540),
    ]

    // MARK: - Container state

    var onDismiss: (() -> Void)?

    /// Forwarded to Diagnostics so its Test-connection button can reach the
    /// shared health monitor.
    weak var cameraVC: CameraViewController?

    private let viewModel = SettingsViewModel()

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "Settings"
        view.backgroundColor = DesignTokens.Colors.pageBackground
        isModalInPresentation = true

        configureNavBarAppearance()

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

        viewModel.load()
        embedSwiftUIHost()
    }

    override func viewDidDisappear(_ animated: Bool) {
        super.viewDidDisappear(animated)
        onDismiss?()
    }

    // MARK: - Nav bar

    private func configureNavBarAppearance() {
        let appearance = UINavigationBarAppearance()
        appearance.configureWithOpaqueBackground()
        appearance.backgroundColor = DesignTokens.Colors.pageBackground
        appearance.shadowColor = .clear
        appearance.titleTextAttributes = [
            .font: DesignTokens.Fonts.body(weight: .semibold),
            .foregroundColor: DesignTokens.Colors.ink
        ]
        navigationItem.standardAppearance = appearance
        navigationItem.scrollEdgeAppearance = appearance
        navigationItem.compactAppearance = appearance
    }

    private func embedSwiftUIHost() {
        let host = UIHostingController(
            rootView: SettingsView(
                viewModel: viewModel,
                onImportIntrinsics: { [weak self] in self?.presentDocumentPicker() },
                onOpenDiagnostics: { [weak self] in self?.openDiagnostics() }
            )
            .appTheme(.light)
        )
        addChild(host)
        host.view.translatesAutoresizingMaskIntoConstraints = false
        host.view.backgroundColor = .clear
        view.addSubview(host.view)
        NSLayoutConstraint.activate([
            host.view.topAnchor.constraint(equalTo: view.topAnchor),
            host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        host.didMove(toParent: self)
    }

    // MARK: - Nav bar actions

    @objc private func cancelTapped() {
        dismiss(animated: true)
    }

    @objc private func saveTapped() {
        view.endEditing(true)
        if let problem = viewModel.validate() {
            let alert = UIAlertController(title: "無法儲存", message: problem, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
            return
        }
        viewModel.save()
        dismiss(animated: true)
    }

    private func openDiagnostics() {
        let vc = SettingsDiagnosticsViewController()
        vc.cameraVC = cameraVC
        navigationController?.pushViewController(vc, animated: true)
    }

    private func presentDocumentPicker() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.json])
        picker.delegate = self
        picker.allowsMultipleSelection = false
        picker.shouldShowFileExtensions = true
        present(picker, animated: true)
    }

    // MARK: - Static persistence API (stable — external callers rely on it)

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
        let parkCameraInStandby = d.object(forKey: keyParkCameraInStandby) as? Bool ?? true

        // Recording resolution is now user-pickable. Read the stored height
        // and look up the matching CaptureResolution; fall back to 1080p
        // when the stored value isn't in the supported set (stale prefs).
        let storedH = intOrDefault(keyCaptureHeight, defaultValue: captureHeightFixed)
        let resolution = captureResolutions.first { $0.height == storedH }
            ?? captureResolutions[0]
        let captureWidth = resolution.width
        let captureHeight = resolution.height

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

    /// One-shot setter used by the main HUD's preview toggle button so it
    /// doesn't need to know the UserDefaults key.
    static func setParkCameraInStandby(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: keyParkCameraInStandby)
    }

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
            try viewModel.applyImportedCharuco(data: data)
        } catch {
            let alert = UIAlertController(
                title: "匯入失敗",
                message: error.localizedDescription,
                preferredStyle: .alert
            )
            alert.addAction(UIAlertAction(title: "OK", style: .default))
            present(alert, animated: true)
        }
    }
}
