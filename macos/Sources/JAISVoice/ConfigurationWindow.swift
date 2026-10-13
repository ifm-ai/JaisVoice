import AppKit
import SwiftUI

private let configurationAccent = JAISBrand.green

enum SettingsTab: String, CaseIterable {
    case general, assistant, models

    var label: String {
        switch self {
        case .general: return L("General")
        case .assistant: return L("Assistant")
        case .models: return L("Models")
        }
    }

    var symbolName: String {
        switch self {
        case .general: return "gearshape"
        case .assistant: return "waveform"
        case .models: return "shippingbox"
        }
    }
}

/// The selected settings tab, kept by the window across openings.
@MainActor final class SettingsNavigation: ObservableObject {
    @Published var tab: SettingsTab = .general
}

struct ConfigurationView: View {
    @ObservedObject var state: TurnState
    @ObservedObject var preferences: AppPreferences
    @ObservedObject var modelStore: ModelStore
    @ObservedObject var navigation: SettingsNavigation
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
    var onShowAdvanced: () -> Void
    var onClose: () -> Void
    var onQuit: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            header
            tabBar
            if state.isBusy {
                Text(L("These settings will be available when the request finishes."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity).padding(.top, 8)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    switch navigation.tab {
                    case .general: generalTab
                    case .assistant: assistantTab
                    case .models: modelsTab
                    }
                }
                .padding(.horizontal, 22)
                .padding(.vertical, 14)
            }
            .scrollIndicators(.hidden)
            footer
        }
        .frame(width: 480, height: 540)
        .background(JAISBrand.background)
        .preferredColorScheme(.dark)
        .environment(\.layoutDirection, preferences.interfaceLanguage.layoutDirection)
        .tint(configurationAccent)
    }

    private var header: some View {
        HStack(spacing: 12) {
            JAISArtwork(fullLogo: true)
                .frame(width: 88, height: 33)
                .padding(.horizontal, 9).padding(.vertical, 7)
                .background(JAISBrand.paper, in: RoundedRectangle(cornerRadius: 10))
            VStack(alignment: .leading, spacing: 3) {
                Text("Voice").font(.system(size: 18, weight: .semibold, design: .rounded))
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Circle().fill(state.ready ? JAISBrand.green : Color.orange).frame(width: 7, height: 7)
                    Text(L(state.serviceMessage)).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(2)
                }.accessibilityElement(children: .combine)
            }
            Spacer(minLength: 0)
        }.padding(.horizontal, 22).padding(.top, 22).padding(.bottom, 12)
    }

    // MARK: Tabs

    private var tabBar: some View {
        HStack(spacing: 4) {
            ForEach(Array(SettingsTab.allCases.enumerated()), id: \.element) { index, tab in
                let selected = navigation.tab == tab
                Button { navigation.tab = tab } label: {
                    HStack(spacing: 6) {
                        Image(systemName: tab.symbolName).font(.system(size: 11))
                        Text(tab.label).font(.system(size: 12, weight: selected ? .semibold : .regular))
                        if needsAttention(tab) {
                            Circle().fill(Color.orange).frame(width: 6, height: 6)
                                .accessibilityLabel(L("Needs attention"))
                        }
                    }
                    .foregroundStyle(selected ? .primary : .secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, 6)
                    .background(selected ? .white.opacity(0.09) : .clear, in: RoundedRectangle(cornerRadius: 7))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .keyboardShortcut(KeyEquivalent(Character(String(index + 1))), modifiers: .command)
                .accessibilityAddTraits(selected ? .isSelected : [])
            }
        }
        .padding(3).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 9))
        .padding(.horizontal, 22)
    }

    /// A tab is flagged while something on it keeps the assistant from working.
    private func needsAttention(_ tab: SettingsTab) -> Bool {
        switch tab {
        case .general: return !preferences.microphoneAuthorized
        case .assistant: return false
        case .models: return modelStore.blockingMessage != nil
        }
    }

    private var generalTab: some View {
        Group {
            instructions
            microphone
            generalCard
            shortcuts
        }
    }

    private var assistantTab: some View {
        Group {
            voiceCard
            actionsCard
            appControl
            privacy
        }
    }

    private var modelsTab: some View {
        Group {
            asrService
            if let endpoint = preferences.remoteEndpoint { remoteService(endpoint) }
            services
            HStack {
                Spacer()
                Button(action: onShowAdvanced) { Label(L("Advanced settings…"), systemImage: "slider.horizontal.3") }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
    }

    /// The OpenAI-compatible API that replaces the local language model, set in the advanced settings.
    private func remoteService(_ endpoint: RemoteEndpoint) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle(L("CLEANUP AND AGENT · API"))
            HStack(spacing: 9) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(endpoint.model).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                    Text(endpoint.host).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                Button(L("Change…"), action: onShowAdvanced).controlSize(.small)
            }.padding(8).background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
            Text(L("The local language models below are not used while the API is on."))
                .font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(13).cardBackground()
    }

    // MARK: Models

    private var services: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle(L("CLEANUP AND AGENT"))
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
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle(L("TRANSCRIPTION · ASR MODEL"))
            if let asr = modelStore.asr { modelRow(asr) }
        }.padding(13).cardBackground()
    }

    private func sectionTitle(_ title: String) -> some View {
        Text(title).font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
    }

    /// Installation and memory state of one catalog model, with its actions.
    private func modelRow(_ model: CatalogModel) -> some View {
        let key = model.memoryKey
        let phase: ModelLoadPhase = model.installed ? state.phase(of: key) : .notInstalled
        let progress = modelStore.downloads[model.id]
        let deleting = modelStore.deleting.contains(model.id)
        return HStack(spacing: 9) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.isASR ? model.display_name : key).font(.system(size: 12, weight: .semibold))
                Text(progress.flatMap(transferDetail) ?? modelDetail(model)).font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
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
            } else {
                Label(state.changingModel == key ? L("Updating…") : phase.label, systemImage: phase.symbolName)
                    .font(.system(size: 11)).foregroundStyle(modelPhaseColor(phase))
                    .help(state.modelInventory[key]?.error ?? phase.label)
            }
            Spacer(minLength: 4)
            if progress != nil {
                Button(L("Cancel")) { onCancelDownload(model.id) }.controlSize(.small)
            } else if !model.installed {
                Button(L("Download")) { onDownloadModel(model.id) }.controlSize(.small)
                    .disabled(deleting || state.isBusy)
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
        let size = model.download_bytes.map { " · " + ModelStore.formatBytes($0) } ?? ""
        let partial = model.partialBytes > 0 ? " · " + L("can resume") : ""
        return name + size + partial
    }

    /// "2.4 GB / 5.4 GB · 35 MB/s" while bytes are downloading.
    private func transferDetail(_ progress: DownloadProgress) -> String? {
        guard progress.phase == "download", progress.total > 0 else { return nil }
        let detail = ModelStore.formatBytes(min(progress.bytes, progress.total)) + " / " + ModelStore.formatBytes(progress.total)
        return progress.speed.map { detail + " · " + ModelStore.formatBytes(Int64($0)) + "/s" } ?? detail
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

    // MARK: Rows

    /// A setting's name on the left, its control on the right.
    private func settingRow<Control: View>(_ title: String, @ViewBuilder control: () -> Control) -> some View {
        HStack {
            Text(title).font(.system(size: 12))
            Spacer(minLength: 12)
            control()
        }.padding(12)
    }

    private var rowDivider: some View {
        Divider().overlay(.white.opacity(0.05)).padding(.horizontal, 12)
    }

    // MARK: General

    private var instructions: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(L("Hold {0}, speak, then release.", String(describing: preferences.shortcutLabel)))
                .font(.system(size: 12, weight: .medium))
            Text(L("Up to 30 seconds per recording. The response appears in a small bubble without switching apps."))
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }.padding(.horizontal, 2)
    }

    private var microphone: some View {
        HStack(spacing: 10) {
            Image(systemName: preferences.microphoneAuthorized ? "mic.fill" : "mic.slash")
                .foregroundStyle(preferences.microphoneAuthorized ? JAISBrand.green : Color.orange)
                .font(.system(size: 14)).frame(width: 20)
            VStack(alignment: .leading, spacing: 3) {
                Text(L("Microphone")).font(.system(size: 12, weight: .medium))
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

    private var generalCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            settingRow(L("Hold to talk")) {
                Picker(L("Hold to talk"), selection: Binding(get: { preferences.shortcutChoice }, set: onShortcutChange)) {
                    ForEach(ShortcutChoice.choices.indices, id: \.self) { index in
                        Text(ShortcutChoice.choices[index].label).tag(index)
                    }
                }.labelsHidden().frame(width: 154).disabled(state.isBusy)
            }
            if let error = preferences.shortcutError {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.system(size: 11)).foregroundStyle(Color.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 12).padding(.bottom, 12)
            }
            rowDivider
            settingRow(L("Interface language")) {
                Picker(L("Interface language"), selection: Binding(
                    get: { preferences.interfaceLanguage },
                    set: { preferences.setInterfaceLanguage($0) }
                )) {
                    ForEach(InterfaceLanguage.allCases, id: \.self) { language in
                        Text(language.label).tag(language)
                    }
                }.labelsHidden().frame(width: 154)
            }
        }.cardBackground()
    }

    private var shortcuts: some View {
        HStack(spacing: 8) {
            Button(action: onShowLast) { Label(L("Last result"), systemImage: "text.bubble") }
                .disabled(!state.hasResult)
            Button(action: onImportAudio) { Label(L("Import audio"), systemImage: "waveform.badge.plus") }
                .disabled(!state.ready || state.isBusy)
            Button(action: onTextRequest) { Label(L("Type a request"), systemImage: "keyboard") }
                .disabled(!state.ready || state.isBusy)
                .help(L("Type a request without a microphone…"))
            Spacer(minLength: 0)
        }.font(.system(size: 11)).controlSize(.small)
    }

    // MARK: Assistant

    private var voiceCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            settingRow(L("Transcription language")) {
                Picker(L("Transcription language"), selection: Binding(get: { preferences.asrLanguage }, set: { preferences.setASRLanguage($0) })) {
                    ForEach(ASRLanguageChoice.choices) { language in
                        Text(language.label).tag(language.code)
                    }
                }.labelsHidden().frame(width: 190).disabled(state.isBusy)
            }
            rowDivider
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text(L("Reasoning")).font(.system(size: 12))
                    Spacer(minLength: 12)
                    Text(preferences.reasoningEffort.detail)
                        .font(.system(size: 11, weight: .medium)).foregroundStyle(configurationAccent)
                        .contentTransition(.opacity)
                }
                ReasoningSlider(selection: Binding(get: { preferences.reasoningEffort }, set: { preferences.setReasoningEffort($0) }))
                    .disabled(state.isBusy)
            }.padding(12)
                .help(L("How long the model thinks before it answers or acts. Higher is more careful, but slower."))
        }.cardBackground()
    }

    private var actionsCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            settingRow(L("Actions on this Mac")) {
                Picker(L("Actions on this Mac"), selection: Binding(
                    get: { preferences.actionPermission },
                    set: { preferences.setActionPermission($0) }
                )) {
                    ForEach(ActionPermission.allCases, id: \.self) { permission in
                        Text(permission.label).tag(permission)
                    }
                }.labelsHidden().frame(width: 190)
            }
            Group {
                if preferences.actionPermission == .always {
                    Label(preferences.actionPermission.detail, systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(Color.orange)
                } else {
                    Text(preferences.actionPermission.detail).foregroundStyle(.secondary)
                }
            }
            .font(.system(size: 10)).fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 12).padding(.bottom, 12).padding(.top, -4)
        }.cardBackground()
    }

    /// The macOS permissions behind the assistant's access to other apps.
    private var appControl: some View {
        VStack(alignment: .leading, spacing: 0) {
            permissionRow(L("Control of your apps"), symbol: "macwindow.on.rectangle", allowed: preferences.accessibilityTrusted,
                          detail: preferences.accessibilityTrusted ? L("Allowed · the assistant can read and use your apps' windows")
                                                                   : L("Accessibility permission, to read and use your apps' windows"),
                          action: preferences.requestAccessibility)
            rowDivider
            permissionRow(L("Text on screen"), symbol: "text.viewfinder", allowed: preferences.screenRecordingAllowed,
                          detail: preferences.screenRecordingAllowed ? L("Allowed · text drawn in a window can be read")
                                                                     : L("Optional: reads text drawn in windows that do not describe themselves"),
                          action: preferences.requestScreenRecording)
        }.cardBackground()
    }

    private func permissionRow(_ title: String, symbol: String, allowed: Bool, detail: String, action: @escaping () -> Void) -> some View {
        HStack(spacing: 10) {
            Image(systemName: symbol).foregroundStyle(allowed ? JAISBrand.green : .secondary)
                .font(.system(size: 13)).frame(width: 20)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 12, weight: .medium))
                Text(detail).font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if allowed {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(JAISBrand.green).accessibilityLabel(L("Allowed"))
            } else {
                Button(L("Allow…"), action: action).controlSize(.small)
            }
        }.padding(12)
    }

    private var privacy: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "network").font(.system(size: 12)).padding(.top, 1)
            Text(preferences.remoteEndpoint.map { L("Transcription runs on this Mac. Cleanup and the agent use the API at {0}: your requests and what the agent reads are sent there.", String(describing: $0.host)) }
                 ?? L("Transcription, cleanup and the agent run on this Mac. Tools may access the Internet to handle your requests."))
                .font(.system(size: 10)).lineSpacing(2).fixedSize(horizontal: false, vertical: true)
        }.foregroundStyle(.secondary).padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.white.opacity(0.025), in: RoundedRectangle(cornerRadius: 9))
    }

    // MARK: Footer

    private var footer: some View {
        VStack(spacing: 0) {
            Divider().overlay(.white.opacity(0.05))
            HStack(spacing: 12) {
                Button(L("Quit"), action: onQuit).buttonStyle(.plain).font(.system(size: 12)).foregroundStyle(.secondary)
                Button(action: onShowServiceLog) { Image(systemName: "doc.text.magnifyingglass") }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .help(L("View service log")).accessibilityLabel(L("View service log"))
                Spacer()
                Text(L("Closing keeps the assistant running."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                Button(action: onClose) {
                    Text(L("Done")).font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(JAISBrand.forest)
                        .padding(.horizontal, 16).padding(.vertical, 7)
                        .background(configurationAccent, in: RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain).keyboardShortcut(.defaultAction)
            }.padding(.horizontal, 22).padding(.vertical, 12)
        }
    }
}

/// Three-stop slider for the reasoning effort: drag the knob or click a level.
private struct ReasoningSlider: View {
    @Binding var selection: ReasoningEffort
    @Environment(\.isEnabled) private var isEnabled
    private let levels = ReasoningEffort.allCases
    private let knob: CGFloat = 16

    var body: some View {
        VStack(spacing: 7) {
            GeometryReader { geometry in
                let span = geometry.size.width - knob
                let index = CGFloat(levels.firstIndex(of: selection) ?? 0)
                let x = knob / 2 + span * index / CGFloat(levels.count - 1)
                ZStack(alignment: .leading) {
                    Capsule().fill(.white.opacity(0.08)).frame(height: 4).padding(.horizontal, knob / 2)
                    Capsule()
                        .fill(LinearGradient(colors: [JAISBrand.forest, configurationAccent], startPoint: .leading, endPoint: .trailing))
                        .frame(width: max(x - knob / 2, 0), height: 4).offset(x: knob / 2)
                    ForEach(levels.indices, id: \.self) { stop in
                        Circle().fill(CGFloat(stop) <= index ? configurationAccent : .white.opacity(0.22))
                            .frame(width: 6, height: 6)
                            .offset(x: knob / 2 + span * CGFloat(stop) / CGFloat(levels.count - 1) - 3)
                    }
                    Circle().fill(JAISBrand.paper)
                        .overlay(Circle().strokeBorder(configurationAccent, lineWidth: 2))
                        .shadow(color: configurationAccent.opacity(0.55), radius: 5)
                        .frame(width: knob, height: knob).offset(x: x - knob / 2)
                }
                .frame(maxHeight: .infinity).contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { drag in
                    let stop = Int(((drag.location.x - knob / 2) / span * CGFloat(levels.count - 1)).rounded())
                    let level = levels[min(max(stop, 0), levels.count - 1)]
                    if level != selection { withAnimation(.spring(response: 0.3, dampingFraction: 0.75)) { selection = level } }
                })
            }.frame(height: knob)
            HStack(spacing: 0) {
                ForEach(levels, id: \.self) { level in
                    Text(level.title)
                        .font(.system(size: 10, weight: level == selection ? .semibold : .regular))
                        .foregroundStyle(level == selection ? Color.primary : Color.secondary)
                        .frame(maxWidth: .infinity, alignment: level == levels.first ? .leading : level == levels.last ? .trailing : .center)
                        .contentShape(Rectangle())
                        .onTapGesture { withAnimation(.spring(response: 0.3, dampingFraction: 0.75)) { selection = level } }
                }
            }
        }
        .opacity(isEnabled ? 1 : 0.5)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(L("Reasoning"))
        .accessibilityValue("\(selection.title), \(selection.detail)")
        .accessibilityAdjustableAction { direction in
            let index = levels.firstIndex(of: selection) ?? 0
            switch direction {
            case .increment: selection = levels[min(index + 1, levels.count - 1)]
            case .decrement: selection = levels[max(index - 1, 0)]
            @unknown default: break
            }
        }
    }
}

extension View {
    func cardBackground() -> some View {
        background(.white.opacity(0.035), in: RoundedRectangle(cornerRadius: 11))
            .overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(.white.opacity(0.075), lineWidth: 0.7))
    }
}

/// A conventional, focusable settings window. Voice/result panels remain independent
/// and non-activating; closing this window never shuts down the assistant.
@MainActor final class ConfigurationWindowController: NSWindowController {
    private let preferences: AppPreferences
    private let modelStore: ModelStore
    private let navigation = SettingsNavigation()

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
        onShowAdvanced: @escaping () -> Void,
        onQuit: @escaping () -> Void
    ) {
        self.preferences = preferences
        self.modelStore = modelStore
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 480, height: 540),
            styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
            backing: .buffered, defer: false
        )
        window.title = L("Settings · Jais Voice")
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.isMovableByWindowBackground = true
        window.backgroundColor = JAISBrand.backgroundNS
        window.appearance = NSAppearance(named: .darkAqua)
        window.setFrameAutosaveName("JAISVoiceConfiguration")
        super.init(window: window)
        let view = ConfigurationView(
            state: state, preferences: preferences, modelStore: modelStore, navigation: navigation,
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
            onShowAdvanced: onShowAdvanced,
            onClose: { [weak self] in self?.close() },
            onQuit: onQuit
        )
        window.contentViewController = NSHostingController(rootView: view)
        window.center()
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func show() {
        preferences.refreshMicrophoneStatus()
        preferences.refreshAppControlStatus()
        // Open where something keeps the assistant from working; otherwise keep the last tab.
        if !preferences.microphoneAuthorized {
            navigation.tab = .general
        } else if modelStore.blockingMessage != nil {
            navigation.tab = .models
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    func select(_ tab: SettingsTab) {
        navigation.tab = tab
    }
}

extension NSWindowController {
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
