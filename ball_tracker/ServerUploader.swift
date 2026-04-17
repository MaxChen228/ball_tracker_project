import Foundation

/// iOS uploader responsible for sending one iPhone pitch payload to the server.
/// Spec:
///   POST http://{server_ip}:{port}/pitch
///   body is JSON:
///     { camera_id, flash_frame_index, flash_timestamp_s, cycle_number, frames[] }
final class ServerUploader {
    struct FramePayload: Codable {
        let frame_index: Int
        let timestamp_s: Double
        let theta_x_rad: Double?
        let theta_z_rad: Double?
        let ball_detected: Bool
    }

    struct IntrinsicsPayload: Codable {
        let fx: Double
        let fz: Double
        let cx: Double
        let cy: Double
    }

    struct PitchPayload: Codable {
        let camera_id: String
        let flash_frame_index: Int
        let flash_timestamp_s: Double
        let cycle_number: Int
        let frames: [FramePayload]
        let intrinsics: IntrinsicsPayload?
        let homography: [Double]?
        let image_width_px: Int?
        let image_height_px: Int?
        /// Session-clock timestamp of the first audio sample in the sidecar
        /// WAV, when syncMode == "audio". Server combines this with the
        /// measured sample-lag from cross-correlation to recover A↔B offset.
        let audio_start_ts_s: Double?
        /// Mac-sync clock offset: phone_monotonic_clock - server_monotonic_clock (seconds).
        /// Server applies: aligned_ts = frame.timestamp_s + mac_clock_offset_s
        /// to convert frame timestamps from phone clock to server clock.
        /// Populated only when sync_mode == "mac" and MacSyncClient has a valid estimate.
        let mac_clock_offset_s: Double?
    }

    /// Server /pitch response summary. All triangulation fields are optional because
    /// they're only populated when both A and B have been received for the cycle.
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
        let sync_method: String?      // "flash" | "audio" | "mac" (server-reported)
        let audio_offset_s: Double?   // audio-sync measured B-clock offset in seconds
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

    /// Upload one pitch payload to the server.
    ///
    /// When `audioFileURL` is non-nil, POSTs `multipart/form-data` with a
    /// `payload` part (JSON) and an `audio` part (WAV bytes). Otherwise still
    /// sends multipart with just the `payload` part — the server accepts both.
    func uploadPitch(
        _ pitch: PitchPayload,
        audioFileURL: URL? = nil,
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

        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue(
            "multipart/form-data; boundary=\(boundary)",
            forHTTPHeaderField: "Content-Type"
        )
        request.httpBody = Self.buildMultipartBody(
            boundary: boundary,
            payloadJSON: payloadData,
            audioFileURL: audioFileURL
        )

        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                completion?(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                let e = NSError(
                    domain: "ServerUploader",
                    code: http.statusCode,
                    userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"]
                )
                completion?(.failure(e))
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

    private static func buildMultipartBody(
        boundary: String,
        payloadJSON: Data,
        audioFileURL: URL?
    ) -> Data {
        var body = Data()
        let boundaryLine = "--\(boundary)\r\n".data(using: .utf8)!
        let terminator = "\r\n".data(using: .utf8)!

        body.append(boundaryLine)
        body.append("Content-Disposition: form-data; name=\"payload\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: application/json\r\n\r\n".data(using: .utf8)!)
        body.append(payloadJSON)
        body.append(terminator)

        if let audioFileURL,
           FileManager.default.fileExists(atPath: audioFileURL.path),
           let audioData = try? Data(contentsOf: audioFileURL) {
            let filename = audioFileURL.lastPathComponent
            body.append(boundaryLine)
            body.append("Content-Disposition: form-data; name=\"audio\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
            body.append("Content-Type: audio/wav\r\n\r\n".data(using: .utf8)!)
            body.append(audioData)
            body.append(terminator)
        }

        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        return body
    }

    func fetchStatus(completion: @escaping (Result<[String: Any], Error>) -> Void) {
        guard let base = config.baseURL() else {
            completion(.failure(NSError(domain: "ServerUploader", code: -1, userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"])))
            return
        }
        let url = base.appendingPathComponent("status")

        let task = URLSession.shared.dataTask(with: url) { data, response, error in
            if let error = error {
                completion(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                let e = NSError(domain: "ServerUploader", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"])
                completion(.failure(e))
                return
            }
            guard let data else {
                completion(.failure(NSError(domain: "ServerUploader", code: -2, userInfo: [NSLocalizedDescriptionKey: "Empty response"])))
                return
            }
            do {
                let obj = try JSONSerialization.jsonObject(with: data, options: [])
                if let dict = obj as? [String: Any] {
                    completion(.success(dict))
                } else {
                    completion(.failure(NSError(domain: "ServerUploader", code: -3, userInfo: [NSLocalizedDescriptionKey: "Invalid JSON shape"])))
                }
            } catch {
                completion(.failure(error))
            }
        }
        task.resume()
    }
}

