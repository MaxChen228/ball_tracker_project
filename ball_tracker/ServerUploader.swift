import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "network")

/// iOS uploader responsible for sending one iPhone pitch payload to the server.
///
/// Wire format:
///   POST http://{server_ip}:{port}/pitch
///   multipart/form-data with two parts:
///     - `payload`: JSON-encoded `PitchPayload` (required)
///     - `video`:   H.264/MOV clip of the cycle (required — server-side ball
///                  detection is the sole data path; no detection runs on
///                  the phone any more)
final class ServerUploader {
    struct IntrinsicsPayload: Codable {
        let fx: Double
        let fz: Double
        let cx: Double
        let cy: Double
        /// OpenCV 5-coefficient distortion `[k1, k2, p1, p2, k3]`. Nil when
        /// no ChArUco calibration has been imported for this camera.
        let distortion: [Double]?
    }

    /// Metadata accompanying the required H.264 MOV. The server decodes the
    /// video, runs HSV ball detection per frame, and triangulates.
    /// One decoded / on-device-detected frame. Wire-identical to
    /// `server/schemas.FramePayload`. `px` / `py` are nil on frames where
    /// the detector didn't find a ball (mode-two still records those so
    /// the server can see the timestamp coverage).
    struct FramePayload: Codable {
        let frame_index: Int
        let timestamp_s: Double
        let px: Double?
        let py: Double?
        let ball_detected: Bool
    }

    struct PitchPayload: Codable {
        let camera_id: String
        /// Server-minted pairing key from `POST /sessions/arm`. A/B pairs
        /// by this alone — iPhones no longer mint any pairing identifier.
        /// Pattern: `s_` + 4–16 hex chars (matches the server regex).
        let session_id: String
        /// Session-clock PTS of the audio-chirp peak (from 時間校正). Nil
        /// when the operator armed without running a fresh time sync; the
        /// server flags this session as unpaireable.
        let sync_anchor_timestamp_s: Double?
        /// Absolute session-clock PTS (seconds) of the first video sample
        /// written to the MOV. The server adds this to each container-
        /// relative frame PTS to reconstruct the iOS master clock — which
        /// is the space `sync_anchor_timestamp_s` lives in.
        let video_start_pts_s: Double
        /// Nominal capture rate of the MOV. Server uses this as a sanity
        /// check against the decoded frame count.
        let video_fps: Double
        /// Device-local recording counter, for operator debugging only —
        /// server doesn't pair on it. Optional so a phone can omit it.
        let local_recording_index: Int?
        let intrinsics: IntrinsicsPayload?
        let homography: [Double]?
        let image_width_px: Int?
        let image_height_px: Int?
        /// Per-frame detection results. Empty in mode-one (`camera_only`):
        /// the server runs detection on the uploaded MOV. Non-empty in
        /// mode-two (`on_device`): iPhone ran its own BTDetectionSession
        /// pipeline and ships the frame list alongside the metadata, no MOV.
        /// Default [] so the field always encodes to a concrete array.
        let frames: [FramePayload]
        /// Dual-mode only: parallel iOS-end detection stream carried
        /// alongside the MOV. Server keeps both streams so the viewer can
        /// overlay them for HSV / shape-gate tuning. Empty list for
        /// camera_only / on_device sessions. Default [] keeps the field
        /// encoded concretely for consistency with `frames`.
        let frames_on_device: [FramePayload]

        /// Return a copy of this payload with `frames` replaced. Used by
        /// the mode-two cycle-complete path to attach the session's
        /// BTDetectionSession output before shipping.
        func withFrames(_ newFrames: [FramePayload]) -> PitchPayload {
            PitchPayload(
                camera_id: camera_id,
                session_id: session_id,
                sync_anchor_timestamp_s: sync_anchor_timestamp_s,
                video_start_pts_s: video_start_pts_s,
                video_fps: video_fps,
                local_recording_index: local_recording_index,
                intrinsics: intrinsics,
                homography: homography,
                image_width_px: image_width_px,
                image_height_px: image_height_px,
                frames: newFrames,
                frames_on_device: frames_on_device
            )
        }

        /// Return a copy of this payload with `frames_on_device` replaced.
        /// Dual-mode cycle-complete uses this to attach the iOS-end
        /// BTDetectionSession output while preserving the MOV path's
        /// (empty) `frames`.
        func withFramesOnDevice(_ newFramesOnDevice: [FramePayload]) -> PitchPayload {
            PitchPayload(
                camera_id: camera_id,
                session_id: session_id,
                sync_anchor_timestamp_s: sync_anchor_timestamp_s,
                video_start_pts_s: video_start_pts_s,
                video_fps: video_fps,
                local_recording_index: local_recording_index,
                intrinsics: intrinsics,
                homography: homography,
                image_width_px: image_width_px,
                image_height_px: image_height_px,
                frames: frames,
                frames_on_device: newFramesOnDevice
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

    /// One device entry as reported by `/heartbeat` / `/status`.
    struct HeartbeatDevice: Codable {
        let camera_id: String
        let last_seen_at: Double
    }

    /// Current armed/ended session, or nil when the server has never
    /// armed one this process lifetime.
    struct HeartbeatSession: Codable {
        let id: String
        let armed: Bool
        let started_at: Double
        let ended_at: Double?
        /// Capture mode snapshotted at arm time. Once armed this is frozen
        /// for the whole recording cycle — a dashboard toggle mid-session
        /// doesn't mutate it. Optional to keep older server builds parseable.
        let mode: String?
    }

    /// Mutual chirp sync context carried in heartbeat/status responses.
    /// `id` is the server-minted run identifier; phones dedupe sync_run
    /// commands on this id so a repeat of an in-flight run doesn't
    /// re-trigger emission.
    struct SyncIntent: Codable {
        let id: String
        let started_at: Double
        let reports_received: [String]?
    }

    /// Most recent solved mutual-sync result. `delta_s` is `A_clock − B_clock`
    /// — positive means A runs ahead of B. Apply as `t_on_A = t_on_B + delta_s`
    /// when re-timing B's events into A's timeline.
    struct SyncResult: Codable {
        let id: String
        let delta_s: Double
        let distance_m: Double
        let solved_at: Double
    }

    /// Response body shared by `POST /heartbeat` and `GET /status`.
    /// `commands[self.camera_id]` drives the dashboard-remote arm/disarm
    /// flow on iPhone.
    struct HeartbeatResponse: Codable {
        let state: String?
        let devices: [HeartbeatDevice]?
        let session: HeartbeatSession?
        let commands: [String: String]?
        /// Dashboard-wide capture mode toggle. iPhones read this in idle
        /// to render the HUD mode chip; during an armed session the phone
        /// prefers `session.mode` (the snapshot). Optional for back-compat.
        let capture_mode: String?
        /// In-flight mutual chirp sync context. Present when a run is
        /// active; `commands[self] == "sync_run"` will fire alongside.
        let sync: SyncIntent?
        /// Most recently solved Δ + D. Used for the dashboard UI; phones
        /// can also read it to apply Δ locally if ever needed (not yet
        /// wired — triangulation currently applies Δ server-side).
        let last_sync: SyncResult?
    }

    /// iOS-side capture-mode enum. Kept string-valued so it round-trips
    /// directly through HeartbeatResponse without a custom decoder.
    enum CaptureMode: String, Codable {
        case cameraOnly = "camera_only"
        case onDevice = "on_device"
        case dual = "dual"

        var displayLabel: String {
            switch self {
            case .cameraOnly: return "Camera-only"
            case .onDevice:   return "On-device"
            case .dual:       return "Dual"
            }
        }
    }

    struct ServerConfig {
        var serverIP: String = "192.168.1.100"
        var serverPort: Int = 8765

        func baseURL() -> URL? {
            return URL(string: "http://\(serverIP):\(serverPort)")
        }
    }

    private let config: ServerConfig

    init(config: ServerConfig = ServerConfig()) {
        self.config = config
    }

    /// Typed upload error. Introduced alongside the legacy
    /// `Result<_, Error>` API so callers can apply differentiated retry
    /// policy (e.g. backoff on `.network`/`.server`, give up on `.client`).
    /// The legacy API maps these back into `NSError` for source
    /// compatibility — a separate migration will move call sites across.
    enum UploadError: Error {
        /// URLSession-level failure: timeout, no network, TLS, DNS, etc.
        case network(URLError)
        /// HTTP 4xx — request is malformed or rejected; retry won't help.
        case client(statusCode: Int, body: Data?)
        /// HTTP 5xx — server-side fault; transient, retry makes sense.
        case server(statusCode: Int, body: Data?)
        /// Body parse failure (JSON shape drift, truncated response, etc.).
        case decoding(Error)
        /// No `HTTPURLResponse` came back — defensive; shouldn't happen
        /// under `URLSession.shared.dataTask(with: URLRequest)`.
        case invalidResponse
    }

    /// Legacy sugar: upload a payload with no video attachment.
    func uploadPitch(
        _ pitch: PitchPayload,
        completion: ((Result<PitchUploadResponse, Error>) -> Void)? = nil
    ) {
        uploadPitch(pitch, videoURL: nil, completion: completion)
    }

    /// Upload one cycle as multipart/form-data. `videoURL` is optional — when
    /// nil (legacy / failed clip writer / Phase-0 payload cache) the request
    /// still succeeds and the server stores only the JSON side.
    ///
    /// Legacy `Error`-typed completion overload. Internally delegates to
    /// `uploadPitchTyped(_:videoURL:completion:)` and maps `UploadError`
    /// back to the `NSError` shape the previous implementation surfaced,
    /// so existing call sites (e.g. `PayloadUploadQueue`) behave identically.
    func uploadPitch(
        _ pitch: PitchPayload,
        videoURL: URL?,
        completion: ((Result<PitchUploadResponse, Error>) -> Void)? = nil
    ) {
        uploadPitchTyped(pitch, videoURL: videoURL) { result in
            switch result {
            case .success(let response):
                completion?(.success(response))
            case .failure(let uploadError):
                completion?(.failure(Self.legacyNSError(for: uploadError)))
            }
        }
    }

    /// Typed variant of `uploadPitch(_:videoURL:completion:)`. Distinguishes
    /// network / 4xx / 5xx / decoding failures so the caller can branch its
    /// retry strategy on `UploadError.isTransient`.
    ///
    /// Body construction is **streaming**: the multipart envelope (header +
    /// video bytes + footer) is assembled into a tmp file by copying the
    /// MOV in 64 KB chunks, then uploaded via
    /// `URLSession.uploadTask(with:fromFile:)`. This avoids loading the
    /// 150-300 MB clip into RAM (previously caused OOM on the device) and
    /// surfaces an explicit `.network(URLError(.fileDoesNotExist))` failure
    /// when the source video is unreadable — instead of the old `try?` path
    /// that silently dropped the video part and hit the server's 422.
    func uploadPitchTyped(
        _ pitch: PitchPayload,
        videoURL: URL?,
        completion: @escaping (Result<PitchUploadResponse, UploadError>) -> Void
    ) {
        guard let base = config.baseURL() else {
            completion(.failure(.network(URLError(.badURL))))
            return
        }
        let url = base.appendingPathComponent("pitch")

        let payloadData: Data
        do {
            payloadData = try JSONEncoder().encode(pitch)
        } catch {
            completion(.failure(.decoding(error)))
            return
        }

        let boundary = "Boundary-\(UUID().uuidString)"
        let sid = pitch.session_id
        let cam = pitch.camera_id

        // Stream the multipart body (potentially 150-300 MB of MOV bytes)
        // to a tmp file on a background queue so callers on the main queue
        // — `PayloadUploadQueue.processNextIfNeeded` is one — don't stall
        // their run loop on disk I/O.
        Self.bodyAssemblyQueue.async {
            let bodyFileURL: URL
            let bodyByteCount: Int
            do {
                let written = try Self.writeMultipartBodyToTempFile(
                    boundary: boundary,
                    payloadJSON: payloadData,
                    videoURL: videoURL
                )
                bodyFileURL = written.url
                bodyByteCount = written.byteCount
            } catch let err as UploadError {
                completion(.failure(err))
                return
            } catch {
                completion(.failure(.network(URLError(.cannotCreateFile))))
                return
            }

            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue(
                "multipart/form-data; boundary=\(boundary)",
                forHTTPHeaderField: "Content-Type"
            )

            let task = URLSession.shared.uploadTask(
                with: request,
                fromFile: bodyFileURL
            ) { data, response, error in
                try? FileManager.default.removeItem(at: bodyFileURL)
                if let error = error {
                    let urlError = (error as? URLError) ?? URLError(.unknown)
                    log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=\(urlError.localizedDescription, privacy: .public) code=\(urlError.code.rawValue)")
                    completion(.failure(.network(urlError)))
                    return
                }
                guard let http = response as? HTTPURLResponse else {
                    log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=invalid_response")
                    completion(.failure(.invalidResponse))
                    return
                }
                if !(200...299).contains(http.statusCode) {
                    if (500...599).contains(http.statusCode) {
                        log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=http_\(http.statusCode) category=server")
                        completion(.failure(.server(statusCode: http.statusCode, body: data)))
                    } else {
                        // 4xx (and any other non-2xx/non-5xx) is non-retryable
                        // from the client's perspective.
                        log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=http_\(http.statusCode) category=client")
                        completion(.failure(.client(statusCode: http.statusCode, body: data)))
                    }
                    return
                }
                guard let data else {
                    // 2xx with an empty body — treat as decoding failure so the
                    // caller sees it as non-transient (retrying won't add bytes).
                    log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=empty_body")
                    completion(.failure(.decoding(URLError(.cannotDecodeContentData))))
                    return
                }
                do {
                    let decoded = try JSONDecoder().decode(PitchUploadResponse.self, from: data)
                    log.info("upload ok session=\(sid, privacy: .public) cam=\(cam, privacy: .public) bytes=\(bodyByteCount) paired=\(decoded.paired) points=\(decoded.triangulated_points)")
                    completion(.success(decoded))
                } catch {
                    log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=\(error.localizedDescription, privacy: .public) category=decoding")
                    completion(.failure(.decoding(error)))
                }
            }
            task.resume()
        }
    }

    /// Serial background queue for streaming-multipart-body assembly so a
    /// 150-300 MB MOV copy never blocks the main run loop. Serial because
    /// writes go to disjoint UUID-named tmp files anyway and one-at-a-time
    /// is friendlier on the disk and on flash wear than parallel writes.
    private static let bodyAssemblyQueue = DispatchQueue(
        label: "com.Max0228.ball-tracker.uploader.body",
        qos: .utility
    )

    /// Map a typed `UploadError` back into the `NSError` shape emitted by
    /// the pre-typed implementation. Kept private so the legacy overload
    /// can preserve wire-compatible error surfaces for unmigrated callers.
    private static func legacyNSError(for error: UploadError) -> NSError {
        switch error {
        case .network(let urlError):
            return urlError as NSError
        case .client(let code, _), .server(let code, _):
            return NSError(
                domain: "ServerUploader",
                code: code,
                userInfo: [NSLocalizedDescriptionKey: "HTTP status \(code)"]
            )
        case .decoding(let underlying):
            return underlying as NSError
        case .invalidResponse:
            return NSError(
                domain: "ServerUploader",
                code: -2,
                userInfo: [NSLocalizedDescriptionKey: "Empty response"]
            )
        }
    }

    /// Build the multipart/form-data envelope into a tmp file by streaming
    /// (a) the JSON header bytes, (b) — when `videoURL` is non-nil — the
    /// MOV in 64 KB chunks via `InputStream`, then (c) the closing
    /// boundary footer. Used so a 150-300 MB clip never has to live in RAM
    /// alongside the rest of the multipart body.
    ///
    /// Throws `UploadError.network(URLError(.fileDoesNotExist))` when
    /// `videoURL` was supplied but cannot be opened — this is the explicit
    /// failure the legacy `try? Data(contentsOf:)` swallowed. Every other
    /// I/O error is rethrown as the underlying `Error` so the caller can
    /// translate it to `.network(URLError(.cannotCreateFile))`.
    private static func writeMultipartBodyToTempFile(
        boundary: String,
        payloadJSON: Data,
        videoURL: URL?
    ) throws -> (url: URL, byteCount: Int) {
        let tmpURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("upload-\(UUID().uuidString).multipart")

        guard let out = OutputStream(url: tmpURL, append: false) else {
            throw UploadError.network(URLError(.cannotCreateFile))
        }
        out.open()
        defer { out.close() }

        var totalWritten = 0

        func writeData(_ data: Data) throws {
            try data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                guard let base = raw.baseAddress?.assumingMemoryBound(to: UInt8.self) else {
                    return
                }
                var remaining = raw.count
                var offset = 0
                while remaining > 0 {
                    let n = out.write(base.advanced(by: offset), maxLength: remaining)
                    if n <= 0 {
                        let underlying = out.streamError ?? URLError(.cannotWriteToFile)
                        throw underlying
                    }
                    offset += n
                    remaining -= n
                    totalWritten += n
                }
            }
        }

        func writeString(_ s: String) throws {
            if let d = s.data(using: .utf8) { try writeData(d) }
        }

        // JSON part.
        try writeString("--\(boundary)\r\n")
        try writeString("Content-Disposition: form-data; name=\"payload\"\r\n")
        try writeString("Content-Type: application/json\r\n\r\n")
        try writeData(payloadJSON)
        try writeString("\r\n")

        // Optional video part — streamed in 64 KB chunks.
        if let videoURL {
            guard let input = InputStream(url: videoURL) else {
                // Surfaced from the throwing call site as the explicit
                // file-unreadable signal. Avoids the legacy `try?` that
                // silently sent a video-less request.
                throw UploadError.network(URLError(.fileDoesNotExist))
            }
            input.open()
            defer { input.close() }

            // Open() can fail post-construction (e.g. permission revoked
            // between init and open) — treat the same as missing.
            if input.streamError != nil {
                throw UploadError.network(URLError(.fileDoesNotExist))
            }

            try writeString("--\(boundary)\r\n")
            try writeString(
                "Content-Disposition: form-data; name=\"video\"; filename=\"\(videoURL.lastPathComponent)\"\r\n"
            )
            try writeString("Content-Type: video/quicktime\r\n\r\n")

            let bufferSize = 64 * 1024
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
            defer { buffer.deallocate() }

            while input.hasBytesAvailable {
                let read = input.read(buffer, maxLength: bufferSize)
                if read < 0 {
                    let underlying = input.streamError ?? URLError(.cannotOpenFile)
                    throw underlying
                }
                if read == 0 { break }
                var remaining = read
                var offset = 0
                while remaining > 0 {
                    let n = out.write(buffer.advanced(by: offset), maxLength: remaining)
                    if n <= 0 {
                        let underlying = out.streamError ?? URLError(.cannotWriteToFile)
                        throw underlying
                    }
                    offset += n
                    remaining -= n
                    totalWritten += n
                }
            }

            try writeString("\r\n")
        }

        try writeString("--\(boundary)--\r\n")
        return (tmpURL, totalWritten)
    }

    /// POST a 1 Hz liveness ping and read back the full status payload —
    /// the server piggy-backs `devices`, `session`, and per-camera
    /// `commands` on every reply, so one round-trip drives both online
    /// presence and remote arm/disarm dispatch.
    ///
    /// `timeSynced` mirrors whether the phone currently holds a valid
    /// audio-chirp anchor (`lastSyncAnchorTimestampS != nil`). The server
    /// surfaces it on `/status` → dashboard so each device row shows a
    /// "time sync ✓ / ✗" dot without waiting for a pitch upload.
    func sendHeartbeat(
        cameraId: String,
        timeSynced: Bool = false,
        completion: @escaping (Result<HeartbeatResponse, Error>) -> Void
    ) {
        guard let base = config.baseURL() else {
            completion(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("heartbeat")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        struct HeartbeatRequestBody: Codable {
            let camera_id: String
            let time_synced: Bool
        }
        do {
            request.httpBody = try JSONEncoder().encode(
                HeartbeatRequestBody(camera_id: cameraId, time_synced: timeSynced)
            )
        } catch {
            completion(.failure(error))
            return
        }

        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                completion(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                completion(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"]
                )))
                return
            }
            guard let data else {
                completion(.failure(NSError(
                    domain: "ServerUploader",
                    code: -2,
                    userInfo: [NSLocalizedDescriptionKey: "Empty response"]
                )))
                return
            }
            do {
                let decoded = try JSONDecoder().decode(HeartbeatResponse.self, from: data)
                completion(.success(decoded))
            } catch {
                completion(.failure(error))
            }
        }
        task.resume()
    }

    /// Convenience: does this failure warrant a retry with backoff?
    /// `true` for network glitches and 5xx; `false` for 4xx, decoding,
    /// and the defensive `invalidResponse` case.
    static func isTransient(_ error: UploadError) -> Bool {
        return error.isTransient
    }

    /// Standalone calibration snapshot — sent after the user saves a fresh
    /// ArUco or manual-handle calibration so the dashboard can draw the
    /// camera's pose in its 3D canvas immediately, without waiting for a
    /// first pitch upload. `image_width_px` / `image_height_px` come from
    /// the live capture frame dimensions (matches what the ball-detection
    /// pipeline already writes to `IntrinsicsStore`).
    struct CalibrationPayload: Codable {
        let camera_id: String
        let intrinsics: IntrinsicsPayload
        let homography: [Double]
        let image_width_px: Int
        let image_height_px: Int
    }

    /// POST the freshly-solved calibration to the server. Fire-and-forget
    /// from the caller's perspective: failures are logged but don't block
    /// the local UserDefaults write — the phone has already persisted the
    /// values locally, the server-side copy is a convenience for the
    /// dashboard canvas only.
    func postCalibration(
        _ payload: CalibrationPayload,
        completion: ((Result<Void, Error>) -> Void)? = nil
    ) {
        guard let base = config.baseURL() else {
            completion?(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("calibration")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        do {
            request.httpBody = try JSONEncoder().encode(payload)
        } catch {
            completion?(.failure(error))
            return
        }

        let cam = payload.camera_id
        let task = URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                log.error("calibration post failed cam=\(cam, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
                completion?(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                log.error("calibration post failed cam=\(cam, privacy: .public) http=\(http.statusCode)")
                completion?(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"]
                )))
                return
            }
            log.info("calibration post ok cam=\(cam, privacy: .public)")
            completion?(.success(()))
        }
        task.resume()
    }

    /// Wire shape for `POST /sync/report`. Mirrors `server/schemas.py`'s
    /// `SyncReport` — any field changes here MUST be reflected there too.
    struct SyncReportPayload: Codable {
        let camera_id: String
        let sync_id: String
        let role: String          // "A" | "B"
        let t_self_s: Double      // mic PTS when own chirp was heard
        let t_from_other_s: Double  // mic PTS when peer's chirp was heard
        let emitted_band: String  // "A" | "B" — rig-config cross-check
        // Optional matched-filter traces (own-band + other-band) for the
        // `/sync` debug plot. Mirrors `SyncTraceSample` / `SyncReport` on
        // the server — both fields optional so a build without trace
        // support still validates.
        let trace_self: [TraceSamplePayload]?
        let trace_other: [TraceSamplePayload]?
    }

    struct TraceSamplePayload: Codable {
        let t: Double
        let peak: Float
        let psr: Float
    }

    /// Upload this phone's mutual-sync matched-filter report. Fire-and-
    /// forget from the controller's perspective: the server waits on both
    /// phones, solves, and publishes Δ via `/status → last_sync`. Retry
    /// on transient failure is the operator's job (re-press the button).
    func postSyncReport(
        _ report: SyncReportPayload,
        completion: ((Result<Void, Error>) -> Void)? = nil
    ) {
        guard let base = config.baseURL() else {
            completion?(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("sync/report")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        do {
            request.httpBody = try JSONEncoder().encode(report)
        } catch {
            completion?(.failure(error))
            return
        }
        let syncId = report.sync_id
        let role = report.role
        let task = URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                log.error("sync report failed sync=\(syncId, privacy: .public) role=\(role, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
                completion?(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                log.error("sync report rejected sync=\(syncId, privacy: .public) role=\(role, privacy: .public) http=\(http.statusCode)")
                completion?(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"]
                )))
                return
            }
            log.info("sync report ok sync=\(syncId, privacy: .public) role=\(role, privacy: .public)")
            completion?(.success(()))
        }
        task.resume()
    }

    /// Push one diagnostic line to the server's mutual-sync log ring.
    /// Fire-and-forget — failures are logged locally via `log.error` but
    /// never surface to the caller, because a dropped log line must not
    /// block the sync flow itself. `detail` is encoded as JSON; keep
    /// values small + primitive (strings, numbers, bools).
    func postSyncLog(
        event: String,
        detail: [String: AnyJSONValue] = [:]
    ) {
        guard let base = config.baseURL() else { return }
        let url = base.appendingPathComponent("sync/log")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // `camera_id` is not available on this struct directly — caller
        // supplies it via a global that ServerUploader doesn't own, so
        // we accept the role out of band by reading UserDefaults here.
        // Single source of truth (SettingsViewController) keeps the role
        // stable; if it's empty we still send so server can log "unknown"
        // rather than swallow the event.
        let cameraId = UserDefaults.standard.string(forKey: "camera_role") ?? ""
        let body: [String: Any] = [
            "camera_id": cameraId,
            "event": event,
            "detail": detail.mapValues { $0.rawValue },
        ]
        do {
            request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [])
        } catch {
            log.error("sync log encode failed event=\(event, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
            return
        }
        URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                log.error("sync log post failed event=\(event, privacy: .public) err=\(error.localizedDescription, privacy: .public)")
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                log.error("sync log rejected event=\(event, privacy: .public) http=\(http.statusCode)")
            }
        }.resume()
    }

    /// Tiny JSON-value wrapper so callers can pass a heterogeneous dict
    /// (`["peak": .double(0.42), "band": .string("A")]`) without fighting
    /// Swift's `Any` type inference on `[String: Any]` literals.
    enum AnyJSONValue {
        case string(String)
        case int(Int)
        case double(Double)
        case bool(Bool)
        var rawValue: Any {
            switch self {
            case .string(let v): return v
            case .int(let v): return v
            case .double(let v): return v
            case .bool(let v): return v
            }
        }
    }

    func fetchStatus(completion: @escaping (Result<[String: Any], Error>) -> Void) {
        guard let base = config.baseURL() else {
            completion(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("status")

        let task = URLSession.shared.dataTask(with: url) { data, response, error in
            if let error = error {
                completion(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                completion(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"]
                )))
                return
            }
            guard let data else {
                completion(.failure(NSError(
                    domain: "ServerUploader",
                    code: -2,
                    userInfo: [NSLocalizedDescriptionKey: "Empty response"]
                )))
                return
            }
            do {
                let obj = try JSONSerialization.jsonObject(with: data, options: [])
                if let dict = obj as? [String: Any] {
                    completion(.success(dict))
                } else {
                    completion(.failure(NSError(
                        domain: "ServerUploader",
                        code: -3,
                        userInfo: [NSLocalizedDescriptionKey: "Invalid JSON shape"]
                    )))
                }
            } catch {
                completion(.failure(error))
            }
        }
        task.resume()
    }
}

extension ServerUploader.UploadError {
    /// `true` for failures that a caller should retry with backoff:
    /// transient network issues and 5xx responses. `false` for 4xx
    /// (request is the problem), decoding (body shape is the problem),
    /// and `invalidResponse` (no HTTPURLResponse came back at all).
    var isTransient: Bool {
        switch self {
        case .network: return true
        case .server: return true
        case .client, .decoding, .invalidResponse: return false
        }
    }
}
