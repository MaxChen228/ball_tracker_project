import Foundation

// Wire-format types shared across iOS↔server boundary. Kept in
// `extension ServerUploader` so the original `ServerUploader.PitchPayload`
// / `ServerUploader.DetectionPath` namespacing across ~50 call sites
// stays valid — the only change is which file these definitions live in.
//
// CLAUDE.md flags wire schemas as one of the load-bearing alignment axes
// (PR #93 “iOS↔server alignment scorecard”), so giving them a dedicated
// file makes diffs against `server/schemas.py` easier to spot.

extension ServerUploader {
    struct HSVRangePayload: Codable, Equatable {
        let h_min: Int
        let h_max: Int
        let s_min: Int
        let s_max: Int
        let v_min: Int
        let v_max: Int

        static let tennis = HSVRangePayload(h_min: 25, h_max: 55, s_min: 90, s_max: 255, v_min: 90, v_max: 255)
    }

    /// Server-owned shape gate (aspect/fill thresholds applied after HSV +
    /// connected-components). Mirrors `ShapeGate` in server/detection.py.
    /// Pushed via WS `settings.shape_gate`; defaults match the server's
    /// `ShapeGate.default()` so iOS rejects the same blobs as server_post.
    struct ShapeGatePayload: Codable, Equatable {
        let aspect_min: Double
        let fill_min: Double

        static let `default` = ShapeGatePayload(aspect_min: 0.70, fill_min: 0.55)
    }

    enum DetectionPath: String, Codable, CaseIterable {
        case live = "live"
        case serverPost = "server_post"

        var displayLabel: String {
            switch self {
            case .live: return "Live stream"
            case .serverPost: return "Server post-pass"
            }
        }
    }

    /// One blob that survived area+aspect+fill on the live path. Mirrors
    /// `server/schemas.BlobCandidate`. The server's `live_pairing` runs
    /// `candidate_selector.select_best_candidate` over this list using a
    /// shape-prior cost (size + aspect + fill) before triangulation.
    ///
    /// `aspect` (min(w,h)/max(w,h)) and `fill` (area/(w*h)) are
    /// scale-invariant geometric stats — they survive the ball flying
    /// near→far without needing a distance-dependent expected size.
    /// Server selector treats both as required from the wire.
    struct BlobCandidate: Codable {
        let px: Double
        let py: Double
        let area: Int
        let area_score: Double
        let aspect: Double
        let fill: Double
    }

    struct FramePayload: Codable {
        let frame_index: Int
        let timestamp_s: Double
        /// Every blob that passed area+aspect+fill. Empty → no detection.
        /// Server's `live_pairing._resolve_candidates` runs the shape-
        /// prior selector to pick the winner.
        let candidates: [BlobCandidate]

        var ballDetected: Bool { !candidates.isEmpty }
    }

    struct CaptureTelemetry: Codable {
        let width_px: Int
        let height_px: Int
        let target_fps: Double
        let applied_fps: Double?
        let format_fov_deg: Double?
        let format_index: Int?
        let is_video_binned: Bool?
        let tracking_exposure_cap: String?
        let applied_max_exposure_s: Double?
    }

    struct PitchPayload: Codable {
        let camera_id: String
        /// Server-minted pairing key from `POST /sessions/arm`. A/B pairs
        /// by this alone — iPhones no longer mint any pairing identifier.
        /// Pattern: `s_` + 4–16 hex chars (matches the server regex).
        let session_id: String
        /// Shared legacy chirp sync-run id. Both phones must stamp the
        /// same id onto their recovered chirp anchor; a mismatch means the
        /// anchors came from different sync attempts and are incomparable.
        let sync_id: String?
        /// Session-clock PTS of the audio-chirp peak (from 時間校正). Nil
        /// when the operator armed without running a fresh time sync; the
        /// server flags this session as unpaireable.
        let sync_anchor_timestamp_s: Double?
        /// Absolute session-clock PTS (seconds) of the first video sample
        /// written to the MOV. The server adds this to each container-
        /// relative frame PTS to reconstruct the iOS master clock — which
        /// is the space `sync_anchor_timestamp_s` lives in.
        let video_start_pts_s: Double
        /// Device-local recording counter, for operator debugging only —
        /// server doesn't pair on it. Optional so a phone can omit it.
        let local_recording_index: Int?
        /// Snapshot of the enabled detection paths for this session.
        let paths: [String]?
        /// Actual capture conditions the phone observed while recording
        /// this take. Server persists this into the session so the web
        /// UI can answer "what format/FOV/exposure did this clip really use?"
        let capture_telemetry: CaptureTelemetry?

        // NOTE: Intrinsics / homography / image dims / video_fps used to
        // live on this struct and were echoed on every upload. Phase 1 of
        // the iOS decoupling refactor moved that into the server's
        // calibration DB (populated via POST /calibration), which is now
        // the single source of truth. The server fills those fields in-
        // memory from the cached snapshot before running detection +
        // triangulation, so the wire shape shrinks to just session-level
        // metadata + per-frame detection output.

        func withPaths(_ newPaths: [DetectionPath]) -> PitchPayload {
            PitchPayload(
                camera_id: camera_id,
                session_id: session_id,
                sync_id: sync_id,
                sync_anchor_timestamp_s: sync_anchor_timestamp_s,
                video_start_pts_s: video_start_pts_s,
                local_recording_index: local_recording_index,
                paths: newPaths.map(\.rawValue),
                capture_telemetry: capture_telemetry
            )
        }
    }

    /// Server `/pitch` response summary. Triangulation fields are optional
    /// because they're only populated once both A and B for a session arrive.
    struct PitchUploadResponse: Codable {
        let ok: Bool
        let session_id: String
        let paired: Bool
        let triangulated_points: Int
        let error: String?
        let mean_residual_m: Double?
        let max_residual_m: Double?
        let peak_z_m: Double?
        let duration_s: Double?
    }

    struct DeviceSocketEvent: Codable {
        let type: String
        let sid: String?
        let paths: [String]?
        let max_duration_s: Double?
        let hsv_range: HSVRangePayload?
        let tracking_exposure_cap: String?
        let chirp_detect_threshold: Double?
        let heartbeat_interval_s: Double?
        let capture_height_px: Int?
        let preview_requested: Bool?
        let calibration_frame_requested: Bool?
    }

    enum TrackingExposureCapMode: String, Codable {
        case frameDuration = "frame_duration"
        case shutter500 = "shutter_500"
        case shutter1000 = "shutter_1000"

        var label: String {
            switch self {
            case .frameDuration: return "1/240"
            case .shutter500: return "1/500"
            case .shutter1000: return "1/1000"
            }
        }

        var maxExposureSeconds: Double? {
            switch self {
            case .frameDuration:
                return nil
            case .shutter500:
                return 1.0 / 500.0
            case .shutter1000:
                return 1.0 / 1000.0
            }
        }
    }
}
