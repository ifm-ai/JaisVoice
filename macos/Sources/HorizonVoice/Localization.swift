import Foundation

/// UI language is independent of speech recognition and the conversation language.
/// Keep the existing defaults domain so upgrades preserve user settings.
enum InterfaceLanguage: String, CaseIterable {
    case french = "fr", english = "en"
    var label: String { self == .french ? "Français" : "English" }
    static let preferenceKey = "interfaceLanguage"
    static func resolved(_ value: String?) -> InterfaceLanguage {
        InterfaceLanguage(rawValue: value ?? "") ?? .english
    }
    static var current: InterfaceLanguage {
        resolved(UserDefaults.standard.string(forKey: preferenceKey))
    }
}

/// Explicit bundles let the interface switch immediately without changing macOS language.
func L(_ key: String, _ arguments: String...) -> String {
    localized(key, language: InterfaceLanguage.current, arguments: arguments)
}

func localized(_ key: String, language: InterfaceLanguage, arguments: [String] = []) -> String {
    let bundle = Bundle.main.path(forResource: language.rawValue, ofType: "lproj")
        .flatMap(Bundle.init(path:)) ?? Bundle.main
    let format = bundle.localizedString(forKey: key, value: key, table: "Localizable")
    // Replace in one pass: user text containing {0} must remain literal.
    let expression = try! NSRegularExpression(pattern: #"\{([0-9]+)\}"#)
    var result = format
    for match in expression.matches(in: format, range: NSRange(format.startIndex..., in: format)).reversed() {
        guard let indexRange = Range(match.range(at: 1), in: format),
              let index = Int(format[indexRange]), arguments.indices.contains(index),
              let range = Range(match.range, in: result) else { continue }
        result.replaceSubrange(range, with: arguments[index])
    }
    return result
}
