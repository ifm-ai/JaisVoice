import Foundation

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
    var error: String? = nil
    var message: String? = nil
    var installed: Bool? = nil
    var agent_reasoning_effort: String? = nil

    func label(fallback: String) -> String {
        guard let name = display_name?.trimmingCharacters(in: .whitespacesAndNewlines), !name.isEmpty else {
            return fallback
        }
        return name
    }
}

/// A failed stage of one turn, reported with the same codes as before.
struct PipelineFailure: Error {
    let code: String
    let message: String
}

/// One turn: local ASR → transcript cleanup (gateway) → Pi agent, in this app.
///
/// Only the cleaned text reaches the agent. A new turn, or `cancel`, aborts the
/// previous one; events of a cancelled turn are never emitted.
@MainActor final class LocalPipeline {
    static let gatewayURL = URL(string: ProcessInfo.processInfo.environment["HORIZON_MLX_URL"] ?? "http://127.0.0.1:8788")!
    static let asrURL = URL(string: ProcessInfo.processInfo.environment["HORIZON_ASR_URL"] ?? "http://127.0.0.1:8790")!
    static let browserPrefix = "mcp__browser__"

    private let agent = AgentProcess()
    private var turnTask: Task<Void, Never>?
    private var activeTurnID: String?
    private var agentConversation: String?
    private var agentModel = "k2-4b"
    private var agentThinking: String?
    /// Each model's agent reasoning effort, from the gateway's catalog.
    private var agentEfforts: [String: String] = [:]
    private var run: AgentRun?
    var onEvent: ((WireEvent) -> Void)?
    var onDisconnect: ((String) -> Void)?

    init() {
        agent.onEvent = { [weak self] record in self?.run?.receive(record) }
        agent.onExit = { [weak self] detail in
            guard let self else { return }
            self.run?.fail(PipelineFailure(code: "agent_unavailable", message: L("L’agent local s’est arrêté. Vérifiez l’état des services dans le menu.")))
            self.agentConversation = nil; self.agentModel = "k2-4b"; self.agentThinking = nil
            self.onDisconnect?(detail)
        }
    }

    // MARK: Services

    private static func getJSON(_ url: URL, timeout: TimeInterval = 3) async throws -> [String: Any] {
        var request = URLRequest(url: url); request.timeoutInterval = timeout
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200, let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw VoiceError.message(L("Les services locaux ne répondent pas encore."))
        }
        return object
    }

    private static func post(_ url: URL, _ body: [String: Any], timeout: TimeInterval) async throws -> (Int, [String: Any]) {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.timeoutInterval = timeout
        let (data, response) = try await URLSession.shared.data(for: request)
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        return ((response as? HTTPURLResponse)?.statusCode ?? 0, object)
    }

    func health() async throws -> ServiceHealth {
        async let gatewayHealth = try? Self.getJSON(Self.gatewayURL.appendingPathComponent("health"))
        async let asrHealth = try? Self.getJSON(Self.asrURL.appendingPathComponent("health"))
        let (models, asr) = await (gatewayHealth, asrHealth)
        guard let models else { throw VoiceError.message(L("Les services locaux ne répondent pas encore.")) }
        let modelsReady = models["status"] as? String == "ready", asrReady = asr?["status"] as? String == "ready"
        let failed = models["status"] as? String == "error" || asr?["status"] as? String == "error"
        var message = modelsReady ? L("Prêt") : (models["message"] as? String ?? L("Démarrage des services locaux…"))
        if !asrReady { message = asr?["message"] as? String ?? L("Démarrage de JAIS-ASR F5 local…") }
        var payload: [String: Any] = ["status": modelsReady && asrReady ? "ready" : (failed ? "error" : "starting"), "message": message,
                                      "models": models["models"] ?? [:]]
        if let asr { payload["asr"] = asr }
        let health = try JSONDecoder().decode(ServiceHealth.self, from: JSONSerialization.data(withJSONObject: payload))
        for model in health.models?.values ?? Dictionary<String, ServiceModelHealth>().values {
            if let size = model.model_size, let effort = model.agent_reasoning_effort { agentEfforts[size] = effort }
        }
        return health
    }

    func changeModelMemory(size: String, load: Bool) async throws -> ServiceHealth {
        let url = size == "ASR" ? Self.asrURL.appendingPathComponent("memory") : Self.gatewayURL.appendingPathComponent("models/\(size)/memory")
        let (status, body) = try await Self.post(url, ["action": load ? "load" : "unload"], timeout: 190)
        guard status == 200 else {
            throw VoiceError.message(body["detail"] as? String ?? body["error"] as? String ?? L("Impossible de modifier la mémoire du modèle."))
        }
        return try await health()
    }

    /// Start the agent if needed and wait until it answers.
    func connect() async throws {
        if !agent.isRunning {
            try agent.start()
            agentConversation = nil; agentModel = "k2-4b"; agentThinking = nil
        }
        try await agent.call("get_state")
    }

    /// Stop the agent and any turn in progress; the next turn restarts it.
    func disconnect() {
        turnTask?.cancel(); turnTask = nil; activeTurnID = nil
        run?.fail(CancellationError()); run = nil
        agent.stop()
    }

    func shutdown() async {
        turnTask?.cancel(); turnTask = nil; activeTurnID = nil
        run?.fail(CancellationError()); run = nil
        await agent.shutdown()
    }

    // MARK: Turns

    struct TurnOptions {
        var conversationID: String?
        var cleanerModel: String?
        var agentModel: String?
        var cleanupGuardEnabled: Bool?
    }

    func sendWAV(_ data: Data, turnID: String, conversationID: String? = nil, language: String? = nil, cleanerModel: String? = nil, agentModel: String? = nil, cleanupGuardEnabled: Bool? = nil) async throws {
        let options = TurnOptions(conversationID: conversationID, cleanerModel: cleanerModel, agentModel: agentModel, cleanupGuardEnabled: cleanupGuardEnabled)
        await startTurn(turnID) { pipeline in
            pipeline.emit(turnID, "status", stage: "transcribing")
            let started = Date()
            let raw = try await pipeline.transcribe(data, language: language)
            pipeline.emit(turnID, "transcript", text: raw, elapsed: started)
            try await pipeline.refineAndAct(raw, turnID: turnID, options: options)
        }
    }

    /// Explicit text input: skips ASR only; the cleaner and agent still run.
    func sendText(_ text: String, turnID: String, conversationID: String? = nil, cleanerModel: String? = nil, agentModel: String? = nil, cleanupGuardEnabled: Bool? = nil) async throws {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.utf8.count <= 4000, !text.contains("\0") else {
            throw VoiceError.message(L("Le texte de test est vide ou trop long."))
        }
        let options = TurnOptions(conversationID: conversationID, cleanerModel: cleanerModel, agentModel: agentModel, cleanupGuardEnabled: cleanupGuardEnabled)
        await startTurn(turnID) { pipeline in
            pipeline.emit(turnID, "transcript", text: text)
            try await pipeline.refineAndAct(text, turnID: turnID, options: options)
        }
    }

    func cancel(turnID: String) async {
        guard activeTurnID == turnID else { return }
        await stopActiveTurn()
    }

    private func stopActiveTurn() async {
        let task = turnTask
        turnTask = nil; activeTurnID = nil
        task?.cancel()
        if run != nil { _ = try? await agent.call("abort") }
        await task?.value
    }

    private func startTurn(_ turnID: String, _ body: @escaping @MainActor (LocalPipeline) async throws -> Void) async {
        await stopActiveTurn()
        activeTurnID = turnID
        turnTask = Task { @MainActor [weak self] in
            guard let self else { return }
            do { try await body(self) }
            catch is CancellationError { }
            catch let failure as PipelineFailure { self.emit(turnID, "error", text: failure.message, errorCode: failure.code) }
            catch {
                if !Task.isCancelled { self.emit(turnID, "error", text: L("Le traitement a échoué. Réessayez depuis la barre des menus."), errorCode: "processing_failed") }
            }
            if self.activeTurnID == turnID { self.activeTurnID = nil }
        }
    }

    private func emit(_ turnID: String, _ type: String, text: String? = nil, stage: String? = nil, errorCode: String? = nil, elapsed: Date? = nil,
                      toolID: String? = nil, toolName: String? = nil, arguments: JSONValue? = nil, result: String? = nil) {
        guard activeTurnID == turnID, !Task.isCancelled else { return }
        onEvent?(WireEvent(type: type, turn_id: turnID, text: text, stage: stage, tool_id: toolID, tool_name: toolName, arguments: arguments,
                           result: result, elapsed_ms: elapsed.map { Date().timeIntervalSince($0) * 1000 }.map { $0.rounded() }, error_code: errorCode))
    }

    /// Automatic detection omits the language; a choice applies to one turn only.
    static func asrBody(audio: Data, language: String?) -> [String: Any] {
        var body: [String: Any] = ["audio": audio.base64EncodedString()]
        let code = AppPreferences.normalizedASRLanguage(language)
        if !code.isEmpty { body["language"] = code }
        return body
    }

    static func cleanBody(_ raw: String, options: TurnOptions) -> [String: Any] {
        var body: [String: Any] = ["text": raw, "guard": options.cleanupGuardEnabled ?? true]
        if let size = options.cleanerModel { body["model_size"] = size }
        return body
    }

    static func agentModelID(_ size: String?) -> String { "k2-" + (size ?? "4B").lowercased() }

    private func transcribe(_ audio: Data, language: String?) async throws -> String {
        let body = Self.asrBody(audio: audio, language: language)
        let status: Int, result: [String: Any]
        do { (status, result) = try await Self.post(Self.asrURL.appendingPathComponent("transcribe"), body, timeout: 180) }
        catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch { throw PipelineFailure(code: "asr_unavailable", message: L("La transcription a échoué. Vérifiez l’état du modèle local JAIS-ASR F5.")) }
        try Task.checkCancellation()
        if status == 400 { throw PipelineFailure(code: "audio_format", message: L("Le fichier doit être un WAV PCM 16 bits, mono, 16 kHz, de 0,1 à 30 secondes.")) }
        guard status == 200 else { throw PipelineFailure(code: "asr_unavailable", message: L("La transcription a échoué. Vérifiez l’état du modèle local JAIS-ASR F5.")) }
        if result["refused"] as? Bool == true { throw PipelineFailure(code: "asr_refused", message: L("Le modèle ASR local a refusé cet enregistrement.")) }
        if result["truncated"] as? Bool == true { throw PipelineFailure(code: "asr_truncated", message: L("La transcription est incomplète. Réessayez avec un extrait plus court.")) }
        guard let text = (result["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
            throw PipelineFailure(code: "asr_empty", message: L("Aucune parole n’a été reconnue."))
        }
        return text
    }

    private func clean(_ raw: String, options: TurnOptions) async throws -> String {
        let body = Self.cleanBody(raw, options: options)
        let status: Int, result: [String: Any]
        do { (status, result) = try await Self.post(Self.gatewayURL.appendingPathComponent("v1/clean"), body, timeout: 180) }
        catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch { throw PipelineFailure(code: "cleaner_unavailable", message: L("Le nettoyage a échoué. Vérifiez l’état des modèles locaux.")) }
        try Task.checkCancellation()
        guard status == 200, let text = result["text"] as? String else {
            let detail = result["detail"] as? [String: Any]
            throw PipelineFailure(code: detail?["code"] as? String ?? "cleaner_unavailable",
                                  message: detail?["message"] as? String ?? L("Le nettoyage a échoué. Vérifiez l’état des modèles locaux."))
        }
        return text
    }

    private func refineAndAct(_ raw: String, turnID: String, options: TurnOptions) async throws {
        emit(turnID, "status", stage: "refining")
        let started = Date()
        let cleaned = try await clean(raw, options: options)
        emit(turnID, "correction", text: cleaned, elapsed: started)
        emit(turnID, "status", stage: "acting")
        try await act(cleaned, turnID: turnID, options: options)
    }

    private func act(_ text: String, turnID: String, options: TurnOptions) async throws {
        do { try await connect() }
        catch { throw PipelineFailure(code: "agent_unavailable", message: L("L’agent local n’a pas démarré. Vérifiez l’état des services dans le menu.")) }
        if options.conversationID != agentConversation {
            try await agent.call("new_session")
            agentConversation = options.conversationID
        }
        let model = Self.agentModelID(options.agentModel)
        if model != agentModel {
            try await agent.call("set_model", ["provider": .string("jais"), "modelId": .string(model)])
            agentModel = model
        }
        // The agent's effort follows the selected model's catalog default.
        if let effort = agentEfforts[options.agentModel ?? "4B"], effort != agentThinking {
            try await agent.call("set_thinking_level", ["level": .string(effort)])
            agentThinking = effort
        }
        try Task.checkCancellation()
        let run = AgentRun { [weak self] type, fields in
            self?.emit(turnID, type, text: fields.text, errorCode: fields.errorCode, elapsed: fields.started,
                       toolID: fields.toolID, toolName: fields.toolName, arguments: fields.arguments, result: fields.result)
        }
        self.run = run
        defer { if self.run === run { self.run = nil } }
        let data = try await agent.call("prompt", ["message": .string(text)])
        if data["disposition"]?.string != "started" {
            throw PipelineFailure(code: "agent_failed", message: L("L’agent local n’a pas accepté la demande."))
        }
        try await withTaskCancellationHandler { try await run.finished() } onCancel: { Task { @MainActor in run.fail(CancellationError()) } }
    }
}

/// Maps one prompt's Pi events to panel events until Pi settles.
@MainActor final class AgentRun {
    struct Fields {
        var text: String?, errorCode: String?, started: Date?
        var toolID: String?, toolName: String?, arguments: JSONValue?, result: String?
    }
    private let emit: (String, Fields) -> Void
    private var continuation: CheckedContinuation<Void, Error>?
    private var outcome: Result<Void, Error>?
    private var response = ""
    private var question: String?
    private var failure: String?
    private var toolStarts: [String: Date] = [:]
    private let started = Date()

    init(emit: @escaping (String, Fields) -> Void) { self.emit = emit }

    static func displayName(_ tool: String) -> String {
        tool.hasPrefix(LocalPipeline.browserPrefix) ? String(tool.dropFirst(LocalPipeline.browserPrefix.count)) : tool
    }

    func finished() async throws {
        if let outcome { return try outcome.get() }
        try await withCheckedThrowingContinuation { self.continuation = $0 }
    }

    func fail(_ error: Error) { finish(.failure(error)) }

    private func finish(_ result: Result<Void, Error>) {
        guard outcome == nil else { return }
        outcome = result
        continuation?.resume(with: result); continuation = nil
    }

    func receive(_ record: JSONValue) {
        guard outcome == nil else { return }
        switch record["type"]?.string {
        case "message_update":
            let update = record["assistantMessageEvent"]
            if update?["type"]?.string == "text_delta", let delta = update?["delta"]?.string, !delta.isEmpty {
                response += delta
                emit("response_delta", Fields(text: delta))
            }
        case "message_end":
            let message = record["message"]
            if message?["role"]?.string == "assistant", message?["stopReason"]?.string == "error" {
                failure = message?["errorMessage"]?.string ?? L("L’agent local n’a pas pu terminer sa réponse.")
            }
        case "tool_execution_start":
            guard let id = record["toolCallId"]?.string, let tool = record["toolName"]?.string else { return }
            if tool == "ask_clarifying_question" {
                question = record["args"]?["question"]?.string?.trimmingCharacters(in: .whitespacesAndNewlines)
                return
            }
            toolStarts[id] = Date()
            emit("tool_start", Fields(toolID: id, toolName: Self.displayName(tool), arguments: record["args"]))
        case "tool_execution_end":
            guard let id = record["toolCallId"]?.string, let tool = record["toolName"]?.string, tool != "ask_clarifying_question" else { return }
            let text = record["result"]?["content"]?.array?.compactMap { $0["text"]?.string }.joined(separator: "\n") ?? ""
            let name = Self.displayName(tool)
            let code = record["isError"]?.bool == true ? (name.hasPrefix("browser_") ? "browser_action_failed" : "shell_failed") : nil
            emit("tool_end", Fields(errorCode: code, started: toolStarts.removeValue(forKey: id), toolID: id, toolName: name, result: String(text.prefix(2500))))
        case "agent_settled":
            if let question, !question.isEmpty {
                emit("clarification_request", Fields(text: question, started: started))
            } else if let failure {
                emit("error", Fields(text: L("L’agent local n’a pas pu terminer sa réponse : {0}", String(describing: failure)), errorCode: "agent_failed"))
            } else if response.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                emit("error", Fields(text: L("Le modèle n’a renvoyé aucune réponse. Réessayez."), errorCode: "agent_empty"))
            } else {
                emit("complete", Fields(text: response, started: started))
            }
            finish(.success(()))
        default: break
        }
    }
}
