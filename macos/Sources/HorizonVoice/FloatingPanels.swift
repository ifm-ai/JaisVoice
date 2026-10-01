import AppKit
import SwiftUI

private let accent = JAISBrand.green

private struct PanelMaterial: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 22, style: .continuous)
            .fill(.ultraThinMaterial)
            .overlay(RoundedRectangle(cornerRadius: 22).fill(JAISBrand.background.opacity(0.94)))
            .overlay(RoundedRectangle(cornerRadius: 22).strokeBorder(JAISBrand.green.opacity(0.20), lineWidth: 0.7))
    }
}

struct RecordingCapsule: View {
    @ObservedObject var state: TurnState
    var body: some View {
        HStack(spacing: 12) {
            JAISArtwork().frame(width: 20, height: 20)
            TimelineView(.animation(minimumInterval: 0.07)) { context in
                let phase = context.date.timeIntervalSinceReferenceDate
                HStack(spacing: 3) {
                    ForEach(0..<11, id: \.self) { index in
                        let shape = abs(sin(Double(index) * 0.73 + phase * 4))
                        Capsule().fill(accent.opacity(index.isMultiple(of: 3) ? 0.7 : 1))
                            .frame(width: 3, height: 3 + CGFloat(state.level) * (7 + shape * 18))
                    }
                }.frame(width: 65, height: 28)
            }
            Text(String(format: "%d:%02d", Int(state.duration) / 60, Int(state.duration) % 60))
                .font(.system(size: 12, weight: .medium, design: .monospaced))
                .foregroundStyle(.white.opacity(0.72)).frame(width: 34)
        }
        .frame(width: 180, height: 48)
        .background(PanelMaterial())
        .clipShape(Capsule())
        .preferredColorScheme(.dark)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(L("Enregistrement en cours, {0} secondes", String(describing: Int(state.duration))))
    }
}

private struct ContentHeight: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

struct ResultBubble: View {
    @ObservedObject var state: TurnState
    var preview: Bool
    var onStop: () -> Void
    var onClose: () -> Void
    var onInteract: () -> Void
    var onHeight: (CGFloat) -> Void
    @State private var contentHeight: CGFloat = 70

    private var scrollHeight: CGFloat { min(340, max(48, contentHeight)) }
    private var totalHeight: CGFloat { 140 + scrollHeight }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                ZStack {
                    Circle().fill(accent.opacity(0.13)).frame(width: 27, height: 27)
                    JAISArtwork().frame(width: 18, height: 18)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(preview ? L("JAIS VOICE · APERÇU") : "JAIS VOICE")
                        .font(.system(size: 10, weight: .bold, design: .rounded)).tracking(1.8)
                        .foregroundStyle(.white.opacity(0.92))
                    Text(state.stageLabel).font(.system(size: 11)).foregroundStyle(.white.opacity(0.54))
                }
                Spacer(minLength: 4)
                if state.awaitingClarification {
                    Image(systemName: "bubble.left.and.questionmark.bubble.right.fill")
                        .foregroundStyle(accent).accessibilityLabel(L("En attente de votre réponse"))
                }
                if state.isBusy { ProgressView().controlSize(.small).scaleEffect(0.7).frame(width: 18) }
                Button(action: onClose) { Image(systemName: "xmark").font(.system(size: 10, weight: .semibold)).frame(width: 23, height: 23) }
                    .buttonStyle(.plain).foregroundStyle(.white.opacity(0.55)).help(L("Fermer"))
            }.padding(.horizontal, 17).frame(height: 62)
            Rectangle().fill(.white.opacity(0.08)).frame(height: 1).padding(.horizontal, 16)
            ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 15) {
                    ForEach(state.history) { exchange in
                        VStack(alignment: .leading, spacing: 8) {
                            sectionLabel(L("VOUS"), tag: "")
                            naturalText(exchange.prompt, size: 14)
                            if !exchange.response.isEmpty {
                                sectionLabel("JAIS VOICE", tag: "")
                                naturalText(exchange.response, size: 14)
                            }
                            if let error = exchange.error {
                                naturalText(error, size: 12)
                            }
                            Divider()
                        }
                    }
                    if let error = state.error {
                        Label(error, systemImage: "exclamationmark.circle")
                            .font(.system(size: 13)).foregroundStyle(Color(red: 1, green: 0.72, blue: 0.6))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if !state.cleaned.isEmpty {
                        VStack(alignment: .leading, spacing: 7) {
                            sectionLabel(L("TRANSCRIPTION CORRIGÉE"), tag: state.cleanerModelLabel)
                            naturalText(state.cleaned, size: 15)
                        }
                    } else if !state.raw.isEmpty {
                        VStack(alignment: .leading, spacing: 7) {
                            sectionLabel("TRANSCRIPTION", tag: "ASR")
                            naturalText(state.raw, size: 14).foregroundStyle(.white.opacity(0.68))
                        }
                    } else if state.isBusy {
                        Text(L("Votre voix devient du texte."))
                            .font(.system(size: 13)).foregroundStyle(.white.opacity(0.48))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if !state.raw.isEmpty && !state.cleaned.isEmpty {
                        VStack(alignment: .leading, spacing: 9) {
                            Button {
                                onInteract(); state.showOriginal.toggle()
                            } label: {
                                Label(state.showOriginal ? L("Masquer l’original") : L("Voir les corrections"), systemImage: state.showOriginal ? "chevron.up" : "chevron.down")
                                    .font(.system(size: 11, weight: .medium)).foregroundStyle(accent.opacity(0.9))
                            }.buttonStyle(.plain)
                            if state.showOriginal {
                                diffText
                                    .font(.system(size: 13)).lineSpacing(4)
                                    .frame(maxWidth: .infinity, alignment: TranscriptDiff.isRTL(state.raw) ? .trailing : .leading)
                                    .environment(\.layoutDirection, TranscriptDiff.isRTL(state.raw) ? .rightToLeft : .leftToRight)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                    if !state.actions.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            sectionLabel("ACTIONS", tag: state.actions.contains(where: { $0.name == "shell_execute" }) ? L("MACOS ET WEB") : L("NAVIGATEUR"))
                            ForEach(state.actions) { action in actionRow(action) }
                        }
                    }
                    if let request = state.pendingApproval { approvalCard(request) }
                    if !state.response.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            sectionLabel(L(state.awaitingClarification ? "PRÉCISION NÉCESSAIRE" : "RÉPONSE"), tag: state.agentModelLabel)
                            naturalText(state.response, size: 14)
                            if state.awaitingClarification {
                                Label(L("Maintenez les touches de dictée, répondez, puis relâchez."), systemImage: "mic.fill")
                                    .font(.system(size: 12, weight: .medium)).foregroundStyle(accent)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(state.awaitingClarification ? 12 : 0)
                        .background(accent.opacity(state.awaitingClarification ? 0.10 : 0), in: RoundedRectangle(cornerRadius: 12))
                    }
                    Color.clear.frame(height: 1).id("latest-output")
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(GeometryReader { geometry in Color.clear.preference(key: ContentHeight.self, value: geometry.size.height) })
            }
            .scrollIndicators(.hidden)
            .frame(height: scrollHeight)
            .padding(.horizontal, 17).padding(.vertical, 13)
            .onPreferenceChange(ContentHeight.self) { height in
                let bounded = min(10_000, max(48, height))
                if abs(contentHeight - bounded) > 0.5 { contentHeight = bounded; onHeight(140 + min(340, bounded)) }
                scrollToLatest(proxy)
            }
            .onChange(of: state.turnID) { scrollToLatest(proxy) }
            .onChange(of: state.response) { scrollToLatest(proxy) }
            .onChange(of: state.cleaned) { scrollToLatest(proxy) }
            .onChange(of: state.raw) { scrollToLatest(proxy) }
            .onChange(of: state.pendingApproval) { scrollToLatest(proxy) }
            .onAppear { scrollToLatest(proxy) }
            }
            Rectangle().fill(.white.opacity(0.08)).frame(height: 1).padding(.horizontal, 16)
            HStack(spacing: 13) {
                Button {
                    onInteract()
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(state.response.isEmpty ? (state.cleaned.isEmpty ? state.raw : state.cleaned) : state.response, forType: .string)
                } label: { Label(L("Copier"), systemImage: "doc.on.doc").font(.system(size: 11, weight: .medium)) }
                    .buttonStyle(.plain).disabled(state.response.isEmpty && state.cleaned.isEmpty && state.raw.isEmpty)
                Spacer()
                if state.isBusy {
                    Button(action: onStop) { Label(L("Arrêter"), systemImage: "stop.fill").font(.system(size: 11, weight: .medium)) }
                        .buttonStyle(.plain).foregroundStyle(accent)
                } else {
                    Text(preview ? L("Aperçu visuel · aucune inférence") : L("Raccourci micro pour continuer"))
                        .font(.system(size: 9)).foregroundStyle(.white.opacity(0.35))
                }
            }.foregroundStyle(.white.opacity(0.7)).padding(.horizontal, 17).frame(height: 50)
        }
        .frame(width: 380, height: totalHeight)
        .background(PanelMaterial())
        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
        .preferredColorScheme(.dark)
        .onHover { state.hovered = $0 }
        .onAppear { onHeight(totalHeight) }
    }

    private func scrollToLatest(_ proxy: ScrollViewProxy) {
        // Wait for the updated text and panel size to finish their layout.
        DispatchQueue.main.async {
            proxy.scrollTo("latest-output", anchor: .bottom)
        }
    }

    private func sectionLabel(_ title: String, tag: String) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.system(size: 9, weight: .semibold)).tracking(1)
                .lineLimit(1).layoutPriority(1)
            Spacer(minLength: 0)
            Text(tag).font(.system(size: 8, weight: .medium, design: .monospaced)).tracking(0.4)
                .lineLimit(1).truncationMode(.middle).frame(maxWidth: 140, alignment: .trailing)
                .help(tag)
        }.foregroundStyle(.white.opacity(0.4))
    }

    private func naturalText(_ text: String, size: CGFloat) -> some View {
        Text(.init(text))
            .font(.system(size: size)).foregroundStyle(.white.opacity(0.9)).tint(accent)
            .lineSpacing(4).textSelection(.enabled)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: TranscriptDiff.isRTL(text) ? .trailing : .leading)
            .environment(\.layoutDirection, TranscriptDiff.isRTL(text) ? .rightToLeft : .leftToRight)
    }

    private var diffText: Text {
        TranscriptDiff.words(original: state.raw, cleaned: state.cleaned).reduce(Text("")) { result, word in
            result + Text(word.text + " ")
                .strikethrough(word.removed, pattern: .solid, color: .orange.opacity(0.8))
                .foregroundColor(word.removed ? .orange.opacity(0.7) : .white.opacity(0.65))
        }
    }

    /// Minimal approval inside the bubble: what runs, then Refuse / Allow.
    private func approvalCard(_ request: ActionRequest) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            sectionLabel(L("AUTORISATION"), tag: L(request.tool))
            if !request.reason.isEmpty { naturalText(request.reason, size: 13) }
            ScrollView {
                Text(request.details)
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(.white.opacity(0.75))
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).environment(\.layoutDirection, .leftToRight)
            }
            .frame(maxHeight: 90).fixedSize(horizontal: false, vertical: true)
            .padding(9).background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 9))
            HStack(spacing: 10) {
                Spacer()
                Button { onInteract(); state.onApproval?(false) } label: {
                    Text(L("Refuser")).font(.system(size: 12, weight: .medium))
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(Color.white.opacity(0.08), in: RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain).foregroundStyle(.white.opacity(0.85))
                Button { onInteract(); state.onApproval?(true) } label: {
                    Text(L("Autoriser")).font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 14).padding(.vertical, 6)
                        .background(accent, in: RoundedRectangle(cornerRadius: 7))
                }.buttonStyle(.plain).foregroundStyle(JAISBrand.forest)
            }
        }
        .padding(12)
        .background(accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(accent.opacity(0.35), lineWidth: 0.7))
    }

    private func actionRow(_ action: ToolAction) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Button {
                onInteract()
                if state.expandedTools.contains(action.id) { state.expandedTools.remove(action.id) }
                else { state.expandedTools.insert(action.id) }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: action.errorCode != nil ? "exclamationmark.circle.fill" : (action.finished ? "checkmark.circle.fill" : "arrow.up.right.circle"))
                        .foregroundStyle(action.errorCode != nil ? Color(red: 1, green: 0.48, blue: 0.44) : (action.finished ? JAISBrand.green : accent))
                    Text(action.label + (action.errorCode != nil ? L(" · échec") : "")).font(.system(size: 12))
                    Spacer()
                    Image(systemName: state.expandedTools.contains(action.id) ? "chevron.up" : "chevron.down").font(.system(size: 8))
                }.foregroundStyle(.white.opacity(0.75))
            }.buttonStyle(.plain)
            if state.expandedTools.contains(action.id) {
                Text([action.name, action.arguments, String(action.result.prefix(6_000))].filter { !$0.isEmpty }.joined(separator: "\n\n"))
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(.white.opacity(0.52))
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading).environment(\.layoutDirection, .leftToRight)
                    .padding(10).background(Color.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
            }
        }
    }
}

final class NonActivatingPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

@MainActor final class FloatingPanels {
    let recording: NSPanel
    let result: NSPanel
    private var anchor: NSRect = .zero
    private var hideTimer: Timer?
    private var clock = DismissalClock()
    private let state: TurnState
    var preview = false

    init(state: TurnState, preview: Bool, onStop: @escaping () -> Void) {
        self.state = state; self.preview = preview
        recording = Self.panel(width: 180, height: 48)
        result = Self.panel(width: 380, height: 220)
        recording.contentViewController = NSHostingController(rootView: RecordingCapsule(state: state))
        let view = ResultBubble(state: state, preview: preview, onStop: onStop, onClose: { [weak self] in self?.hide() }, onInteract: { [weak self] in self?.interacted() }, onHeight: { [weak self] height in self?.resize(height: height) })
        result.contentViewController = NSHostingController(rootView: view)
        anchorToPointer()
    }

    private static func panel(width: CGFloat, height: CGFloat) -> NSPanel {
        let panel = NonActivatingPanel(contentRect: NSRect(x: 0, y: 0, width: width, height: height), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = true
        panel.level = .floating; panel.hidesOnDeactivate = false; panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
        panel.isMovable = false; panel.becomesKeyOnlyIfNeeded = true
        panel.animationBehavior = .utilityWindow
        return panel
    }

    func anchorToPointer() {
        anchor = (NSScreen.screens.first(where: { $0.frame.contains(NSEvent.mouseLocation) }) ?? NSScreen.main)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
    }
    private func origin(for size: NSSize) -> NSPoint {
        NSPoint(x: anchor.midX - size.width / 2, y: anchor.minY + 22)
    }
    func showRecording() {
        cancelDismissal()
        if state.history.isEmpty { result.orderOut(nil) }
        var position = origin(for: recording.frame.size)
        if result.isVisible { position.y = result.frame.maxY + 10 }
        recording.setFrameOrigin(position); recording.orderFrontRegardless()
    }
    func showResult() {
        cancelDismissal()
        recording.orderOut(nil); result.setFrameOrigin(origin(for: result.frame.size)); result.orderFrontRegardless()
        if state.finished { scheduleHide() }
    }
    func resize(height: CGFloat) {
        let size = NSSize(width: 380, height: min(480, max(188, height)))
        if ProcessInfo.processInfo.environment["HORIZON_DEBUG_LAYOUT"] == "1" { print("Horizon layout: resize \(size)") }
        result.setFrame(NSRect(origin: origin(for: size), size: size), display: true)
    }
    func hide() {
        cancelDismissal()
        recording.orderOut(nil); result.orderOut(nil); state.hovered = false
    }
    func interacted() { clock.reset() }
    func cancelDismissal() {
        hideTimer?.invalidate(); hideTimer = nil
    }
    func scheduleHide() {
        cancelDismissal()
        guard !preview, state.finished, !state.isBusy, !state.awaitingClarification,
              let finishedTurn = state.turnID else { return }
        clock.reset()
        hideTimer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] timer in
            // Timer callbacks run on the main run loop. Avoid queuing a Task
            // that could survive invalidation and close a subsequent turn.
            MainActor.assumeIsolated {
                guard let self, self.hideTimer === timer else { return }
                guard self.state.turnID == finishedTurn, self.state.finished,
                      !self.state.isBusy else {
                    self.cancelDismissal()
                    return
                }
                let pause = self.state.hovered || self.result.isKeyWindow
                if self.clock.tick(at: Date(), paused: pause) { self.hide() }
            }
        }
    }
    func savePreview(to url: URL) throws {
        guard let view = result.contentView,
              let representation = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            throw VoiceError.message(L("Impossible de capturer l’aperçu."))
        }
        view.cacheDisplay(in: view.bounds, to: representation)
        guard let data = representation.representation(using: .png, properties: [:]) else { throw VoiceError.message(L("Échec de l’export PNG.")) }
        try data.write(to: url)
    }
}
