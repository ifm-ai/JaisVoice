import Foundation
import Combine

struct WireEvent: Decodable {
    let type: String
    let turn_id: String
    var text: String?
    var stage: String?
    var tool_id: String?
    var tool_name: String?
    var arguments: JSONValue?
    var result: String?
    var elapsed_ms: Double?
    var error_code: String?
}

enum JSONValue: Codable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let x = try? value.decode(Bool.self) { self = .bool(x) }
        else if let x = try? value.decode(Double.self) { self = .number(x) }
        else if let x = try? value.decode(String.self) { self = .string(x) }
        else if let x = try? value.decode([String: JSONValue].self) { self = .object(x) }
        else { self = .array(try value.decode([JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .string(let x): try value.encode(x)
        case .number(let x): try value.encode(x)
        case .bool(let x): try value.encode(x)
        case .object(let x): try value.encode(x)
        case .array(let x): try value.encode(x)
        case .null: try value.encodeNil()
        }
    }
    var pretty: String {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return (try? String(data: encoder.encode(self), encoding: .utf8)) ?? ""
    }
}

struct ToolAction: Identifiable {
    let id: String
    var name: String
    var arguments: String
    var result: String = ""
    var finished = false
    var errorCode: String?
    var label: String {
        switch name {
        case "shell_execute": return L("macOS command")
        case "browser_navigate": return L("Opening page")
        case "browser_snapshot": return L("Reading page")
        case "browser_click": return L("Clicking on page")
        case "browser_type", "browser_fill_form": return L("Entering text")
        case "browser_select_option": return L("Selecting an option")
        case "browser_navigate_back": return L("Going back")
        case "browser_wait_for": return L("Loading page")
        case "browser_tabs": return L("Switching tabs")
        default: return L("Browser action")
        }
    }
}

enum ModelLoadPhase: String, CaseIterable {
    case waiting, loading, ready, error, unavailable, unloaded
    case notInstalled = "not_installed"

    var label: String {
        switch self {
        case .unloaded: return L("Unloaded")
        case .notInstalled: return L("Not installed")
        case .waiting: return L("Waiting")
        case .loading: return L("Loading…")
        case .ready: return L("Loaded")
        case .error: return L("Loading error")
        case .unavailable: return L("Status unavailable")
        }
    }

    var symbolName: String {
        switch self {
        case .unloaded: return "memorychip"
        case .notInstalled: return "arrow.down.circle"
        case .waiting: return "clock"
        case .loading: return "arrow.trianglehead.2.clockwise.rotate.90"
        case .ready: return "checkmark.circle.fill"
        case .error: return "exclamationmark.circle.fill"
        case .unavailable: return "questionmark.circle"
        }
    }

    init(status: String?) { self = status.flatMap(ModelLoadPhase.init(rawValue:)) ?? .unavailable }
}

struct ConversationExchange: Identifiable {
    let id: String
    let prompt: String
    let response: String
    let error: String?
}

@MainActor final class TurnState: ObservableObject {
    @Published private(set) var conversationID = UUID().uuidString
    @Published private(set) var history: [ConversationExchange] = []
    @Published var turnID: String?
    @Published var stage = ""
    @Published var raw = ""
    @Published var cleaned = ""
    @Published var response = ""
    @Published var actions: [ToolAction] = []
    @Published var error: String?
    @Published var finished = false
    @Published var recording = false
    @Published var level: Float = 0
    @Published var duration: TimeInterval = 0
    /// Each model's state in the services, by size ("3.7B") or "ASR".
    @Published var modelInventory: [String: ModelHealth] = [:]
    @Published var changingModel: String?
    @Published var modelMemoryError: String?
    @Published var modelsUnloaded = false
    @Published var modelsChanging = false
    @Published var ready = false
    @Published var serviceMessage = "Starting services…"
    /// The selected language model, shared by cleanup and the agent.
    @Published var modelLabel = L("Local model")
    @Published var modelPhase: ModelLoadPhase = .waiting
    @Published var modelError: String?
    @Published var hovered = false
    @Published var showOriginal = false
    @Published var expandedTools: Set<String> = []
    /// An action waiting for the user's decision in the bubble.
    @Published var pendingApproval: ActionRequest?
    var onApproval: ((Bool) -> Void)?
    var onFinish: (() -> Void)?
    var onBegin: (() -> Void)?

    var awaitingClarification: Bool { stage == "awaiting_clarification" }
    var hasResult: Bool { turnID != nil }
    var isBusy: Bool { turnID != nil && !finished }
    var stageLabel: String {
        if recording { return L("Listening") }
        if pendingApproval != nil { return L("Approval needed") }
        switch stage {
        case "transcribing": return L("Transcribing…")
        case "refining": return L("Correcting transcript…")
        case "acting": return actions.last?.finished == false ? L("Browsing…") : L("Preparing response…")
        case "awaiting_clarification": return L("Waiting for your answer")
        case "complete": return L("Done")
        case "cancelled": return L("Request stopped")
        case "error": return L("Request interrupted")
        default: return stage.isEmpty ? L("Ready to listen") : stage
        }
    }
    /// Follow the services' state for the selected size; nil when they are unreachable.
    func updateModels(_ health: ServiceHealth?, selected size: String) {
        modelInventory = health?.models ?? [:]
        let model = modelInventory[size]
        modelLabel = model?.display_name ?? size
        modelPhase = health == nil ? .unavailable : ModelLoadPhase(status: model?.status)
        modelError = model?.error.map(ServiceDiagnostics.concise)
    }
    func phase(of key: String) -> ModelLoadPhase {
        modelsUnloaded ? .unloaded : ModelLoadPhase(status: modelInventory[key]?.status)
    }
    func canChangeModel(_ size: String) -> Bool {
        !isBusy && !modelsChanging && !modelsUnloaded && [.ready, .unloaded, .error].contains(phase(of: size))
    }
    var canToggleModelMemory: Bool { !isBusy && !modelsChanging }
    var modelMemoryButton: String {
        L(modelsUnloaded ? "Reload models" : "Unload models")
    }
    func finishUnloadingModels() {
        modelsUnloaded = true; modelsChanging = false; ready = false
        serviceMessage = "Models unloaded · memory released"
        modelPhase = .unloaded; modelError = nil
        conversationID = UUID().uuidString; history = []
        turnID = nil; raw = ""; cleaned = ""; response = ""; actions = []
        error = nil; stage = ""; finished = false
    }

    func resetModelsLoading() {
        modelInventory = [:]; modelPhase = .waiting; modelError = nil
    }
    @discardableResult func begin(recording: Bool = false, continuingConversation: Bool = false) -> String {
        if continuingConversation {
            if let previous = turnID, !cleaned.isEmpty || !response.isEmpty {
                history.append(ConversationExchange(id: previous, prompt: cleaned, response: response, error: error))
            }
        } else {
            conversationID = UUID().uuidString
            history = []
        }
        onBegin?()
        let id = UUID().uuidString
        turnID = id; stage = recording ? "recording" : "transcribing"
        raw = ""; cleaned = ""; response = ""; actions = []
        error = nil; finished = false; self.recording = recording
        level = 0; duration = 0; hovered = false; showOriginal = false; expandedTools = []
        return id
    }
    func fail(_ message: String) {
        recording = false; stage = "error"; error = message; finished = true; onFinish?()
    }
    func cancel() {
        recording = false; stage = "cancelled"; finished = true; onFinish?()
    }
    func accept(_ event: WireEvent) {
        guard event.turn_id == turnID, !finished else { return }
        switch event.type {
        case "status": stage = event.stage ?? stage
        case "transcript": raw = event.text ?? ""; stage = "refining"
        case "correction": cleaned = event.text ?? ""; stage = "acting"
        case "response_delta": response += event.text ?? ""
        case "tool_start":
            guard let id = event.tool_id, !actions.contains(where: { $0.id == id }) else { return }
            actions.append(ToolAction(id: id, name: event.tool_name ?? "", arguments: event.arguments?.pretty ?? ""))
        case "tool_end":
            if let index = actions.firstIndex(where: { $0.id == event.tool_id }) {
                actions[index].finished = true; actions[index].result = event.result ?? event.text ?? ""
                actions[index].errorCode = event.error_code
            }
        case "clarification_request":
            response = event.text ?? ""
            stage = "awaiting_clarification"; finished = true; onFinish?()
        case "complete":
            if let text = event.text, !text.isEmpty { response = text }
            stage = "complete"; finished = true; onFinish?()
        case "cancelled": cancel()
        case "error": fail(event.text.map { L($0) } ?? L("An error occurred. Please try again."))
        default: break
        }
    }
}

struct DiffWord: Equatable {
    let text: String
    let removed: Bool
}

enum TranscriptDiff {
    static func words(original: String, cleaned: String) -> [DiffWord] {
        let source = original.split(whereSeparator: \.isWhitespace).map(String.init)
        let target = cleaned.split(whereSeparator: \.isWhitespace).map(String.init)
        // Transcript length is bounded by the recording limit; cap quadratic work on imported text.
        guard source.count * target.count <= 250_000 else {
            return source.map { DiffWord(text: $0, removed: !target.contains($0)) }
        }
        func normalized(_ word: String) -> String { word.trimmingCharacters(in: .punctuationCharacters) }
        let a = source.map(normalized), b = target.map(normalized)
        var table = Array(repeating: Array(repeating: 0, count: b.count + 1), count: a.count + 1)
        if !a.isEmpty && !b.isEmpty {
            for i in (0..<a.count).reversed() {
                for j in (0..<b.count).reversed() {
                    table[i][j] = a[i] == b[j] ? 1 + table[i+1][j+1] : max(table[i+1][j], table[i][j+1])
                }
            }
        }
        var retained: Set<Int> = []; var i = 0; var j = 0
        while i < a.count && j < b.count {
            if a[i] == b[j] { retained.insert(i); i += 1; j += 1 }
            else if table[i+1][j] >= table[i][j+1] { i += 1 } else { j += 1 }
        }
        return source.enumerated().map { DiffWord(text: $0.element, removed: !retained.contains($0.offset)) }
    }
    static func isRTL(_ text: String) -> Bool {
        for scalar in text.unicodeScalars where CharacterSet.letters.contains(scalar) {
            return (0x0590...0x08FF).contains(scalar.value) || (0xFB1D...0xFEFF).contains(scalar.value)
        }
        return false
    }
}

/// A clock-based timeout that preserves the remaining display duration during interaction.
struct DismissalClock {
    private(set) var remaining: TimeInterval = 10
    private var lastTick: Date?
    mutating func reset(at now: Date = Date()) { remaining = 10; lastTick = now }
    mutating func tick(at now: Date, paused: Bool) -> Bool {
        defer { lastTick = now }
        guard let previous = lastTick else { return false }
        guard !paused else { return false }
        remaining -= max(0, now.timeIntervalSince(previous))
        return remaining <= 0
    }
}
