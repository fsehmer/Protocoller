import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit
import Darwin

final class StopFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func stop() { lock.lock(); value = true; lock.unlock() }
    var stopped: Bool { lock.lock(); defer { lock.unlock() }; return value }
}

@available(macOS 15.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate, AVCaptureAudioDataOutputSampleBufferDelegate, @unchecked Sendable {
    let queue = DispatchQueue(label: "protocoller.audio")
    let directory: URL
    let stop: StopFlag
    var tracks: [String: PCMTrack] = [:]
    var failure: String?
    var captureOrigin = 0.0
    var lastReport = ContinuousClock.now

    init(directory: URL, sources: [String], chunkSeconds: Double, stop: StopFlag) throws {
        self.directory = directory
        self.stop = stop
        super.init()
        for source in sources {
            tracks[source] = try PCMTrack(directory: directory, name: source, chunkSeconds: chunkSeconds)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async { self.failure = error.localizedDescription; self.stop.stop() }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        switch type {
        case .audio: consume(sample, name: "system")
        case .microphone: consume(sample, name: "microphone")
        default: return // No screen frames are persisted.
        }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sample: CMSampleBuffer, from connection: AVCaptureConnection) {
        consume(sample, name: "microphone")
    }

    func consume(_ sample: CMSampleBuffer, name: String) {
        guard failure == nil, let track = tracks[name], CMSampleBufferIsValid(sample),
              CMSampleBufferDataIsReady(sample), CMSampleBufferGetFormatDescription(sample) != nil else { return }
        let count = CMSampleBufferGetNumSamples(sample)
        guard count > 0, count <= Int(Int32.max) else { return }
        do {
            let pcm = try capturePCM(sample)
            track.peakDBFS = pcm.peakDBFS
            let start = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sample)) - captureOrigin
            try track.append(pcm.bytes, rate: pcm.rate, start: start)
            if lastReport.duration(to: .now) >= .seconds(1) {
                try saveManifest(status: "recording")
                let levels = tracks.keys.sorted().map { "\($0)=\(String(format: "%.1f", tracks[$0]!.peakDBFS))dBFS" }.joined(separator: " ")
                print("RECORDING \(String(format: "%.1f", max(0, start)))s \(levels)")
                fflush(stdout)
                lastReport = .now
            }
        } catch {
            failure = error.localizedDescription
            stop.stop()
        }
    }

    func saveManifest(status: String) throws {
        var metadata: [String: Any] = [
            "schema_version": 2, "status": status, "clock": "host-clock seconds from recording start",
            "tracks": tracks.mapValues { $0.manifest() }
        ]
        if let failure { metadata["error"] = failure }
        try atomicJSON(metadata, to: directory.appendingPathComponent("capture.json"))
    }
}

@main
struct Capture {
    static func main() async {
        let parent = getppid()
        let parentMonitor = DispatchSource.makeTimerSource(queue: .global())
        parentMonitor.schedule(deadline: .now() + 1, repeating: 1)
        parentMonitor.setEventHandler {
            if getppid() != parent {
                FileHandle.standardError.write(Data("Capture parent exited; saved PCM chunks remain recoverable.\n".utf8))
                exit(1)
            }
        }
        parentMonitor.resume()
        defer { parentMonitor.cancel() }
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            guard #available(macOS 15.0, *) else { throw CaptureError.message("Capture requires macOS 15 or newer") }
            try await run(args)
        } catch {
            FileHandle.standardError.write(Data("Capture error: \(error.localizedDescription)\n".utf8))
            exit(1)
        }
    }

    @available(macOS 15.0, *)
    static func run(_ args: [String]) async throws {
        let setupTimeout = DispatchWorkItem {
            FileHandle.standardError.write(Data("Capture setup timed out; grant the requested macOS permissions and retry.\n".utf8))
            exit(1)
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 30, execute: setupTimeout)
        defer { setupTimeout.cancel() }
        let devices = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone], mediaType: .audio, position: .unspecified).devices
        if args == ["--devices"] {
            var entries: [[String: String]] = [["id": "default", "name": "System playback (follows speaker/headphone output)", "kind": "system"]]
            entries += devices.map { ["id": $0.uniqueID, "name": $0.localizedName, "kind": "microphone"] }
            let data = try JSONSerialization.data(withJSONObject: entries, options: [.prettyPrinted])
            print(String(decoding: data, as: UTF8.self))
            return
        }
        guard args.count == 5, let duration = Double(args[1]), duration.isFinite, duration > 0,
              let chunkSeconds = Double(args[4]), chunkSeconds >= 0.1, chunkSeconds <= 60,
              ["system", "microphone", "both"].contains(args[2]) else {
            throw CaptureError.message("Usage: capture OUTPUT SECONDS system|microphone|both MICROPHONE_ID_OR_default CHUNK_SECONDS")
        }
        let directory = URL(fileURLWithPath: args[0], isDirectory: true)
        let microphone = args[2] != "system"
        let system = args[2] != "microphone"
        let sources = args[2] == "both" ? ["system", "microphone"] : [args[2]]
        let flag = StopFlag()
        let recorder = try Recorder(directory: directory, sources: sources, chunkSeconds: chunkSeconds, stop: flag)
        try recorder.saveManifest(status: "preparing")
        var selectedDevice: AVCaptureDevice?
        if microphone {
            selectedDevice = args[3] == "default" ? AVCaptureDevice.default(for: .audio) : devices.first { $0.uniqueID == args[3] }
            guard selectedDevice != nil else { throw CaptureError.message("Microphone unavailable; choose an ID listed by devices") }
            let allowed = await AVCaptureDevice.requestAccess(for: .audio)
            guard allowed else { throw CaptureError.message("Allow Microphone access in System Settings > Privacy & Security") }
        }
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let signals = [SIGINT, SIGTERM].map { number -> DispatchSourceSignal in
            let source = DispatchSource.makeSignalSource(signal: number, queue: .global())
            source.setEventHandler { flag.stop() }
            source.resume()
            return source
        }
        defer { signals.forEach { $0.cancel() } }
        var stream: SCStream?
        var session: AVCaptureSession?
        do {
            if system {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                guard let display = content.displays.first else { throw CaptureError.message("No active display; system audio requires a desktop session") }
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
                if let device = selectedDevice { config.microphoneCaptureDeviceID = device.uniqueID }
                let capture = SCStream(filter: filter, configuration: config, delegate: recorder)
                try capture.addStreamOutput(recorder, type: .screen, sampleHandlerQueue: recorder.queue)
                try capture.addStreamOutput(recorder, type: .audio, sampleHandlerQueue: recorder.queue)
                if microphone { try capture.addStreamOutput(recorder, type: .microphone, sampleHandlerQueue: recorder.queue) }
                stream = capture
            } else {
                let capture = AVCaptureSession()
                let input = try AVCaptureDeviceInput(device: selectedDevice!)
                let output = AVCaptureAudioDataOutput()
                output.setSampleBufferDelegate(recorder, queue: recorder.queue)
                guard capture.canAddInput(input), capture.canAddOutput(output) else { throw CaptureError.message("Cannot configure microphone capture") }
                capture.addInput(input)
                capture.addOutput(output)
                session = capture
            }
            if flag.stopped { throw CaptureError.message("Recording cancelled before capture started") }
            recorder.captureOrigin = CMTimeGetSeconds(CMClockGetTime(CMClockGetHostTimeClock()))
            if let stream { try await stream.startCapture() }
            if let session { session.startRunning() }
            setupTimeout.cancel()
            try recorder.queue.sync { try recorder.saveManifest(status: "recording") }
            print("RECORDING \(args[2]) for up to \(duration)s; Ctrl-C stops and flushes chunks.")
            fflush(stdout)
            let start = ContinuousClock.now
            while !flag.stopped && start.duration(to: .now) < .seconds(duration) {
                try await Task.sleep(for: .milliseconds(100))
                if let device = selectedDevice, !device.isConnected {
                    recorder.queue.sync { recorder.failure = "Microphone disconnected; earlier chunks are preserved" }
                    flag.stop()
                }
                if let session, !session.isRunning {
                    recorder.queue.sync { recorder.failure = "Microphone capture stopped unexpectedly" }
                    flag.stop()
                }
            }
        } catch {
            recorder.queue.sync { recorder.failure = "\(error.localizedDescription). For system capture, check Screen & System Audio Recording access." }
        }
        if let stream {
            do { try await stream.stopCapture() }
            catch { recorder.queue.sync { if recorder.failure == nil { recorder.failure = error.localizedDescription } } }
        }
        session?.stopRunning()
        try recorder.queue.sync {
            for track in recorder.tracks.values {
                do { try track.finish() }
                catch { recorder.failure = error.localizedDescription }
            }
            if recorder.tracks.values.contains(where: { $0.sampleCount == 0 }) && recorder.failure == nil {
                recorder.failure = "A requested source produced no samples; check input devices and permissions"
            }
            try recorder.saveManifest(status: recorder.failure == nil ? "completed" : "failed")
        }
        if let failure = recorder.failure { throw CaptureError.message(failure) }
        print("Saved durable PCM chunks and capture.json")
    }
}
