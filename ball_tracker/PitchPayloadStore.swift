import Foundation

/// Persists pitch payload JSON files locally until upload succeeds. A
/// completed cycle optionally has a companion H.264 .mov clip stored with
/// the same basename so both travel together through the upload queue.
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

    /// Write the payload JSON to disk. If a video clip is supplied, move it
    /// (preserving the fileURL's extension) alongside with the same basename.
    /// Callers pass the writer's tmp URL; on success we take ownership and
    /// any earlier copy at the destination is overwritten.
    ///
    /// Atomicity: the video is placed FIRST, JSON last. JSON existing on disk
    /// is the "commit" signal the upload path keys off, so if JSON write
    /// throws we roll back the video placement. If video placement fails but
    /// JSON succeeds we continue (video is optional in the upload path) —
    /// and any partial destination file from a torn cross-volume move is
    /// cleaned up before we decide there's no video.
    @discardableResult
    func save(
        _ payload: ServerUploader.PitchPayload,
        videoURL: URL? = nil
    ) throws -> URL {
        try ensureDirectory()
        // Filename combines the server-minted session_id with a millisecond
        // timestamp so retried uploads for the same session don't clobber
        // each other on disk (the latest attempt wins on upload).
        let basename = String(
            format: "session_%@_%lld",
            payload.session_id,
            Int64(Date().timeIntervalSince1970 * 1000.0)
        )
        let jsonURL = directoryURL.appendingPathComponent("\(basename).json")
        let data = try encoder.encode(payload)

        var placedVideoURL: URL? = nil
        if let videoURL {
            let ext = videoURL.pathExtension.isEmpty ? "mov" : videoURL.pathExtension
            let destVideoURL = directoryURL.appendingPathComponent("\(basename).\(ext)")
            try? FileManager.default.removeItem(at: destVideoURL)
            do {
                try FileManager.default.moveItem(at: videoURL, to: destVideoURL)
                placedVideoURL = destVideoURL
            } catch {
                // moveItem can leave a partial file at the destination on a
                // cross-volume failure. Clean it up before falling back so
                // the retry starts from a known-clean state.
                try? FileManager.default.removeItem(at: destVideoURL)
                if (try? FileManager.default.copyItem(at: videoURL, to: destVideoURL)) != nil {
                    try? FileManager.default.removeItem(at: videoURL)
                    placedVideoURL = destVideoURL
                }
                // If both failed: no video, but JSON upload still works.
            }
        }

        do {
            try data.write(to: jsonURL, options: .atomic)
        } catch {
            // JSON is the commit signal; without it the video would be an
            // undiscoverable orphan. Roll back so the save is all-or-nothing.
            if let placedVideoURL {
                try? FileManager.default.removeItem(at: placedVideoURL)
            }
            throw error
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

    /// Return the companion video URL for a JSON payload file if one exists
    /// on disk. Matches any extension (`.mov`, `.mp4`) against the shared
    /// basename; returns the first hit.
    func videoURL(forPayload jsonURL: URL) -> URL? {
        let basename = jsonURL.deletingPathExtension().lastPathComponent
        let candidates = ["mov", "mp4", "m4v"]
        for ext in candidates {
            let candidate = directoryURL.appendingPathComponent("\(basename).\(ext)")
            if FileManager.default.fileExists(atPath: candidate.path) {
                return candidate
            }
        }
        return nil
    }

    /// Remove the JSON payload and any companion video clip in one shot.
    func delete(_ fileURL: URL) {
        if let video = videoURL(forPayload: fileURL) {
            try? FileManager.default.removeItem(at: video)
        }
        try? FileManager.default.removeItem(at: fileURL)
    }

    /// A fresh temp URL suitable for handing to `ClipRecorder`. Lives under
    /// the app's tmp dir so AVAssetWriter can drop failed writes without
    /// polluting Documents; successful clips are moved by `save`.
    func makeTempVideoURL() -> URL {
        let name = "clip_\(UUID().uuidString).mov"
        return FileManager.default.temporaryDirectory.appendingPathComponent(name)
    }
}
