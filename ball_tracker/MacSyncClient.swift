import Foundation

/// NTP-style clock offset estimator between this iPhone and the Mac server.
///
/// Algorithm:
///   - Fire POST /sync/time every 500 ms, recording phone_t0 (before) and phone_t3 (after).
///   - Server records server_t1 the instant it receives the request and returns it.
///   - RTT = phone_t3 - phone_t0
///   - offset = server_t1 - (phone_t0 + phone_t3) / 2
///     (assumes symmetric path; offset > 0 means server clock is ahead of phone clock)
///   - Keep a rolling window of 10 samples; use the 3 with smallest RTT and take their
///     offset median as currentOffset.
///
/// Thread safety: all mutable state is protected by a serial DispatchQueue.
/// `currentOffset` is safe to read from any queue (capture queue included).
final class MacSyncClient {

    // MARK: - Types

    struct Sample {
        let offset: Double  // server_clock - phone_clock (seconds)
        let rtt: Double     // round-trip time (seconds)
    }

    struct DebugSnapshot {
        let currentOffset: Double?  // nil until first estimate
        let lastRTT: Double?
        let sampleCount: Int
    }

    // MARK: - Configuration

    private let serverBaseURL: URL
    private let sampleWindowSize = 10
    private let bestSampleCount = 3
    private let pollInterval: TimeInterval = 0.5

    // MARK: - State (guarded by serialQueue)

    private let serialQueue = DispatchQueue(label: "mac.sync.client.serial")
    private var samples: [Sample] = []
    private var _currentOffset: Double? = nil
    private var _lastRTT: Double? = nil
    private var timer: DispatchSourceTimer? = nil

    // MARK: - Public read-only accessors (thread-safe)

    /// Best offset estimate, nil until at least `bestSampleCount` samples collected.
    /// Safe to call from any thread/queue.
    var currentOffset: Double? {
        serialQueue.sync { _currentOffset }
    }

    var debugSnapshot: DebugSnapshot {
        serialQueue.sync {
            DebugSnapshot(
                currentOffset: _currentOffset,
                lastRTT: _lastRTT,
                sampleCount: samples.count
            )
        }
    }

    // MARK: - Init

    init(serverBaseURL: URL) {
        self.serverBaseURL = serverBaseURL
    }

    // MARK: - Control

    func startSyncing() {
        serialQueue.async { [weak self] in
            guard let self, self.timer == nil else { return }
            let t = DispatchSource.makeTimerSource(queue: self.serialQueue)
            t.schedule(deadline: .now(), repeating: self.pollInterval)
            t.setEventHandler { [weak self] in self?.fireSyncRequest() }
            t.resume()
            self.timer = t
        }
    }

    func stopSyncing() {
        serialQueue.async { [weak self] in
            self?.timer?.cancel()
            self?.timer = nil
        }
    }

    // MARK: - Internal

    private func fireSyncRequest() {
        // Capture t0 just before network call.
        let phoneT0 = monotonicNow()

        let url = serverBaseURL.appendingPathComponent("sync/time")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 0.4  // shorter than poll interval to avoid pileup

        // Perform the request synchronously on a background thread via a semaphore
        // so we can capture t3 immediately after data arrives.
        // We are already on serialQueue, so use a detached task with semaphore.
        let semaphore = DispatchSemaphore(value: 0)
        var serverT1: Double? = nil
        var phoneT3: Double = phoneT0

        let task = URLSession.shared.dataTask(with: request) { data, _, _ in
            phoneT3 = self.monotonicNow()
            if let data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let st = json["server_time_s"] as? Double {
                serverT1 = st
            }
            semaphore.signal()
        }
        task.resume()

        // Wait up to 450 ms (just under the poll interval).
        let deadline = DispatchTime.now() + .milliseconds(450)
        guard semaphore.wait(timeout: deadline) == .success, let st1 = serverT1 else {
            return  // timeout or parse failure — skip sample
        }

        let rtt = phoneT3 - phoneT0
        let offset = st1 - (phoneT0 + phoneT3) / 2.0

        // Push sample, maintain window.
        samples.append(Sample(offset: offset, rtt: rtt))
        if samples.count > sampleWindowSize {
            samples.removeFirst()
        }
        _lastRTT = rtt
        _currentOffset = computeOffset()
    }

    /// Median offset of the `bestSampleCount` samples with smallest RTT.
    private func computeOffset() -> Double? {
        guard samples.count >= bestSampleCount else { return nil }
        let sorted = samples.sorted { $0.rtt < $1.rtt }
        let best = sorted.prefix(bestSampleCount).map { $0.offset }.sorted()
        let mid = best.count / 2
        return best[mid]
    }

    /// Monotonic clock in seconds. Uses `ProcessInfo.processInfo.systemUptime`
    /// which matches `CACurrentMediaTime()` and is stable across NTP adjustments.
    private func monotonicNow() -> Double {
        return ProcessInfo.processInfo.systemUptime
    }
}
