import AppKit
import AVFoundation
import Combine
import UniformTypeIdentifiers

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let state = TurnState()
    private let transport = LocalPipeline()
    private let recorder = AudioRecorder()
    private let shortcut = GlobalShortcut()
    private let launcher = ServiceLauncher()
    private let preferences = AppPreferences()
    private var configuration: ConfigurationWindowController!
    private var panels: FloatingPanels!
    private var statusItem: NSStatusItem!
    private var startupTask: Task<Void, Never>?
    private var uploadTask: Task<Void, Never>?
    private var subscriptions: Set<AnyCancellable> = []
    private var keyHeld = false
    private var shortcutError: String?
    private var serviceRequiresRelaunch = false
    private var shuttingDown = false
    private var preview = false
    private var menuOpen = false
    private var menuRefreshTimer: Timer?
    private var serviceMenuItem: NSMenuItem?
    private var cleanerMenuItem: NSMenuItem?
    private var agentMenuItem: NSMenuItem?
    private var smokeFile: URL?
    private var smokeText: String?
    private var smokeStarted = false
    private var smokeReported = false
    private var latestHealth: ServiceHealth?

    func applicationDidFinishLaunching(_ notification: Notification) {
        preview = CommandLine.arguments.contains("--preview") || CommandLine.arguments.contains("--render-preview") || CommandLine.arguments.contains("--render-settings")
        if let index = CommandLine.arguments.firstIndex(of: "--smoke-file"), CommandLine.arguments.count > index + 1 {
            smokeFile = URL(fileURLWithPath: CommandLine.arguments[index + 1])
        }
        if let index = CommandLine.arguments.firstIndex(of: "--smoke-text"), CommandLine.arguments.count > index + 1 {
            smokeText = CommandLine.arguments[index + 1]
        }
        panels = FloatingPanels(state: state, preview: preview, onStop: { [weak self] in self?.stopCurrent() })
        statusItem = StatusItemPresentation.makeItem(shortcutTitle: preferences.shortcutLabel)
        configuration = ConfigurationWindowController(
            state: state, preferences: preferences,
            onShortcutChange: { [weak self] in self?.registerShortcut($0) },
            onAuthorizeMicrophone: { [weak self] in self?.authorizeMicrophone() },
            onShowLast: { [weak self] in self?.showLast() },
            onImportAudio: { [weak self] in self?.importAudio() },
            onTextRequest: { [weak self] in self?.textRequest() },
            onChangeModelMemory: { [weak self] in self?.changeModelMemory($0) },
            onToggleModelMemory: { [weak self] in self?.toggleModelMemory() },
            onShowServiceLog: { [weak self] in self?.showServiceLog() },
            onQuit: { [weak self] in self?.quit() }
        )
        state.objectWillChange.throttle(for: .milliseconds(150), scheduler: RunLoop.main, latest: true).sink { [weak self] _ in
            self?.updateMenu()
        }.store(in: &subscriptions)
        preferences.objectWillChange.receive(on: RunLoop.main).sink { [weak self] _ in
            self?.state.objectWillChange.send()
            self?.configuration.window?.title = L("Réglages · JAIS Voice")
            self?.updateSelectedModelHealth()
            self?.updateMenu()
        }.store(in: &subscriptions)
        updateMenu()
        state.onBegin = { [weak self] in self?.panels.cancelDismissal() }
        state.onFinish = { [weak self] in
            self?.panels.scheduleHide()
            self?.reportSmokeIfNeeded()
        }
        transport.onEvent = { [weak self] event in self?.receive(event) }
        transport.onDisconnect = { [weak self] message in
            guard let self, !self.shuttingDown, !self.state.modelsChanging, !self.state.modelsUnloaded else { return }
            self.state.ready = false; self.state.serviceMessage = message
            if self.state.isBusy { self.state.fail(message); self.panels.showResult() }
        }
        recorder.onMeter = { [weak self] level, seconds in
            guard self?.state.recording == true else { return }
            self?.state.level = level; self?.state.duration = seconds
        }
        recorder.onLimit = { [weak self] in self?.release() }
        launcher.onExit = { [weak self] _, message in
            guard let self, !self.shuttingDown, !self.state.modelsChanging, !self.state.modelsUnloaded else { return }
            self.startupTask?.cancel()
            self.state.modelsUnavailable()
            self.serviceFailed(message)
        }
        if preview {
            if CommandLine.arguments.contains("--render-settings") { showSettingsPreview() }
            else { showPreview() }
            return
        }
        shortcut.onDown = { [weak self] in self?.press() }
        shortcut.onUp = { [weak self] in self?.release() }
        registerShortcut(preferences.shortcutChoice)
        if smokeFile == nil && smokeText == nil { configuration.show() }
        startServices()
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(sleeping), name: NSWorkspace.willSleepNotification, object: nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard configuration != nil else { return true }
        configuration.show()
        return true
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        preferences.refreshMicrophoneStatus()
        updateMenu()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !shuttingDown else { return .terminateLater }
        shuttingDown = true
        startupTask?.cancel(); uploadTask?.cancel()
        recorder.cancel(); shortcut.unregister()
        menuRefreshTimer?.invalidate()
        Task { @MainActor in
            await transport.shutdown()
            do {
                try await launcher.stopAndWait()
                sender.reply(toApplicationShouldTerminate: true)
            } catch {
                shuttingDown = false
                state.serviceMessage = error.localizedDescription
                sender.reply(toApplicationShouldTerminate: false)
            }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        shuttingDown = true; startupTask?.cancel(); uploadTask?.cancel()
        recorder.cancel(); shortcut.unregister(); panels?.hide()
        menuRefreshTimer?.invalidate()
        transport.disconnect()
        launcher.stop()
    }

    @objc private func sleeping() { stopCurrent() }

    private func receive(_ event: WireEvent) {
        if smokeStarted && event.type != "response_delta" {
            let record: [String: Any] = ["type": event.type, "turn_id": event.turn_id, "tool_id": event.tool_id ?? "", "tool_name": event.tool_name ?? "", "text": event.text ?? "", "result": event.result ?? ""]
            if let data = try? JSONSerialization.data(withJSONObject: record, options: [.sortedKeys]), let line = String(data: data, encoding: .utf8) {
                print("HORIZON_SMOKE_EVENT=\(line)"); fflush(stdout)
            }
        }
        guard event.turn_id == state.turnID, !state.finished else { return }
        state.accept(event)
        if state.awaitingClarification { panels.showResult() }
    }

    private func updateSelectedModelHealth() {
        guard !state.modelsUnloaded, let health = latestHealth else { return }
        state.updateModelInventory(health.models)
        state.modelInventory["ASR"] = health.asr
        // Status follows the checkpoint selected for each role, including when
        // both roles share one set of resident weights.
        let selected = health.selectedModels(cleaner: preferences.cleanerModel, agent: preferences.agentModel)
        state.updateModelHealth(selected, serviceStatus: health.status)
        if state.cleanerModelPhase != .ready || state.agentModelPhase != .ready {
            state.ready = false
            if health.status == "ready" { state.serviceMessage = L("Chargez le modèle sélectionné pour continuer.") }
        }
    }

    private func startServices() {
        guard !state.modelsChanging, !state.modelsUnloaded else { return }
        preferences.setModelSize("4B")
        startupTask?.cancel()
        serviceRequiresRelaunch = false
        state.ready = false; state.serviceMessage = "Démarrage des services…"
        state.resetModelsLoading()
        startupTask = Task { @MainActor [weak self] in
            guard let self else { return }
            guard !Task.isCancelled, !self.shuttingDown else { return }
            if !self.launcher.isRunning {
                do { try self.launcher.start() }
                catch { self.serviceFailed(error.localizedDescription); return }
            }
            while !Task.isCancelled {
                do {
                    let health = try await self.transport.health()
                    try Task.checkCancellation()
                    if self.state.modelsChanging {
                        try? await Task.sleep(nanoseconds: 250_000_000)
                        continue
                    }
                    self.state.serviceMessage = health.message
                    self.latestHealth = health
                    self.updateSelectedModelHealth()
                    if health.status == "error" {
                        // Reconnecting does not reload a failed MLX worker. Keep the
                        // diagnostic visible until a deliberate application restart.
                        self.serviceFailed(health.message)
                        return
                    } else if health.status == "ready" {
                        guard self.state.cleanerModelPhase == .ready && self.state.agentModelPhase == .ready else {
                            self.state.ready = false
                            self.state.serviceMessage = L("Chargez le modèle sélectionné pour continuer.")
                            try? await Task.sleep(nanoseconds: 500_000_000)
                            continue
                        }
                        if !self.state.ready {
                            try await self.transport.connect()
                            try Task.checkCancellation()
                            guard !self.state.modelsChanging, self.state.cleanerModelPhase == .ready, self.state.agentModelPhase == .ready else { continue }
                            self.state.ready = true
                        }
                        self.state.serviceMessage = "Prêt · modèles locaux chargés"
                        self.startSmokeIfNeeded()
                    } else {
                        self.state.ready = false
                    }
                } catch {
                    if Task.isCancelled { return }
                    self.latestHealth = nil
                    self.state.ready = false
                    self.state.modelsUnavailable()
                    self.state.serviceMessage = "Connexion aux services locaux…"
                }
                try? await Task.sleep(nanoseconds: self.state.ready ? 2_000_000_000 : 500_000_000)
            }
        }
    }

    private func changeModelMemory(_ size: String) {
        guard !preview, state.canChangeModel(size) else { return }
        let load = state.modelPhase(size) != .ready
        state.modelsChanging = true; state.changingModel = size
        state.modelMemoryError = nil; state.ready = false
        Task { @MainActor in
            defer { self.state.modelsChanging = false; self.state.changingModel = nil }
            do {
                self.latestHealth = try await self.transport.changeModelMemory(size: size, load: load)
                guard !self.shuttingDown else { return }
                if load && size != "ASR" { self.preferences.setModelSize(size) }
                self.updateSelectedModelHealth()
            } catch {
                self.state.modelMemoryError = ServiceDiagnostics.concise(error.localizedDescription)
                // A failed or timed-out request must never leave a stale loaded badge.
                self.latestHealth = try? await self.transport.health()
                if self.latestHealth != nil { self.updateSelectedModelHealth() }
                else { self.state.modelsUnavailable() }
            }
        }
    }

    @objc private func toggleModelMemory() {
        guard !preview, state.canToggleModelMemory else { return }
        if state.modelsUnloaded {
            state.modelsUnloaded = false
            startServices()
            return
        }
        // Only the supervisor owned by this app may be stopped.
        guard launcher.isRunning else {
            state.serviceMessage = L("Cette instance ne contrôle pas les services. Déchargez les modèles depuis l’application qui les a démarrés.")
            return
        }
        state.modelsChanging = true; state.ready = false
        state.serviceMessage = "Libération de la mémoire…"
        latestHealth = nil
        let previousStartup = startupTask
        previousStartup?.cancel(); startupTask = nil
        panels.hide()
        Task { @MainActor in
            await self.transport.shutdown()
            await previousStartup?.value
            do {
                try await self.launcher.stopAndWait()
                guard !self.shuttingDown else { return }
                self.serviceRequiresRelaunch = false
                self.state.finishUnloadingModels()
            } catch {
                self.state.modelsChanging = false
                self.state.modelsUnavailable()
                self.serviceFailed(error.localizedDescription)
            }
            self.updateMenu()
        }
    }

    private func serviceFailed(_ message: String) {
        serviceRequiresRelaunch = true
        state.ready = false
        state.serviceMessage = ServiceDiagnostics.concise(message)
        if state.isBusy { state.fail(message + "\n\n" + ServiceDiagnostics.restartInstruction) }
        updateMenu()
    }

    private func press() {
        guard !keyHeld else { return }
        keyHeld = true
        guard state.ready else {
            if state.modelsUnloaded || state.modelsChanging { configuration.show(); return }
            let message = serviceRequiresRelaunch
                ? state.serviceMessage + "\n\n" + ServiceDiagnostics.restartInstruction
                : L("Les modèles se préparent encore. {0}", String(describing: L(state.serviceMessage)))
            state.begin(); state.fail(message)
            panels.anchorToPointer(); panels.showResult(); return
        }
        preferences.refreshMicrophoneStatus()
        guard preferences.microphoneAuthorized else {
            state.begin(); state.fail(L("Ouvrez les réglages de JAIS Voice et autorisez le microphone avant de parler."))
            panels.anchorToPointer(); panels.showResult(); return
        }
        let continuingConversation = (panels.result.isVisible || state.awaitingClarification) && state.turnID != nil
        cancelPendingTurn()
        panels.anchorToPointer()
        state.begin(recording: true, continuingConversation: continuingConversation)
        do {
            try recorder.start()
            panels.showRecording()
        } catch {
            state.fail(L("Le microphone n’a pas démarré : {0}", String(describing: error.localizedDescription)))
            panels.showResult()
        }
    }

    private func release() {
        keyHeld = false
        guard state.recording, let turnID = state.turnID else { return }
        state.recording = false; state.stage = "transcribing"
        panels.showResult()
        do { upload(try recorder.stop(), turnID: turnID) }
        catch { state.fail(error.localizedDescription) }
    }

    private func upload(_ data: Data, turnID: String) {
        let language = preferences.asrLanguageCode
        let conversationID = state.conversationID
        let cleanerModel = preferences.cleanerModel, agentModel = preferences.agentModel
        let cleanupGuardEnabled = preferences.cleanupGuardEnabled
        uploadTask = Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                try await self.transport.sendWAV(data, turnID: turnID, conversationID: conversationID, language: language, cleanerModel: cleanerModel, agentModel: agentModel, cleanupGuardEnabled: cleanupGuardEnabled)
            } catch is CancellationError { return }
            catch {
                guard self.state.turnID == turnID, !self.state.finished else { return }
                self.state.fail(L("L’enregistrement n’a pas pu être transmis : {0}", String(describing: error.localizedDescription)))
            }
        }
    }

    private func cancelPendingTurn() {
        uploadTask?.cancel(); uploadTask = nil
        recorder.cancel()
        if let id = state.turnID, !state.finished {
            Task { await transport.cancel(turnID: id) }
        }
    }

    private func stopCurrent() {
        guard state.isBusy else { return }
        keyHeld = false; cancelPendingTurn(); state.cancel(); panels.showResult()
    }

    private func registerShortcut(_ index: Int) {
        guard !state.isBusy else { return }
        let previous = preferences.shortcutChoice
        do {
            try shortcut.register(choice: index)
            preferences.setShortcutChoice(shortcut.choiceIndex)
            shortcutError = nil
        } catch {
            let message = error.localizedDescription
            do {
                try shortcut.register(choice: previous)
                shortcutError = message
            } catch {
                shortcutError = message + L(" Aucun raccourci n’est actif.")
            }
        }
        preferences.shortcutError = shortcutError
        updateMenu()
    }

    private func updateMenu() {
        refreshMenuStatus()
        guard statusItem != nil, !menuOpen else { return }
        let menu = NSMenu(); menu.delegate = self; menu.autoenablesItems = false
        func add(_ label: String, action: Selector? = nil, enabled: Bool = true) -> NSMenuItem {
            let item = NSMenuItem(title: label, action: action, keyEquivalent: "")
            item.target = self; item.isEnabled = enabled && action != nil; menu.addItem(item); return item
        }
        serviceMenuItem = add(preview ? L("Aperçu visuel · services désactivés") : L(state.serviceMessage), enabled: false)
        cleanerMenuItem = add("", enabled: false)
        agentMenuItem = add("", enabled: false)
        _ = add(L("Réglages…"), action: #selector(showConfiguration))
        if serviceRequiresRelaunch { _ = add(ServiceDiagnostics.restartInstruction, enabled: false) }
        if let error = shortcutError { _ = add(error, enabled: false) }
        if !preferences.microphoneAuthorized && !preview {
            _ = add(L("Autoriser le microphone…"), action: #selector(authorizeMicrophone))
        }
        menu.addItem(.separator())
        _ = add(L("Dernier résultat"), action: #selector(showLast), enabled: state.hasResult)
        _ = add(L("Importer un fichier audio…"), action: #selector(importAudio), enabled: state.ready && !state.isBusy && !preview)
        _ = add(L("Saisir une demande…"), action: #selector(textRequest), enabled: state.ready && !state.isBusy && !preview)
        _ = add(L("Arrêter la demande"), action: #selector(stopFromMenu), enabled: state.isBusy)
        let shortcutMenu = NSMenu()
        for (index, choice) in ShortcutChoice.choices.enumerated() {
            let item = NSMenuItem(title: choice.label, action: #selector(changeShortcut(_:)), keyEquivalent: "")
            item.target = self; item.tag = index; item.state = shortcut.choiceIndex == index ? .on : .off
            item.isEnabled = !state.isBusy && !preview
            shortcutMenu.addItem(item)
        }
        let shortcutItem = NSMenuItem(title: L("Maintenir pour parler"), action: nil, keyEquivalent: "")
        shortcutItem.submenu = shortcutMenu; menu.addItem(shortcutItem)
        _ = add(state.modelMemoryButton, action: #selector(toggleModelMemory), enabled: state.canToggleModelMemory && !preview)
        if !state.ready && !state.modelsChanging && !state.modelsUnloaded && !preview && !serviceRequiresRelaunch { _ = add(L("Relancer la connexion"), action: #selector(retryConnection)) }
        if launcher.logURL != nil && !preview { _ = add(L("Voir le journal des services…"), action: #selector(showServiceLog)) }
        menu.addItem(.separator())
        let quit = add(L("Quitter JAIS Voice"), action: #selector(quit))
        quit.keyEquivalent = "q"
        statusItem.menu = menu
        refreshMenuStatus()
        StatusItemPresentation.update(statusItem, shortcutTitle: preferences.shortcutLabel)
    }

    private func refreshMenuStatus() {
        serviceMenuItem?.title = preview ? L("Aperçu visuel · services désactivés") : L(state.serviceMessage)
        cleanerMenuItem?.title = L("Nettoyage · {0} — {1}", String(describing: state.cleanerModelLabel), String(describing: state.cleanerModelPhase.label))
        agentMenuItem?.title = "Agent · \(state.agentModelLabel) — \(state.agentModelPhase.label)"
        cleanerMenuItem?.image = NSImage(systemSymbolName: state.cleanerModelPhase.symbolName, accessibilityDescription: state.cleanerModelPhase.label)
        agentMenuItem?.image = NSImage(systemSymbolName: state.agentModelPhase.symbolName, accessibilityDescription: state.agentModelPhase.label)
        cleanerMenuItem?.toolTip = state.cleanerModelError
        agentMenuItem?.toolTip = state.agentModelError
    }

    func menuWillOpen(_ menu: NSMenu) {
        menuOpen = true; state.hovered = true; refreshMenuStatus()
        menuRefreshTimer?.invalidate()
        let timer = Timer(timeInterval: 0.25, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshMenuStatus() }
        }
        // Keep status labels live while AppKit tracks an open dropdown, without
        // replacing the menu or moving the user's selected item.
        RunLoop.main.add(timer, forMode: .common)
        menuRefreshTimer = timer
    }
    func menuDidClose(_ menu: NSMenu) {
        menuRefreshTimer?.invalidate(); menuRefreshTimer = nil
        menuOpen = false; state.hovered = false; panels.interacted(); updateMenu()
    }

    @objc private func authorizeMicrophone() {
        guard !preview, !preferences.microphoneRequestPending else { return }
        preferences.microphoneRequestPending = true
        Task { @MainActor in
            let allowed = await AudioRecorder.requestPermission()
            preferences.microphoneRequestPending = false
            preferences.refreshMicrophoneStatus()
            if !allowed, let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") { NSWorkspace.shared.open(url) }
            updateMenu()
        }
    }
    @objc private func showConfiguration() { configuration.show() }
    @objc private func showLast() { panels.showResult() }
    @objc private func stopFromMenu() { stopCurrent() }
    @objc private func retryConnection() { if !serviceRequiresRelaunch && !state.modelsChanging && !state.modelsUnloaded { startServices() } }
    @objc private func showServiceLog() { if let url = launcher.logURL { NSWorkspace.shared.open(url) } }
    @objc private func changeShortcut(_ sender: NSMenuItem) { registerShortcut(sender.tag) }
    @objc private func quit() { NSApplication.shared.terminate(nil) }

    /// `terminate` waits for shutdown tasks on the main actor (terminateLater).
    /// Called from inside a main-actor task, it would block them: run it from a
    /// run-loop source instead, as a menu action does.
    private static func terminateFromRunLoop() {
        RunLoop.main.perform(inModes: [.default]) { NSApplication.shared.terminate(nil) }
    }

    @objc private func textRequest() {
        guard state.ready, !state.isBusy, !preview else { return }
        let alert = NSAlert()
        alert.messageText = L("Que souhaitez-vous faire ?")
        alert.informativeText = L("Le texte passe par le correcteur puis l’agent local, sans utiliser le micro ni l’ASR. Les commandes macOS s’exécutent directement avec vos droits utilisateur.")
        alert.addButton(withTitle: L("Envoyer"))
        alert.addButton(withTitle: L("Annuler"))
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 460, height: 60))
        input.placeholderString = L("Ouvre une nouvelle note et écris bonjour…")
        input.usesSingleLineMode = false
        input.cell?.wraps = true
        input.setAccessibilityLabel(L("Demande écrite"))
        alert.accessoryView = input
        NSApplication.shared.activate(ignoringOtherApps: true)
        alert.window.initialFirstResponder = input
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = input.stringValue
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        cancelPendingTurn(); panels.anchorToPointer()
        let id = state.begin(); panels.showResult()
        uploadTask = Task { @MainActor in
            do { try await transport.sendText(text, turnID: id, conversationID: state.conversationID, cleanerModel: preferences.cleanerModel, agentModel: preferences.agentModel, cleanupGuardEnabled: preferences.cleanupGuardEnabled) }
            catch { if state.turnID == id, !state.finished { state.fail(error.localizedDescription) } }
        }
    }

    @objc private func importAudio() {
        guard state.ready, !state.isBusy, !preview else { return }
        let picker = NSOpenPanel()
        picker.title = L("Importer un enregistrement")
        picker.prompt = L("Transcrire")
        picker.allowedContentTypes = [.wav, .audio]
        picker.allowsMultipleSelection = false
        picker.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        picker.level = .floating
        picker.begin { [weak self] response in
            guard response == .OK, let url = picker.url else { return }
            Task { @MainActor in
                guard let self else { return }
                self.cancelPendingTurn(); self.panels.anchorToPointer()
                let id = self.state.begin(); self.panels.showResult()
                do {
                    let data = try await Task.detached(priority: .userInitiated) { try AudioRecorder.importWAV(from: url) }.value
                    guard self.state.turnID == id, !self.state.finished else { return }
                    self.upload(data, turnID: id)
                } catch { if self.state.turnID == id { self.state.fail(error.localizedDescription) } }
            }
        }
    }

    private func showSettingsPreview() {
        state.ready = true
        state.serviceMessage = "Aperçu · aucun service ni microphone démarré"
        state.cleanerModelLabel = "MBZUAI · 1B"
        state.agentModelLabel = "MBZUAI · 4B"
        state.cleanerModelPhase = .ready
        state.agentModelPhase = .ready
        if CommandLine.arguments.contains("--settings-loading") {
            state.ready = false
            state.serviceMessage = "Aperçu · chargement du modèle agent"
            state.agentModelPhase = .loading
        }
        if CommandLine.arguments.contains("--settings-error") {
            state.ready = false
            state.serviceMessage = "Aperçu · échec de chargement du modèle agent"
            state.agentModelPhase = .error
            state.agentModelError = L("Exemple de diagnostic de chargement.")
        }
        state.updateModelInventory([
            "k2-cleaner": ServiceModelHealth(model_size: "1B", display_name: "MBZUAI · 1B", repo: nil, status: "unloaded", error: nil),
            "k2-agent": ServiceModelHealth(model_size: "4B", display_name: "MBZUAI · 4B", repo: nil, status: CommandLine.arguments.contains("--settings-unloaded") ? "unloaded" : state.agentModelPhase.rawValue, error: nil)
        ])
        state.modelInventory["ASR"] = ServiceModelHealth(model_size: "ASR", display_name: "JAIS-ASR F5", repo: nil, status: "ready", error: nil)
        let previewHealth = ServiceHealth(status: "ready", message: "", models: state.modelInventory)
        state.updateModelHealth(previewHealth.selectedModels(cleaner: preferences.cleanerModel, agent: preferences.agentModel), serviceStatus: "ready")
        configuration.show()
        guard let index = CommandLine.arguments.firstIndex(of: "--render-settings"), CommandLine.arguments.count > index + 1 else { return }
        let url = URL(fileURLWithPath: CommandLine.arguments[index + 1])
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            let wasVisible = configuration.window?.isVisible == true
            configuration.close()
            let closed = configuration.window?.isVisible == false
            _ = applicationShouldHandleReopen(NSApplication.shared, hasVisibleWindows: false)
            var diagnostics = StatusItemPresentation.diagnostics(statusItem)
            diagnostics["settings_visible_at_launch"] = wasVisible
            diagnostics["settings_closes_without_quitting"] = closed
            diagnostics["settings_reopens"] = configuration.window?.isVisible == true
            refreshMenuStatus()
            diagnostics["cleaner_menu_title"] = cleanerMenuItem?.title ?? ""
            diagnostics["agent_menu_title"] = agentMenuItem?.title ?? ""
            if let menu = statusItem.menu {
                let phase = state.agentModelPhase
                menuWillOpen(menu)
                state.agentModelPhase = .loading; updateMenu()
                let loadingTitle = agentMenuItem?.title ?? ""
                state.agentModelPhase = .ready; updateMenu()
                diagnostics["open_menu_updates_in_place"] = statusItem.menu === menu && loadingTitle.contains(ModelLoadPhase.loading.label) && agentMenuItem?.title.contains(L("Chargé")) == true
                state.agentModelPhase = phase
                menuDidClose(menu)
            }
            if let data = try? JSONSerialization.data(withJSONObject: diagnostics, options: [.sortedKeys]), let json = String(data: data, encoding: .utf8) {
                print("HORIZON_SETTINGS_TEST=\(json)")
            }
            do { try configuration.renderPreview(to: url); print("Réglages exportés : \(url.path)") }
            catch { fputs("\(error.localizedDescription)\n", stderr) }
            Self.terminateFromRunLoop()
        }
    }

    private func showPreview() {
        state.serviceMessage = "Aperçu visuel · aucune inférence"
        let id = state.begin()
        let events = [
            WireEvent(type: "transcript", turn_id: id, text: "ابحث عن متحف اللوفر في أبوظبي… لا، في باريس. افتح الموقع الرسمي."),
            WireEvent(type: "correction", turn_id: id, text: "ابحث عن متحف اللوفر في باريس. افتح الموقع الرسمي."),
            WireEvent(type: "tool_start", turn_id: id, tool_id: "preview-only", tool_name: "browser_navigate", arguments: .object(["url": .string("https://www.louvre.fr")])),
            WireEvent(type: "tool_end", turn_id: id, tool_id: "preview-only", result: L("Exemple visuel, aucune navigation exécutée.")),
            WireEvent(type: "complete", turn_id: id, text: "هذا مثال بصري للواجهة. لم يُنفَّذ بحث أو استدلال.\n\n[Musée du Louvre](https://www.louvre.fr)")
        ]
        for event in events { state.accept(event) }
        if CommandLine.arguments.contains("--preview-followup") {
            let followup = state.begin(continuingConversation: true)
            state.accept(WireEvent(type: "transcript", turn_id: followup, text: "Et dans quelle ville se trouve ce musée ?"))
            state.accept(WireEvent(type: "correction", turn_id: followup, text: "Dans quelle ville se trouve ce musée ?"))
            state.accept(WireEvent(type: "complete", turn_id: followup, text: "Le musée du Louvre dont nous parlons se trouve à Paris."))
        }
        if CommandLine.arguments.contains("--preview-clarification") {
            let clarification = state.begin()
            state.accept(WireEvent(type: "correction", turn_id: clarification, text: "Find a restaurant for tonight."))
            state.accept(WireEvent(type: "clarification_request", turn_id: clarification, text: "Which city should I search in?"))
        }
        state.showOriginal = true
        let previousFrontmost = NSWorkspace.shared.frontmostApplication?.processIdentifier
        panels.showResult()
        if let index = CommandLine.arguments.firstIndex(of: "--render-preview"), CommandLine.arguments.count > index + 1 {
            let url = URL(fileURLWithPath: CommandLine.arguments[index + 1])
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_500_000_000)
                let currentFrontmost = NSWorkspace.shared.frontmostApplication?.processIdentifier
                print("HORIZON_PREVIEW_FOCUS before=\(previousFrontmost ?? -1) after=\(currentFrontmost ?? -1) unchanged=\(previousFrontmost == currentFrontmost)")
                do { try panels.savePreview(to: url); print("Aperçu enregistré : \(url.path)") }
                catch { fputs("\(error.localizedDescription)\n", stderr) }
                Self.terminateFromRunLoop()
            }
        }
    }

    private func startSmokeIfNeeded() {
        guard (smokeFile != nil || smokeText != nil), !smokeStarted else { return }
        smokeStarted = true
        let id = state.begin(); panels.anchorToPointer(); panels.showResult()
        Task { @MainActor in
            do {
                if let text = smokeText {
                    try await transport.sendText(text, turnID: id, conversationID: state.conversationID, cleanerModel: preferences.cleanerModel, agentModel: preferences.agentModel, cleanupGuardEnabled: preferences.cleanupGuardEnabled)
                } else if let file = smokeFile {
                    let data = try await Task.detached { try AudioRecorder.importWAV(from: file) }.value
                    upload(data, turnID: id)
                }
            } catch { state.fail(error.localizedDescription) }
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 300_000_000_000)
            if state.turnID == id && !state.finished {
                cancelPendingTurn(); state.fail(L("Le test a dépassé cinq minutes."))
            }
        }
    }

    private func reportSmokeIfNeeded() {
        guard (smokeFile != nil || smokeText != nil), smokeStarted, !smokeReported else { return }
        smokeReported = true
        let result: [String: Any] = ["stage": state.stage, "transcript": state.raw, "correction": state.cleaned, "response": state.response, "error": state.error ?? "", "tool_count": state.actions.count, "asr_used": smokeText == nil]
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .withoutEscapingSlashes]), let text = String(data: data, encoding: .utf8) {
            print("HORIZON_SMOKE_RESULT=\(text)"); fflush(stdout)
        }
        if !CommandLine.arguments.contains("--smoke-stay") {
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                Self.terminateFromRunLoop()
            }
        }
    }
}
