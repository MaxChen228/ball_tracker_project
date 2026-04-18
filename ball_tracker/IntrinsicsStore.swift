import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "sensing")

/// Centralised UserDefaults access for calibration-derived intrinsics,
/// homography, and captured image dimensions. Keeps the keys and the
/// parsing rules in one place so the camera VC, the calibration screen,
/// and the payload enrichment path all agree on the schema.
enum IntrinsicsStore {
    static let keyHorizontalFovRad = "horizontal_fov_rad"
    static let keyImageWidthPx = "image_width_px"
    static let keyImageHeightPx = "image_height_px"
    static let keyIntrinsicCx = "intrinsic_cx"
    static let keyIntrinsicCy = "intrinsic_cy"
    static let keyIntrinsicFx = "intrinsic_fx"
    static let keyIntrinsicFz = "intrinsic_fz"
    static let keyIntrinsicDistortion = "intrinsic_distortion"
    static let keyHomography = "homography_3x3"

    /// Return the payload-shaped intrinsics (including optional OpenCV
    /// 5-coefficient distortion), or nil when the core four are missing.
    static func loadIntrinsicsPayload() -> ServerUploader.IntrinsicsPayload? {
        let d = UserDefaults.standard
        guard
            d.object(forKey: keyIntrinsicFx) != nil,
            d.object(forKey: keyIntrinsicFz) != nil,
            d.object(forKey: keyIntrinsicCx) != nil,
            d.object(forKey: keyIntrinsicCy) != nil
        else { return nil }
        var distortion: [Double]? = nil
        if let arr = d.array(forKey: keyIntrinsicDistortion) as? [Double], arr.count == 5 {
            distortion = arr
        }
        return ServerUploader.IntrinsicsPayload(
            fx: d.double(forKey: keyIntrinsicFx),
            fz: d.double(forKey: keyIntrinsicFz),
            cx: d.double(forKey: keyIntrinsicCx),
            cy: d.double(forKey: keyIntrinsicCy),
            distortion: distortion
        )
    }

    static func loadHomography() -> [Double]? {
        guard let h = UserDefaults.standard.array(forKey: keyHomography) as? [Double] else {
            log.warning("homography missing — triangulation will fail server-side")
            return nil
        }
        log.debug("homography loaded")
        return h
    }

    /// Non-logging existence probe — cheap to call from UI ticks without
    /// spamming the log with "missing / loaded" lines.
    static func hasHomography() -> Bool {
        UserDefaults.standard.array(forKey: keyHomography) != nil
    }

    /// Return the captured image dimensions, or nil when they have not
    /// been written yet (the capture callback writes them lazily).
    static func loadImageDimensions() -> (width: Int, height: Int)? {
        let d = UserDefaults.standard
        let w = d.integer(forKey: keyImageWidthPx)
        let h = d.integer(forKey: keyImageHeightPx)
        guard w > 0, h > 0 else { return nil }
        return (w, h)
    }

    static func setHorizontalFov(_ radians: Double) {
        UserDefaults.standard.set(radians, forKey: keyHorizontalFovRad)
    }

    static func setImageDimensions(width: Int, height: Int) {
        let d = UserDefaults.standard
        d.set(width, forKey: keyImageWidthPx)
        d.set(height, forKey: keyImageHeightPx)
    }
}
