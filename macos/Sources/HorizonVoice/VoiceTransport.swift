import Foundation
import LiveKit

struct ServiceHealth: Decodable {
    let status: String
    let message: String
    let models: [String: ServiceModelHealth]?
    var asr: ServiceModelHealth? = nil

    func selectedModels(cleaner: String, agent: String) -> [String: ServiceModelHealth] {
        var result: [String: ServiceModelHealth] = [:]
        for (role, size) in [("k2-cleaner", cleaner), ("k2-agent", agent)] {
            result[role] = models?.values.first(where: { $0.model_size == size })
                ?? ServiceModelHealth(model_size: size, display_name: size, repo: nil, status: "unavailable", error: L("Ce modèle n’est pas disponible dans les services actifs."))
        }
        return result
    }
}

struct ServiceModelHealth: Decodable {
    let model_size: String?
    let display_name: String?
    let repo: String?
    let status: String?
    let error: String?

    func label(fallback: String) -> String {
        guard let name = display_name?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty else {
            return fallback
        }
        return name
    }
}

@MainActor final class VoiceTransport: NSObject, RoomDelegate {
    private lazy var room = Room(delegate: self)
    private var connectionTask: Task<Void, Error>?
    private let baseURL: URL
    var onEvent: ((WireEvent) -> Void)?
    var onDisconnect: ((String) -> Void)?

    override init() {
        baseURL = URL(string: ProcessInfo.processInfo.environment["HORIZON_CONTROL_URL"] ?? "http://127.0.0.1:8787")!
        super.init()
    }

    func health() async throws -> ServiceHealth {
        var request = URLRequest(url: baseURL.appendingPathComponent("health"))
        request.timeoutInterval = 3
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw VoiceError.message(L("Les services locaux ne répondent pas encore."))
        }
        return try JSONDecoder().decode(ServiceHealth.self, from: data)
    }

    func changeModelMemory(size: String, load: Bool) async throws -> ServiceHealth {
        var request = URLRequest(url: baseURL.appendingPathComponent("models/\(size)/memory"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["action": load ? "load" : "unload"])
        request.timeoutInterval = 190
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw VoiceError.message(detail?["detail"] as? String ?? L("Impossible de modifier la mémoire du modèle."))
        }
        return try JSONDecoder().decode(ServiceHealth.self, from: data)
    }

    func connect() async throws {
        if room.connectionState == .connected && room.remoteParticipants.values.contains(where: { $0.kind == .agent }) { return }
        if let task = connectionTask { return try await task.value }
        let task = Task { @MainActor in
            await self.room.disconnect()
            struct Credentials: Decodable { let url: String; let token: String; let room: String }
            var request = URLRequest(url: self.baseURL.appendingPathComponent("session"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: ["client_id": UUID().uuidString])
            request.timeoutInterval = 45
            let (data, response) = try await URLSession.shared.data(for: request)
            guard (response as? HTTPURLResponse)?.statusCode == 200 else {
                throw VoiceError.message(L("La session vocale n’est pas prête. Vérifiez l’état des services dans le menu."))
            }
            let credentials = try JSONDecoder().decode(Credentials.self, from: data)
            try await self.room.connect(url: credentials.url, token: credentials.token)
            let deadline = Date().addingTimeInterval(35)
            while !self.room.remoteParticipants.values.contains(where: { $0.kind == .agent }) {
                try Task.checkCancellation()
                guard Date() < deadline else {
                    throw VoiceError.message(L("L’agent n’a pas rejoint la session. Réessayez dans quelques instants."))
                }
                try await Task.sleep(nanoseconds: 150_000_000)
            }
        }
        connectionTask = task
        defer { connectionTask = nil }
        try await task.value
    }

    static func audioAttributes(turnID: String, conversationID: String? = nil, language: String?, cleanerModel: String? = nil, agentModel: String? = nil, cleanupGuardEnabled: Bool? = nil) -> [String: String] {
        var attributes = ["turn_id": turnID]
        if let conversationID { attributes["conversation_id"] = conversationID }
        let code = AppPreferences.normalizedASRLanguage(language)
        if !code.isEmpty { attributes["language"] = code }
        if let cleanerModel { attributes["cleaner_model"] = cleanerModel }
        if let agentModel { attributes["agent_model"] = agentModel }
        if let cleanupGuardEnabled { attributes["cleanup_guard_enabled"] = cleanupGuardEnabled ? "true" : "false" }
        return attributes
    }

    func sendWAV(_ data: Data, turnID: String, conversationID: String? = nil, language: String? = nil, cleanerModel: String? = nil, agentModel: String? = nil, cleanupGuardEnabled: Bool? = nil) async throws {
        try await connect()
        try Task.checkCancellation()
        let writer = try await room.localParticipant.streamBytes(options: StreamByteOptions(
            topic: "horizon.audio", attributes: Self.audioAttributes(turnID: turnID, conversationID: conversationID, language: language, cleanerModel: cleanerModel, agentModel: agentModel, cleanupGuardEnabled: cleanupGuardEnabled),
            mimeType: "audio/wav", name: "\(turnID).wav", totalSize: data.count
        ))
        do {
            // Small chunks bound cancellation latency and transport backpressure.
            for offset in stride(from: 0, to: data.count, by: 16_384) {
                try Task.checkCancellation()
                try await writer.write(data.subdata(in: offset..<min(offset + 16_384, data.count)))
            }
            try Task.checkCancellation()
            try await writer.close()
        } catch {
            try? await writer.close(reason: "cancelled")
            throw error
        }
    }

    func cancel(turnID: String) async {
        guard room.connectionState == .connected else { return }
        let data = try? JSONSerialization.data(withJSONObject: ["type": "cancel", "turn_id": turnID])
        if let data { try? await room.localParticipant.publish(data: data, options: DataPublishOptions(topic: "horizon.control", reliable: true)) }
    }

    func sendShellResult(turnID: String, toolID: String, result: ShellCommandResult) async throws {
        guard room.connectionState == .connected else { throw VoiceError.message(L("La session est déconnectée.")) }
        let payload: [String: Any] = [
            "type": "shell_result", "turn_id": turnID, "tool_id": toolID,
            "approved": result.approved, "exit_code": result.exitCode.map { $0 as Any } ?? NSNull(),
            "output": String(decoding: result.output.utf8.prefix(6000), as: UTF8.self),
            "error": result.error.map { $0 as Any } ?? NSNull(), "timed_out": result.timedOut
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        try await room.localParticipant.publish(data: data, options: DataPublishOptions(topic: "horizon.control", reliable: true))
    }

    /// Explicit diagnostic input: the actual cleaner/agent still run; only ASR
    /// is skipped. This is not an unauthenticated command-execution endpoint.
    func sendText(_ text: String, turnID: String, conversationID: String? = nil, cleanerModel: String? = nil, agentModel: String? = nil, cleanupGuardEnabled: Bool? = nil) async throws {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.utf8.count <= 4000 else {
            throw VoiceError.message(L("Le texte de test est vide ou trop long."))
        }
        try await connect()
        try Task.checkCancellation()
        var payload = Self.audioAttributes(turnID: turnID, conversationID: conversationID, language: nil, cleanerModel: cleanerModel, agentModel: agentModel, cleanupGuardEnabled: cleanupGuardEnabled)
        payload["type"] = "text_request"; payload["text"] = text
        let data = try JSONSerialization.data(withJSONObject: payload)
        try await room.localParticipant.publish(data: data, options: DataPublishOptions(topic: "horizon.control", reliable: true))
    }

    func disconnect() async {
        connectionTask?.cancel(); connectionTask = nil
        await room.disconnect()
    }

    nonisolated func room(_ room: Room, participant: RemoteParticipant?, didReceiveData data: Data, forTopic topic: String, encryptionType: EncryptionType) {
        guard topic == "horizon.events", participant?.kind == .agent,
              let event = try? JSONDecoder().decode(WireEvent.self, from: data) else { return }
        Task { @MainActor [weak self] in self?.onEvent?(event) }
    }

    nonisolated func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        guard let error else { return }
        Task { @MainActor [weak self] in self?.onDisconnect?(L("La connexion à l’agent a été interrompue : {0}", String(describing: error.localizedDescription))) }
    }

    nonisolated func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        guard participant.kind == .agent else { return }
        Task { @MainActor [weak self] in self?.onDisconnect?(L("L’agent local s’est arrêté. Vérifiez l’état des services dans le menu.")) }
    }
}
