import AppKit
import ApplicationServices
import AVFoundation
import Combine
import Foundation

struct ASRLanguageChoice: Identifiable, Equatable {
    let code: String
    let label: String
    var id: String { code }

    static var choices: [Self] { [
        ASRLanguageChoice(code: "", label: L("Automatic")),
        ASRLanguageChoice(code: "ar", label: L("Arabic")),
        ASRLanguageChoice(code: "fr", label: L("French")),
        ASRLanguageChoice(code: "en", label: L("English")),
        ASRLanguageChoice(code: "es", label: L("Spanish")),
        ASRLanguageChoice(code: "it", label: L("Italian"))
    ] }
}

/// How long the model thinks before it answers or acts: the chat template's
/// reasoning_effort. The model cards recommend high; low answers fastest.
enum ReasoningEffort: String, CaseIterable {
    case low, medium, high

    var title: String {
        switch self {
        case .low: return L("Low")
        case .medium: return L("Medium")
        case .high: return L("High")
        }
    }

    var detail: String {
        switch self {
        case .low: return L("Fastest")
        case .medium: return L("Balanced")
        case .high: return L("Most careful")
        }
    }
}

/// Whether the agent may run actions on this Mac (shell, Notes) and read or use other apps.
enum ActionPermission: String, CaseIterable {
    case never, ask, always

    var label: String {
        switch self {
        case .never: return L("Never allow")
        case .ask: return L("Ask every time")
        case .always: return L("Always allow")
        }
    }

    var detail: String {
        switch self {
        case .never: return L("The assistant can neither run commands nor use your apps. Web browsing remains available.")
        case .ask: return L("Each command, and each app the assistant wants to read or control, is shown first. You allow or refuse it.")
        case .always: return L("Warning: the assistant runs commands and uses your apps without confirmation, with your permissions. A model can make mistakes or follow instructions hidden in a web page or a window, and can delete or send your files and messages. Only enable this mode if you watch what the assistant does.")
        }
    }

    /// JAIS_ACTION_PERMISSION overrides the preference, for scripted smoke tests.
    static var override: ActionPermission? {
        ProcessInfo.processInfo.environment["JAIS_ACTION_PERMISSION"].flatMap(ActionPermission.init(rawValue:))
    }
}

enum MicrophoneAuthorization: Equatable {
    case notDetermined, authorized, denied, restricted

    var label: String {
        switch self {
        case .notDetermined: return L("Allow access before speaking")
        case .authorized: return L("Allowed · ready to listen")
        case .denied: return L("Access denied in Mac settings")
        case .restricted: return L("Access restricted in Mac settings")
        }
    }

    var actionLabel: String {
        self == .notDetermined ? L("Allow…") : L("System Settings…")
    }
}

/// An OpenAI-compatible API that replaces the local Jais 3 model for cleanup and the agent.
struct RemoteEndpoint: Equatable {
    var baseURL: String
    var model: String
    var apiKey = ""
    /// Whether the model accepts `reasoning_effort`: the Reasoning setting is then sent.
    var reasoning = false

    /// "https://host/v1" without a trailing slash, when it is an http(s) URL with a host.
    static func normalizedBaseURL(_ value: String) -> String? {
        var text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text), ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
              !(url.host ?? "").isEmpty else { return nil }
        return text
    }

    var host: String { URL(string: baseURL)?.host ?? baseURL }

    /// The gateway's `/v1/clean` endpoint field.
    var cleanupBody: [String: Any] { ["base_url": baseURL, "model": model, "api_key": apiKey] }
}

/// Persist the shortcut, languages, model size, transcript cleanup and action permission.
/// Permission is a live macOS state, not an application preference.
@MainActor final class AppPreferences: ObservableObject {
    static let shortcutKey = "shortcutChoice"
    static let asrLanguageKey = "asrLanguage"
    static let modelSizeKey = "sharedModelSize"
    static let actionPermissionKey = "actionPermission"
    static let reasoningEffortKey = "reasoningEffort"
    static let remoteEnabledKey = "remoteAPIEnabled"
    static let remoteBaseURLKey = "remoteAPIBaseURL"
    static let remoteModelKey = "remoteAPIModel"
    static let remoteReasoningKey = "remoteAPIReasoning"
    static let transcriptCleanupKey = "transcriptCleanup"
    @Published private(set) var interfaceLanguage: InterfaceLanguage
    private let defaults: UserDefaults
    /// The API key stays out of the preferences, in a file only this user can read.
    private let apiKeyURL: URL

    @Published private(set) var shortcutChoice: Int
    @Published private(set) var asrLanguage: String
    /// The Jais 3 size shared by transcript cleanup and the agent.
    @Published private(set) var modelSize: String
    @Published private(set) var actionPermission: ActionPermission
    @Published private(set) var reasoningEffort: ReasoningEffort
    /// Advanced: an OpenAI-compatible API, kept when it is switched off.
    @Published private(set) var remoteEnabled: Bool
    @Published private(set) var remoteDraft: RemoteEndpoint
    /// Whether the language model corrects the transcript before the agent sees it.
    @Published private(set) var transcriptCleanup: Bool
    @Published var microphoneStatus: MicrophoneAuthorization = .notDetermined
    /// macOS permissions to read and use other apps, and to read text drawn in their windows.
    @Published var accessibilityTrusted = false
    @Published var screenRecordingAllowed = false
    @Published var microphoneRequestPending = false
    @Published var shortcutError: String?

    var asrLanguageCode: String? { asrLanguage.isEmpty ? nil : asrLanguage }
    var microphoneAuthorized: Bool { microphoneStatus == .authorized }
    var shortcutLabel: String { ShortcutChoice.choices[shortcutChoice].label }
    /// The API cleanup and the agent use instead of the local model, when enabled and complete.
    var remoteEndpoint: RemoteEndpoint? {
        remoteEnabled && !remoteDraft.baseURL.isEmpty && !remoteDraft.model.isEmpty ? remoteDraft : nil
    }

    nonisolated static var defaultAPIKeyURL: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Jais Voice/remote-api-key")
    }

    init(defaults: UserDefaults = .standard, apiKeyURL: URL = AppPreferences.defaultAPIKeyURL) {
        self.defaults = defaults
        self.apiKeyURL = apiKeyURL
        interfaceLanguage = .resolved(defaults.string(forKey: InterfaceLanguage.preferenceKey))
        shortcutChoice = Self.normalizedShortcutChoice(defaults.integer(forKey: Self.shortcutKey))
        asrLanguage = Self.normalizedASRLanguage(defaults.string(forKey: Self.asrLanguageKey))
        modelSize = Self.normalizedModel(defaults.string(forKey: Self.modelSizeKey))
        actionPermission = defaults.string(forKey: Self.actionPermissionKey).flatMap(ActionPermission.init(rawValue:)) ?? .ask
        reasoningEffort = defaults.string(forKey: Self.reasoningEffortKey).flatMap(ReasoningEffort.init(rawValue:)) ?? .low
        remoteEnabled = defaults.bool(forKey: Self.remoteEnabledKey)
        remoteDraft = RemoteEndpoint(
            baseURL: defaults.string(forKey: Self.remoteBaseURLKey).flatMap(RemoteEndpoint.normalizedBaseURL) ?? "",
            model: defaults.string(forKey: Self.remoteModelKey)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "",
            apiKey: (try? String(contentsOf: apiKeyURL, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "",
            reasoning: defaults.bool(forKey: Self.remoteReasoningKey))
        transcriptCleanup = defaults.object(forKey: Self.transcriptCleanupKey) as? Bool ?? true
        refreshMicrophoneStatus()
        refreshAppControlStatus()
    }

    static func normalizedShortcutChoice(_ index: Int) -> Int {
        ShortcutChoice.choices.indices.contains(index) ? index : 0
    }

    static func normalizedASRLanguage(_ value: String?) -> String {
        let code = value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        return ASRLanguageChoice.choices.contains(where: { $0.code == code }) ? code : ""
    }

    static let modelSizes = ["0.9B", "3.7B", "7B"]
    static let defaultModelSize = "3.7B"

    static func normalizedModel(_ value: String?) -> String {
        modelSizes.contains(value ?? "") ? value! : defaultModelSize
    }

    func setInterfaceLanguage(_ language: InterfaceLanguage) {
        defaults.set(language.rawValue, forKey: InterfaceLanguage.preferenceKey)
        interfaceLanguage = language
    }

    func setActionPermission(_ permission: ActionPermission) {
        actionPermission = permission
        defaults.set(permission.rawValue, forKey: Self.actionPermissionKey)
    }

    func setReasoningEffort(_ effort: ReasoningEffort) {
        reasoningEffort = effort
        defaults.set(effort.rawValue, forKey: Self.reasoningEffortKey)
    }

    /// Save the API settings; enabling requires a valid http(s) URL and a model.
    func setRemote(enabled: Bool, endpoint: RemoteEndpoint) throws {
        let model = endpoint.model.trimmingCharacters(in: .whitespacesAndNewlines)
        let apiKey = endpoint.apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmedURL = endpoint.baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let baseURL = RemoteEndpoint.normalizedBaseURL(trimmedURL)
        if enabled || !trimmedURL.isEmpty, baseURL == nil {
            throw VoiceError.message(L("Enter the API's base URL, starting with https:// or http://."))
        }
        if enabled && model.isEmpty { throw VoiceError.message(L("Enter the model name the API expects.")) }
        try writeAPIKey(apiKey)
        remoteDraft = RemoteEndpoint(baseURL: baseURL ?? "", model: model, apiKey: apiKey, reasoning: endpoint.reasoning)
        remoteEnabled = enabled
        defaults.set(enabled, forKey: Self.remoteEnabledKey)
        defaults.set(remoteDraft.baseURL, forKey: Self.remoteBaseURLKey)
        defaults.set(model, forKey: Self.remoteModelKey)
        defaults.set(endpoint.reasoning, forKey: Self.remoteReasoningKey)
    }

    private func writeAPIKey(_ key: String) throws {
        let manager = FileManager.default
        if key.isEmpty {
            if manager.fileExists(atPath: apiKeyURL.path) { try manager.removeItem(at: apiKeyURL) }
            return
        }
        try manager.createDirectory(at: apiKeyURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        guard manager.createFile(atPath: apiKeyURL.path, contents: Data(key.utf8), attributes: [.posixPermissions: 0o600]) else {
            throw VoiceError.message(L("The API key could not be saved."))
        }
        // createFile keeps the permissions of a file it replaces.
        try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: apiKeyURL.path)
    }

    func setTranscriptCleanup(_ enabled: Bool) {
        transcriptCleanup = enabled
        defaults.set(enabled, forKey: Self.transcriptCleanupKey)
    }

    func setModelSize(_ value: String) {
        modelSize = Self.normalizedModel(value)
        defaults.set(modelSize, forKey: Self.modelSizeKey)
    }

    /// Call only after the global shortcut has been successfully registered.
    func setShortcutChoice(_ index: Int) {
        shortcutChoice = Self.normalizedShortcutChoice(index)
        defaults.set(shortcutChoice, forKey: Self.shortcutKey)
    }

    func setASRLanguage(_ code: String?) {
        asrLanguage = Self.normalizedASRLanguage(code)
        defaults.set(asrLanguage, forKey: Self.asrLanguageKey)
    }

    func refreshMicrophoneStatus() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: microphoneStatus = .authorized
        case .denied: microphoneStatus = .denied
        case .restricted: microphoneStatus = .restricted
        case .notDetermined: microphoneStatus = .notDetermined
        @unknown default: microphoneStatus = .restricted
        }
    }

    func refreshAppControlStatus() {
        accessibilityTrusted = AXIsProcessTrusted()
        screenRecordingAllowed = CGPreflightScreenCaptureAccess()
    }

    /// The system prompt appears the first time; afterwards, the matching Privacy pane opens.
    func requestAccessibility() {
        if !AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary) {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
        }
        refreshAppControlStatus()
    }

    func requestScreenRecording() {
        if !CGRequestScreenCaptureAccess() {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!)
        }
        refreshAppControlStatus()
    }
}
