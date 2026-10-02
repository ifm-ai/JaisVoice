import AppKit
import SwiftUI

private let configurationAccent = JAISBrand.green

struct ConfigurationView: View {
    @ObservedObject var state: TurnState
    @ObservedObject var preferences: AppPreferences
    @ObservedObject var modelStore: ModelStore
    var onShortcutChange: (Int) -> Void
    var onAuthorizeMicrophone: () -> Void
    var onShowLast: () -> Void
    var onImportAudio: () -> Void
    var onTextRequest: () -> Void
    var onChangeModelMemory: (String) -> Void
    var onToggleModelMemory: () -> Void
    var onDownloadModel: (String) -> Void
    var onCancelDownload: (String) -> Void
    var onDeleteModel: (String) -> Void
    var onShowServiceLog: () -> Void
    var onClose: () -> Void
    var onQuit: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    asrService
                    services
                    preferencesCard
                    actionsCard
                    microphone
                    instructions
                    privacy
                    shortcuts
                    Button(L("Type a request without a microphone…"), action: onTextRequest)
                        .font(.system(size: 11)).disabled(!state.ready || state.isBusy)
                }
                .padding(.horizontal, 22)
                .padding(.bottom, 16)
            }
            .scrollIndicators(.hidden)
            footer
        }
        .frame(width: 480, height: 580)
        .background(JAISBrand.background)
        .preferredColorScheme(.dark)
        .tint(configurationAccent)
    }

    private var header: some View {
        HStack(spacing: 12) {
            JAISArtwork(fullLogo: true)
                .frame(width: 124, height: 47)
                .padding(.horizontal, 12).padding(.vertical, 10)
                .background(JAISBrand.paper, in: RoundedRectangle(cornerRadius: 13))
            VStack(alignment: .leading, spacing: 4) {
                Text("Voice").font(.system(size: 21, weight: .semibold, design: .rounded))
                Text(L("Your voice assistant settings")).font(.system(size: 12)).foregroundStyle(.secondary)
            }
            Spacer()
        }.padding(.horizontal, 22).padding(.top, 24).padding(.bottom, 14)
    }

    private var services: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 8) {
                Circle().fill(state.ready ? JAISBrand.green : configurationAccent).frame(width: 7, height: 7).padding(.top, 4)
                Text(L(state.serviceMessage)).font(.system(size: 12, weight: .medium))
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }.accessibilityElement(children: .combine)
            Text(L("CLEANUP AND AGENT")).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
            if modelStore.languageModels.isEmpty {
                Text(modelStore.loaded ? L("No model in the catalog.") : L("Reading the model catalog…"))
                    .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            ForEach(modelStore.languageModels) { model in modelRow(model) }
            ForEach([state.modelMemoryError, modelStore.error].compactMap { $0 }, id: \.self) { error in
                Text(error).font(.system(size: 10)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            Button(action: onToggleModelMemory) {
                Label(state.modelsChanging ? L("Updating…") : state.modelMemoryButton,
                      systemImage: state.modelsUnloaded ? "arrow.clockwise" : "memorychip")
            }.disabled(!state.canToggleModelMemory)
        }.padding(13).cardBackground()
    }

    private var asrService: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(L("TRANSCRIPTION · ASR MODEL")).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
            if let asr = modelStore.asr { modelRow(asr) }
        }.padding(13).cardBackground()
    }

    /// Installation and memory state of one catalog model, with its actions.
    private func modelRow(_ model: CatalogModel) -> some View {
        let key = model.memoryKey
        let phase: ModelLoadPhase = model.installed ? state.modelPhase(key) : .notInstalled
        let progress = modelStore.downloads[model.id]
        let deleting = modelStore.deleting.contains(model.id)
        return HStack(spacing: 9) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.isASR ? model.display_name : key).font(.system(size: 12, weight: .semibold))
                Text(modelDetail(model)).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
            }
            if let progress {
                // Redrawn every second so a download that stops receiving data shows it.
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    if progress.isActive(at: context.date) {
                        ProgressView().controlSize(.small).scaleEffect(0.6).frame(width: 14, height: 14)
                            .help(L("Downloading…"))
                    } else {
                        Image(systemName: "exclamationmark.circle").font(.system(size: 11)).foregroundStyle(.orange)
                            .frame(width: 14, height: 14).help(L("No data received for a while"))
                    }
                }
                ProgressView(value: progress.fraction ?? 0).frame(width: 70)
                    .accessibilityLabel(L("Downloading {0}", String(describing: model.display_name)))
                Text(downloadLabel(progress)).font(.system(size: 10)).foregroundStyle(.secondary).monospacedDigit()
            } else if deleting {
                Label(L("Deleting…"), systemImage: "trash").font(.system(size: 11)).foregroundStyle(.secondary)
            } else if model.installed || model.downloadable {
                Label(state.changingModel == key ? L("Updating…") : phase.label, systemImage: phase.symbolName)
                    .font(.system(size: 11)).foregroundStyle(modelPhaseColor(phase))
                    .help(state.modelInventory[key]?.error ?? phase.label)
            }
            Spacer(minLength: 4)
            if progress != nil {
                Button(L("Cancel")) { onCancelDownload(model.id) }.controlSize(.small)
            } else if !model.installed {
                if model.downloadable {
                    Button(L("Download")) { onDownloadModel(model.id) }.controlSize(.small)
                        .disabled(deleting || state.isBusy)
                } else {
                    Text(L("Coming soon")).font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                }
            } else {
                Button(phase == .ready ? L("Unload") : L("Load")) { onChangeModelMemory(key) }
                    .controlSize(.small).disabled(!state.canChangeModel(key) || deleting)
                    .accessibilityLabel((phase == .ready ? L("Unload") : L("Load")) + " " + key)
                Button { onDeleteModel(model.id) } label: { Image(systemName: "trash") }
                    .controlSize(.small).disabled(deleting || state.isBusy || state.modelsChanging)
                    .help(L("Delete this model from the Mac")).accessibilityLabel(L("Delete {0}", String(describing: model.display_name)))
            }
        }.padding(8).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
    }

    private func modelDetail(_ model: CatalogModel) -> String {
        let name = model.isASR ? L("Local transcription") : model.display_name
        if model.installed { return name + " · " + ModelStore.formatBytes(model.diskBytes) }
        if !model.downloadable { return name }
        let size = model.download_bytes.map { " · " + ModelStore.formatBytes($0) } ?? ""
        let partial = model.partialBytes > 0 ? " · " + L("can resume") : ""
        return name + size + partial
    }

    private func downloadLabel(_ progress: DownloadProgress) -> String {
        switch progress.phase {
        case "verify": return L("Verifying…")
        case "install": return L("Installing…")
        default: return progress.fraction.map { "\(Int($0 * 100)) %" } ?? L("Preparing…")
        }
    }

    private func modelPhaseColor(_ phase: ModelLoadPhase) -> Color {
        switch phase {
        case .ready: return JAISBrand.green
        case .loading: return configurationAccent
        case .error: return Color(red: 1, green: 0.48, blue: 0.44)
        case .waiting, .unavailable, .unloaded, .notInstalled: return .secondary
        }
    }

    private var preferencesCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(L("Interface language")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Interface language"), selection: Binding(
                    get: { preferences.interfaceLanguage },
                    set: { preferences.setInterfaceLanguage($0) }
                )) {
                    ForEach(InterfaceLanguage.allCases, id: \.self) { language in
                        Text(language.label).tag(language)
                    }
                }.labelsHidden().frame(width: 154)
            }.padding(12)
            Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
            HStack {
                Text(L("Hold to talk")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Hold to talk"), selection: Binding(get: { preferences.shortcutChoice }, set: onShortcutChange)) {
                    ForEach(ShortcutChoice.choices.indices, id: \.self) { index in
                        Text(ShortcutChoice.choices[index].label).tag(index)
                    }
                }.labelsHidden().frame(width: 154).disabled(state.isBusy)
            }.padding(12)
            Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
            HStack {
                Text(L("Transcription language")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Transcription language"), selection: Binding(get: { preferences.asrLanguage }, set: { preferences.setASRLanguage($0) })) {
                    ForEach(ASRLanguageChoice.choices) { language in
                        Text(language.label).tag(language.code)
                    }
                }.labelsHidden().frame(width: 154).disabled(state.isBusy)
            }.padding(12)
            Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
            VStack(alignment: .leading, spacing: 5) {
                Toggle(L("Correction safeguard"), isOn: Binding(
                    get: { preferences.cleanupGuardEnabled },
                    set: { preferences.setCleanupGuardEnabled($0) }
                )).toggleStyle(.switch).font(.system(size: 12)).disabled(state.isBusy)
                Text(preferences.cleanupGuardEnabled
                     ? L("Blocks corrections that appear to change your request.")
                     : L("Corrections are sent even if they change words or meaning."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }.padding(12)
            if let error = preferences.shortcutError {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.system(size: 11)).foregroundStyle(configurationAccent)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12).padding(.bottom, 12)
            }
            if state.isBusy {
                Text(L("These settings will be available when the request finishes."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .padding(.horizontal, 12).padding(.bottom, 10)
            }
        }.cardBackground()
    }

    private var actionsCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(L("Actions on this Mac")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Actions on this Mac"), selection: Binding(
                    get: { preferences.actionPermission },
                    set: { preferences.setActionPermission($0) }
                )) {
                    ForEach(ActionPermission.allCases, id: \.self) { permission in
                        Text(permission.label).tag(permission)
                    }
                }.labelsHidden().frame(width: 190)
            }
            if preferences.actionPermission == .always {
                Label(preferences.actionPermission.detail, systemImage: "exclamationmark.triangle.fill")
                    .font(.system(size: 10)).foregroundStyle(configurationAccent)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(preferences.actionPermission.detail)
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }.padding(12).cardBackground()
    }

    private var microphone: some View {
        HStack(spacing: 10) {
            Image(systemName: preferences.microphoneAuthorized ? "mic.fill" : "mic.slash")
                .foregroundStyle(preferences.microphoneAuthorized ? JAISBrand.green : configurationAccent)
                .font(.system(size: 16)).frame(width: 22)
            VStack(alignment: .leading, spacing: 4) {
                Text("Microphone").font(.system(size: 12, weight: .medium))
                Text(preferences.microphoneRequestPending ? L("Requesting access…") : preferences.microphoneStatus.label)
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if !preferences.microphoneAuthorized {
                Button(preferences.microphoneStatus.actionLabel, action: onAuthorizeMicrophone)
                    .controlSize(.small)
                    .disabled(preferences.microphoneRequestPending || state.isBusy)
            } else {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(JAISBrand.green)
                    .accessibilityLabel(L("Microphone access allowed"))
            }
        }.padding(12).cardBackground()
    }

    private var instructions: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(L("Hold {0}, speak, then release.", String(describing: preferences.shortcutLabel)))
                .font(.system(size: 12, weight: .medium))
            Text(L("Up to 30 seconds per recording. The response appears in a small bubble without switching apps."))
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }.padding(.horizontal, 2)
    }

    private var privacy: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "network").font(.system(size: 12)).padding(.top, 1)
            Text(L("F5 transcription, cleanup and the agent run on this Mac. Tools may access the Internet to handle your requests."))
                .font(.system(size: 10)).lineSpacing(2).fixedSize(horizontal: false, vertical: true)
        }.foregroundStyle(configurationAccent.opacity(0.9)).padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(configurationAccent.opacity(0.055), in: RoundedRectangle(cornerRadius: 9))
    }

    private var shortcuts: some View {
        HStack(spacing: 10) {
            Button(action: onShowLast) { Label(L("Last result"), systemImage: "text.bubble") }
                .disabled(!state.hasResult)
            Button(action: onImportAudio) { Label(L("Import audio"), systemImage: "waveform.badge.plus") }
                .disabled(!state.ready || state.isBusy)
            Spacer(minLength: 0)
            Button(action: onShowServiceLog) { Image(systemName: "doc.text.magnifyingglass") }
                .help(L("View service log")).accessibilityLabel(L("View service log"))
        }.font(.system(size: 11)).controlSize(.small)
    }

    private var footer: some View {
        VStack(spacing: 0) {
            Divider().overlay(.white.opacity(0.05))
            HStack(spacing: 14) {
                Button(L("Quit"), action: onQuit).buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
                Spacer()
                Text(L("Closing keeps the assistant running."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                Button(action: onClose) {
                    Text(L("Done")).font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(JAISBrand.forest)
                        .padding(.horizontal, 16).padding(.vertical, 7)
                        .background(configurationAccent, in: RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain).keyboardShortcut(.defaultAction)
            }.padding(.horizontal, 22).padding(.vertical, 14)
        }
    }
}

private extension View {
    func cardBackground() -> some View {
        background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 11))
            .overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(.white.opacity(0.075), lineWidth: 0.7))
    }
}

/// A conventional, focusable settings window. Voice/result panels remain independent
/// and non-activating; closing this window never shuts down the assistant.
@MainActor final class ConfigurationWindowController: NSWindowController {
    private let preferences: AppPreferences

    init(
        state: TurnState,
        preferences: AppPreferences,
        onShortcutChange: @escaping (Int) -> Void,
        onAuthorizeMicrophone: @escaping () -> Void,
        onShowLast: @escaping () -> Void,
        onImportAudio: @escaping () -> Void,
        onTextRequest: @escaping () -> Void,
        modelStore: ModelStore,
        onChangeModelMemory: @escaping (String) -> Void,
        onToggleModelMemory: @escaping () -> Void,
        onDownloadModel: @escaping (String) -> Void,
        onCancelDownload: @escaping (String) -> Void,
        onDeleteModel: @escaping (String) -> Void,
        onShowServiceLog: @escaping () -> Void,
        onQuit: @escaping () -> Void
    ) {
        self.preferences = preferences
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 580),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
            backing: .buffered, defer: false
        )
        window.title = L("Settings · JAIS Voice")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.isMovableByWindowBackground = true
        window.backgroundColor = JAISBrand.backgroundNS
        window.appearance = NSAppearance(named: .darkAqua)
        window.setFrameAutosaveName("HorizonVoiceConfiguration")
        super.init(window: window)
        let view = ConfigurationView(
            state: state, preferences: preferences, modelStore: modelStore,
            onShortcutChange: onShortcutChange,
            onAuthorizeMicrophone: onAuthorizeMicrophone,
            onShowLast: onShowLast,
            onImportAudio: onImportAudio,
            onTextRequest: onTextRequest,
            onChangeModelMemory: onChangeModelMemory,
            onToggleModelMemory: onToggleModelMemory,
            onDownloadModel: onDownloadModel,
            onCancelDownload: onCancelDownload,
            onDeleteModel: onDeleteModel,
            onShowServiceLog: onShowServiceLog,
            onClose: { [weak self] in self?.close() },
            onQuit: onQuit
        )
        window.contentViewController = NSHostingController(rootView: view)
        window.center()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func show() {
        preferences.refreshMicrophoneStatus()
        NSApplication.shared.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    /// Render the real SwiftUI content for visual QA, without starting services or audio.
    func renderPreview(to url: URL) throws {
        guard let view = window?.contentView else { throw VoiceError.message(L("The settings window is missing.")) }
        view.layoutSubtreeIfNeeded()
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw VoiceError.message(L("Unable to capture settings."))
        }
        view.cacheDisplay(in: view.bounds, to: representation)
        guard let data = representation.representation(using: .png, properties: [:]) else {
            throw VoiceError.message(L("Unable to export settings as PNG."))
        }
        try data.write(to: url)
    }
}
