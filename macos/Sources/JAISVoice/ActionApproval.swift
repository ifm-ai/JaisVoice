import Foundation

/// One action the agent asks to run on this Mac, as the Pi extension describes it:
/// the confirm title is the tool label, the message a JSON {reason, details}.
/// Requests about other apps and files add `kind` (app_read, app_control, overview,
/// web_after_desktop, files_read, file_write) and `app`, so the question is worded
/// in the interface language.
struct ActionRequest: Equatable {
    let tool: String
    let reason: String
    let details: String
    var kind: String?
    var app: String?

    init(tool: String, reason: String, details: String, kind: String? = nil, app: String? = nil) {
        self.tool = tool; self.reason = reason; self.details = details; self.kind = kind; self.app = app
    }

    init(title: String, message: String) {
        let fields = (try? JSONSerialization.jsonObject(with: Data(message.utf8))) as? [String: Any]
        self.init(tool: title, reason: fields?["reason"] as? String ?? "",
                  details: fields?["details"] as? String ?? message,
                  kind: fields?["kind"] as? String, app: fields?["app"] as? String)
    }

    /// Listing the open windows is not shown for approval; only "never" refuses it.
    var isOverview: Bool { kind == "overview" }

    var question: String {
        switch kind {
        case "app_read": return L("Let Jais Voice read the windows of {0} until this request ends?", app ?? "")
        case "app_control": return L("Let Jais Voice control {0} until this request ends? It can click, type, and use its menus and keys.", app ?? "")
        case "web_after_desktop": return L("Jais Voice read your apps during this request. Open this page?")
        case "files_read": return L("Let Jais Voice read your files until this request ends?")
        // The extension words it: create, replace or change this file.
        case "file_write": return L(reason)
        default: return reason
        }
    }
}

/// Shows each action in the result bubble while the agent waits for the answer.
/// Without an answer within two minutes, or when the turn stops, it is refused.
@MainActor final class ActionApprover {
    static let timeout: TimeInterval = 120
    private let state: TurnState
    private let show: () -> Void
    private var pending: CheckedContinuation<Bool, Never>?
    private var timer: Timer?

    init(state: TurnState, show: @escaping () -> Void) {
        self.state = state; self.show = show
    }

    func approve(_ request: ActionRequest) async -> Bool {
        resolve(false)
        return await withCheckedContinuation { continuation in
            pending = continuation
            state.pendingApproval = request
            state.onApproval = { [weak self] allowed in self?.resolve(allowed) }
            show()
            // Pi gives up a little later; resolving first keeps both sides consistent.
            timer = Timer.scheduledTimer(withTimeInterval: Self.timeout, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated { self?.resolve(false) }
            }
        }
    }

    /// Refuse the displayed action, if any.
    func dismiss() { resolve(false) }

    private func resolve(_ allowed: Bool) {
        timer?.invalidate(); timer = nil
        state.pendingApproval = nil; state.onApproval = nil
        pending?.resume(returning: allowed); pending = nil
    }
}
