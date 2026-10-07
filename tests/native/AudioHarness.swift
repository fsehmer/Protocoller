import Foundation
import AVFoundation
import Darwin

@main
struct AudioHarness {
    static func main() throws {
        let args = Array(CommandLine.arguments.dropFirst())
        let mode = args[0]
        let directory = URL(fileURLWithPath: args[1], isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let system = try PCMTrack(directory: directory, name: "system", chunkSeconds: 2,
                                  minimumFreeBytes: mode == "disk-full" ? Int64.max : 0)
        let microphone = try PCMTrack(directory: directory, name: "microphone", chunkSeconds: 2, minimumFreeBytes: 0)
        try atomicJSON(["schema_version": 2, "status": "recording",
                        "tracks": ["system": system.manifest(), "microphone": microphone.manifest()]],
                       to: directory.appendingPathComponent("capture.json"))
        func pcm(_ frames: Int, _ value: Int16) -> Data {
            let samples = [Int16](repeating: value.littleEndian, count: frames)
            return samples.withUnsafeBytes { Data($0) }
        }
        if mode == "formats" {
            for interleaved in [false, true] {
                let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48000, channels: 2, interleaved: interleaved)!
                let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 160)!
                input.frameLength = 160
                let buffers = UnsafeMutableAudioBufferListPointer(input.mutableAudioBufferList)
                for frame in 0..<160 {
                    for channel in 0..<2 {
                        let buffer = buffers[interleaved ? 0 : channel].mData!.assumingMemoryBound(to: Float.self)
                        buffer[interleaved ? frame * 2 + channel : frame] = channel == 0 ? 0.25 : 0.75
                    }
                }
                let converted = try monoPCM16(input)
                guard converted.bytes == pcm(160, 16384), converted.rate == 48000,
                      abs(converted.peakDBFS + 6.0206) < 0.001 else { throw CaptureError.message("PCM channel downmix failed") }
            }
            let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 44100, channels: 2, interleaved: true)!
            let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 160)!
            input.frameLength = 160
            for index in 0..<320 { input.int16ChannelData![0][index] = 1234 }
            guard try monoPCM16(input).bytes == pcm(160, 1234) else { throw CaptureError.message("Int16 conversion failed") }
            print("Capture PCM conversion passed")
            return
        }
        if mode == "disk-full" {
            do {
                try system.append(pcm(160, 1234), rate: 16000, start: 0)
                throw CaptureError.message("Disk space guard did not fire")
            } catch CaptureError.message(let text) {
                guard text.contains("Disk space is low") else { throw CaptureError.message(text) }
                print("Disk space guard passed")
                return
            }
        }
        if mode == "long" {
            let data = pcm(32000, 1234)
            for index in 0..<1800 {
                try system.append(data, rate: 16000, start: Double(index * 2))
                try microphone.append(data, rate: 16000, start: Double(index * 2) + 0.25)
            }
        } else if mode == "crash" {
            for index in 0..<5 { try system.append(pcm(32000, 1234), rate: 16000, start: Double(index * 2)) }
            try system.append(pcm(8000, 2345), rate: 16000, start: 10)
            // An incomplete final PCM frame must be reported and omitted by recovery.
            let tail = try FileHandle(forWritingTo: directory.appendingPathComponent("chunks/system-000006.pcm"))
            try tail.seekToEnd()
            try tail.write(contentsOf: Data([255]))
            try tail.synchronize()
            raise(SIGKILL) // No writer finalization or manifest refresh.
        } else if mode == "backwards" {
            try system.append(pcm(160, 1234), rate: 16000, start: 0)
            do {
                try system.append(pcm(160, 1234), rate: 16000, start: 0)
                throw CaptureError.message("Backwards timestamp guard did not fire")
            } catch CaptureError.message(let text) {
                guard text.contains("backwards") else { throw CaptureError.message(text) }
                print("Timestamp guard passed")
                return
            }
        } else if mode == "gap" {
            try system.append(pcm(48000, 1234), rate: 48000, start: 0)
            try system.append(pcm(32000, 2345), rate: 32000, start: 1)
            try microphone.append(pcm(44100, 1234), rate: 44100, start: 0.25)
            try microphone.append(pcm(44100, 2345), rate: 44100, start: 1.5)
        } else { throw CaptureError.message("Unknown test mode") }
        try system.finish()
        try microphone.finish()
        try atomicJSON(["schema_version": 2, "status": "completed",
                        "tracks": ["system": system.manifest(), "microphone": microphone.manifest()]],
                       to: directory.appendingPathComponent("capture.json"))
        print("Synthetic \(mode) capture written")
    }
}
