import AppKit
import ApplicationServices

/// How far the agent may go with one app, whatever the user approves.
///
/// The per-request approval (in the Pi extension) can only grant what this
/// allows: `blocked` apps are never read, `read` apps are never driven.
enum DesktopTier: String {
    case control, read, blocked
}

enum DesktopPolicy {
    static let ownBundleID = "ae.ac.mbzuai.jaisvoice"
    /// Apps that change the Mac's security or show its secrets.
    static let blockedBundleIDs: Set<String> = [
        "com.apple.systempreferences", "com.apple.keychainaccess", "com.apple.Passwords", "com.apple.SecurityAgent",
        "com.apple.loginwindow", "com.apple.coreservices.uiagent", "com.apple.UserNotificationCenter",
    ]
    static let secretKeywords = ["password", "keychain", "1password", "bitwarden", "lastpass", "dashlane", "keepass", "enpass", "authenticator", "authy"]
    /// Apps that run code or act with the user's web accounts: driving them would
    /// bypass the shell approval or the dedicated browser.
    static let readOnlyBundleIDs: Set<String> = [
        "com.apple.Terminal", "com.googlecode.iterm2", "dev.warp.Warp-Stable", "com.mitchellh.ghostty", "net.kovidgoyal.kitty",
        "org.alacritty", "io.alacritty", "com.github.wez.wezterm", "co.zeit.hyper", "com.apple.dt.Xcode", "com.microsoft.VSCode",
        "com.microsoft.VSCodeInsiders", "com.todesktop.230313mzl4w4u92", "dev.zed.Zed", "com.sublimetext.4", "com.apple.ScriptEditor2",
        "com.apple.Automator", "com.apple.shortcuts", "com.apple.ActivityMonitor", "com.apple.DiskUtility", "com.apple.Safari",
        "com.apple.SafariTechnologyPreview", "org.mozilla.firefox", "company.thebrowser.Browser", "com.operasoftware.Opera",
        "com.vivaldi.Vivaldi", "app.zen-browser.zen", "com.anthropic.claudefordesktop",
    ]
    static let readOnlyPrefixes = ["com.google.Chrome", "com.google.chrome", "com.microsoft.edgemac", "com.brave.Browser", "org.chromium.", "com.jetbrains.", "com.google.android.studio"]

    /// `regular`: the app shows in the Dock. Agents, dialogs and background
    /// components (password prompts, Gatekeeper, consent alerts) never are.
    static func tier(bundleID: String?, regular: Bool) -> (DesktopTier, String?) {
        guard let id = bundleID, !id.isEmpty else { return (.blocked, "this app has no bundle identifier") }
        if id == ownBundleID { return (.blocked, "JAIS Voice never controls itself") }
        if !regular { return (.blocked, "system and background components are not accessible") }
        let lower = id.lowercased()
        if blockedBundleIDs.contains(id) || secretKeywords.contains(where: lower.contains) {
            return (.blocked, "this app manages passwords or security settings")
        }
        if readOnlyBundleIDs.contains(id) || readOnlyPrefixes.contains(where: id.hasPrefix) {
            return (.read, "this app runs code or uses your web accounts, so it is read-only")
        }
        return (.control, nil)
    }
}

/// One app, running or installed, as the agent names it.
struct DesktopApp {
    let name: String
    let bundleID: String?
    let pid: pid_t?
    let url: URL?
    let regular: Bool
    /// Other names: the bundle's file name and its names in the user's languages.
    var aliases: [String] = []

    var tier: DesktopTier { DesktopPolicy.tier(bundleID: bundleID, regular: regular).0 }
    var note: String? { DesktopPolicy.tier(bundleID: bundleID, regular: regular).1 }

    var json: [String: Any] {
        var object: [String: Any] = ["name": name, "tier": tier.rawValue, "running": pid != nil]
        if let bundleID { object["bundle_id"] = bundleID }
        if let pid { object["pid"] = Int(pid) }
        if let note { object["note"] = note }
        return object
    }
}

enum DesktopApps {
    /// Case, accents, ".app" and surrounding spaces do not matter.
    static func normalized(_ text: String) -> String {
        var value = text.folding(options: [.caseInsensitive, .diacriticInsensitive, .widthInsensitive], locale: nil)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        if value.hasSuffix(".app") { value.removeLast(4) }
        return value
    }

    static func running() -> [DesktopApp] {
        NSWorkspace.shared.runningApplications.filter { !$0.isTerminated }.compactMap { app in
            // LaunchServices reports pid -1 for an app it thinks idle-terminated
            // (TextEdit) even after the app came back: find its process.
            var pid = app.processIdentifier
            if pid <= 0, let executable = app.executableURL?.resolvingSymlinksInPath().path { pid = processes(executable: executable).first ?? -1 }
            guard pid > 0 else { return nil }
            return DesktopApp(name: app.localizedName ?? app.bundleURL?.deletingPathExtension().lastPathComponent ?? "pid \(pid)",
                              bundleID: app.bundleIdentifier, pid: pid, url: app.bundleURL, regular: app.activationPolicy == .regular,
                              aliases: app.bundleURL.map { [$0.deletingPathExtension().lastPathComponent] } ?? [])
        }
    }

    static func processes(executable: String) -> [pid_t] {
        let capacity = Int(proc_listallpids(nil, 0)) + 64
        var pids = [pid_t](repeating: 0, count: capacity)
        let count = Int(proc_listallpids(&pids, Int32(capacity * MemoryLayout<pid_t>.size)))
        var path = [CChar](repeating: 0, count: 4096)
        return pids.prefix(max(0, count)).filter { $0 > 0 && proc_pidpath($0, &path, UInt32(path.count)) > 0 && String(cString: path) == executable }
    }

    /// An app's names in the user's languages, from `InfoPlist.loctable` (Apple's
    /// apps) or `<language>.lproj/InfoPlist.strings`: "Calculatrice" for Calculator.
    static func localizedNames(_ bundle: Bundle) -> [String] {
        let languages = Locale.preferredLanguages.prefix(4).flatMap { language in
            [language.replacingOccurrences(of: "-", with: "_"), String(language.prefix(while: { $0 != "-" && $0 != "_" }))]
        }
        var tables: [[String: Any]] = []
        if let url = bundle.url(forResource: "InfoPlist", withExtension: "loctable"), let table = NSDictionary(contentsOf: url) as? [String: Any] {
            tables += languages.compactMap { table[$0] as? [String: Any] }
        }
        for language in languages {
            if let url = bundle.url(forResource: "InfoPlist", withExtension: "strings", subdirectory: nil, localization: language),
               let table = NSDictionary(contentsOf: url) as? [String: Any] { tables.append(table) }
        }
        return tables.flatMap { [$0["CFBundleDisplayName"], $0["CFBundleName"]].compactMap { $0 as? String } }
    }

    static func running(pid: pid_t) -> DesktopApp? { running().first { $0.pid == pid } }

    /// Installed apps in the usual folders, by their displayed (localized) name.
    static func installed() -> [DesktopApp] {
        let manager = FileManager.default
        let home = manager.homeDirectoryForCurrentUser
        let folders = ["/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
                       home.appendingPathComponent("Applications").path]
        var apps: [DesktopApp] = []
        var seen: Set<String> = []
        for folder in folders {
            guard let names = try? manager.contentsOfDirectory(atPath: folder) else { continue }
            for name in names where name.hasSuffix(".app") {
                let url = URL(fileURLWithPath: folder).appendingPathComponent(name)
                guard seen.insert(url.resolvingSymlinksInPath().path).inserted, let bundle = Bundle(url: url) else { continue }
                let info = bundle.infoDictionary ?? [:]
                func flag(_ key: String) -> Bool { info[key] as? Bool ?? ((info[key] as? String) == "1") }
                let background = flag("LSUIElement") || flag("LSBackgroundOnly")
                var display = manager.displayName(atPath: url.path)
                if display.hasSuffix(".app") { display.removeLast(4) }
                let localized = localizedNames(bundle)
                apps.append(DesktopApp(name: localized.first ?? display, bundleID: bundle.bundleIdentifier, pid: nil, url: url, regular: !background,
                                       aliases: [display, url.deletingPathExtension().lastPathComponent] + localized))
            }
        }
        return apps
    }

    /// The app the agent means: exact name or bundle identifier first, then a
    /// unique name prefix, then a unique substring. Running apps win.
    static func resolve(_ query: String, includeInstalled: Bool) throws -> DesktopApp {
        let wanted = normalized(query)
        guard !wanted.isEmpty else { throw VoiceError.message("Name the app, as shown by desktop_list_windows.") }
        let running = running().filter { $0.regular || $0.bundleID == DesktopPolicy.ownBundleID }
        func keys(_ app: DesktopApp) -> [String] {
            ([app.name, app.bundleID ?? ""] + app.aliases).map(normalized).filter { !$0.isEmpty }
        }
        func pick(_ apps: [DesktopApp]) throws -> DesktopApp? {
            if let exact = apps.first(where: { keys($0).contains(wanted) }) { return exact }
            for test in [{ (key: String) in key.hasPrefix(wanted) }, { (key: String) in key.contains(wanted) }] {
                let matches = apps.filter { keys($0).contains(where: test) }
                let distinct = Dictionary(grouping: matches, by: { $0.bundleID ?? $0.name })
                if distinct.count == 1 { return matches[0] }
                if distinct.count > 1 {
                    throw VoiceError.message("Several apps match \"\(query)\": " + distinct.values.map { $0[0].name }.sorted().joined(separator: ", ") + ". Use the exact name.")
                }
            }
            return nil
        }
        if let app = try pick(running) { return app }
        if includeInstalled, let app = try pick(installed()) {
            // An installed app that is already running is the running one.
            return running.first(where: { $0.bundleID != nil && $0.bundleID == app.bundleID }) ?? app
        }
        let names = running.filter { $0.regular }.map(\.name).sorted()
        throw VoiceError.message("No running app is named \"\(query)\"." + (includeInstalled ? " No installed app either." : " Open it with desktop_open_app.")
            + (names.isEmpty ? "" : " Running apps: " + names.joined(separator: ", ") + "."))
    }
}
