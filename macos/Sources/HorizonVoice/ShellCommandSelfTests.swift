import Darwin
import Foundation

/// Safe local tests only: no AppleEvents, app launches, networking, or user files.
/// The callback runs on the executor's worker queue, so a synchronous CLI may wait.
enum ShellCommandSelfTests {
    private final class ResultBox: @unchecked Sendable {
        private let lock = NSLock()
        private var stored: ShellCommandResult?
        private var deliveries = 0
        func accept(_ value: ShellCommandResult) {
            lock.lock(); stored = value; deliveries += 1; lock.unlock()
        }
        var value: ShellCommandResult? { lock.lock(); defer { lock.unlock() }; return stored }
        var count: Int { lock.lock(); defer { lock.unlock() }; return deliveries }
    }

    static func run() -> Int32 {
        var passed = 0
        func check(_ condition: @autoclosure () -> Bool, _ name: String) throws {
            guard condition() else { throw failure(name) }
            passed += 1
            print("OK shell : \(name)")
        }
        do {
            let valid = ShellCommandRequest(turnID: "turn-test", toolID: "tool-test", command: "printf 'مرحبا\\n'", reason: "Test sans mutation")
            try check(valid.validationError == nil, "les commandes Unicode restent acceptées")
            try check(valid.matchesCancellation(turnID: "turn-test", toolID: "tool-test"), "l’annulation exacte correspond au tour et à l’outil")
            try check(!valid.matchesCancellation(turnID: "turn-test", toolID: "old-tool"), "une annulation tardive n’arrête pas un nouvel outil du même tour")
            try check(!valid.matchesCancellation(turnID: "old-turn", toolID: "tool-test"), "une annulation d’un ancien tour est ignorée")
            try check(valid.matchesCancellation(turnID: "turn-test"), "l’annulation locale explicite du tour reste possible")
            try check(ShellCommandRequest(turnID: "", toolID: "tool", command: "true", reason: "").validationError != nil, "identifiant de tour requis")
            try check(ShellCommandRequest(turnID: "turn", toolID: "tool", command: " \n\t", reason: "").validationError != nil, "commande vide refusée")
            try check(ShellCommandRequest(turnID: "turn", toolID: "tool", command: "printf x\0printf y", reason: "").validationError != nil, "octet nul refusé avant exécution")
            try check(ShellCommandRequest(turnID: "turn", toolID: "tool", command: "printf \u{202E}hidden", reason: "").validationError != nil, "contrôle bidi trompeur refusé")
            try check(ShellCommandRequest(turnID: "turn", toolID: "tool", command: String(repeating: "é", count: 4_097), reason: "").validationError != nil, "la limite de commande compte les octets UTF-8")
            try check(ShellCommandRequest(turnID: "turn", toolID: "tool", command: String(repeating: "x", count: 8_192), reason: "").validationError == nil, "la limite exacte de 8 Ko est acceptée")
            let refusal = ShellCommandResult.rejected("Commande invalide.")
            try check(!refusal.approved && refusal.exitCode == nil && !refusal.timedOut && refusal.output.isEmpty, "une commande invalide ne prétend pas avoir été exécutée")
            var binaryCapture = ShellOutputCapture()
            binaryCapture.append(Data(repeating: 0xFF, count: 20_000))
            try check(binaryCapture.text.utf8.count <= 8_192 && binaryCapture.truncated, "les sorties binaires restent bornées après conversion Unicode")

            let hello = try execute("printf 'bonjour, je tourne en local !\\n'; printf 'stderr capturé\\n' >&2")
            try check(hello.approved && hello.exitCode == 0 && hello.error == nil, "commande locale préautorisée et code de sortie zéro")
            try check(hello.output.contains("bonjour, je tourne en local !") && hello.output.contains("stderr capturé"), "stdout et stderr sont capturés ensemble")
            let failed = try execute("printf 'échec voulu\\n' >&2; exit 7")
            try check(failed.exitCode == 7 && failed.error != nil && failed.output.contains("échec voulu"), "un code de sortie non nul est transmis comme erreur")
            let output = try execute("/usr/bin/yes x | /usr/bin/head -c 100000")
            try check(output.output.utf8.count <= 8_192 && output.output.contains("Sortie limitée"), "la sortie est drainée sans blocage puis limitée à 8 Ko")

            let secretName = "HORIZON_SHELL_SELF_TEST_SECRET"
            let previous = getenv(secretName).map { String(cString: $0) }
            setenv(secretName, "not-for-the-child", 1)
            defer {
                if let previous { setenv(secretName, previous, 1) } else { unsetenv(secretName) }
            }
            let environment = try execute("if [ -n \"${HORIZON_SHELL_SELF_TEST_SECRET-}\" ]; then printf 'LEAK'; exit 2; fi; printf '%s' \"$PATH\"")
            try check(environment.exitCode == 0 && environment.output == "/usr/bin:/bin:/usr/sbin:/sbin", "l’environnement hérité et ses secrets ne sont pas transmis")
            let stdin = try execute("/bin/cat; printf 'stdin fermé'")
            try check(stdin.exitCode == 0 && stdin.output == "stdin fermé", "stdin est fermé proprement avec /dev/null")

            let started = ProcessInfo.processInfo.systemUptime
            let cancelled = try execute("/bin/sleep 30 & child=$!; printf 'CHILD=%s\\n' \"$child\"; wait", cancelAfter: 0.25)
            try check(cancelled.approved && cancelled.exitCode == nil && cancelled.error == "Commande annulée." && !cancelled.timedOut, "l’annulation arrête une commande préautorisée sans inventer un succès")
            try check(ProcessInfo.processInfo.systemUptime - started < 4, "l’annulation interrompt rapidement une commande longue")
            guard let child = childPID(from: cancelled.output) else { throw failure("PID du descendant absent") }
            try check(waitUntilGone(child), "le descendant du groupe possédé ne reste pas actif après annulation")

            let detached = try execute("/bin/sleep 30 & printf 'CHILD=%s\\n' \"$!\"; exit 0")
            guard let backgroundChild = childPID(from: detached.output) else { throw failure("PID de l’enfant en arrière-plan absent") }
            try check(detached.exitCode == 0 && waitUntilGone(backgroundChild), "une commande finie ne laisse pas son enfant en arrière-plan")

            let immediate = try execute("/bin/sleep 30", cancelAfter: 0)
            try check(immediate.error == "Commande annulée." && immediate.exitCode == nil, "l’annulation immédiate couvre la course avec le lancement")

            guard Thread.isMainThread else { throw failure("les tests du contrôleur nécessitent le thread principal") }
            let automated = try MainActor.assumeIsolated { try executeThroughController() }
            try check(automated.result.approved && automated.result.exitCode == 0 && automated.result.output == "automatique", "le contrôleur exécute immédiatement sans clic ni fenêtre JAIS Voice")
            try check(!automated.duplicate.approved && automated.duplicate.exitCode == nil, "le contrôleur automatique continue de refuser un identifiant déjà traité")
            print("\(passed) vérifications du shell réussies.")
            return 0
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            return 1
        }
    }

    private static func execute(_ command: String, cancelAfter: TimeInterval? = nil) throws -> ShellCommandResult {
        let semaphore = DispatchSemaphore(value: 0)
        let box = ResultBox()
        let execution = ShellCommandExecution(command: command)
        execution.start { result in box.accept(result); semaphore.signal() }
        if let cancelAfter {
            if cancelAfter == 0 { execution.cancel() }
            else { DispatchQueue.global().asyncAfter(deadline: .now() + cancelAfter) { execution.cancel() } }
        }
        guard semaphore.wait(timeout: .now() + 5) == .success else {
            execution.cancel()
            throw failure("commande de test bloquée plus de cinq secondes")
        }
        guard box.count == 1, let result = box.value else { throw failure("résultat manquant ou livré plusieurs fois") }
        return result
    }

    private static func childPID(from text: String) -> pid_t? {
        text.split(separator: "\n").first { $0.hasPrefix("CHILD=") }.flatMap { Int32($0.dropFirst(6)) }
    }

    @MainActor private static func executeThroughController() throws -> (result: ShellCommandResult, duplicate: ShellCommandResult) {
        let controller = ShellCommandController()
        let request = ShellCommandRequest(turnID: "controller-test", toolID: "automated", command: "printf automatique", reason: "Test local sans interaction")
        var firstResult: ShellCommandResult?
        var duplicateResult: ShellCommandResult?
        controller.present(request: request) { firstResult = $0 }
        controller.present(request: request) { duplicateResult = $0 }
        let deadline = ProcessInfo.processInfo.systemUptime + 5
        while firstResult == nil, ProcessInfo.processInfo.systemUptime < deadline {
            // Exercise the real asynchronous controller on the main actor, with
            // no NSApplication, event synthesis, UI clicks, or permission grants.
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
        }
        guard let firstResult, let duplicateResult else {
            controller.cancelAll()
            throw failure("le contrôleur n’a pas exécuté la commande sans interaction")
        }
        return (firstResult, duplicateResult)
    }

    private static func waitUntilGone(_ processID: pid_t) -> Bool {
        let deadline = ProcessInfo.processInfo.systemUptime + 2
        while ProcessInfo.processInfo.systemUptime < deadline {
            if Darwin.kill(processID, 0) != 0, errno == ESRCH { return true }
            usleep(20_000)
        }
        return false
    }

    private static func failure(_ message: String) -> NSError {
        NSError(domain: "HorizonVoice.ShellSelfTest", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "ÉCHEC shell : \(message)"])
    }
}
