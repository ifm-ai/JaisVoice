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

/// Persist the shortcut, languages, model size and action permission.
/// Permission is a live macOS state, not an application preference.
@MainActor final class AppPreferences: ObservableObject {
    static let shortcutKey = "shortcutChoice"
    static let asrLanguageKey = "asrLanguage"
    static let modelSizeKey = "sharedModelSize"
    static let actionPermissionKey = "actionPermission"
    @Published private(set) var interfaceLanguage: InterfaceLanguage
    private let defaults: UserDefaults

    @Published private(set) var shortcutChoice: Int
    @Published private(set) var asrLanguage: String
    /// The Jais 3 size shared by transcript cleanup and the agent.
    @Published private(set) var modelSize: String
    @Published private(set) var actionPermission: ActionPermission
    @Published var microphoneStatus: MicrophoneAuthorization = .notDetermined
    /// macOS permissions to read and use other apps, and to read text drawn in their windows.
    @Published var accessibilityTrusted = false
    @Published var screenRecordingAllowed = false
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
        modelSize = Self.normalizedModel(defaults.string(forKey: Self.modelSizeKey))
        actionPermission = defaults.string(forKey: Self.actionPermissionKey).flatMap(ActionPermission.init(rawValue:)) ?? .ask
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
