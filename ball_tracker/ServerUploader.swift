import Foundation
import os

private let log = Logger(subsystem: "com.Max0228.ball-tracker", category: "network")

/// iOS uploader responsible for sending one iPhone pitch payload to the server.
///
/// Wire format:
///   POST http://{server_ip}:{port}/pitch
///   multipart/form-data with two parts:
///     - `payload`: JSON-encoded `PitchPayload` (required)
///     - `video`:   H.264/MOV clip of the cycle (optional; Phase 1 raw-video
///                  experiment — server stores the file but still relies on
///                  the JSON payload for triangulation)
final class ServerUploader {
    struct FramePayload: Codable {
        let frame_index: Int
        let timestamp_s: Double
        let theta_x_rad: Double?
        let theta_z_rad: Double?
        /// Raw (distorted) ball pixel coords. When present and paired with
        /// `intrinsics.distortion`, the server undistorts these for
        /// triangulation. Nil when no ball was detected.
        let px: Double?
        let py: Double?
        let ball_detected: Bool
    }

    struct IntrinsicsPayload: Codable {
        let fx: Double
        let fz: Double
        let cx: Double
        let cy: Double
        /// OpenCV 5-coefficient distortion `[k1, k2, p1, p2, k3]`. Nil when
        /// no ChArUco calibration has been imported for this camera.
        let distortion: [Double]?
    }

    struct PitchPayload: Codable {
        let camera_id: String
        /// Server-minted pairing key from `POST /sessions/arm`. A/B pairs
        /// by this alone — iPhones no longer mint any pairing identifier.
        /// Pattern: `s_` + 4–16 hex chars (matches the server regex).
        let session_id: String
        /// Shared time anchor for A/B pairing, recovered from an audio-chirp
        /// matched-filter hit during the 時間校正 step. `frame_index` has no
        /// meaningful value for an audio anchor (set to 0); the server pairs
        /// by `timestamp_s` alone. Kept separate for legibility and for
        /// potential future anchors (e.g. visual markers).
        let sync_anchor_frame_index: Int
        let sync_anchor_timestamp_s: Double
        /// Device-local recording counter, for operator debugging only —
        /// server doesn't pair on it. Optional so a phone can omit it.
        let local_recording_index: Int?
        let frames: [FramePayload]
        let intrinsics: IntrinsicsPayload?
        let homography: [Double]?
        let image_width_px: Int?
        let image_height_px: Int?
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
        let end_reason: String?
    }

    /// Response body shared by `POST /heartbeat` and `GET /status`.
    /// `commands[self.camera_id]` drives the dashboard-remote arm/disarm
    /// flow on iPhone.
    struct HeartbeatResponse: Codable {
        let state: String?
        let devices: [HeartbeatDevice]?
        let session: HeartbeatSession?
        let commands: [String: String]?
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

        let videoData: Data?
        if let videoURL {
            videoData = try? Data(contentsOf: videoURL)
        } else {
            videoData = nil
        }

        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = Self.makeMultipartBody(
            boundary: boundary,
            payloadJSON: payloadData,
            videoFilename: videoURL?.lastPathComponent ?? "clip.mov",
            videoData: videoData
        )

        let sid = pitch.session_id
        let cam = pitch.camera_id
        let bytes = payloadData.count + (videoData?.count ?? 0)

        let task = URLSession.shared.dataTask(with: request) { data, response, error in
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
                log.info("upload ok session=\(sid, privacy: .public) cam=\(cam, privacy: .public) bytes=\(bytes) paired=\(decoded.paired) points=\(decoded.triangulated_points)")
                completion(.success(decoded))
            } catch {
                log.error("upload failed session=\(sid, privacy: .public) cam=\(cam, privacy: .public) err=\(error.localizedDescription, privacy: .public) category=decoding")
                completion(.failure(.decoding(error)))
            }
        }
        task.resume()
    }

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

    private static func makeMultipartBody(
        boundary: String,
        payloadJSON: Data,
        videoFilename: String,
        videoData: Data?
    ) -> Data {
        var body = Data()
        func appendString(_ s: String) {
            if let d = s.data(using: .utf8) { body.append(d) }
        }

        // JSON part.
        appendString("--\(boundary)\r\n")
        appendString("Content-Disposition: form-data; name=\"payload\"\r\n")
        appendString("Content-Type: application/json\r\n\r\n")
        body.append(payloadJSON)
        appendString("\r\n")

        // Optional video part.
        if let videoData {
            appendString("--\(boundary)\r\n")
            appendString(
                "Content-Disposition: form-data; name=\"video\"; filename=\"\(videoFilename)\"\r\n"
            )
            appendString("Content-Type: video/quicktime\r\n\r\n")
            body.append(videoData)
            appendString("\r\n")
        }

        appendString("--\(boundary)--\r\n")
        return body
    }

    /// POST `/sessions/arm` and decode the returned session id. Used by
    /// the iPhone's local "啟動追蹤" escape-hatch button: normally the
    /// dashboard does the arming and the id flows in via the next
    /// heartbeat, but a phone tapping the local button needs the id
    /// synchronously so its first detected-ball frame lands with the
    /// correct `session_id` stamped on it.
    func armSession(
        completion: @escaping (Result<HeartbeatSession, Error>) -> Void
    ) {
        guard let base = config.baseURL() else {
            completion(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("sessions/arm")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                log.error("arm failed err=\(error.localizedDescription, privacy: .public)")
                completion(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                log.error("arm failed err=http_\(http.statusCode)")
                completion(.failure(NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"]
                )))
                return
            }
            guard let data else {
                log.error("arm failed err=empty_body")
                completion(.failure(NSError(
                    domain: "ServerUploader",
                    code: -2,
                    userInfo: [NSLocalizedDescriptionKey: "Empty response"]
                )))
                return
            }
            do {
                struct Wrapper: Codable { let ok: Bool; let session: HeartbeatSession }
                let wrapper = try JSONDecoder().decode(Wrapper.self, from: data)
                log.info("arm ok session=\(wrapper.session.id, privacy: .public)")
                completion(.success(wrapper.session))
            } catch {
                log.error("arm failed err=\(error.localizedDescription, privacy: .public) category=decoding")
                completion(.failure(error))
            }
        }
        task.resume()
    }

    /// POST a 1 Hz liveness ping and read back the full status payload —
    /// the server piggy-backs `devices`, `session`, and per-camera
    /// `commands` on every reply, so one round-trip drives both online
    /// presence and remote arm/disarm dispatch.
    func sendHeartbeat(
        cameraId: String,
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
        do {
            request.httpBody = try JSONEncoder().encode(["camera_id": cameraId])
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
