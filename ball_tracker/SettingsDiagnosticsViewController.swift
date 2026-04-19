import UIKit
import SwiftUI

/// Diagnostics subpage reached from Settings → 診斷. Thin UIKit container
/// that hosts the SwiftUI `DiagnosticsView` and forwards the "Test" tap to
/// the live camera VC's health probe.
final class SettingsDiagnosticsViewController: UIViewController {
    /// Optional reference to the live camera VC so the Test button can
    /// trigger an actual probe. Nil-safe — the page is still useful as a
    /// read-only diagnostic when the camera VC is not in the hierarchy.
    weak var cameraVC: CameraViewController?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = DesignTokens.Colors.pageBackground
        title = "診斷"

        configureNavBarAppearance()

        let host = UIHostingController(
            rootView: DiagnosticsView(
                onTest: { [weak self] in self?.cameraVC?.testServerConnection() }
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
}
