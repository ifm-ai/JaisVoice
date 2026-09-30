import AVFoundation
import Foundation

enum VoiceError: LocalizedError {
    case message(String)
    var errorDescription: String? { if case .message(let message) = self { return message }; return nil }
}

enum WAVEncoder {
    static func encode(samples: [Float], sampleRate: UInt32 = 16_000) -> Data {
        var data = Data()
        func ascii(_ text: String) { data.append(contentsOf: text.utf8) }
        func u16(_ value: UInt16) { var value = value.littleEndian; withUnsafeBytes(of: &value) { data.append(contentsOf: $0) } }
        func u32(_ value: UInt32) { var value = value.littleEndian; withUnsafeBytes(of: &value) { data.append(contentsOf: $0) } }
        let bytes = UInt32(samples.count * 2)
        ascii("RIFF"); u32(36 + bytes); ascii("WAVEfmt "); u32(16); u16(1); u16(1)
        u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16); ascii("data"); u32(bytes)
        for sample in samples {
            let clamped = min(1, max(-1, sample.isFinite ? sample : 0))
            u16(UInt16(bitPattern: Int16(clamped * 32767)))
        }
        return data
    }
}

final class AudioRecorder {
    private let engine = AVAudioEngine()
    private let lock = NSLock()
    private var samples: [Float] = []
    private var sampleRate: Double = 48_000
    private var isRecording = false
    private var tapInstalled = false
    private var lastMeterTime: TimeInterval = 0
    var onMeter: ((Float, TimeInterval) -> Void)?
    var onLimit: (() -> Void)?
    static let maximumSeconds: Double = 30

    static func requestPermission() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    func start() throws {
        cancel()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw VoiceError.message(L("Aucun microphone disponible. Vérifiez l’entrée audio du Mac."))
        }
        lock.lock(); samples = []; sampleRate = format.sampleRate; isRecording = true; lastMeterTime = 0; lock.unlock()
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { [weak self] buffer, _ in self?.consume(buffer) }
        tapInstalled = true
        engine.prepare()
        do { try engine.start() } catch { cancel(); throw error }
    }

    private func consume(_ buffer: AVAudioPCMBuffer) {
        guard let channels = buffer.floatChannelData else { return }
        let count = Int(buffer.frameLength), channelCount = Int(buffer.format.channelCount)
        var mixed = Array(repeating: Float.zero, count: count)
        var energy: Float = 0
        for index in 0..<count {
            var value: Float = 0
            for channel in 0..<channelCount { value += channels[channel][index] }
            value /= Float(channelCount); mixed[index] = value; energy += value * value
        }
        lock.lock()
        guard isRecording else { lock.unlock(); return }
        let remaining = max(0, Int(sampleRate * Self.maximumSeconds) - samples.count)
        samples.append(contentsOf: mixed.prefix(remaining))
        let seconds = Double(samples.count) / sampleRate
        let notify = seconds - lastMeterTime > 0.08
        if notify { lastMeterTime = seconds }
        let atLimit = seconds >= Self.maximumSeconds
        if atLimit { isRecording = false }
        lock.unlock()
        if notify {
            let rms = sqrt(energy / Float(max(count, 1)))
            let normalized = min(1, max(0, (20 * log10(max(rms, 0.00001)) + 55) / 55))
            DispatchQueue.main.async { [weak self] in self?.onMeter?(normalized, seconds) }
        }
        if atLimit { DispatchQueue.main.async { [weak self] in self?.onLimit?() } }
    }

    func stop() throws -> Data {
        engine.stop()
        if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
        lock.lock(); isRecording = false; let captured = samples; let rate = sampleRate; samples = []; lock.unlock()
        guard Double(captured.count) / rate >= 0.15 else { throw VoiceError.message(L("Enregistrement trop court. Maintenez le raccourci pendant que vous parlez.")) }
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate, channels: 1, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(captured.count))!
        buffer.frameLength = AVAudioFrameCount(captured.count)
        captured.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: captured.count) }
        return try Self.convertToWAV(buffer)
    }

    func cancel() {
        engine.stop()
        if tapInstalled { engine.inputNode.removeTap(onBus: 0); tapInstalled = false }
        lock.lock(); isRecording = false; samples = []; lock.unlock()
    }

    static func importWAV(from url: URL) throws -> Data {
        let file = try AVAudioFile(forReading: url)
        let seconds = Double(file.length) / file.processingFormat.sampleRate
        guard seconds > 0 && seconds <= maximumSeconds else {
            throw VoiceError.message(L("Choisissez un fichier audio de 30 secondes maximum."))
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length)) else {
            throw VoiceError.message(L("Le fichier audio ne peut pas être lu."))
        }
        try file.read(into: buffer)
        return try convertToWAV(buffer)
    }

    static func convertToWAV(_ source: AVAudioPCMBuffer) throws -> Data {
        let target = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
        guard let converter = AVAudioConverter(from: source.format, to: target) else {
            throw VoiceError.message(L("Le format audio n’est pas pris en charge."))
        }
        converter.downmix = true
        let expectedFrames = Int(ceil(Double(source.frameLength) * 16_000 / source.format.sampleRate))
        let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: 4096)!
        var supplied = false
        var samples: [Float] = []
        samples.reserveCapacity(expectedFrames)
        while true {
            var error: NSError?
            output.frameLength = 0
            let status = converter.convert(to: output, error: &error) { _, state in
                if supplied { state.pointee = .endOfStream; return nil }
                supplied = true; state.pointee = .haveData; return source
            }
            guard status != .error, error == nil else {
                throw error ?? VoiceError.message(L("La conversion de l’enregistrement a échoué."))
            }
            samples.append(contentsOf: UnsafeBufferPointer(start: output.floatChannelData![0], count: Int(output.frameLength)))
            if status == .endOfStream { break }
            guard output.frameLength > 0, samples.count <= expectedFrames + 8192 else {
                throw VoiceError.message(L("La conversion de l’enregistrement est incomplète."))
            }
        }
        guard !samples.isEmpty else { throw VoiceError.message(L("L’enregistrement ne contient aucun son.")) }
        // Resampling can add filter padding; do not let it extend the spoken duration.
        return WAVEncoder.encode(samples: Array(samples.prefix(expectedFrames)))
    }
}
