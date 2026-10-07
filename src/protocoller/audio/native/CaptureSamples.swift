import AVFoundation
import CoreMedia
import Foundation

// Downmix capture PCM at its native rate. No codec service is needed to persist it.
func monoPCM16(_ input: AVAudioPCMBuffer) throws -> (bytes: Data, rate: Int, peakDBFS: Double) {
    let channels = Int(input.format.channelCount)
    let frames = Int(input.frameLength)
    guard channels > 0, input.format.sampleRate > 0 else { throw CaptureError.message("Invalid PCM input format") }
    let buffers = UnsafeMutableAudioBufferListPointer(input.mutableAudioBufferList)
    let interleaved = input.format.isInterleaved
    let width: Int
    switch input.format.commonFormat {
    case .pcmFormatFloat32, .pcmFormatInt32: width = 4
    case .pcmFormatFloat64: width = 8
    case .pcmFormatInt16: width = 2
    default: throw CaptureError.message("Unsupported capture PCM representation")
    }
    let requiredBuffers = interleaved ? 1 : channels
    guard buffers.count >= requiredBuffers else { throw CaptureError.message("Missing capture channel buffers") }
    for index in 0..<requiredBuffers {
        let samples = frames * (interleaved ? channels : 1)
        guard buffers[index].mData != nil, Int(buffers[index].mDataByteSize) >= samples * width else {
            throw CaptureError.message("Capture buffer is shorter than its frame count")
        }
    }
    var output = [Int16](repeating: 0, count: frames)
    var peak = 0
    for frame in 0..<frames {
        var total = 0.0
        for channel in 0..<channels {
            let buffer = buffers[interleaved ? 0 : channel].mData!
            let position = interleaved ? frame * channels + channel : frame
            switch input.format.commonFormat {
            case .pcmFormatFloat32: total += Double(buffer.assumingMemoryBound(to: Float.self)[position])
            case .pcmFormatFloat64: total += buffer.assumingMemoryBound(to: Double.self)[position]
            case .pcmFormatInt16: total += Double(buffer.assumingMemoryBound(to: Int16.self)[position]) / 32768
            case .pcmFormatInt32: total += Double(buffer.assumingMemoryBound(to: Int32.self)[position]) / 2147483648
            default: break
            }
        }
        guard total.isFinite else { throw CaptureError.message("Capture produced invalid PCM samples") }
        let value = Int((min(1, max(-1, total / Double(channels))) * 32768).rounded())
        let clipped = min(32767, max(-32768, value))
        output[frame] = Int16(clipped).littleEndian
        peak = max(peak, abs(clipped))
    }
    let bytes = output.withUnsafeBytes { Data($0) }
    return (bytes, Int(input.format.sampleRate), peak == 0 ? -120 : 20 * log10(Double(peak) / 32768))
}

func capturePCM(_ sample: CMSampleBuffer) throws -> (bytes: Data, rate: Int, peakDBFS: Double) {
    let count = CMSampleBufferGetNumSamples(sample)
    guard count > 0, count <= Int(Int32.max), let description = CMSampleBufferGetFormatDescription(sample) else {
        throw CaptureError.message("Invalid capture sample buffer")
    }
    let format = AVAudioFormat(cmAudioFormatDescription: description)
    guard let input = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count)) else {
        throw CaptureError.message("Cannot allocate capture PCM buffer")
    }
    input.frameLength = AVAudioFrameCount(count)
    let copied = CMSampleBufferCopyPCMDataIntoAudioBufferList(sample, at: 0, frameCount: Int32(count), into: input.mutableAudioBufferList)
    guard copied == noErr else { throw CaptureError.message("Cannot copy capture PCM (\(copied))") }
    return try monoPCM16(input)
}
