import Foundation
import Darwin

enum CaptureError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        switch self { case .message(let text): return text }
    }
}

func atomicJSON(_ value: [String: Any], to destination: URL) throws {
    let temporary = destination.deletingLastPathComponent().appendingPathComponent(".\(UUID().uuidString).tmp")
    defer { try? FileManager.default.removeItem(at: temporary) }
    let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted])
    try data.write(to: temporary)
    let file = try FileHandle(forWritingTo: temporary)
    defer { try? file.close() }
    try file.synchronize()
    guard rename(temporary.path, destination.path) == 0 else {
        throw CaptureError.message("Cannot update recording metadata: \(String(cString: strerror(errno)))")
    }
}

// A raw PCM tail needs no container finalization and can be recovered after SIGKILL.
// Sidecars are written before PCM; finalized chunks are synced before publication.
final class PCMTrack {
    let directory: URL
    let name: String
    let chunkSeconds: Double
    let minimumFreeBytes: Int64
    var chunks: [[String: Any]] = []
    var sampleCount = 0
    var peakDBFS = -120.0
    private var file: FileHandle?
    private var active: [String: Any]?
    private var activeFrames = 0
    private var lastEnd: Double?
    private var lastSync = ContinuousClock.now

    init(directory: URL, name: String, chunkSeconds: Double, minimumFreeBytes: Int64 = 16 * 1024 * 1024) throws {
        guard chunkSeconds.isFinite, chunkSeconds > 0, ["system", "microphone"].contains(name) else {
            throw CaptureError.message("Invalid PCM track configuration")
        }
        self.directory = directory
        self.name = name
        self.chunkSeconds = chunkSeconds
        self.minimumFreeBytes = minimumFreeBytes
        try FileManager.default.createDirectory(at: directory.appendingPathComponent("chunks"), withIntermediateDirectories: true)
    }

    private func begin(rate: Int, start: Double) throws {
        let attributes = try FileManager.default.attributesOfFileSystem(forPath: directory.path)
        if let space = attributes[.systemFreeSize] as? NSNumber, space.int64Value < minimumFreeBytes {
            throw CaptureError.message("Disk space is low; recording stopped with earlier chunks preserved")
        }
        let path = String(format: "chunks/%@-%06d.pcm", name, chunks.count + 1)
        let url = directory.appendingPathComponent(path)
        let descriptor = open(url.path, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw CaptureError.message("Cannot create recording chunk: \(String(cString: strerror(errno)))") }
        file = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        activeFrames = 0
        active = ["path": path, "source": name, "start_seconds": start, "sample_rate": rate,
                  "channels": 1, "sample_width": 2, "frames": 0, "complete": false]
        try checkpoint()
    }

    private func checkpoint() throws {
        guard var metadata = active, let file else { return }
        try file.synchronize()
        metadata["frames"] = activeFrames
        try atomicJSON(metadata, to: directory.appendingPathComponent((metadata["path"] as! String) + ".json"))
        lastSync = .now
    }

    func append(_ bytes: Data, rate: Int, start: Double) throws {
        guard rate > 0, start.isFinite, start >= 0, bytes.count % 2 == 0 else {
            throw CaptureError.message("Invalid PCM samples or timestamps")
        }
        if let end = lastEnd, start < end - 0.002 {
            throw CaptureError.message("Audio timestamps moved backwards; recording stopped")
        }
        let start = lastEnd.map { abs(start - $0) <= 0.002 ? $0 : start } ?? start
        if let metadata = active, let end = lastEnd,
           metadata["sample_rate"] as? Int != rate || abs(start - end) > 0.002 {
            try finishChunk() // Preserve gaps and format/device changes in sidecar timing.
        }
        let frames = bytes.count / 2
        var position = 0
        while position < frames {
            if file == nil { try begin(rate: rate, start: start + Double(position) / Double(rate)) }
            let capacity = max(1, Int((Double(rate) * chunkSeconds).rounded()))
            let count = min(frames - position, capacity - activeFrames)
            try file!.write(contentsOf: bytes.subdata(in: position * 2 ..< (position + count) * 2))
            activeFrames += count
            sampleCount += count
            position += count
            if activeFrames == capacity { try finishChunk() }
            else if lastSync.duration(to: .now) >= .seconds(1) { try checkpoint() }
        }
        lastEnd = start + Double(frames) / Double(rate)
    }

    private func finishChunk() throws {
        guard var metadata = active, let handle = file else { return }
        try handle.synchronize()
        try handle.close()
        file = nil
        metadata["frames"] = activeFrames
        metadata["complete"] = true
        try atomicJSON(metadata, to: directory.appendingPathComponent((metadata["path"] as! String) + ".json"))
        chunks.append(metadata)
        active = nil
    }

    func finish() throws { try finishChunk() }

    func manifest() -> [String: Any] {
        var entries = chunks
        if var pending = active {
            pending["frames"] = activeFrames
            entries.append(pending)
        }
        return ["chunks": entries, "sample_count": sampleCount, "has_audio": sampleCount > 0,
                "peak_dbfs": peakDBFS]
    }
}
