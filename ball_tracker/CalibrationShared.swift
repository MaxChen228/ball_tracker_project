import UIKit
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Shared calibration helpers used by both the manual 5-handle flow and
/// the auto ArUco flow. Kept out of either VC so the two can evolve
/// independently without dragging the other's math into their file.
enum CalibrationShared {
    // Real home-plate pentagon vertices in meters. Axes: X = left/right,
    // Y = depth from front edge (pitcher side) toward back tip (catcher side).
    static let plateWidthM = 0.432       // 17" front edge
    static let plateShoulderYM = 0.216   // 8.5" back to shoulder
    static let plateTipYM = 0.432        // 17" back to back tip

    /// 6 ArUco markers (DICT_4X4_50, IDs 0-5) on home-plate landmarks:
    /// FL / FR / RS / LS / BT / MF. Shared so the overlay / solve paths
    /// on the Auto VC stay in lock-step.
    static let markerWorldPoints: [Int: (Double, Double)] = [
        0: (-plateWidthM / 2.0, 0.0),                 // FL
        1: ( plateWidthM / 2.0, 0.0),                 // FR
        2: ( plateWidthM / 2.0, plateShoulderYM),     // RS
        3: (-plateWidthM / 2.0, plateShoulderYM),     // LS
        4: ( 0.0,                plateTipYM),         // BT (back tip)
        5: ( 0.0,                0.0),                // MF (mid-front edge)
    ]

    static func plateWorldPoints() -> [(Double, Double)] {
        return [
            (-plateWidthM / 2.0, 0.0),             // FL
            (plateWidthM / 2.0, 0.0),              // FR
            (plateWidthM / 2.0, plateShoulderYM),  // RS
            (0.0, plateTipYM),                     // BT
            (-plateWidthM / 2.0, plateShoulderYM), // LS
        ]
    }

    // UserDefaults keys shared with IntrinsicsStore.
    static let keyHomography = "homography_3x3"
    static let keyHorizontalFovRad = "horizontal_fov_rad"
    static let keyImageWidthPx = "image_width_px"
    static let keyImageHeightPx = "image_height_px"
    static let keyIntrinsicCx = "intrinsic_cx"
    static let keyIntrinsicCy = "intrinsic_cy"
    static let keyIntrinsicFx = "intrinsic_fx"
    static let keyIntrinsicFz = "intrinsic_fz"

    /// Fire-and-forget POST of a freshly-persisted calibration to the
    /// server so the dashboard's calibration canvas can draw this phone's
    /// pose without waiting for a first pitch upload. Shared between both
    /// manual and auto save paths.
    static func postCalibrationToServer(source: String) {
        guard let intrinsics = IntrinsicsStore.loadIntrinsicsPayload(),
              let homography = IntrinsicsStore.loadHomography(),
              let dims = IntrinsicsStore.loadImageDimensions() else {
            log.info("calibration upload skipped reason=incomplete_local_state source=\(source, privacy: .public)")
            return
        }
        let settings = SettingsViewController.loadFromUserDefaults()
        let uploader = ServerUploader(config: ServerUploader.ServerConfig(
            serverIP: settings.serverIP,
            serverPort: settings.serverPort
        ))
        let payload = ServerUploader.CalibrationPayload(
            camera_id: settings.cameraRole,
            intrinsics: intrinsics,
            homography: homography,
            image_width_px: dims.width,
            image_height_px: dims.height
        )
        uploader.postCalibration(payload) { result in
            switch result {
            case .success:
                log.info("calibration upload ok cam=\(settings.cameraRole, privacy: .public) source=\(source, privacy: .public)")
            case .failure(let error):
                log.error("calibration upload failed cam=\(settings.cameraRole, privacy: .public) source=\(source, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// FOV-approximation intrinsics writer. Respects the Settings →
    /// "Use ChArUco values" override — if the user has pasted precise
    /// intrinsics, leave them alone.
    static func persistFovIntrinsicsIfPossible() {
        let d = UserDefaults.standard
        if d.string(forKey: SettingsViewController.keyIntrinsicsSource) == "manual" {
            return
        }
        let imageW = d.integer(forKey: keyImageWidthPx)
        let imageH = d.integer(forKey: keyImageHeightPx)
        let hFovRad = d.double(forKey: keyHorizontalFovRad)
        guard imageW > 0, imageH > 0, hFovRad > 0 else { return }

        // Spec approximation:
        // fx = (imageWidth / 2) / tan(hFOV/2)
        // verticalFov = 2*atan(tan(hFOV/2) * (imageHeight/imageWidth))
        // fz = (imageHeight / 2) / tan(verticalFov/2)
        let fx = (Double(imageW) / 2.0) / tan(hFovRad / 2.0)
        let verticalFov = 2.0 * atan(tan(hFovRad / 2.0) * (Double(imageH) / Double(imageW)))
        let fz = (Double(imageH) / 2.0) / tan(verticalFov / 2.0)
        let cx = Double(imageW) / 2.0
        let cy = Double(imageH) / 2.0

        d.set(cx, forKey: keyIntrinsicCx)
        d.set(cy, forKey: keyIntrinsicCy)
        d.set(fx, forKey: keyIntrinsicFx)
        d.set(fz, forKey: keyIntrinsicFz)
    }

}
