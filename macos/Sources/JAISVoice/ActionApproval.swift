import Foundation

/// One action the agent asks to run on this Mac, as the Pi extension describes it:
/// the confirm title is the tool label, the message a JSON {reason, details}.
struct ActionRequest: Equatable {
    let tool: String
    let reason: String
    let details: String

    init(tool: String, reason: String, details: String) {
        self.tool = tool; self.reason = reason; self.details = details
    }

    init(title: String, message: String) {
        let fields = (try? JSONSerialization.jsonObject(with: Data(message.utf8))) as? [String: Any]
        self.init(tool: title, reason: fields?["reason"] as? String ?? "",
                  details: fields?["details"] as? String ?? message)
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
