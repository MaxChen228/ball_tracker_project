import Foundation

final class AnalysisJobStore {
    struct Job: Codable {
        enum UploadMode: String, Codable {
            case onDevicePrimary = "on_device_primary"
            case dualSidecar = "dual_sidecar"
        }

        let uploadMode: UploadMode
        let pitch: ServerUploader.PitchPayload
    }

    private let directoryURL: URL
    private let encoder = JSONEncoder()

    init(directoryName: String = "analysis_jobs") {
        let base = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        directoryURL = base.appendingPathComponent(directoryName, isDirectory: true)
    }

    func ensureDirectory() throws {
        try FileManager.default.createDirectory(at: directoryURL, withIntermediateDirectories: true)
    }

    @discardableResult
    func save(_ job: Job, videoURL: URL) throws -> URL {
        try ensureDirectory()
        let basename = String(
            format: "session_%@_%lld",
            job.pitch.session_id,
            Int64(Date().timeIntervalSince1970 * 1000.0)
        )
        let jsonURL = directoryURL.appendingPathComponent("\(basename).json")
        let ext = videoURL.pathExtension.isEmpty ? "mov" : videoURL.pathExtension
        let destVideoURL = directoryURL.appendingPathComponent("\(basename).\(ext)")
        let data = try encoder.encode(job)

        try? FileManager.default.removeItem(at: destVideoURL)
        do {
            try FileManager.default.moveItem(at: videoURL, to: destVideoURL)
        } catch {
            try? FileManager.default.removeItem(at: destVideoURL)
            try FileManager.default.copyItem(at: videoURL, to: destVideoURL)
            try? FileManager.default.removeItem(at: videoURL)
        }

        do {
            try data.write(to: jsonURL, options: .atomic)
        } catch {
            try? FileManager.default.removeItem(at: destVideoURL)
            throw error
        }
        return jsonURL
    }

    func listJobFiles() throws -> [URL] {
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

    func load(_ fileURL: URL) throws -> Job {
        let data = try Data(contentsOf: fileURL)
        return try JSONDecoder().decode(Job.self, from: data)
    }

    func videoURL(forJob jsonURL: URL) -> URL? {
        let basename = jsonURL.deletingPathExtension().lastPathComponent
        for ext in ["mov", "mp4", "m4v"] {
            let candidate = directoryURL.appendingPathComponent("\(basename).\(ext)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
        }
        return nil
    }

    func delete(_ fileURL: URL) {
        if let video = videoURL(forJob: fileURL) {
            try? FileManager.default.removeItem(at: video)
        }
        try? FileManager.default.removeItem(at: fileURL)
    }
}
