import Darwin
import Foundation

struct ShellCommandRequest: Equatable, Sendable {
    let turnID: String
    let toolID: String
    let command: String
    let reason: String

    static let maximumCommandBytes = 8_192

    func matchesCancellation(turnID: String, toolID: String? = nil) -> Bool {
        self.turnID == turnID && (toolID == nil || self.toolID == toolID)
    }

    var validationError: String? {
        guard !turnID.isEmpty, !toolID.isEmpty, turnID.utf8.count <= 256, toolID.utf8.count <= 256 else {
            return L("L’identifiant de la commande est invalide.")
        }
        guard !command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return L("La commande est vide.")
        }
        guard command.utf8.count <= Self.maximumCommandBytes, !command.contains("\0") else {
            return L("La commande dépasse 8 Ko ou contient un caractère interdit.")
        }
        let concealedControls = command.unicodeScalars.contains { scalar in
            let code = scalar.value
            return (code < 32 && code != 9 && code != 10 && code != 13) || code == 127
                || (0x202A...0x202E).contains(code) || (0x2066...0x2069).contains(code)
        }
        guard !concealedControls else { return L("La commande contient des caractères de contrôle invisibles.") }
        guard reason.utf8.count <= 2_048, !reason.contains("\0") else {
            return L("L’explication de la commande est invalide.")
        }
        return nil
    }
}

struct ShellCommandResult: Equatable, Sendable {
    // Wire compatibility: true means the user's standing authorization applies,
    // not that an additional per-command JAIS Voice prompt was displayed.
    let approved: Bool
    let exitCode: Int?
    let output: String
    let error: String?
    let timedOut: Bool

    static func rejected(_ reason: String) -> Self {
        Self(approved: false, exitCode: nil, output: "", error: reason, timedOut: false)
    }
}

/// Captures only a bounded prefix while the executor continues draining its pipe.
struct ShellOutputCapture {
    static let maximumBytes = 8_192
    private(set) var bytes = Data()
    private(set) var truncated = false

    mutating func append(_ data: Data) {
        let remaining = Self.maximumBytes - bytes.count
        if data.count > remaining { truncated = true }
        if remaining > 0 { bytes.append(data.prefix(remaining)) }
    }

    var text: String {
        // Binary bytes can expand to the Unicode replacement character; bound the
        // encoded result too, without returning a broken trailing UTF-8 sequence.
        let decoded = Data(String(decoding: bytes, as: UTF8.self).utf8)
        let suffix = truncated || decoded.count > Self.maximumBytes ? L("\\n[Sortie limitée à 8 Ko.]") : ""
        let limit = Self.maximumBytes - suffix.utf8.count
        var prefix = decoded.prefix(limit)
        while String(data: prefix, encoding: .utf8) == nil, !prefix.isEmpty { prefix = prefix.dropLast() }
        return (String(data: prefix, encoding: .utf8) ?? "") + suffix
    }
}

/// One preauthorized command in a fresh process group. This is not a security
/// sandbox: commands have the current user's privileges; macOS TCC still applies.
final class ShellCommandExecution: @unchecked Sendable {
    static let maximumSeconds: TimeInterval = 30
    static let shellPath = "/bin/zsh"
    static let environment = [
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG=en_US.UTF-8",
        "LC_CTYPE=en_US.UTF-8",
        "HOME=\(FileManager.default.homeDirectoryForCurrentUser.path)",
    ]

    private let lock = NSLock()
    private var cancelled = false
    private var ownedProcessID: pid_t?
    private let command: String

    init(command: String) { self.command = command }

    func cancel() {
        lock.lock()
        cancelled = true
        // Synchronous signalling is important when the app is about to quit:
        // the worker may not get another scheduling opportunity before exit.
        if let ownedProcessID { Darwin.kill(-ownedProcessID, SIGKILL) }
        lock.unlock()
    }

    private var isCancelled: Bool {
        lock.lock(); defer { lock.unlock() }; return cancelled
    }

    func start(onResult: @escaping @Sendable (ShellCommandResult) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [self] in onResult(run()) }
    }

    private func run() -> ShellCommandResult {
        guard !isCancelled else { return stoppedResult(output: "", timedOut: false) }
        guard !command.contains("\0"), command.utf8.count <= ShellCommandRequest.maximumCommandBytes else {
            return ShellCommandResult(approved: true, exitCode: nil, output: "", error: "Commande invalide.", timedOut: false)
        }
        do {
            let caches = try FileManager.default.url(for: .cachesDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            let directory = caches.appendingPathComponent("local.horizon.voice/Shell", isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            return try spawnAndWait(directory: directory)
        } catch {
            return ShellCommandResult(approved: true, exitCode: nil, output: "", error: error.localizedDescription, timedOut: false)
        }
    }

    private func spawnAndWait(directory: URL) throws -> ShellCommandResult {
        var descriptors: [Int32] = [0, 0]
        guard pipe(&descriptors) == 0 else { throw posixError(errno) }
        var readDescriptor = descriptors[0], writeDescriptor = descriptors[1]
        defer {
            if readDescriptor >= 0 { Darwin.close(readDescriptor) }
            if writeDescriptor >= 0 { Darwin.close(writeDescriptor) }
        }
        guard fcntl(readDescriptor, F_SETFL, O_NONBLOCK) != -1 else { throw posixError(errno) }

        var actions: posix_spawn_file_actions_t?
        var attributes: posix_spawnattr_t?
        try requireSuccess(posix_spawn_file_actions_init(&actions))
        defer { posix_spawn_file_actions_destroy(&actions) }
        try requireSuccess(posix_spawnattr_init(&attributes))
        defer { posix_spawnattr_destroy(&attributes) }
        try requireSuccess(posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null", O_RDONLY, 0))
        try requireSuccess(posix_spawn_file_actions_adddup2(&actions, writeDescriptor, STDOUT_FILENO))
        try requireSuccess(posix_spawn_file_actions_adddup2(&actions, writeDescriptor, STDERR_FILENO))
        try requireSuccess(posix_spawn_file_actions_addclose(&actions, readDescriptor))
        try requireSuccess(posix_spawn_file_actions_addclose(&actions, writeDescriptor))
        try requireSuccess(posix_spawn_file_actions_addchdir_np(&actions, directory.path))
        try requireSuccess(posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)))
        try requireSuccess(posix_spawnattr_setpgroup(&attributes, 0))

        let argumentStrings: [String] = [Self.shellPath, "-f", "-c", command]
        let arguments: [UnsafeMutablePointer<CChar>?] = argumentStrings.map { $0.withCString { strdup($0) } } + [nil]
        let environment: [UnsafeMutablePointer<CChar>?] = Self.environment.map { $0.withCString { strdup($0) } } + [nil]
        defer {
            arguments.compactMap { $0 }.forEach { free($0) }
            environment.compactMap { $0 }.forEach { free($0) }
        }
        // Hold the ownership lock across spawn: cancellation either prevents the
        // spawn or sees the newly registered group, never an untracked child.
        lock.lock()
        guard !cancelled else { lock.unlock(); return stoppedResult(output: "", timedOut: false) }
        var processID: pid_t = 0
        let spawnCode = arguments.withUnsafeBufferPointer { argumentsBuffer in
            environment.withUnsafeBufferPointer { environmentBuffer in
                posix_spawn(&processID, Self.shellPath, &actions, &attributes,
                            UnsafeMutablePointer(mutating: argumentsBuffer.baseAddress!),
                            UnsafeMutablePointer(mutating: environmentBuffer.baseAddress!))
            }
        }
        if spawnCode == 0 { ownedProcessID = processID }
        lock.unlock()
        try requireSuccess(spawnCode)
        Darwin.close(writeDescriptor); writeDescriptor = -1

        let started = ProcessInfo.processInfo.systemUptime
        var terminatedAt: TimeInterval?
        var timedOut = false
        var stopped = false
        var capture = ShellOutputCapture()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        var exitInfo = siginfo_t()

        func drain() {
            // Bounded work per pass ensures an infinitely noisy command cannot
            // delay cancellation or timeout checks, even after its output is full.
            for _ in 0..<32 {
                let count = Darwin.read(readDescriptor, &buffer, buffer.count)
                if count > 0 { capture.append(Data(buffer.prefix(count))) }
                else if count < 0 && errno == EINTR { continue }
                else { break }
            }
        }

        while true {
            drain()
            let now = ProcessInfo.processInfo.systemUptime
            if terminatedAt == nil, isCancelled || now - started >= Self.maximumSeconds {
                timedOut = !isCancelled
                stopped = true
                terminatedAt = now
                // Negative pid targets only this child's new, owned process group.
                Darwin.kill(-processID, SIGTERM)
            }
            if let terminatedAt, now - terminatedAt >= 0.25 { Darwin.kill(-processID, SIGKILL) }

            exitInfo = siginfo_t()
            let waitCode = waitid(P_PID, id_t(processID), &exitInfo, WEXITED | WNOHANG | WNOWAIT)
            if waitCode == 0, exitInfo.si_pid == processID { break }
            if waitCode < 0, errno != EINTR {
                // Never signal a group after losing ownership of its waitable
                // leader. In the normal path WNOWAIT pins the pid until cleanup.
                let waitError = errno
                lock.lock(); ownedProcessID = nil; lock.unlock()
                return ShellCommandResult(approved: true, exitCode: nil, output: capture.text,
                                          error: "Impossible de suivre la commande : \(posixError(waitError).localizedDescription)", timedOut: timedOut)
            }
            var pollDescriptor = pollfd(fd: readDescriptor, events: Int16(POLLIN), revents: 0)
            _ = poll(&pollDescriptor, 1, 40)
            // EOF causes poll to return immediately; avoid spinning on a process
            // which has closed stdout/stderr but continues working.
            if pollDescriptor.revents & Int16(POLLHUP) != 0 { usleep(10_000) }
        }

        // The leader remains waitable (WNOWAIT), so its process-group id cannot
        // be recycled while we stop any still-running children/background work.
        lock.lock()
        Darwin.kill(-processID, SIGKILL)
        ownedProcessID = nil
        lock.unlock()
        drain()
        var status: Int32 = 0
        while waitpid(processID, &status, 0) < 0 && errno == EINTR {}
        Darwin.close(readDescriptor); readDescriptor = -1
        if stopped { return stoppedResult(output: capture.text, timedOut: timedOut) }
        let exitCode = exitInfo.si_code == CLD_EXITED ? Int(exitInfo.si_status) : 128 + Int(exitInfo.si_status)
        return ShellCommandResult(approved: true, exitCode: exitCode, output: capture.text,
                                  error: exitCode == 0 ? nil : L("La commande s’est terminée avec le code {0}.", String(describing: exitCode)), timedOut: false)
    }

    private func stoppedResult(output: String, timedOut: Bool) -> ShellCommandResult {
        ShellCommandResult(approved: true, exitCode: nil, output: output,
                           error: timedOut ? L("Commande arrêtée après 30 secondes.") : L("Commande annulée."), timedOut: timedOut)
    }

    private func requireSuccess(_ code: Int32) throws {
        if code != 0 { throw posixError(code) }
    }

    private func posixError(_ code: Int32) -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(code))
    }
}

/// Executes valid model requests under the user's explicit standing authorization.
/// No JAIS Voice permission window is created; system permission prompts are
/// still owned by macOS and are neither suppressed nor approved by this code.
@MainActor final class ShellCommandController {
    private struct Session {
        let token: UUID
        let request: ShellCommandRequest
        let onResult: (ShellCommandResult) -> Void
        let execution: ShellCommandExecution
    }

    private var active: Session?
    private var seenRequests: Set<String> = []

    /// The method name is retained for transport integration compatibility only.
    func present(request: ShellCommandRequest, onResult: @escaping (ShellCommandResult) -> Void) {
        if let error = request.validationError { onResult(.rejected(error)); return }
        let key = request.turnID + "\0" + request.toolID
        guard seenRequests.insert(key).inserted else { onResult(.rejected(L("Cette commande a déjà été traitée."))); return }
        guard active == nil else { onResult(.rejected(L("Une autre commande est encore en cours ou se termine."))); return }
        let token = UUID()
        let execution = ShellCommandExecution(command: request.command)
        active = Session(token: token, request: request, onResult: onResult, execution: execution)
        execution.start { [weak self] result in
            DispatchQueue.main.async { self?.finish(token: token, result: result) }
        }
    }

    func cancel(turnID: String, toolID: String? = nil) {
        guard let session = active, session.request.matchesCancellation(turnID: turnID, toolID: toolID) else { return }
        session.execution.cancel()
    }

    func cancelAll() {
        // Keep the slot until the group is stopped and its result is delivered;
        // a second command must not overlap cancellation of the previous one.
        active?.execution.cancel()
    }

    private func finish(token: UUID, result: ShellCommandResult) {
        guard let session = active, session.token == token else { return }
        active = nil
        session.onResult(result)
    }
}
