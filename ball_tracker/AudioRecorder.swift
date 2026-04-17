import AVFoundation
import Foundation

/// Records audio from an `AVCaptureAudioDataOutput` to a mono 16-bit PCM WAV
/// file. Used for server-side cross-correlation time sync: A and B both record
/// the full cycle's audio; server correlates the two to recover the clock
/// offset to <1 ms precision.
///
/// Threading: the owner must install this as the output's sample-buffer
/// delegate with `deliveryQueue` as the queue. All internal state is accessed
/// on that serial queue, so no locks are required.
final class AudioRecorder: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    /// Pass this to `AVCaptureAudioDataOutput.setSampleBufferDelegate(_:queue:)`.
    let deliveryQueue = DispatchQueue(label: "audio.recorder.queue")

    // State — accessed only on `deliveryQueue`.
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var sessionStarted = false

    /// Begin writing a new WAV file. Any existing file at the URL is replaced.
    /// First delivered sample buffer's PTS becomes the session start; audio is
    /// written with its native capture sample rate preserved via AVAssetWriter.
    func startRecording(to fileURL: URL) {
        deliveryQueue.async { [weak self] in
            guard let self else { return }
            self.writer?.cancelWriting()
            self.writer = nil
            self.input = nil
            self.sessionStarted = false

            try? FileManager.default.removeItem(at: fileURL)
            guard let w = try? AVAssetWriter(outputURL: fileURL, fileType: .wav) else { return }
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: 44100.0,
                AVNumberOfChannelsKey: 1,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsFloatKey: false,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false,
            ]
            let i = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
            i.expectsMediaDataInRealTime = true
            guard w.canAdd(i) else { return }
            w.add(i)
            self.writer = w
            self.input = i
        }
    }

    /// Finalize the WAV file. `completion` fires on an internal AVFoundation
    /// queue — the caller is responsible for bouncing to main if needed.
    func stopRecording(completion: @escaping (Result<URL, Error>) -> Void) {
        deliveryQueue.async { [weak self] in
            guard let self, let w = self.writer, let i = self.input else {
                completion(.failure(NSError(
                    domain: "AudioRecorder",
                    code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "not recording"]
                )))
                return
            }
            self.writer = nil
            self.input = nil
            self.sessionStarted = false

            // If session never started (no sample buffers received), finishWriting
            // will produce an empty file or error — handle both as failure.
            if w.status != .writing {
                w.cancelWriting()
                completion(.failure(NSError(
                    domain: "AudioRecorder",
                    code: 2,
                    userInfo: [NSLocalizedDescriptionKey: "no samples captured"]
                )))
                return
            }
            i.markAsFinished()
            w.finishWriting {
                if let err = w.error {
                    completion(.failure(err))
                } else {
                    completion(.success(w.outputURL))
                }
            }
        }
    }

    /// Discard current recording without finalizing.
    func cancelRecording() {
        deliveryQueue.async { [weak self] in
            guard let self else { return }
            self.writer?.cancelWriting()
            self.writer = nil
            self.input = nil
            self.sessionStarted = false
        }
    }

    // MARK: - AVCaptureAudioDataOutputSampleBufferDelegate

    func captureOutput(
        _ output: AVCaptureOutput,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection
    ) {
        // Runs on deliveryQueue (installed by the caller).
        guard let w = writer, let i = input else { return }

        if !sessionStarted {
            let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
            guard w.startWriting() else { return }
            w.startSession(atSourceTime: pts)
            sessionStarted = true
        }

        guard w.status == .writing, i.isReadyForMoreMediaData else { return }
        i.append(sampleBuffer)
    }
}
