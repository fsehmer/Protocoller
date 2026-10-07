import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit
import Darwin

enum CaptureError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let text): return text }
    }
}

final class StopFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func stop() { lock.lock(); value = true; lock.unlock() }
    var stopped: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

// Each source has its own file. Both use the capture clock in the manifest.
final class AudioTrack {
    let writer: AVAssetWriter
    var input: AVAssetWriterInput?
    var firstPTS: Double?
    var lastEnd: Double?
    var sampleCount = 0
    let path: String

    init(directory: URL, name: String) throws {
        path = "\(name).m4a"
        writer = try AVAssetWriter(outputURL: directory.appendingPathComponent(path), fileType: .m4a)
    }

    func append(_ sample: CMSampleBuffer) throws {
        guard CMSampleBufferIsValid(sample), CMSampleBufferDataIsReady(sample),
              CMSampleBufferGetNumSamples(sample) > 0,
              let description = CMSampleBufferGetFormatDescription(sample),
              let format = CMAudioFormatDescriptionGetStreamBasicDescription(description) else { return }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        if input == nil {
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: format.pointee.mSampleRate,
                AVNumberOfChannelsKey: Int(format.pointee.mChannelsPerFrame),
                AVEncoderBitRateKey: 128000
            ]
            let newInput = AVAssetWriterInput(mediaType: .audio, outputSettings: settings)
            newInput.expectsMediaDataInRealTime = true
            guard writer.canAdd(newInput) else { throw CaptureError.message("Cannot encode audio track") }
            writer.add(newInput)
            guard writer.startWriting() else { throw writer.error ?? CaptureError.message("Cannot start audio writer") }
            writer.startSession(atSourceTime: pts)
            input = newInput
            firstPTS = CMTimeGetSeconds(pts)
        }
        guard let input, input.isReadyForMoreMediaData else {
            throw CaptureError.message("Audio encoder cannot keep up; recording stopped to avoid silent data loss")
        }
        guard input.append(sample) else { throw writer.error ?? CaptureError.message("Failed to write audio") }
        sampleCount += CMSampleBufferGetNumSamples(sample)
        lastEnd = CMTimeGetSeconds(pts) + Double(CMSampleBufferGetNumSamples(sample)) / format.pointee.mSampleRate
    }

    func finish() async throws {
        guard let input else { return }
        input.markAsFinished()
        await writer.finishWriting()
        guard writer.status == .completed else { throw writer.error ?? CaptureError.message("Could not finalize recording") }
    }

    func manifest(origin: Double) -> [String: Any] {
        return ["path": path, "offset_seconds": (firstPTS ?? origin) - origin,
                "sample_count": sampleCount, "has_audio": sampleCount > 0]
    }
}

@available(macOS 15.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    let queue = DispatchQueue(label: "protocoller.audio")
    let directory: URL
    let stop: StopFlag
    var tracks: [String: AudioTrack] = [:]
    var failure: String?
    var captureOrigin = 0.0

    init(directory: URL, microphone: Bool, stop: StopFlag) throws {
        self.directory = directory
        self.stop = stop
        super.init()
        tracks["system"] = try AudioTrack(directory: directory, name: "system")
        if microphone { tracks["microphone"] = try AudioTrack(directory: directory, name: "microphone") }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.failure = error.localizedDescription; self.stop.stop() }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        let name: String
        switch type {
        case .audio: name = "system"
        case .microphone: name = "microphone"
        default: return // Screen frames are discarded; only audio is persisted.
        }
        guard failure == nil else { return }
        do { try tracks[name]?.append(sample) }
        catch { failure = error.localizedDescription; stop.stop() }
    }

    func saveManifest(final: Bool) throws {
        let active = tracks.values.compactMap { $0.firstPTS }
        let origin = active.min() ?? captureOrigin
        var metadata: [String: Any] = [
            "schema_version": 1, "status": final ? (failure == nil ? "completed" : "failed") : "recording",
            "clock": "ScreenCaptureKit presentation timestamps",
            "tracks": tracks.mapValues { $0.manifest(origin: origin) }
        ]
        if let failure { metadata["error"] = failure }
        let data = try JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: directory.appendingPathComponent("capture.json"), options: .atomic)
    }
}

@main
struct Capture {
    static func main() async {
        do {
            guard #available(macOS 15.0, *) else { throw CaptureError.message("Capture requires macOS 15 or newer") }
            try await run()
        } catch {
            FileHandle.standardError.write(Data("Capture error: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }

    @available(macOS 15.0, *)
    static func run() async throws {
        let args = Array(CommandLine.arguments.dropFirst())
        if args == ["--devices"] {
            let devices = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone], mediaType: .audio, position: .unspecified).devices
            let data = try JSONSerialization.data(withJSONObject: devices.map {
                ["id": $0.uniqueID, "name": $0.localizedName]
            }, options: [.prettyPrinted])
            print(String(decoding: data, as: UTF8.self))
            return
        }
        guard args.count == 4, let duration = Double(args[1]), duration.isFinite, duration > 0 else {
            throw CaptureError.message("Usage: capture OUTPUT_DIRECTORY SECONDS system|both MICROPHONE_ID_OR_default")
        }
        let directory = URL(fileURLWithPath: args[0], isDirectory: true)
        let microphone = args[2] == "both"
        guard microphone || args[2] == "system" else { throw CaptureError.message("Source must be system or both") }
        if microphone {
            let allowed = await AVCaptureDevice.requestAccess(for: .audio)
            guard allowed else { throw CaptureError.message("Allow microphone access in System Settings > Privacy & Security") }
        }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let display = content.displays.first else { throw CaptureError.message("No active display; system-audio capture needs a desktop session") }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let config = SCStreamConfiguration()
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        config.sampleRate = 48000
        config.channelCount = 2
        config.captureMicrophone = microphone
        if microphone && args[3] != "default" { config.microphoneCaptureDeviceID = args[3] }
        let flag = StopFlag()
        let recorder = try Recorder(directory: directory, microphone: microphone, stop: flag)
        let stream = SCStream(filter: filter, configuration: config, delegate: recorder)
        try stream.addStreamOutput(recorder, type: .screen, sampleHandlerQueue: recorder.queue)
        try stream.addStreamOutput(recorder, type: .audio, sampleHandlerQueue: recorder.queue)
        if microphone { try stream.addStreamOutput(recorder, type: .microphone, sampleHandlerQueue: recorder.queue) }
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let signals = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
            source.setEventHandler { flag.stop() }
            source.resume()
            return source
        }
        defer { signals.forEach { $0.cancel() } }
        recorder.captureOrigin = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
        try recorder.saveManifest(final: false)
        try await stream.startCapture()
        print("RECORDING \(microphone ? "system + microphone" : "system") for up to \(duration)s; Ctrl-C stops and finalizes.")
        fflush(stdout)
        let start = ContinuousClock.now
        while !flag.stopped && start.duration(to: .now) < .seconds(duration) {
            try await Task.sleep(for: .milliseconds(100))
        }
        do { try await stream.stopCapture() }
        catch { recorder.queue.sync { recorder.failure = error.localizedDescription } }
        recorder.queue.sync {} // Drain all queued samples before finalizing writers.
        for track in recorder.tracks.values {
            do { try await track.finish() }
            catch { recorder.failure = error.localizedDescription }
        }
        if recorder.tracks.values.contains(where: { $0.sampleCount == 0 }) && recorder.failure == nil {
            recorder.failure = "A requested source produced no audio; check permissions, playback, and input device"
        }
        try recorder.saveManifest(final: true)
        if let failure = recorder.failure { throw CaptureError.message(failure) }
        print("Saved separate source tracks and capture.json")
    }
}
