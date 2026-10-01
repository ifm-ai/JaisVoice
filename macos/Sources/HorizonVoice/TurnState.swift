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
    var command: String?
    var reason: String?
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
        case "shell_execute": return L("Commande macOS")
        case "browser_navigate": return L("Ouverture de la page")
        case "browser_snapshot": return L("Lecture de la page")
        case "browser_click": return L("Clic dans la page")
        case "browser_type", "browser_fill_form": return L("Saisie du texte")
        case "browser_select_option": return L("Sélection d’une option")
        case "browser_navigate_back": return L("Retour à la page précédente")
        case "browser_wait_for": return L("Chargement de la page")
        case "browser_tabs": return L("Navigation entre les onglets")
        default: return L("Action dans le navigateur")
        }
    }
}

enum ModelLoadPhase: String, CaseIterable {
    case waiting, loading, ready, error, unavailable, unloaded
    case notInstalled = "not_installed"

    var label: String {
        switch self {
        case .unloaded: return L("Déchargé")
        case .notInstalled: return L("Non installé")
        case .waiting: return L("En attente")
        case .loading: return L("Chargement…")
        case .ready: return L("Chargé")
        case .error: return L("Erreur de chargement")
        case .unavailable: return L("État indisponible")
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

    var isLoading: Bool { self == .loading }

    static func resolved(modelStatus: String?, serviceStatus: String) -> ModelLoadPhase {
        // A loaded model can remain ready when a different model or service fails.
        // Prefer the actual per-model state whenever the server supplies it.
        if let modelStatus { return ModelLoadPhase(rawValue: modelStatus) ?? .unavailable }
        switch serviceStatus {
        case "ready": return .ready
        case "error": return .error
        case "unavailable": return .unavailable
        default: return .waiting
        }
    }
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
    @Published var modelInventory: [String: ServiceModelHealth] = [:]
    @Published var changingModel: String?
    @Published var modelMemoryError: String?
    @Published var modelsUnloaded = false
    @Published var modelsChanging = false
    @Published var ready = false
    @Published var serviceMessage = "Démarrage des services…"
    @Published var cleanerModelLabel = L("Correcteur local")
    @Published var agentModelLabel = L("Agent local")
    @Published var cleanerModelPhase: ModelLoadPhase = .waiting
    @Published var agentModelPhase: ModelLoadPhase = .waiting
    @Published var cleanerModelError: String?
    @Published var agentModelError: String?
    @Published var hovered = false
    @Published var showOriginal = false
    @Published var expandedTools: Set<String> = []
    var onFinish: (() -> Void)?
    var onBegin: (() -> Void)?

    var awaitingClarification: Bool { stage == "awaiting_clarification" }
    var hasResult: Bool { turnID != nil }
    var isBusy: Bool { turnID != nil && !finished }
    var stageLabel: String {
        if recording { return L("Je vous écoute") }
        switch stage {
        case "uploading": return L("Envoi de l’enregistrement…")
        case "transcribing": return L("Transcription…")
        case "refining": return L("Correction de la transcription…")
        case "acting": return actions.last?.finished == false ? L("Navigation en cours…") : L("Préparation de la réponse…")
        case "awaiting_clarification": return L("En attente de votre réponse")
        case "complete": return L("Terminé")
        case "cancelled": return L("Demande arrêtée")
        case "error": return L("Demande interrompue")
        default: return stage.isEmpty ? L("Prêt à vous écouter") : stage
        }
    }
    func updateModelLabels(_ models: [String: ServiceModelHealth]?) {
        cleanerModelLabel = models?["k2-cleaner"]?.label(fallback: L("Correcteur local")) ?? L("Correcteur local")
        agentModelLabel = models?["k2-agent"]?.label(fallback: L("Agent local")) ?? L("Agent local")
    }
    func updateModelHealth(_ models: [String: ServiceModelHealth]?, serviceStatus: String) {
        updateModelLabels(models)
        let cleaner = models?["k2-cleaner"], agent = models?["k2-agent"]
        cleanerModelPhase = .resolved(modelStatus: cleaner?.status, serviceStatus: serviceStatus)
        agentModelPhase = .resolved(modelStatus: agent?.status, serviceStatus: serviceStatus)
        cleanerModelError = cleaner?.error.map(ServiceDiagnostics.concise)
        agentModelError = agent?.error.map(ServiceDiagnostics.concise)
    }
    func updateModelInventory(_ models: [String: ServiceModelHealth]?) {
        modelInventory = [:]
        for model in models?.values ?? Dictionary<String, ServiceModelHealth>().values {
            if let size = model.model_size { modelInventory[size] = model }
        }
    }
    func modelPhase(_ size: String) -> ModelLoadPhase {
        if modelsUnloaded { return .unloaded }
        return .resolved(modelStatus: modelInventory[size]?.status, serviceStatus: "unavailable")
    }
    func canChangeModel(_ size: String) -> Bool {
        !isBusy && !modelsChanging && !modelsUnloaded && [.ready, .unloaded, .error].contains(modelPhase(size))
    }
    var canToggleModelMemory: Bool { !isBusy && !modelsChanging }
    var modelMemoryButton: String {
        L(modelsUnloaded ? "Recharger les modèles" : "Décharger les modèles")
    }
    func finishUnloadingModels() {
        modelsUnloaded = true; modelsChanging = false; ready = false
        serviceMessage = "Modèles déchargés · mémoire libérée"
        cleanerModelPhase = .unloaded; agentModelPhase = .unloaded
        cleanerModelError = nil; agentModelError = nil
        conversationID = UUID().uuidString; history = []
        turnID = nil; raw = ""; cleaned = ""; response = ""; actions = []
        error = nil; stage = ""; finished = false
    }

    func resetModelsLoading() {
        modelInventory = [:]
        cleanerModelPhase = .waiting; agentModelPhase = .waiting
        cleanerModelError = nil; agentModelError = nil
    }
    func modelsUnavailable() {
        modelInventory = [:]
        cleanerModelPhase = .unavailable; agentModelPhase = .unavailable
        cleanerModelError = nil; agentModelError = nil
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
        case "error": fail(event.text.map { L($0) } ?? L("Une erreur est survenue. Réessayez la demande."))
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
