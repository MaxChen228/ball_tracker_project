import Foundation

struct AppSettings {
    var serverIP: String
    var serverPort: Int
    var cameraRole: String

    static let captureWidthFixed = 1920
    static let captureHeightFixed = 1080
}

enum AppSettingsStore {
    private static let keyServerIP = "server_ip"
    private static let keyServerPort = "server_port"
    private static let keyCameraRole = "camera_role"

    static func load() -> AppSettings {
        let defaults = UserDefaults.standard
        let serverIP = defaults.string(forKey: keyServerIP) ?? "192.168.1.100"
        let serverPort: Int
        if defaults.object(forKey: keyServerPort) == nil {
            serverPort = 8765
        } else {
            serverPort = defaults.integer(forKey: keyServerPort)
        }
        let cameraRole = defaults.string(forKey: keyCameraRole) ?? "A"
        return AppSettings(serverIP: serverIP, serverPort: serverPort, cameraRole: cameraRole)
    }

    static func save(_ settings: AppSettings) {
        let defaults = UserDefaults.standard
        defaults.set(settings.serverIP, forKey: keyServerIP)
        defaults.set(settings.serverPort, forKey: keyServerPort)
        defaults.set(settings.cameraRole, forKey: keyCameraRole)
    }
}
