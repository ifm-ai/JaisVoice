import Foundation

enum ServiceDiagnostics {
    static var restartInstruction: String { L("Resolve the issue, then quit and reopen JAIS Voice.") }

    static func concise(_ message: String) -> String {
        let line = message.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard line.count > 240 else { return line }
        // Keep both the failing resource and the final explanation readable in a menu.
        return String(line.prefix(110)) + " … " + String(line.suffix(110))
    }

    static func exitMessage(code: Int32, output: String) -> String {
        if code != 0, let lastLine = output.split(whereSeparator: \.isNewline).last {
            let detail = concise(String(lastLine))
            if !detail.isEmpty { return L("Startup interrupted: {0}", String(describing: detail)) }
        }
        return L("Local services stopped (code {0}).", String(describing: code))
    }
}

/// A separate file avoids pipe backpressure and preserves errors before /health exists.
final class ServiceLaunchLog {
    let url: URL
    let handle: FileHandle
    let startOffset: UInt64

    init(url: URL) throws {
        self.url = url
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: url.path) {
            guard FileManager.default.createFile(atPath: url.path, contents: nil) else {
                throw VoiceError.message(L("Unable to create the service log: {0}", String(describing: url.path)))
            }
        }
        handle = try FileHandle(forWritingTo: url)
        do {
            try handle.seekToEnd()
            try handle.write(contentsOf: Data("\n--- Launch \(ISO8601DateFormatter().string(from: Date())) ---\n".utf8))
            startOffset = try handle.offset()
        } catch {
            try? handle.close()
            throw error
        }
    }

    static func tail(url: URL, since offset: UInt64, maximumBytes: Int = 16_384) -> String {
        guard let reader = try? FileHandle(forReadingFrom: url) else { return "" }
        defer { try? reader.close() }
        do {
            let end = try reader.seekToEnd()
            let limit = UInt64(max(1, maximumBytes))
            try reader.seek(toOffset: min(end, max(offset, end > limit ? end - limit : 0)))
            return String(decoding: try reader.read(upToCount: Int(limit)) ?? Data(), as: UTF8.self)
        } catch { return "" }
    }

    func close() { try? handle.close() }
}

@MainActor final class ServiceLauncher {
    private var process: Process?
    private var ownershipPipe: Pipe?
    private var output: ServiceLaunchLog?
    private(set) var logURL: URL?
    var onExit: ((Int32, String) -> Void)?
    var isRunning: Bool { process?.isRunning ?? false }

    nonisolated static var logsDirectory: URL {
        if let runtime = ProcessInfo.processInfo.environment["JAIS_RUNTIME_DIR"], !runtime.isEmpty {
            return URL(fileURLWithPath: runtime).appendingPathComponent("logs", isDirectory: true)
        }
        return FileManager.default.urls(for: .libraryDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Logs/JAIS Voice", isDirectory: true)
    }

    /// Start the supervisor; the gateway first loads `modelSize` when it is installed.
    func start(modelSize: String) throws {
        guard !isRunning else { return }
        let environment = ProcessInfo.processInfo.environment
        let executable: URL
        if let explicit = environment["JAIS_LAUNCHER"] {
            executable = URL(fileURLWithPath: explicit)
        } else if let resource = Bundle.main.url(forResource: "launch-services", withExtension: nil) {
            executable = resource
        } else {
            throw VoiceError.message(L("The local launcher is missing. Open JAIS Voice.app created by the installation script."))
        }
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw VoiceError.message(L("The local launcher is not executable. Run application setup again."))
        }
        let task = Process()
        task.executableURL = executable
        var ownedEnvironment = environment
        ownedEnvironment["JAIS_APP_OWNS_SERVICES"] = "1"
        ownedEnvironment["JAIS_MODEL_SIZE"] = modelSize
        task.environment = ownedEnvironment
        let ownership = Pipe()
        task.standardInput = ownership
        let log = try ServiceLaunchLog(url: Self.logsDirectory.appendingPathComponent("supervisor.log"))
        logURL = log.url
        task.standardOutput = log.handle; task.standardError = log.handle
        let offset = log.startOffset, url = log.url
        task.terminationHandler = { [weak self] task in
            let message = ServiceDiagnostics.exitMessage(code: task.terminationStatus, output: ServiceLaunchLog.tail(url: url, since: offset))
            Task { @MainActor in
                guard let self, self.process === task else { return }
                try? self.ownershipPipe?.fileHandleForWriting.close()
                self.ownershipPipe = nil
                self.output?.close(); self.output = nil; self.process = nil
                self.onExit?(task.terminationStatus, message)
            }
        }
        do { try task.run() }
        catch { log.close(); throw error }
        try? ownership.fileHandleForReading.close()
        ownershipPipe = ownership
        process = task; output = log
    }

    /// Wait for the supervisor to reap its model processes before permitting reload.
    func stopAndWait() async throws {
        guard let task = process else { return }
        task.terminationHandler = nil
        try? ownershipPipe?.fileHandleForWriting.close()
        ownershipPipe = nil
        if task.isRunning { task.terminate() }
        let deadline = Date().addingTimeInterval(25)
        while task.isRunning {
            guard Date() < deadline else {
                throw VoiceError.message(L("Stopping the models is taking too long. Try unloading again."))
            }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        guard task.terminationStatus == 0 else {
            output?.close(); output = nil; process = nil
            throw VoiceError.message(L("Stopping the services failed. Check the log before reloading."))
        }
        output?.close(); output = nil; process = nil
    }

    func stop() {
        guard let process else { return }
        process.terminationHandler = nil
        try? ownershipPipe?.fileHandleForWriting.close()
        ownershipPipe = nil
        if process.isRunning { process.terminate() }
        output?.close()
        self.process = nil; output = nil
    }
}
