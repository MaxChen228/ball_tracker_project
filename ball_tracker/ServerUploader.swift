import Foundation

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
        /// Shared time anchor for A/B pairing, recovered from an audio-chirp
        /// matched-filter hit during the 時間校正 step. `frame_index` has no
        /// meaningful value for an audio anchor (set to 0); the server pairs
        /// by `timestamp_s` alone. Kept separate for legibility and for
        /// potential future anchors (e.g. visual markers).
        let sync_anchor_frame_index: Int
        let sync_anchor_timestamp_s: Double
        let cycle_number: Int
        let frames: [FramePayload]
        let intrinsics: IntrinsicsPayload?
        let homography: [Double]?
        let image_width_px: Int?
        let image_height_px: Int?
    }

    /// Server `/pitch` response summary. Triangulation fields are optional
    /// because they're only populated once both A and B for a cycle arrive.
    struct PitchUploadResponse: Codable {
        let ok: Bool
        let cycle: Int
        let paired: Bool
        let triangulated_points: Int
        let error: String?
        let mean_residual_m: Double?
        let max_residual_m: Double?
        let peak_z_m: Double?
        let duration_s: Double?
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
    func uploadPitch(
        _ pitch: PitchPayload,
        videoURL: URL?,
        completion: ((Result<PitchUploadResponse, Error>) -> Void)? = nil
    ) {
        guard let base = config.baseURL() else {
            completion?(.failure(NSError(
                domain: "ServerUploader",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"]
            )))
            return
        }
        let url = base.appendingPathComponent("pitch")

        let payloadData: Data
        do {
            payloadData = try JSONEncoder().encode(pitch)
        } catch {
            completion?(.failure(error))
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

        let task = URLSession.shared.dataTask(with: request) { data, response, error in
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
            guard let data else {
                completion?(.failure(NSError(
                    domain: "ServerUploader",
                    code: -2,
                    userInfo: [NSLocalizedDescriptionKey: "Empty response"]
                )))
                return
            }
            do {
                let decoded = try JSONDecoder().decode(PitchUploadResponse.self, from: data)
                completion?(.success(decoded))
            } catch {
                completion?(.failure(error))
            }
        }
        task.resume()
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
