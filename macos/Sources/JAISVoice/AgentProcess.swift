import Foundation

extension JSONValue {
    subscript(key: String) -> JSONValue? {
        if case .object(let object) = self { return object[key] }
        return nil
    }
    var string: String? { if case .string(let value) = self { return value }; return nil }
    var bool: Bool? { if case .bool(let value) = self { return value }; return nil }
    var array: [JSONValue]? { if case .array(let value) = self { return value }; return nil }
}

/// Splits a byte stream on LF only: JSON strings may contain U+2028/U+2029.
struct JSONLineSplitter {
    private var buffer = Data()

    mutating func append(_ data: Data) -> [Data] {
        buffer.append(data)
        var lines: [Data] = []
        while let index = buffer.firstIndex(of: 0x0A) {
            var line = buffer.subdata(in: buffer.startIndex..<index)
            buffer.removeSubrange(buffer.startIndex...index)
            if line.last == 0x0D { line.removeLast() }
            if !line.isEmpty { lines.append(line) }
        }
        return lines
    }
}

/// Accessed only from the pipe's serial readability handler.
private final class RecordReader: @unchecked Sendable {
    private var splitter = JSONLineSplitter()
    func records(from data: Data) -> [JSONValue] {
        splitter.append(data).compactMap { try? JSONDecoder().decode(JSONValue.self, from: $0) }
    }
}

/// The Pi agent in RPC mode, owned by the app: closing stdin stops it.
@MainActor final class AgentProcess {
    private var process: Process?
    private var input: FileHandle?
    private var log: ServiceLaunchLog?
    private var pending: [String: CheckedContinuation<JSONValue, Error>] = [:]
    private var nextID = 0
    var onEvent: ((JSONValue) -> Void)?
    var onExit: ((String) -> Void)?
    var isRunning: Bool { process?.isRunning ?? false }

    func start() throws {
        guard !isRunning else { return }
        let environment = ProcessInfo.processInfo.environment
        let executable: URL
        if let explicit = environment["JAIS_AGENT_LAUNCHER"] {
            executable = URL(fileURLWithPath: explicit)
        } else if let resource = Bundle.main.url(forResource: "launch-agent", withExtension: nil) {
            executable = resource
        } else {
            throw VoiceError.message(L("The agent launcher is missing. Open the JAIS Voice.app bundle created by the setup script."))
        }
        let task = Process()
        task.executableURL = executable
        task.environment = environment
        let stdin = Pipe(), stdout = Pipe()
        let log = try ServiceLaunchLog(url: ServiceLauncher.logsDirectory.appendingPathComponent("agent.log"))
        task.standardInput = stdin; task.standardOutput = stdout; task.standardError = log.handle
        let reader = RecordReader()
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            let records = reader.records(from: data)
            // The main queue preserves record order, unlike independent Tasks.
            DispatchQueue.main.async { MainActor.assumeIsolated { records.forEach { self?.receive($0) } } }
        }
        let offset = log.startOffset, url = log.url
        task.terminationHandler = { [weak self] task in
            let detail = ServiceDiagnostics.exitMessage(code: task.terminationStatus, output: ServiceLaunchLog.tail(url: url, since: offset))
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.process === task else { return }
                    self.cleanup(error: VoiceError.message(L("The local agent stopped. Check service status in the menu.")))
                    self.onExit?(detail)
                }
            }
        }
        do { try task.run() }
        catch { log.close(); throw error }
        try? stdin.fileHandleForReading.close()
        try? stdout.fileHandleForWriting.close()
        process = task; input = stdin.fileHandleForWriting; self.log = log
    }

    private func receive(_ record: JSONValue) {
        if record["type"]?.string == "response", let id = record["id"]?.string, let continuation = pending.removeValue(forKey: id) {
            if record["success"]?.bool == true { continuation.resume(returning: record["data"] ?? .null) }
            else { continuation.resume(throwing: VoiceError.message(record["error"]?.string ?? L("The agent rejected the command."))) }
            return
        }
        onEvent?(record)
    }

    /// Send one command and wait for its correlated response.
    @discardableResult func call(_ type: String, _ fields: [String: JSONValue] = [:]) async throws -> JSONValue {
        guard let input, isRunning else { throw VoiceError.message(L("The local agent is not running.")) }
        nextID += 1
        let id = "jais-\(nextID)"
        var command = fields; command["type"] = .string(type); command["id"] = .string(id)
        var line = try JSONEncoder().encode(JSONValue.object(command)); line.append(0x0A)
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            do { try input.write(contentsOf: line) }
            catch { pending.removeValue(forKey: id)?.resume(throwing: error) }
        }
    }

    /// Write one record that Pi does not answer, such as an extension_ui_response.
    func send(_ record: [String: JSONValue]) {
        guard let input, isRunning, var line = try? JSONEncoder().encode(JSONValue.object(record)) else { return }
        line.append(0x0A)
        try? input.write(contentsOf: line)
    }

    private func cleanup(error: Error) {
        for continuation in pending.values { continuation.resume(throwing: error) }
        pending = [:]
        try? input?.close(); input = nil
        log?.close(); log = nil; process = nil
    }

    /// Orderly shutdown: Pi exits after stdin EOF and stops its MCP servers.
    /// Waiting lets it flush stdout while this app still reads the pipe.
    func shutdown() async {
        guard let process else { return }
        process.terminationHandler = nil
        try? input?.close(); input = nil
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline { try? await Task.sleep(nanoseconds: 50_000_000) }
        if process.isRunning { process.terminate() }
        cleanup(error: CancellationError())
    }

    func stop() {
        guard let process else { return }
        process.terminationHandler = nil
        try? input?.close()
        if process.isRunning { process.terminate() }
        cleanup(error: CancellationError())
    }
}
