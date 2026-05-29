import Foundation
import os

private let pendingLog = Logger(subsystem: "com.Max0228.ball-tracker", category: "intrinsics.pending")

/// On-device persistence for a freshly-solved ChArUco intrinsics record
/// that hasn't yet been confirmed-200 by the server. Written immediately
/// after `solveWithImageWidth:` succeeds, deleted only after the POST
/// returns 200. If the operator loses network mid-flow, re-entering the
/// IntrinsicsVC offers "retry upload" instead of forcing a full re-shoot.
///
/// File: `Documents/intrinsics-pending.json`. Single record — solving
/// while a pending record exists overwrites it (the new shoot is presumed
/// fresher/better than a stale failure).
struct IntrinsicsPendingRecord: Codable {
    let deviceId: String
    let deviceModel: String
    let sourceWidthPx: Int
    let sourceHeightPx: Int
    let fx: Double
    let fy: Double
    let cx: Double
    let cy: Double
    let distortion: [Double]      // exactly 5: k1, k2, p1, p2, k3
    let rmsReprojectionPx: Double
    let nImages: Int
    let calibratedAt: TimeInterval
    let sourceLabel: String        // e.g. "ios-charuco-v1-lens0.83"
}

enum IntrinsicsPendingCache {
    private static var url: URL {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return docs.appendingPathComponent("intrinsics-pending.json")
    }

    static func write(_ record: IntrinsicsPendingRecord) {
        do {
            let data = try JSONEncoder().encode(record)
            try data.write(to: url, options: .atomic)
            pendingLog.info("intrinsics-pending written rms=\(record.rmsReprojectionPx, privacy: .public) n=\(record.nImages, privacy: .public)")
        } catch {
            pendingLog.error("intrinsics-pending write failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    static func read() -> IntrinsicsPendingRecord? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do {
            let data = try Data(contentsOf: url)
            return try JSONDecoder().decode(IntrinsicsPendingRecord.self, from: data)
        } catch {
            pendingLog.error("intrinsics-pending read failed: \(error.localizedDescription, privacy: .public)")
            return nil
        }
    }

    static func clear() {
        try? FileManager.default.removeItem(at: url)
    }
}
