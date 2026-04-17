import Foundation

/// Persists pitch payload JSON files locally until upload succeeds. When the
/// current sync mode is "audio", each cycle also has a sidecar .wav of the
/// same base filename living in the same directory — the store treats the two
/// as an atomic pair (list / load / delete stay in sync).
final class PitchPayloadStore {
    private let directoryURL: URL
    private let encoder = JSONEncoder()

    init(directoryName: String = "pitch_payloads") {
        let base = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        directoryURL = base.appendingPathComponent(directoryName, isDirectory: true)
    }

    func ensureDirectory() throws {
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
    }

    /// Save the JSON payload and, if provided, the WAV sidecar alongside it.
    /// The audio file is moved (not copied) into the store to avoid leaving
    /// duplicates in the source (typically the tmp dir from AudioRecorder).
    @discardableResult
    func save(_ payload: ServerUploader.PitchPayload, audioFileURL: URL? = nil) throws -> URL {
        try ensureDirectory()
        let filename = String(
            format: "pitch_%06d_%lld.json",
            payload.cycle_number,
            Int64(Date().timeIntervalSince1970 * 1000.0)
        )
        let jsonURL = directoryURL.appendingPathComponent(filename)
        let data = try encoder.encode(payload)
        try data.write(to: jsonURL, options: .atomic)

        if let audioFileURL, FileManager.default.fileExists(atPath: audioFileURL.path) {
            let sidecarURL = audioSidecarURL(for: jsonURL)
            try? FileManager.default.removeItem(at: sidecarURL)
            try FileManager.default.moveItem(at: audioFileURL, to: sidecarURL)
        }
        return jsonURL
    }

    func listPayloadFiles() throws -> [URL] {
        try ensureDirectory()
        let files = try FileManager.default.contentsOfDirectory(
            at: directoryURL,
            includingPropertiesForKeys: [.contentModificationDateKey],
            options: [.skipsHiddenFiles]
        )
        return files
            .filter { $0.pathExtension.lowercased() == "json" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    func load(_ fileURL: URL) throws -> ServerUploader.PitchPayload {
        let data = try Data(contentsOf: fileURL)
        return try JSONDecoder().decode(ServerUploader.PitchPayload.self, from: data)
    }

    /// Returns the audio sidecar URL for a payload JSON URL, or nil if no WAV
    /// exists next to it (e.g. payload saved in flash/mac mode).
    func audioURL(for fileURL: URL) -> URL? {
        let sidecar = audioSidecarURL(for: fileURL)
        return FileManager.default.fileExists(atPath: sidecar.path) ? sidecar : nil
    }

    func delete(_ fileURL: URL) {
        try? FileManager.default.removeItem(at: fileURL)
        try? FileManager.default.removeItem(at: audioSidecarURL(for: fileURL))
    }

    private func audioSidecarURL(for jsonURL: URL) -> URL {
        jsonURL.deletingPathExtension().appendingPathExtension("wav")
    }
}
