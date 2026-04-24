import SwiftUI
import UIKit

@main
struct ball_trackerApp: App {
    init() {
        // Strip any stale UserDefaults keys left over from pre-Phase-1
        // builds (intrinsics / runtime knobs / capture mode etc. that
        // are now server-owned). Idempotent; no-op on fresh installs.
        AppSettingsStore.purgeLegacyKeys()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .ignoresSafeArea()
                .preferredColorScheme(.dark)
                .persistentSystemOverlays(.hidden)
        }
    }
}

private struct RootView: UIViewControllerRepresentable {
    func makeUIViewController(context: Context) -> UINavigationController {
        let camera = CameraViewController()
        let nav = UINavigationController(rootViewController: camera)
        let appearance = UINavigationBarAppearance()
        appearance.configureWithTransparentBackground()
        appearance.titleTextAttributes = [.foregroundColor: UIColor.white]
        nav.navigationBar.standardAppearance = appearance
        nav.navigationBar.scrollEdgeAppearance = appearance
        nav.navigationBar.tintColor = .white
        nav.modalPresentationCapturesStatusBarAppearance = true
        return nav
    }

    func updateUIViewController(_ uiViewController: UINavigationController, context: Context) {}
}
