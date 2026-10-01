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

/// Whether the agent may run actions on this Mac (shell, Notes, radios).
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
        case .never: return L("The assistant can neither run commands nor change your apps. Web browsing remains available.")
        case .ask: return L("Each command is shown before it runs. You allow or refuse it.")
        case .always: return L("Warning: commands chosen by the assistant run without confirmation, with your permissions. A model can make mistakes or follow instructions hidden in a web page, and a command can delete or send your files. Only enable this mode if you watch what the assistant does.")
        }
    }

    /// HORIZON_ACTION_PERMISSION overrides the preference, for scripted smoke tests.
    static var override: ActionPermission? {
        ProcessInfo.processInfo.environment["HORIZON_ACTION_PERMISSION"].flatMap(ActionPermission.init(rawValue:))
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

/// Persist the shortcut, transcription language and shared model choice.
/// Permission is a live macOS state, not an application preference.
@MainActor final class AppPreferences: ObservableObject {
    static let shortcutKey = "shortcutChoice"
    static let asrLanguageKey = "asrLanguage"
    static let modelSizeKey = "sharedModelSize"
    static let cleanerModelKey = "cleanerModelSize"
    static let agentModelKey = "agentModelSize"
    static let cleanupGuardKey = "cleanupGuardEnabled"
    static let actionPermissionKey = "actionPermission"
    @Published private(set) var interfaceLanguage: InterfaceLanguage
    private let defaults: UserDefaults

    @Published private(set) var shortcutChoice: Int
    @Published private(set) var asrLanguage: String
    @Published private(set) var modelSize: String
    var cleanerModel: String { modelSize }
    var agentModel: String { modelSize }
    @Published private(set) var cleanupGuardEnabled: Bool
    @Published private(set) var actionPermission: ActionPermission
    @Published var microphoneStatus: MicrophoneAuthorization = .notDetermined
    @Published var microphoneRequestPending = false
    @Published var shortcutError: String?

    var asrLanguageCode: String? { asrLanguage.isEmpty ? nil : asrLanguage }
    var microphoneAuthorized: Bool { microphoneStatus == .authorized }
    var shortcutLabel: String { ShortcutChoice.choices[shortcutChoice].label }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        Self.importLegacyPreferences(into: defaults)
        interfaceLanguage = .resolved(defaults.string(forKey: InterfaceLanguage.preferenceKey))
        shortcutChoice = Self.normalizedShortcutChoice(defaults.integer(forKey: Self.shortcutKey))
        asrLanguage = Self.normalizedASRLanguage(defaults.string(forKey: Self.asrLanguageKey))
        // Migrate the previous agent choice, which determines response/tool quality.
        modelSize = Self.normalizedModel(
            defaults.string(forKey: Self.modelSizeKey)
                ?? defaults.string(forKey: Self.agentModelKey)
                ?? defaults.string(forKey: Self.cleanerModelKey), fallback: Self.defaultModelSize)
        cleanupGuardEnabled = defaults.object(forKey: Self.cleanupGuardKey) as? Bool ?? true
        actionPermission = defaults.string(forKey: Self.actionPermissionKey).flatMap(ActionPermission.init(rawValue:)) ?? .ask
        defaults.set(modelSize, forKey: Self.modelSizeKey)
        refreshMicrophoneStatus()
    }

    /// Preferences saved before the bundle identifier changed, imported once.
    static let legacyDomain = "local.horizon.voice"

    static func importLegacyPreferences(into defaults: UserDefaults) {
        guard defaults === UserDefaults.standard, defaults.object(forKey: modelSizeKey) == nil,
              let legacy = defaults.persistentDomain(forName: legacyDomain) else { return }
        for (key, value) in legacy { defaults.set(value, forKey: key) }
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

    /// Sizes from before Jais 3 map to the closest current size.
    static func normalizedModel(_ value: String?, fallback: String) -> String {
        let value = ["1B": "0.9B", "4B": "3.7B"][value ?? ""] ?? value ?? ""
        return modelSizes.contains(value) ? value : fallback
    }

    func setInterfaceLanguage(_ language: InterfaceLanguage) {
        defaults.set(language.rawValue, forKey: InterfaceLanguage.preferenceKey)
        interfaceLanguage = language
    }

    func setCleanupGuardEnabled(_ enabled: Bool) {
        cleanupGuardEnabled = enabled
        defaults.set(enabled, forKey: Self.cleanupGuardKey)
    }

    func setActionPermission(_ permission: ActionPermission) {
        actionPermission = permission
        defaults.set(permission.rawValue, forKey: Self.actionPermissionKey)
    }

    func setModelSize(_ value: String) {
        modelSize = Self.normalizedModel(value, fallback: Self.defaultModelSize)
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
}
