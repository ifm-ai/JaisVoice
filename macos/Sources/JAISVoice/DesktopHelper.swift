import AppKit
import ApplicationServices

/// `--desktop-helper`: the Pi extension's bridge to the apps on this Mac.
///
/// One JSON request per line on stdin, `{"id", "method", "params"}`; one JSON
/// response per line on stdout, `{"id", "result"}` or `{"id", "error"}`. The
/// helper runs below the agent, itself a child of JAIS Voice, so macOS applies
/// the app's Accessibility and Screen Recording permissions. It exits when its
/// stdin closes. The extension asks the user before each app is read or driven;
/// this side enforces what no approval can grant (see `DesktopPolicy`).
@MainActor final class DesktopHelper {
    private let refs = DesktopRefs()

    static func runForever() -> Never {
        let (lines, continuation) = AsyncStream<String>.makeStream()
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) { continuation.yield(line) }
            continuation.finish()
        }
        Task { @MainActor in
            let helper = DesktopHelper()
            for await line in lines { await helper.handle(line) }
            exit(0)
        }
        // NSWorkspace keeps its list of running apps current through the main run loop.
        RunLoop.main.add(Port(), forMode: .default)
        while true { RunLoop.main.run(mode: .default, before: .distantFuture) }
    }

    private func handle(_ line: String) async {
        guard let request = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any], let id = request["id"] else { return }
        var response: [String: Any] = ["id": id]
        do {
            response["result"] = try await dispatch(request["method"] as? String ?? "", request["params"] as? [String: Any] ?? [:])
        } catch {
            response["error"] = error.localizedDescription
        }
        guard var data = try? JSONSerialization.data(withJSONObject: response, options: [.withoutEscapingSlashes]) else { return }
        data.append(0x0A)
        FileHandle.standardOutput.write(data)
    }

    private func requireAccessibility() throws {
        guard AXIsProcessTrusted() else {
            throw VoiceError.message("JAIS Voice does not have the Accessibility permission, so it cannot see or use other apps. "
                + "Tell the user to allow it in JAIS Voice Settings > Assistant.")
        }
        guard !Self.screenLocked else {
            throw VoiceError.message("The Mac's screen is locked, so apps cannot be read or used. Tell the user, in the language of their request, to unlock it and ask again.")
        }
    }

    static var screenLocked: Bool {
        ((CGSessionCopyCurrentDictionary() as? [String: Any])?["CGSSessionScreenIsLocked"] as? NSNumber)?.boolValue ?? false
    }

    /// The app a request targets, by the pid the extension resolved and had approved.
    private func app(_ params: [String: Any], need: DesktopTier) throws -> DesktopApp {
        try requireAccessibility()
        let app: DesktopApp
        if let pid = (params["pid"] as? NSNumber)?.int32Value {
            guard let running = DesktopApps.running(pid: pid) else { throw VoiceError.message("This app has quit. Use desktop_list_windows.") }
            app = running
        } else {
            app = try DesktopApps.resolve(params["app"] as? String ?? "", includeInstalled: false)
        }
        switch (app.tier, need) {
        case (.blocked, _): throw VoiceError.message("\(app.name) is not accessible to JAIS Voice: \(app.note ?? "").")
        case (.read, .control): throw VoiceError.message("\(app.name) is read-only for JAIS Voice: \(app.note ?? ""). Do not try another way.")
        default: return app
        }
    }

    private func string(_ params: [String: Any], _ key: String) -> String? {
        (params[key] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The outcome of an action, then the app's window as it is now.
    private func afterAction(_ message: String, app: DesktopApp, delay: TimeInterval = 0.4) async -> [String: Any] {
        // A window that is opening or closing may need a moment before it shows.
        for wait in [delay, 0.6] {
            try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            if let current = app.pid.flatMap(DesktopApps.running(pid:)),
               let snapshot = try? DesktopReader.snapshot(app: current, windowQuery: nil, refs: refs) {
                return ["text": message + "\n### Snapshot\n" + snapshot.text]
            }
        }
        return ["text": message + "\n[\(app.name) shows no window now.]"]
    }

    private func dispatch(_ method: String, _ params: [String: Any]) async throws -> [String: Any] {
        switch method {
        case "status":
            return ["accessibility": AXIsProcessTrusted(), "screen_recording": CGPreflightScreenCaptureAccess()]
        case "reset":
            refs.reset()
            return [:]
        case "resolve":
            try requireAccessibility()
            return try DesktopApps.resolve(string(params, "app") ?? "", includeInstalled: params["installed"] as? Bool ?? false).json
        case "list_windows":
            try requireAccessibility()
            return ["text": DesktopReader.listWindows(refs: refs)]
        case "open_app":
            try requireAccessibility()
            let found = try DesktopApps.resolve(string(params, "app") ?? "", includeInstalled: true)
            guard found.tier != .blocked else { throw VoiceError.message("\(found.name) is not accessible to JAIS Voice: \(found.note ?? "").") }
            let opened = try await DesktopActions.open(app: found, windowQuery: string(params, "window"), refs: refs)
            return await afterAction("\(opened.name) is open and in front.", app: opened, delay: 0.3)
        case "snapshot":
            let app = try app(params, need: .read)
            let snapshot = try DesktopReader.snapshot(app: app, windowQuery: string(params, "window"), refs: refs)
            var text = snapshot.text
            // Canvas apps and games expose little to accessibility: read their pixels instead.
            if params["ocr"] as? Bool == true || snapshot.itemCount < 3 {
                do { text += "\n" + (try await DesktopActions.recognizeText(app: app, window: snapshot.window, refs: refs)) }
                catch { text += "\n[Text from the screen unavailable: \(error.localizedDescription)]" }
            }
            return ["text": text]
        case "find":
            let app = try app(params, need: .read)
            return ["text": try DesktopReader.find(app: app, windowQuery: string(params, "window"), text: string(params, "text") ?? "", refs: refs)]
        case "click":
            let app = try app(params, need: .control)
            let message = try DesktopActions.click(app: app, ref: string(params, "ref") ?? "", right: string(params, "button") == "right",
                                                   double: params["double"] as? Bool ?? false, refs: refs)
            return await afterAction(message, app: app)
        case "type":
            let app = try app(params, need: .control)
            let message = try DesktopActions.type(app: app, ref: string(params, "ref") ?? "", text: params["text"] as? String ?? "",
                                                  mode: string(params, "mode"), submit: params["submit"] as? Bool ?? false, refs: refs)
            return await afterAction(message, app: app)
        case "press_key":
            let app = try app(params, need: .control)
            return await afterAction(try DesktopActions.pressKeys(app: app, keys: string(params, "keys") ?? ""), app: app)
        case "menu":
            let app = try app(params, need: .control)
            let result = try DesktopActions.menu(app: app, path: string(params, "path") ?? "")
            return result.chose ? await afterAction(result.text, app: app, delay: 0.6) : ["text": result.text]
        case "scroll":
            let app = try app(params, need: .read)
            let message = try DesktopActions.scroll(app: app, ref: string(params, "ref"), direction: string(params, "direction") ?? "down",
                                                    pages: (params["pages"] as? NSNumber)?.doubleValue ?? 1, refs: refs)
            return await afterAction(message, app: app, delay: 0.3)
        default:
            throw VoiceError.message("Unknown desktop method \"\(method)\".")
        }
    }
}
