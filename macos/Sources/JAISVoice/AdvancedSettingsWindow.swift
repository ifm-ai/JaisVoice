import AppKit
import SwiftUI

extension RemoteEndpoint {
    /// One short completion: checks the URL, the key and the model name together.
    /// Returns how long the API took to answer.
    func test() async throws -> TimeInterval {
        guard let url = URL(string: baseURL + "/chat/completions") else {
            throw VoiceError.message(L("Enter the API's base URL, starting with https:// or http://."))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 60
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !apiKey.isEmpty { request.setValue("Bearer " + apiKey, forHTTPHeaderField: "Authorization") }
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "model": model, "stream": false, "messages": [["role": "user", "content": "Reply with OK."]]
        ])
        let started = Date()
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard status == 200, (object?["choices"] as? [Any])?.isEmpty == false else {
            let error = object?["error"]
            let detail = (error as? [String: Any])?["message"] as? String ?? error as? String
                ?? String(decoding: data.prefix(300), as: UTF8.self)
            throw VoiceError.message(L("The API answered {0}: {1}", String(describing: status), detail))
        }
        return Date().timeIntervalSince(started)
    }
}

/// The advanced settings being edited; saved only with Save.
@MainActor final class RemoteAPIDraft: ObservableObject {
    struct Status { let text: String; let failed: Bool }
    @Published var enabled = false
    @Published var baseURL = ""
    @Published var model = ""
    @Published var apiKey = ""
    @Published var reasoning = false
    @Published var status: Status?
    @Published var testing = false
    private var test: Task<Void, Never>?

    var endpoint: RemoteEndpoint {
        let url = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        return RemoteEndpoint(baseURL: RemoteEndpoint.normalizedBaseURL(url) ?? url,
                              model: model.trimmingCharacters(in: .whitespacesAndNewlines),
                              apiKey: apiKey.trimmingCharacters(in: .whitespacesAndNewlines), reasoning: reasoning)
    }
    var canTest: Bool { !testing && RemoteEndpoint.normalizedBaseURL(baseURL) != nil && !endpoint.model.isEmpty }

    func load(_ preferences: AppPreferences) {
        test?.cancel(); testing = false; status = nil
        enabled = preferences.remoteEnabled
        baseURL = preferences.remoteDraft.baseURL
        model = preferences.remoteDraft.model
        apiKey = preferences.remoteDraft.apiKey
        reasoning = preferences.remoteDraft.reasoning
    }

    func runTest() {
        let endpoint = endpoint
        testing = true; status = nil
        test = Task { @MainActor in
            defer { self.testing = false }
            do {
                let seconds = try await endpoint.test()
                guard !Task.isCancelled else { return }
                self.status = Status(text: L("Connected · {0} answered in {1} s", endpoint.model, String(format: "%.1f", seconds)), failed: false)
            } catch {
                guard !Task.isCancelled else { return }
                self.status = Status(text: ServiceDiagnostics.concise(error.localizedDescription), failed: true)
            }
        }
    }
}

struct AdvancedSettingsView: View {
    @ObservedObject var state: TurnState
    @ObservedObject var preferences: AppPreferences
    @ObservedObject var draft: RemoteAPIDraft
    var onSave: () -> Void
    var onCancel: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 12) {
                Text(L("Advanced settings")).font(.system(size: 18, weight: .semibold, design: .rounded))
                Text(L("LANGUAGE MODEL API")).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                apiCard
                testRow
                if draft.enabled {
                    Label(L("Your requests, cleaned transcripts and what the agent reads (pages, files, windows) are sent to this server."),
                          systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 10)).foregroundStyle(Color.orange).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 22).padding(.top, 26).padding(.bottom, 14)
            footer
        }
        .frame(width: 460, height: 540)
        .background(JAISBrand.background)
        .preferredColorScheme(.dark)
        .environment(\.layoutDirection, preferences.interfaceLanguage.layoutDirection)
        .tint(JAISBrand.green)
    }

    private var apiCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Toggle(isOn: $draft.enabled) {
                    Text(L("Use an OpenAI-compatible API")).font(.system(size: 12, weight: .medium))
                }.toggleStyle(.switch).controlSize(.small)
                Text(L("Transcript cleanup and the agent use this API instead of the local Jais 3 model, for much larger models. Transcription stays on this Mac."))
                    .font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }.padding(12)
            divider
            field(L("Base URL")) { TextField("https://api.openai.com/v1", text: $draft.baseURL) }
            divider
            field(L("API key")) { SecureField(L("Optional for local servers"), text: $draft.apiKey) }
            divider
            field(L("Model")) { TextField("gpt-4.1", text: $draft.model) }
            divider
            VStack(alignment: .leading, spacing: 4) {
                Toggle(isOn: $draft.reasoning) { Text(L("Reasoning model")).font(.system(size: 12)) }
                    .toggleStyle(.switch).controlSize(.small)
                Text(L("Sends the Reasoning setting as reasoning_effort. Leave off if the API refuses it."))
                    .font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }.padding(12)
        }
        .cardBackground()
        .disabled(state.isBusy)
    }

    private func field<Field: View>(_ title: String, @ViewBuilder input: () -> Field) -> some View {
        HStack(spacing: 12) {
            Text(title).font(.system(size: 12)).frame(width: 90, alignment: .leading)
            input().textFieldStyle(.roundedBorder).font(.system(size: 12)).autocorrectionDisabled()
        }.padding(.horizontal, 12).padding(.vertical, 9)
    }

    private var divider: some View {
        Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
    }

    private var testRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Button(action: draft.runTest) { Label(L("Test connection"), systemImage: "bolt.horizontal") }
                .controlSize(.small).disabled(!draft.canTest)
            if draft.testing {
                ProgressView().controlSize(.small).scaleEffect(0.6).frame(width: 14, height: 14)
            } else if let status = draft.status {
                Text(status.text).font(.system(size: 10))
                    .foregroundStyle(status.failed ? Color(red: 1, green: 0.48, blue: 0.44) : JAISBrand.green)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
    }

    private var footer: some View {
        VStack(spacing: 0) {
            Divider().overlay(.white.opacity(0.05))
            HStack(spacing: 12) {
                if state.isBusy {
                    Text(L("These settings will be available when the request finishes."))
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
                Spacer()
                Button(L("Cancel"), action: onCancel).buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
                    .keyboardShortcut(.cancelAction)
                Button(action: onSave) {
                    Text(L("Save")).font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(JAISBrand.forest)
                        .padding(.horizontal, 16).padding(.vertical, 7)
                        .background(JAISBrand.green, in: RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain).keyboardShortcut(.defaultAction).disabled(state.isBusy)
            }.padding(.horizontal, 22).padding(.vertical, 12)
        }
    }
}

/// Settings kept out of the main window: an OpenAI-compatible API for the language model.
@MainActor final class AdvancedSettingsWindowController: NSWindowController {
    private let preferences: AppPreferences
    private let draft = RemoteAPIDraft()

    /// `onSave` applies the API settings, or throws why they cannot be saved.
    init(state: TurnState, preferences: AppPreferences, onSave: @escaping (Bool, RemoteEndpoint) throws -> Void) {
        self.preferences = preferences
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 460, height: 540),
            styleMask: [.titled, .closable, .fullSizeContentView],
            backing: .buffered, defer: false
        )
        window.title = L("Advanced settings")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.isMovableByWindowBackground = true
        window.backgroundColor = JAISBrand.backgroundNS
        window.appearance = NSAppearance(named: .darkAqua)
        super.init(window: window)
        let draft = draft
        let view = AdvancedSettingsView(
            state: state, preferences: preferences, draft: draft,
            onSave: { [weak self] in
                do {
                    try onSave(draft.enabled, draft.endpoint)
                    self?.close()
                } catch {
                    draft.status = .init(text: error.localizedDescription, failed: true)
                }
            },
            onCancel: { [weak self] in self?.close() }
        )
        window.contentViewController = NSHostingController(rootView: view)
        window.center()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// Open with the saved settings; unsaved edits from a previous opening are dropped.
    func show() {
        draft.load(preferences)
        window?.title = L("Advanced settings")
        NSApplication.shared.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }
}
