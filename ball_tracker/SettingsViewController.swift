import UIKit
import SwiftUI

/// Thin UIKit container for the bootstrap Settings screen — owns the
/// navigation bar (Save / Cancel) and the push to the diagnostics subpage.
/// The form content itself is SwiftUI (`SettingsView` + `SettingsViewModel`).
///
/// Phase 6: Settings holds only server IP / port / camera role. Everything
/// else (chirp threshold, heartbeat interval, HSV, capture mode, auto-cal)
/// is dashboard-pushed or server-owned.
final class SettingsViewController: UIViewController {

    // MARK: - Data model

    struct Settings {
        var serverIP: String
        var serverPort: Int
        var cameraRole: String          // "A" or "B"
    }

    // MARK: - UserDefaults keys

    private static let keyServerIP = "server_ip"
    private static let keyServerPort = "server_port"
    private static let keyCameraRole = "camera_role"

    // Capture resolution is fixed at 1080p — the only 240 fps slow-mo preset
    // that is baseline across every shipping iPhone. `CameraViewController`
    // reads these directly via `SettingsViewController.capture{Width,Height}Fixed`.
    static let captureWidthFixed = 1920
    static let captureHeightFixed = 1080

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

    // MARK: - Static persistence API (stable — external callers rely on it)

    static func loadFromUserDefaults() -> Settings {
        let d = UserDefaults.standard
        func intOrDefault(_ key: String, defaultValue: Int) -> Int {
            if d.object(forKey: key) == nil { return defaultValue }
            return d.integer(forKey: key)
        }
        let serverIP = d.string(forKey: keyServerIP) ?? "192.168.1.100"
        let serverPort = intOrDefault(keyServerPort, defaultValue: 8765)
        let cameraRole = d.string(forKey: keyCameraRole) ?? "A"
        return Settings(
            serverIP: serverIP,
            serverPort: serverPort,
            cameraRole: cameraRole
        )
    }

    static func saveToUserDefaults(_ settings: Settings) {
        let d = UserDefaults.standard
        d.set(settings.serverIP, forKey: keyServerIP)
        d.set(settings.serverPort, forKey: keyServerPort)
        d.set(settings.cameraRole, forKey: keyCameraRole)
    }
}
