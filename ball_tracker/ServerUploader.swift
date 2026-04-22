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
final class ServerUploader: @unchecked Sendable {
    enum DetectionPath: String, Codable, CaseIterable {
        case live = "live"
        case iosPost = "ios_post"
        case serverPost = "server_post"

        var displayLabel: String {
            switch self {
            case .live: return "Live stream"
            case .iosPost: return "iOS post-pass"
            case .serverPost: return "Server post-pass"
            }
        }
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

        /// Return a copy of this payload with `frames` replaced. Used by
        /// the mode-two cycle-complete path to attach the session's
        /// BTDetectionSession output before shipping.
        func withFrames(_ newFrames: [FramePayload]) -> PitchPayload {
            PitchPayload(
                camera_id: camera_id,
                session_id: session_id,
                sync_id: sync_id,
                sync_anchor_timestamp_s: sync_anchor_timestamp_s,
                video_start_pts_s: video_start_pts_s,
                local_recording_index: local_recording_index,
                paths: paths,
                frames: newFrames,
                frames_on_device: frames_on_device,
                capture_telemetry: capture_telemetry
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
                sync_id: sync_id,
                sync_anchor_timestamp_s: sync_anchor_timestamp_s,
                video_start_pts_s: video_start_pts_s,
                local_recording_index: local_recording_index,
                paths: paths,
                frames: frames,
                frames_on_device: newFramesOnDevice,
                capture_telemetry: capture_telemetry
            )
        }

        func withPaths(_ newPaths: [DetectionPath]) -> PitchPayload {
            PitchPayload(
                camera_id: camera_id,
                session_id: session_id,
                sync_id: sync_id,
                sync_anchor_timestamp_s: sync_anchor_timestamp_s,
                video_start_pts_s: video_start_pts_s,
                local_recording_index: local_recording_index,
                paths: newPaths.map(\.rawValue),
                frames: frames,
                frames_on_device: frames_on_device,
                capture_telemetry: capture_telemetry
            )
        }
    }

    struct PitchAnalysisPayload: Codable {
        let camera_id: String
        let session_id: String
        let frames_on_device: [FramePayload]
        let capture_telemetry: CaptureTelemetry?
    }

    struct PitchAnalysisResponse: Codable {
        let ok: Bool
        let session_id: String
        let camera_id: String
        let frames_on_device: Int
        let triangulated_on_device: Int
        let error_on_device: String?
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
        let tracking_exposure_cap: String?
        let chirp_detect_threshold: Double?
        let heartbeat_interval_s: Double?
        let capture_height_px: Int?
        let preview_requested: Bool?
        let calibration_frame_requested: Bool?
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

    func uploadPitchAnalysis(
        _ payload: PitchAnalysisPayload,
        completion: @escaping (Result<PitchAnalysisResponse, UploadError>) -> Void
    ) {
        guard let base = config.baseURL() else {
            completion(.failure(.network(URLError(.badURL))))
            return
        }
        let url = base.appendingPathComponent("pitch_analysis")
        let body: Data
        do {
            body = try JSONEncoder().encode(payload)
        } catch {
            completion(.failure(.decoding(error)))
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let sid = payload.session_id
        let cam = payload.camera_id
        let task = URLSession.shared.uploadTask(with: request, from: body) { data, response, error in
            if let error = error {
                let urlError = (error as? URLError) ?? URLError(.unknown)
                log.error("analysis upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=\(urlError.localizedDescription, privacy: .public) code=\(urlError.code.rawValue)")
                completion(.failure(.network(urlError)))
                return
            }
            guard let http = response as? HTTPURLResponse else {
                completion(.failure(.invalidResponse))
                return
            }
            if !(200...299).contains(http.statusCode) {
                if (500...599).contains(http.statusCode) {
                    completion(.failure(.server(statusCode: http.statusCode, body: data)))
                } else {
                    completion(.failure(.client(statusCode: http.statusCode, body: data)))
                }
                return
            }
            guard let data else {
                completion(.failure(.decoding(URLError(.cannotDecodeContentData))))
                return
            }
            do {
                let decoded = try JSONDecoder().decode(PitchAnalysisResponse.self, from: data)
                log.info("analysis upload ok session=\(sid, privacy: .public) cam=\(cam, privacy: .public) frames=\(decoded.frames_on_device) triangulated=\(decoded.triangulated_on_device)")
                completion(.success(decoded))
            } catch {
                completion(.failure(.decoding(error)))
            }
        }
        task.resume()
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



    struct TimeSyncClaimResponse: Codable {
        let ok: Bool
        let sync_id: String
        let started_at: Double
        let expires_at: Double
    }

    /// Claim the currently-live legacy chirp sync run id from the server,
    /// minting a fresh one when the previous listening window expired.
    /// Both phones calling this within the same window receive the same
    /// `sync_id`, which is what lets their third-device chirp anchors be
    /// proven to belong to the same event.
    func claimTimeSyncIntent(
        completion: @escaping (Result<TimeSyncClaimResponse, Error>) -> Void
    ) {
        guard let base = config.baseURL() else {
            completion(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("sync/claim")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
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
                completion(.success(try JSONDecoder().decode(TimeSyncClaimResponse.self, from: data)))
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

    /// Generic "POST raw bytes as Content-Type: image/jpeg" used by
    /// `PreviewUploader` (Phase 4a). Not tied to a specific endpoint —
    /// the caller provides the path so this can grow to support other
    /// tiny-body binary POSTs without a second copy of this plumbing.
    /// Fire-and-forget from the caller's POV: preview is transient, we
    /// don't retry a dropped frame.
    func postRawJPEG(
        path: String,
        jpeg: Data,
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
        let url = base.appendingPathComponent(path.hasPrefix("/") ? String(path.dropFirst()) : path)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.httpBody = jpeg
        let task = URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                completion?(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                completion?(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"]
                )))
                return
            }
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
        // Nullable: null when aborted without that band's detection. Server
        // routes any-null reports to the diagnostic (aborted) path.
        let t_self_s: Double?
        let t_from_other_s: Double?
        let emitted_band: String  // "A" | "B" — rig-config cross-check
        // Optional matched-filter traces (own-band + other-band) for the
        // `/sync` debug plot. Mirrors `SyncTraceSample` / `SyncReport` on
        // the server — both fields optional so a build without trace
        // support still validates.
        let trace_self: [TraceSamplePayload]?
        let trace_other: [TraceSamplePayload]?
        // Abort telemetry. `aborted=true` means the phone gave up before
        // both bands fired — the report still ships to give the server
        // post-mortem data (sub-threshold peaks, noise floor, which band
        // fired, timing).
        let aborted: Bool
        let abort_reason: String?
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
    /// Metadata ferried alongside a raw-PCM WAV to `/sync/audio_upload`.
    /// Phase A of the sync refactor — iOS is a dumb recorder; server
    /// runs the matched filter and fills in the `SyncReport` shape from
    /// the WAV + these fields.
    struct SyncAudioUploadMeta: Encodable {
        let sync_id: String
        let camera_id: String
        let role: String
        /// Host-clock seconds of the first recorded sample. Server uses
        /// this to convert its detected chirp sample offsets back into
        /// iOS session-clock PTS (same clock as video frame PTS).
        let audio_start_pts_s: Double
        /// Informational — WAV header is authoritative on decode.
        let sample_rate: Int
        /// Host-clock seconds at which the local player node was
        /// instructed to play this run's chirp. Optional; debug cross-
        /// check only — the server compares it against its detected
        /// self-chirp center to diagnose "did we emit when planned?".
        let emission_pts_s: Double?
    }

    /// Ship the full 3 s listening window to the server for detection.
    /// Multipart body: `payload` (JSON metadata) + `audio` (WAV bytes).
    /// ~288 KB at 48 kHz × 3 s × 16-bit mono — fits in RAM cleanly,
    /// no streaming-upload plumbing needed.
    func uploadSyncAudio(
        meta: SyncAudioUploadMeta,
        wavData: Data,
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
        let url = base.appendingPathComponent("sync/audio_upload")

        let metaData: Data
        do {
            metaData = try JSONEncoder().encode(meta)
        } catch {
            completion?(.failure(error))
            return
        }

        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )

        var body = Data()
        func appendLine(_ s: String) {
            if let d = (s + "\r\n").data(using: .utf8) { body.append(d) }
        }
        // payload field (JSON string)
        appendLine("--\(boundary)")
        appendLine("Content-Disposition: form-data; name=\"payload\"")
        appendLine("Content-Type: application/json")
        appendLine("")
        body.append(metaData)
        appendLine("")
        // audio field (WAV file)
        appendLine("--\(boundary)")
        appendLine(
            "Content-Disposition: form-data; name=\"audio\"; "
            + "filename=\"\(meta.sync_id)_\(meta.camera_id).wav\""
        )
        appendLine("Content-Type: audio/wav")
        appendLine("")
        body.append(wavData)
        appendLine("")
        appendLine("--\(boundary)--")

        request.httpBody = body
        let syncId = meta.sync_id
        let role = meta.role
        let wavBytes = wavData.count
        let task = URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                log.error("sync audio upload failed sync=\(syncId, privacy: .public) role=\(role, privacy: .public) bytes=\(wavBytes) err=\(error.localizedDescription, privacy: .public)")
                completion?(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                log.error("sync audio upload rejected sync=\(syncId, privacy: .public) role=\(role, privacy: .public) http=\(http.statusCode)")
                completion?(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode)"]
                )))
                return
            }
            log.info("sync audio upload ok sync=\(syncId, privacy: .public) role=\(role, privacy: .public) bytes=\(wavBytes)")
            completion?(.success(()))
        }
        task.resume()
    }

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
        // Sync logs still need a camera id even though they are emitted
        // outside the normal pitch payload path.
        let cameraId = AppSettingsStore.load().cameraRole
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
