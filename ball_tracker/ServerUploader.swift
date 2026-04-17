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

    func uploadPitch(_ pitch: PitchPayload, completion: ((Result<PitchUploadResponse, Error>) -> Void)? = nil) {
        guard let base = config.baseURL() else {
            completion?(.failure(NSError(domain: "ServerUploader", code: -1, userInfo: [NSLocalizedDescriptionKey: "Invalid server URL"])))
            return
        }
        let url = base.appendingPathComponent("pitch")

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        do {
            let body = try JSONEncoder().encode(pitch)
            request.httpBody = body
        } catch {
            completion?(.failure(error))
            return
        }

        // Spec: do not use background upload; use foreground upload for low latency.
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                completion?(.failure(error))
                return
            }
            if let http = response as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
                let e = NSError(domain: "ServerUploader", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: "HTTP status \(http.statusCode)"])
                completion?(.failure(e))
                return
            }
            guard let data else {
                completion?(.failure(NSError(domain: "ServerUploader", code: -2, userInfo: [NSLocalizedDescriptionKey: "Empty response"])))
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

