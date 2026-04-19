import UIKit
import SwiftUI

/// Thin UIKit container for the calibration-method chooser. Hosts the
/// SwiftUI `CalibrationChooserView`; owns the landscape lock + navigation
/// push into `AutoCalibrationViewController` / `ManualCalibrationViewController`
/// which stay in UIKit because they drive OpenCV overlays / drag gestures
/// on the live camera preview.
final class CalibrationChooserViewController: UIViewController {
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask {
        [.landscapeLeft, .landscapeRight]
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = DesignTokens.Colors.pageBackground
        title = "位置校正"

        navigationItem.leftBarButtonItem = UIBarButtonItem(
            title: "關閉",
            style: .plain,
            target: self,
            action: #selector(close)
        )

        configureNavBarAppearance()
        embedSwiftUIHost()
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

    private func embedSwiftUIHost() {
        let host = UIHostingController(
            rootView: CalibrationChooserView(
                onAuto: { [weak self] in self?.openAuto() },
                onManual: { [weak self] in self?.openManual() }
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

    private func openAuto() {
        navigationController?.pushViewController(AutoCalibrationViewController(), animated: true)
    }

    private func openManual() {
        navigationController?.pushViewController(ManualCalibrationViewController(), animated: true)
    }

    @objc private func close() {
        dismiss(animated: true)
    }
}
