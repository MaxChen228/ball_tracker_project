import Foundation

struct AppSettings {
    var serverIP: String
    var cameraRole: String

    static let captureWidthFixed = 1920
    static let captureHeightFixed = 1080
    static let serverPortFixed = 8765
}

enum AppSettingsStore {
    private static let keyServerIP = "server_ip"
    private static let keyCameraRole = "camera_role"

    static func load() -> AppSettings {
        let defaults = UserDefaults.standard
        let serverIP = defaults.string(forKey: keyServerIP) ?? "192.168.1.100"
        let cameraRole = defaults.string(forKey: keyCameraRole) ?? "A"
        return AppSettings(serverIP: serverIP, cameraRole: cameraRole)
    }

    static func save(_ settings: AppSettings) {
        let defaults = UserDefaults.standard
        defaults.set(settings.serverIP, forKey: keyServerIP)
        defaults.set(settings.cameraRole, forKey: keyCameraRole)
    }
}
