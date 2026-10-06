import Foundation

/// The local services' state: the gateway's language models by size ("3.7B") and the ASR ("ASR").
struct ServiceHealth: Decodable {
    let status: String
    let message: String
    var models: [String: ModelHealth] = [:]
}

struct ModelHealth: Decodable {
    var display_name: String? = nil
    var status: String? = nil
    var error: String? = nil
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
    static let gatewayURL = URL(string: ProcessInfo.processInfo.environment["JAIS_MLX_URL"] ?? "http://127.0.0.1:8788")!
    static let asrURL = URL(string: ProcessInfo.processInfo.environment["JAIS_ASR_URL"] ?? "http://127.0.0.1:8790")!
    static let browserPrefix = "mcp__browser__"

    private let agent = AgentProcess()
    private var turnTask: Task<Void, Never>?
    private var activeTurnID: String?
    private var agentConversation: String?
    private var agentModel: String?
    private var agentEffort: String?
    private var run: AgentRun?
    var onEvent: ((WireEvent) -> Void)?
    var onDisconnect: ((String) -> Void)?
    /// The current "actions on this Mac" setting, read for each request.
    var actionPermission: () -> ActionPermission = { .ask }
    /// Shows one action to the user; resolves false when refused or dismissed.
    var approveAction: ((ActionRequest) async -> Bool)?
    /// Closes any approval still displayed, which then resolves false.
    var dismissApprovals: (() -> Void)?

    init() {
        agent.onEvent = { [weak self] record in
            guard let self else { return }
            if record["type"]?.string == "extension_ui_request" { self.answer(record) } else { self.run?.receive(record) }
        }
        agent.onExit = { [weak self] detail in
            guard let self else { return }
            self.dismissApprovals?()
            self.run?.fail(PipelineFailure(code: "agent_unavailable", message: L("The local agent stopped. Check service status in the menu.")))
            self.agentConversation = nil; self.agentModel = nil; self.agentEffort = nil
            self.onDisconnect?(detail)
        }
    }

    // MARK: Services

    private static func getJSON(_ url: URL, timeout: TimeInterval = 3) async throws -> [String: Any] {
        var request = URLRequest(url: url); request.timeoutInterval = timeout
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200, let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw VoiceError.message(L("Local services are not responding yet."))
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
        let (gateway, asr) = await (gatewayHealth, asrHealth)
        guard let gateway else { throw VoiceError.message(L("Local services are not responding yet.")) }
        let modelsReady = gateway["status"] as? String == "ready", asrReady = asr?["status"] as? String == "ready"
        let failed = gateway["status"] as? String == "error" || asr?["status"] as? String == "error"
        var message = modelsReady ? L("Ready") : (gateway["message"] as? String ?? L("Starting local services…"))
        if !asrReady { message = asr?["message"] as? String ?? L("Starting local Jais-ASR F5…") }
        var models = gateway["models"] as? [String: Any] ?? [:]
        if let asr { models["ASR"] = asr }
        let payload: [String: Any] = ["status": modelsReady && asrReady ? "ready" : (failed ? "error" : "starting"), "message": message, "models": models]
        return try JSONDecoder().decode(ServiceHealth.self, from: JSONSerialization.data(withJSONObject: payload))
    }

    func changeModelMemory(size: String, load: Bool) async throws -> ServiceHealth {
        let url = size == "ASR" ? Self.asrURL.appendingPathComponent("memory") : Self.gatewayURL.appendingPathComponent("models/\(size)/memory")
        let (status, body) = try await Self.post(url, ["action": load ? "load" : "unload"], timeout: 190)
        guard status == 200 else {
            throw VoiceError.message(body["detail"] as? String ?? body["error"] as? String ?? L("Could not change model memory."))
        }
        return try await health()
    }

    /// Start the agent if needed and wait until it answers.
    func connect() async throws {
        if !agent.isRunning {
            try agent.start()
            agentConversation = nil; agentModel = nil; agentEffort = nil
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
        dismissApprovals?()
        turnTask?.cancel(); turnTask = nil; activeTurnID = nil
        run?.fail(CancellationError()); run = nil
        await agent.shutdown()
    }

    // MARK: Turns

    /// What a turn needs besides its input: the conversation, the shared model size
    /// and how long the agent thinks (Pi's thinking level, the template's reasoning_effort).
    struct TurnOptions {
        var conversationID: String
        var modelSize: String
        var reasoningEffort = ReasoningEffort.low.rawValue
    }

    func sendWAV(_ data: Data, turnID: String, language: String?, options: TurnOptions) async throws {
        await startTurn(turnID) { pipeline in
            pipeline.emit(turnID, "status", stage: "transcribing")
            let started = Date()
            let raw = try await pipeline.transcribe(data, language: language)
            pipeline.emit(turnID, "transcript", text: raw, elapsed: started)
            try await pipeline.refineAndAct(raw, turnID: turnID, options: options)
        }
    }

    /// Explicit text input: skips ASR only; the cleaner and agent still run.
    func sendText(_ text: String, turnID: String, options: TurnOptions) async throws {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.utf8.count <= 4000, !text.contains("\0") else {
            throw VoiceError.message(L("The text is empty or too long."))
        }
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
        dismissApprovals?()
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
                if !Task.isCancelled { self.emit(turnID, "error", text: L("Processing failed. Try again from the menu bar."), errorCode: "processing_failed") }
            }
            if self.activeTurnID == turnID { self.activeTurnID = nil }
        }
    }

    /// The extension asks before each action on this Mac (shell, Notes) and
    /// before reading or controlling each app, once per request.
    /// Pi waits for the answer; other dialogs are declined, notifications ignored.
    private func answer(_ record: JSONValue) {
        guard let id = record["id"]?.string, let method = record["method"]?.string else { return }
        guard method == "confirm" else {
            if ["select", "input", "editor"].contains(method) {
                agent.send(["type": .string("extension_ui_response"), "id": .string(id), "cancelled": .bool(true)])
            }
            return
        }
        let request = ActionRequest(title: record["title"]?.string ?? "", message: record["message"]?.string ?? "")
        let permission = ActionPermission.override ?? actionPermission()
        Task { @MainActor [weak self] in
            guard let self else { return }
            let allowed: Bool
            switch permission {
            case .never: allowed = false
            case .always: allowed = true
            case .ask where request.isOverview: allowed = self.run != nil
            case .ask: allowed = self.run != nil ? await self.approveAction?(request) ?? false : false
            }
            self.agent.send(["type": .string("extension_ui_response"), "id": .string(id), "confirmed": .bool(allowed)])
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
        ["text": raw, "model_size": options.modelSize]
    }

    /// The agent's model ID for a size, as listed in agent/models.json.
    static func agentModelID(_ size: String) -> String { "jais-3-" + size.lowercased() }

    private func transcribe(_ audio: Data, language: String?) async throws -> String {
        let body = Self.asrBody(audio: audio, language: language)
        let status: Int, result: [String: Any]
        do { (status, result) = try await Self.post(Self.asrURL.appendingPathComponent("transcribe"), body, timeout: 180) }
        catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch { throw PipelineFailure(code: "asr_unavailable", message: L("Transcription failed. Check the status of the local Jais-ASR F5 model.")) }
        try Task.checkCancellation()
        if status == 400 { throw PipelineFailure(code: "audio_format", message: L("The file must be a 16-bit PCM WAV, mono, 16 kHz, from 0.1 to 30 seconds.")) }
        guard status == 200 else { throw PipelineFailure(code: "asr_unavailable", message: L("Transcription failed. Check the status of the local Jais-ASR F5 model.")) }
        if result["refused"] as? Bool == true { throw PipelineFailure(code: "asr_refused", message: L("The local ASR model refused this recording.")) }
        if result["truncated"] as? Bool == true { throw PipelineFailure(code: "asr_truncated", message: L("The transcript is incomplete. Try a shorter recording.")) }
        guard let text = (result["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else {
            throw PipelineFailure(code: "asr_empty", message: L("No speech was recognized."))
        }
        return text
    }

    private func clean(_ raw: String, options: TurnOptions) async throws -> String {
        let body = Self.cleanBody(raw, options: options)
        let status: Int, result: [String: Any]
        do { (status, result) = try await Self.post(Self.gatewayURL.appendingPathComponent("v1/clean"), body, timeout: 180) }
        catch let error as URLError where error.code == .cancelled { throw CancellationError() }
        catch { throw PipelineFailure(code: "cleaner_unavailable", message: L("Cleanup failed. Check the local model status.")) }
        try Task.checkCancellation()
        guard status == 200, let text = result["text"] as? String else {
            let detail = result["detail"] as? [String: Any]
            throw PipelineFailure(code: detail?["code"] as? String ?? "cleaner_unavailable",
                                  message: detail?["message"] as? String ?? L("Cleanup failed. Check the local model status."))
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
        catch { throw PipelineFailure(code: "agent_unavailable", message: L("The local agent did not start. Check the service status in the menu.")) }
        if options.conversationID != agentConversation {
            try await agent.call("new_session")
            agentConversation = options.conversationID
        }
        let model = Self.agentModelID(options.modelSize)
        if model != agentModel {
            try await agent.call("set_model", ["provider": .string("jais"), "modelId": .string(model)])
            agentModel = model; agentEffort = nil
        }
        if options.reasoningEffort != agentEffort {
            try await agent.call("set_thinking_level", ["level": .string(options.reasoningEffort)])
            agentEffort = options.reasoningEffort
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
            throw PipelineFailure(code: "agent_failed", message: L("The local agent did not accept the request."))
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
                failure = message?["errorMessage"]?.string ?? L("The local agent could not finish its answer.")
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
            let code = record["isError"]?.bool == true
                ? (name.hasPrefix("browser_") ? "browser_action_failed" : (name.hasPrefix("desktop_") ? "desktop_action_failed"
                    : (ToolAction.fileTools.contains(name) ? "file_action_failed" : "shell_failed"))) : nil
            emit("tool_end", Fields(errorCode: code, started: toolStarts.removeValue(forKey: id), toolID: id, toolName: name, result: String(text.prefix(2500))))
        case "agent_settled":
            if let question, !question.isEmpty {
                emit("clarification_request", Fields(text: question, started: started))
            } else if let failure {
                emit("error", Fields(text: L("The local agent could not finish its answer: {0}", String(describing: failure)), errorCode: "agent_failed"))
            } else if response.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                emit("error", Fields(text: L("The model returned no answer. Try again."), errorCode: "agent_empty"))
            } else {
                emit("complete", Fields(text: response, started: started))
            }
            finish(.success(()))
        default: break
        }
    }
}
