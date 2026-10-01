import AVFoundation
import Combine
import Foundation

struct ASRLanguageChoice: Identifiable, Equatable {
    let code: String
    let label: String
    var id: String { code }

    static var choices: [Self] { [
        ASRLanguageChoice(code: "", label: L("Automatique")),
        ASRLanguageChoice(code: "ar", label: L("Arabe")),
        ASRLanguageChoice(code: "fr", label: L("Français")),
        ASRLanguageChoice(code: "en", label: L("Anglais")),
        ASRLanguageChoice(code: "es", label: L("Espagnol")),
        ASRLanguageChoice(code: "it", label: L("Italien"))
    ] }
}

/// Whether the agent may run actions on this Mac (shell, Notes, radios).
enum ActionPermission: String, CaseIterable {
    case never, ask, always

    var label: String {
        switch self {
        case .never: return L("Ne jamais autoriser")
        case .ask: return L("Demander à chaque fois")
        case .always: return L("Toujours autoriser")
        }
    }

    var detail: String {
        switch self {
        case .never: return L("L’assistant ne peut ni lancer de commande ni modifier vos applications. La navigation web reste possible.")
        case .ask: return L("Chaque commande est affichée avant de s’exécuter. Vous l’autorisez ou la refusez.")
        case .always: return L("Attention : les commandes choisies par l’assistant s’exécutent sans confirmation, avec vos droits. Un modèle peut se tromper ou suivre des instructions cachées dans une page web, et une commande peut supprimer ou envoyer vos fichiers. N’activez ce mode que si vous surveillez ce que fait l’assistant.")
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
        case .notDetermined: return L("À autoriser avant de parler")
        case .authorized: return L("Autorisé · prêt à vous écouter")
        case .denied: return L("Accès refusé dans les réglages du Mac")
        case .restricted: return L("Accès limité par les réglages du Mac")
        }
    }

    var actionLabel: String {
        self == .notDetermined ? L("Autoriser…") : L("Réglages Système…")
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
        interfaceLanguage = .resolved(defaults.string(forKey: InterfaceLanguage.preferenceKey))
        shortcutChoice = Self.normalizedShortcutChoice(defaults.integer(forKey: Self.shortcutKey))
        asrLanguage = Self.normalizedASRLanguage(defaults.string(forKey: Self.asrLanguageKey))
        // Migrate the previous agent choice, which determines response/tool quality.
        modelSize = Self.normalizedModel(
            defaults.string(forKey: Self.modelSizeKey)
                ?? defaults.string(forKey: Self.agentModelKey)
                ?? defaults.string(forKey: Self.cleanerModelKey), fallback: "4B")
        cleanupGuardEnabled = defaults.object(forKey: Self.cleanupGuardKey) as? Bool ?? true
        actionPermission = defaults.string(forKey: Self.actionPermissionKey).flatMap(ActionPermission.init(rawValue:)) ?? .ask
        defaults.set(modelSize, forKey: Self.modelSizeKey)
        refreshMicrophoneStatus()
    }

    static func normalizedShortcutChoice(_ index: Int) -> Int {
        ShortcutChoice.choices.indices.contains(index) ? index : 0
    }

    static func normalizedASRLanguage(_ value: String?) -> String {
        let code = value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        return ASRLanguageChoice.choices.contains(where: { $0.code == code }) ? code : ""
    }

    static func normalizedModel(_ value: String?, fallback: String) -> String {
        ["1B", "4B", "7B"].contains(value ?? "") ? value! : fallback
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
        modelSize = Self.normalizedModel(value, fallback: "4B")
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
