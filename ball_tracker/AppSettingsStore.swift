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

    /// Legacy UserDefaults keys from pre-Phase-1 builds where iOS owned the
    /// full settings surface (intrinsics, chirp threshold, heartbeat
    /// interval, HSV, capture mode, tracking exposure cap, capture height,
    /// server port). Phase 1-5 decoupling (commits #54–#60) moved everything
    /// except bootstrap (server IP / role) to server-owned state pushed over
    /// WS. Long-lived installs may still have these keys lingering in
    /// `Library/Preferences/com.Max0228.ball-tracker.plist`; this list is
    /// what `purgeLegacyKeys` removes once per launch.
    private static let legacyKeys: [String] = [
        // Server bootstrap — port is now hardcoded `AppSettings.serverPortFixed`
        "server_port",
        // Intrinsics (pre-Phase-1; now server-owned at data/calibrations/<cam>.json)
        "intrinsic_fx", "intrinsic_fy", "intrinsic_fz",
        "intrinsic_cx", "intrinsic_cy",
        "intrinsic_image_width_px", "intrinsic_image_height_px",
        "intrinsic_distortion", "intrinsic_distortion_k1",
        "intrinsic_distortion_k2", "intrinsic_distortion_p1",
        "intrinsic_distortion_p2", "intrinsic_distortion_k3",
        "intrinsic_override_enabled",
        // Runtime knobs (now server-owned, pushed over WS `settings`)
        "chirp_threshold", "cfar_multiplier",
        "heartbeat_interval_s", "heartbeat_base_s",
        "tracking_exposure_cap", "capture_height_px", "capture_width_px",
        "hsv_h_min", "hsv_h_max", "hsv_s_min", "hsv_s_max", "hsv_v_min", "hsv_v_max",
        "capture_mode", "detection_paths",
        // Sync / capture UI toggles — moved to hard-coded defaults
        "park_camera_in_standby", "standby_fps", "tracking_fps",
    ]

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

    /// Strip any legacy Phase 0 / Phase 1 keys that may still sit in
    /// UserDefaults on long-running installs. Idempotent — safe to call on
    /// every launch. No-op for fresh installs (keys won't be present).
    static func purgeLegacyKeys() {
        let defaults = UserDefaults.standard
        for key in legacyKeys where defaults.object(forKey: key) != nil {
            defaults.removeObject(forKey: key)
        }
    }
}
