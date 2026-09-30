import AppKit
import SwiftUI

private let configurationAccent = JAISBrand.green

struct ConfigurationView: View {
    @ObservedObject var state: TurnState
    @ObservedObject var preferences: AppPreferences
    var onShortcutChange: (Int) -> Void
    var onAuthorizeMicrophone: () -> Void
    var onShowLast: () -> Void
    var onImportAudio: () -> Void
    var onTextRequest: () -> Void
    var onChangeModelMemory: (String) -> Void
    var onToggleModelMemory: () -> Void
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
                    microphone
                    instructions
                    privacy
                    shortcuts
                    Button(L("Saisir une demande sans microphone…"), action: onTextRequest)
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
                Text(L("Réglages de votre assistant vocal")).font(.system(size: 12)).foregroundStyle(.secondary)
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
            Text(L("NETTOYAGE ET AGENT")).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
            ForEach(["1B", "4B"], id: \.self) { size in modelMemoryRow(size) }
            if let error = state.modelMemoryError {
                Text(error).font(.system(size: 10)).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            Button(action: onToggleModelMemory) {
                Label(state.modelsChanging ? L("Mise à jour…") : state.modelMemoryButton,
                      systemImage: state.modelsUnloaded ? "arrow.clockwise" : "memorychip")
            }.disabled(!state.canToggleModelMemory)
        }.padding(13).cardBackground()
    }

    private var asrService: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(L("TRANSCRIPTION · MODÈLE ASR")).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
            modelMemoryRow("ASR")
        }.padding(13).cardBackground()
    }

    private func modelMemoryRow(_ size: String) -> some View {
        let phase = state.modelPhase(size)
        return HStack(spacing: 9) {
            Text(size == "ASR" ? "JAIS-ASR · F5" : size).font(.system(size: 12, weight: .semibold))
            Label(state.changingModel == size ? L("Mise à jour…") : phase.label, systemImage: phase.symbolName)
                .font(.system(size: 11)).foregroundStyle(modelPhaseColor(phase))
                .help(state.modelInventory[size]?.error ?? phase.label)
            Spacer()
            Button(phase == .ready ? L("Décharger") : L("Charger")) { onChangeModelMemory(size) }
                .controlSize(.small).disabled(!state.canChangeModel(size))
                .accessibilityLabel((phase == .ready ? L("Décharger") : L("Charger")) + " " + size)
        }.padding(8).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
    }

    private func modelPhaseColor(_ phase: ModelLoadPhase) -> Color {
        switch phase {
        case .ready: return JAISBrand.green
        case .loading: return configurationAccent
        case .error: return Color(red: 1, green: 0.48, blue: 0.44)
        case .waiting, .unavailable, .unloaded: return .secondary
        }
    }

    private var preferencesCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(L("Langue de l’interface")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Langue de l’interface"), selection: Binding(
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
                Text(L("Maintenir pour parler")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Maintenir pour parler"), selection: Binding(get: { preferences.shortcutChoice }, set: onShortcutChange)) {
                    ForEach(ShortcutChoice.choices.indices, id: \.self) { index in
                        Text(ShortcutChoice.choices[index].label).tag(index)
                    }
                }.labelsHidden().frame(width: 154).disabled(state.isBusy)
            }.padding(12)
            Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
            HStack {
                Text(L("Langue de transcription")).font(.system(size: 12))
                Spacer(minLength: 12)
                Picker(L("Langue de transcription"), selection: Binding(get: { preferences.asrLanguage }, set: { preferences.setASRLanguage($0) })) {
                    ForEach(ASRLanguageChoice.choices) { language in
                        Text(language.label).tag(language.code)
                    }
                }.labelsHidden().frame(width: 154).disabled(state.isBusy)
            }.padding(12)
            Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
            VStack(alignment: .leading, spacing: 5) {
                Toggle(L("Garde-fou de correction"), isOn: Binding(
                    get: { preferences.cleanupGuardEnabled },
                    set: { preferences.setCleanupGuardEnabled($0) }
                )).toggleStyle(.switch).font(.system(size: 12)).disabled(state.isBusy)
                Text(preferences.cleanupGuardEnabled
                     ? L("Bloque les corrections qui semblent modifier votre demande.")
                     : L("Les corrections sont transmises même si elles modifient des mots ou le sens."))
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
                Text(L("Ces réglages seront disponibles à la fin de la demande."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .padding(.horizontal, 12).padding(.bottom, 10)
            }
        }.cardBackground()
    }

    private var microphone: some View {
        HStack(spacing: 10) {
            Image(systemName: preferences.microphoneAuthorized ? "mic.fill" : "mic.slash")
                .foregroundStyle(preferences.microphoneAuthorized ? JAISBrand.green : configurationAccent)
                .font(.system(size: 16)).frame(width: 22)
            VStack(alignment: .leading, spacing: 4) {
                Text("Microphone").font(.system(size: 12, weight: .medium))
                Text(preferences.microphoneRequestPending ? L("Autorisation en cours…") : preferences.microphoneStatus.label)
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
                    .accessibilityLabel(L("Microphone autorisé"))
            }
        }.padding(12).cardBackground()
    }

    private var instructions: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(L("Maintenez {0}, parlez, puis relâchez.", String(describing: preferences.shortcutLabel)))
                .font(.system(size: 12, weight: .medium))
            Text(L("30 secondes maximum par prise. La réponse apparaît dans une petite bulle, sans changer d’application."))
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }.padding(.horizontal, 2)
    }

    private var privacy: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "network").font(.system(size: 12)).padding(.top, 1)
            Text(L("La transcription F5, le nettoyage et l’agent s’exécutent sur ce Mac. Les outils peuvent accéder à Internet pour répondre à vos demandes."))
                .font(.system(size: 10)).lineSpacing(2).fixedSize(horizontal: false, vertical: true)
        }.foregroundStyle(configurationAccent.opacity(0.9)).padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(configurationAccent.opacity(0.055), in: RoundedRectangle(cornerRadius: 9))
    }

    private var shortcuts: some View {
        HStack(spacing: 10) {
            Button(action: onShowLast) { Label(L("Dernier résultat"), systemImage: "text.bubble") }
                .disabled(!state.hasResult)
            Button(action: onImportAudio) { Label(L("Importer un audio"), systemImage: "waveform.badge.plus") }
                .disabled(!state.ready || state.isBusy)
            Spacer(minLength: 0)
            Button(action: onShowServiceLog) { Image(systemName: "doc.text.magnifyingglass") }
                .help(L("Voir le journal des services")).accessibilityLabel(L("Voir le journal des services"))
        }.font(.system(size: 11)).controlSize(.small)
    }

    private var footer: some View {
        VStack(spacing: 0) {
            Divider().overlay(.white.opacity(0.05))
            HStack(spacing: 14) {
                Button(L("Quitter"), action: onQuit).buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
                Spacer()
                Text(L("Fermer laisse l’assistant actif."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                Button(action: onClose) {
                    Text(L("Terminé")).font(.system(size: 12, weight: .semibold))
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
        onChangeModelMemory: @escaping (String) -> Void,
        onToggleModelMemory: @escaping () -> Void,
        onShowServiceLog: @escaping () -> Void,
        onQuit: @escaping () -> Void
    ) {
        self.preferences = preferences
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 580),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
            backing: .buffered, defer: false
        )
        window.title = L("Réglages · JAIS Voice")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.isMovableByWindowBackground = true
        window.backgroundColor = JAISBrand.backgroundNS
        window.appearance = NSAppearance(named: .darkAqua)
        window.setFrameAutosaveName("HorizonVoiceConfiguration")
        super.init(window: window)
        let view = ConfigurationView(
            state: state, preferences: preferences,
            onShortcutChange: onShortcutChange,
            onAuthorizeMicrophone: onAuthorizeMicrophone,
            onShowLast: onShowLast,
            onImportAudio: onImportAudio,
            onTextRequest: onTextRequest,
            onChangeModelMemory: onChangeModelMemory,
            onToggleModelMemory: onToggleModelMemory,
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
        guard let view = window?.contentView else { throw VoiceError.message(L("La fenêtre de réglages est absente.")) }
        view.layoutSubtreeIfNeeded()
        guard let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw VoiceError.message(L("Impossible de capturer les réglages."))
        }
        view.cacheDisplay(in: view.bounds, to: representation)
        guard let data = representation.representation(using: .png, properties: [:]) else {
            throw VoiceError.message(L("Échec de l’export des réglages en PNG."))
        }
        try data.write(to: url)
    }
}
