import Foundation
import os.log

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
        // Phase 0 PR3 — `cameraRole` is now a *cached local label*, not
        // authoritative. The server resolves device_uuid → camera_id at
        // WS handshake and pushes `cam_id_assigned`. The UserDefaults
        // value here is used for local UI labelling and as the `cam`
        // field of outbound frame/heartbeat payloads (server treats
        // that field as advisory; URL-resolved cam_id wins). Operators
        // assign cam_ids from the dashboard "Device Pool" panel; this
        // default only matters until that assignment lands.
        let defaults = UserDefaults.standard
        let serverIP = defaults.string(forKey: keyServerIP) ?? "192.168.50.106"
        let cameraRoleStored = defaults.string(forKey: keyCameraRole)
        if cameraRoleStored == nil {
            os_log("AppSettingsStore: cameraRole not set — defaulting to A. Server will reassign via /devices/assign.", type: .info)
        }
        let cameraRole = cameraRoleStored ?? "A"
        return AppSettings(serverIP: serverIP, cameraRole: cameraRole)
    }

    static func save(_ settings: AppSettings) {
        let defaults = UserDefaults.standard
        defaults.set(settings.serverIP, forKey: keyServerIP)
        defaults.set(settings.cameraRole, forKey: keyCameraRole)
    }
}
