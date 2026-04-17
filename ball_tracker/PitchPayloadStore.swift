import Foundation

/// Persists pitch payload JSON files locally until upload succeeds.
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

    func save(_ payload: ServerUploader.PitchPayload) throws -> URL {
        try ensureDirectory()
        let filename = String(format: "pitch_%06d_%lld.json", payload.cycle_number, Int64(Date().timeIntervalSince1970 * 1000.0))
        let url = directoryURL.appendingPathComponent(filename)
        let data = try encoder.encode(payload)
        try data.write(to: url, options: .atomic)
        return url
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

    func delete(_ fileURL: URL) {
        try? FileManager.default.removeItem(at: fileURL)
    }
}
