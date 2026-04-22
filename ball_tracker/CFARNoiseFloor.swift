import Foundation

/// Rolling noise-floor estimator for CFAR-style fire gates.
///
/// CFAR (Constant False Alarm Rate) is the standard radar / sonar approach:
/// instead of comparing a matched-filter peak against a FIXED absolute
/// threshold, compare it against a MULTIPLE of the recent background
/// noise level measured from the same stream. This makes the fire gate
/// scale-invariant — a rig where everything runs 10× louder doesn't need
/// threshold re-tuning, because the noise floor moves with it.
///
/// Used by `AudioChirpDetector` (quick-chirp fallback path). Mutual
/// sync used to share this estimator via the now-deleted
/// `AudioSyncDetector`; Phase A moved mutual detection to the server,
/// so only the quick-chirp caller remains on iOS. Each detector band
/// gets its own instance.
///
/// The key subtlety: feed only scans that are NOT a real chirp into the
/// estimator, otherwise the chirp's own peak bumps the floor up and
/// subsequent scans under-fire. `saturationCap` is the simple heuristic
/// — any observed peak above it is assumed to be signal (or a known
/// startup artefact above real noise levels) and skipped.
///
/// Thread-safety: this is a value type. Callers serialize access on their
/// own queue — there are no locks here.
struct CFARNoiseFloor {
    private var samples: [Float] = []
    private let windowSize: Int
    private let percentile: Float
    private let saturationCap: Float

    /// - windowSize: how many recent non-chirp scan values to keep. At a
    ///   33 ms scan interval, 30 samples ≈ 1 s of recent audio history,
    ///   long enough to track AGC / ambient drift but short enough that
    ///   the estimator re-calibrates after a transient.
    /// - percentile: which order statistic to report as the "noise
    ///   level". 0.75 = p75 is robust against the occasional transient
    ///   click while staying responsive.
    /// - saturationCap: scans whose peak exceeds this are assumed to be
    ///   a real detection (or a big non-noise artefact) and excluded
    ///   from the floor estimate. Sized at ~1.5× the typical quiet-room
    ///   noise peak to exclude chirps but still let real noise land.
    init(
        windowSize: Int = 30,
        percentile: Float = 0.75,
        saturationCap: Float = 0.15
    ) {
        self.windowSize = windowSize
        self.percentile = percentile
        self.saturationCap = saturationCap
        self.samples.reserveCapacity(windowSize)
    }

    mutating func observe(_ peak: Float) {
        // Skip the chirp itself + any artefact that's obviously not ambient
        // noise. The sync plot still sees every raw peak (trace buffer is
        // separate); this is purely for the fire gate's noise estimate.
        if peak > saturationCap { return }
        samples.append(peak)
        if samples.count > windowSize {
            samples.removeFirst(samples.count - windowSize)
        }
    }

    /// Current p75 of the stored non-chirp peaks. 0 before we've seen any
    /// sample — callers should pair the estimate with an absolute-floor
    /// safety net so the early-boot "no history yet" case doesn't
    /// degenerate into a zero-threshold free-fire zone.
    var estimate: Float {
        guard !samples.isEmpty else { return 0 }
        // Small N: just sort; the window is capped at 30-ish so this is
        // O(n log n) on a tiny array, well under a millisecond.
        let sorted = samples.sorted()
        let idx = min(sorted.count - 1, Int(Float(sorted.count) * percentile))
        return sorted[idx]
    }

    /// Number of non-chirp samples currently in the window. Used for
    /// "warm-up" gating — the estimate isn't trustworthy with 1-2
    /// samples, so callers require a minimum sample count before
    /// letting the CFAR gate fire.
    var sampleCount: Int { samples.count }

    mutating func reset() {
        samples.removeAll(keepingCapacity: true)
    }
}
