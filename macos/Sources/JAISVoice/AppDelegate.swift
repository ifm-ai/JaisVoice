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
    private let modelStore = ModelStore()
    private let updates = UpdateChecker()
    private var actionApprover: ActionApprover!
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
    /// `--render-preview` and `--render-settings` export a screenshot without services or microphone.
    private var preview = false
    private var menuOpen = false
    private var menuRefreshTimer: Timer?
    private var serviceMenuItem: NSMenuItem?
    private var modelMenuItem: NSMenuItem?
    private var smokeFile: URL?
    private var smokeText: String?
    private var smokeStarted = false
    private var smokeReported = false
    private var latestHealth: ServiceHealth?

    private static func argument(after flag: String) -> String? {
        guard let index = CommandLine.arguments.firstIndex(of: flag), CommandLine.arguments.count > index + 1 else { return nil }
        return CommandLine.arguments[index + 1]
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        preview = Self.argument(after: "--render-preview") != nil || Self.argument(after: "--render-settings") != nil
        smokeFile = Self.argument(after: "--smoke-file").map { URL(fileURLWithPath: $0) }
        smokeText = Self.argument(after: "--smoke-text")
        panels = FloatingPanels(state: state, preview: preview, onStop: { [weak self] in self?.stopCurrent() })
        statusItem = StatusItemPresentation.makeItem(shortcutTitle: preferences.shortcutLabel)
        configuration = ConfigurationWindowController(
            state: state, preferences: preferences,
            onShortcutChange: { [weak self] in self?.registerShortcut($0) },
            onAuthorizeMicrophone: { [weak self] in self?.authorizeMicrophone() },
            onShowLast: { [weak self] in self?.showLast() },
            onImportAudio: { [weak self] in self?.importAudio() },
            onTextRequest: { [weak self] in self?.textRequest() },
            modelStore: modelStore,
            onChangeModelMemory: { [weak self] in self?.changeModelMemory($0) },
            onToggleModelMemory: { [weak self] in self?.toggleModelMemory() },
            onDownloadModel: { [weak self] in self?.modelStore.download($0) },
            onCancelDownload: { [weak self] in self?.modelStore.cancel($0) },
            onDeleteModel: { [weak self] in self?.deleteModel($0) },
            onShowServiceLog: { [weak self] in self?.showServiceLog() },
            onQuit: { [weak self] in self?.quit() }
        )
        state.objectWillChange.throttle(for: .milliseconds(150), scheduler: RunLoop.main, latest: true).sink { [weak self] _ in
            self?.updateMenu()
        }.store(in: &subscriptions)
        preferences.objectWillChange.receive(on: RunLoop.main).sink { [weak self] _ in
            self?.state.objectWillChange.send()
            self?.configuration.window?.title = L("Settings · Jais Voice")
            self?.updateModelState()
            self?.updateMenu()
        }.store(in: &subscriptions)
        updateMenu()
        state.onBegin = { [weak self] in self?.panels.cancelDismissal() }
        state.onFinish = { [weak self] in
            self?.panels.scheduleHide()
            self?.reportSmokeIfNeeded()
        }
        transport.onEvent = { [weak self] event in self?.receive(event) }
        actionApprover = ActionApprover(state: state) { [weak self] in self?.panels.showResult() }
        transport.actionPermission = { [weak self] in self?.preferences.actionPermission ?? .never }
        transport.approveAction = { [weak self] request in await self?.actionApprover.approve(request) ?? false }
        transport.dismissApprovals = { [weak self] in self?.actionApprover.dismiss() }
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
            self.latestHealth = nil; self.updateModelState()
            self.serviceFailed(message)
        }
        if preview {
            if Self.argument(after: "--render-settings") != nil { renderSettings() } else { renderPreview() }
            return
        }
        shortcut.onDown = { [weak self] in self?.press() }
        shortcut.onUp = { [weak self] in self?.release() }
        registerShortcut(preferences.shortcutChoice)
        modelStore.onChange = { [weak self] model in self?.modelInstallationChanged(model) }
        let interactive = smokeFile == nil && smokeText == nil
        if interactive {
            configuration.show()
            updates.onAvailable = { [weak self] release in
                self?.updateMenu()
                self?.updates.present(release)
            }
            updates.startAutomaticChecks()
        }
        Task { @MainActor in
            await modelStore.refresh()
            if interactive { offerMissingDownloads() }
        }
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
        preferences.refreshAppControlStatus()
        updateMenu()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !shuttingDown else { return .terminateLater }
        shuttingDown = true
        startupTask?.cancel(); uploadTask?.cancel()
        recorder.cancel(); shortcut.unregister(); modelStore.cancelAll()
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
        guard event.turn_id == state.turnID, !state.finished else { return }
        state.accept(event)
        if state.awaitingClarification { panels.showResult() }
    }

    /// Show the selected model's state; the assistant is usable only while it is loaded.
    private func updateModelState() {
        guard !state.modelsUnloaded else { return }
        state.updateModels(latestHealth, selected: preferences.modelSize)
        if latestHealth != nil && state.modelPhase != .ready {
            state.ready = false
            if latestHealth?.status == "ready" { state.serviceMessage = L("Load the selected model to continue.") }
        }
    }

    private func startServices() {
        guard !state.modelsChanging, !state.modelsUnloaded else { return }
        startupTask?.cancel()
        serviceRequiresRelaunch = false
        state.ready = false; state.serviceMessage = "Starting services…"
        state.resetModelsLoading()
        startupTask = Task { @MainActor [weak self] in
            guard let self else { return }
            guard !Task.isCancelled, !self.shuttingDown else { return }
            if !self.launcher.isRunning {
                do { try self.launcher.start(modelSize: self.preferences.modelSize) }
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
                    self.followLoadedModel(health)
                    self.updateModelState()
                    if let missing = self.modelStore.blockingMessage {
                        self.state.ready = false
                        self.state.serviceMessage = missing
                    } else if health.status == "error" {
                        // Reconnecting does not reload a failed MLX worker. Keep the
                        // diagnostic visible until a deliberate application restart.
                        self.serviceFailed(health.message)
                        return
                    } else if health.status == "ready" && self.state.modelPhase == .ready {
                        if !self.state.ready {
                            try await self.transport.connect()
                            try Task.checkCancellation()
                            guard !self.state.modelsChanging, self.state.modelPhase == .ready else { continue }
                            self.state.ready = true
                        }
                        self.state.serviceMessage = "Ready · local models loaded"
                        self.startSmokeIfNeeded()
                    } else {
                        self.state.ready = false
                    }
                } catch {
                    if Task.isCancelled { return }
                    self.latestHealth = nil
                    self.state.ready = false
                    self.updateModelState()
                    self.state.serviceMessage = "Connecting to local services…"
                }
                try? await Task.sleep(nanoseconds: self.state.ready ? 2_000_000_000 : 500_000_000)
            }
        }
    }

    /// When the selected size is not installed, the gateway loads another one: follow it.
    private func followLoadedModel(_ health: ServiceHealth) {
        guard !state.ready, !state.modelsChanging, health.models[preferences.modelSize]?.status != "ready",
              let loaded = AppPreferences.modelSizes.first(where: { health.models[$0]?.status == "ready" }) else { return }
        preferences.setModelSize(loaded)
    }

    /// Offer once per launch the required models that are absent.
    private func offerMissingDownloads() {
        let missing = modelStore.missingDownloads
        guard !missing.isEmpty else { return }
        let alert = NSAlert()
        alert.messageText = L("Download the models?")
        let lines = missing.map { model in
            "• " + model.display_name + (model.download_bytes.map { " (" + ModelStore.formatBytes($0) + ")" } ?? "")
        }
        alert.informativeText = L("Jais Voice needs these models to work:") + "\n" + lines.joined(separator: "\n") + "\n\n"
            + L("They are downloaded from Hugging Face and stay on this Mac. You can delete them from Settings.")
        alert.addButton(withTitle: L("Download"))
        alert.addButton(withTitle: L("Later"))
        NSApplication.shared.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        missing.forEach { modelStore.download($0.id) }
        configuration.show()
    }

    /// A model was installed or removed outside the services: refresh their view,
    /// and load a newly installed language model when none is ready.
    private func modelInstallationChanged(_ model: CatalogModel) {
        Task { @MainActor in
            self.latestHealth = try? await self.transport.health()
            self.updateModelState()
            guard model.installed, let size = model.size, !self.state.modelsUnloaded,
                  self.state.phase(of: self.preferences.modelSize) != .ready else { return }
            self.preferences.setModelSize(size)
            self.changeModelMemory(size)
        }
    }

    private func deleteModel(_ id: String) {
        guard let model = modelStore.models.first(where: { $0.id == id }), !state.isBusy, !state.modelsChanging else { return }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = L("Delete {0}?", String(describing: model.display_name))
        alert.informativeText = L("{0} will be freed on this Mac.", String(describing: ModelStore.formatBytes(model.diskBytes)))
            + " " + L("You can download it again from Settings.")
        alert.addButton(withTitle: L("Delete")).hasDestructiveAction = true
        alert.addButton(withTitle: L("Cancel"))
        NSApplication.shared.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task { @MainActor in
            do {
                // Release the weights before removing their files.
                if let size = model.size, self.state.phase(of: size) == .ready {
                    self.latestHealth = try await self.transport.changeModelMemory(size: size, load: false)
                }
                try await self.modelStore.delete(id)
                if model.size == self.preferences.modelSize,
                   let other = self.modelStore.languageModels.last(where: \.installed)?.size {
                    self.preferences.setModelSize(other)
                }
                self.state.ready = false
            } catch {
                self.modelStore.error = ServiceDiagnostics.concise(error.localizedDescription)
            }
            self.latestHealth = try? await self.transport.health()
            self.updateModelState()
        }
    }

    private func changeModelMemory(_ size: String) {
        guard state.canChangeModel(size) else { return }
        let load = state.phase(of: size) != .ready
        state.modelsChanging = true; state.changingModel = size
        state.modelMemoryError = nil; state.ready = false
        Task { @MainActor in
            defer { self.state.modelsChanging = false; self.state.changingModel = nil }
            do {
                self.latestHealth = try await self.transport.changeModelMemory(size: size, load: load)
                guard !self.shuttingDown else { return }
                if load && size != "ASR" { self.preferences.setModelSize(size) }
            } catch {
                self.state.modelMemoryError = ServiceDiagnostics.concise(error.localizedDescription)
                // A failed or timed-out request must never leave a stale loaded badge.
                self.latestHealth = try? await self.transport.health()
            }
            self.updateModelState()
        }
    }

    @objc private func toggleModelMemory() {
        guard state.canToggleModelMemory else { return }
        if state.modelsUnloaded {
            state.modelsUnloaded = false
            startServices()
            return
        }
        // Only the supervisor owned by this app may be stopped.
        guard launcher.isRunning else {
            state.serviceMessage = L("This instance does not own the services. Unload models from the app that started them.")
            return
        }
        state.modelsChanging = true; state.ready = false
        state.serviceMessage = "Releasing memory…"
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
                self.updateModelState()
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

    private var turnOptions: LocalPipeline.TurnOptions {
        .init(conversationID: state.conversationID, modelSize: preferences.modelSize, reasoningEffort: preferences.reasoningEffort.rawValue)
    }

    private func press() {
        guard !keyHeld else { return }
        keyHeld = true
        guard state.ready else {
            if state.modelsUnloaded || state.modelsChanging { configuration.show(); return }
            let message = serviceRequiresRelaunch
                ? state.serviceMessage + "\n\n" + ServiceDiagnostics.restartInstruction
                : L("Models are still preparing. {0}", String(describing: L(state.serviceMessage)))
            state.begin(); state.fail(message)
            panels.anchorToPointer(); panels.showResult(); return
        }
        preferences.refreshMicrophoneStatus()
        guard preferences.microphoneAuthorized else {
            state.begin(); state.fail(L("Open Jais Voice settings and allow microphone access before speaking."))
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
            state.fail(L("The microphone could not start: {0}", String(describing: error.localizedDescription)))
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
        let language = preferences.asrLanguageCode, options = turnOptions
        uploadTask = Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                try await self.transport.sendWAV(data, turnID: turnID, language: language, options: options)
            } catch is CancellationError { return }
            catch {
                guard self.state.turnID == turnID, !self.state.finished else { return }
                self.state.fail(L("The recording could not be sent: {0}", String(describing: error.localizedDescription)))
            }
        }
    }

    private func send(text: String, turnID: String) {
        let options = turnOptions
        uploadTask = Task { @MainActor in
            do { try await transport.sendText(text, turnID: turnID, options: options) }
            catch { if state.turnID == turnID, !state.finished { state.fail(error.localizedDescription) } }
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
                shortcutError = message + L(" No shortcut is active.")
            }
        }
        preferences.shortcutError = shortcutError
        updateMenu()
    }

    private func updateMenu() {
        refreshMenuStatus()
        guard statusItem != nil, !menuOpen else { return }
        let menu = NSMenu(); menu.delegate = self; menu.autoenablesItems = false
        menu.userInterfaceLayoutDirection = InterfaceLanguage.current.userInterfaceLayoutDirection
        func add(_ label: String, action: Selector? = nil, enabled: Bool = true) -> NSMenuItem {
            let item = NSMenuItem(title: label, action: action, keyEquivalent: "")
            item.target = self; item.isEnabled = enabled && action != nil; menu.addItem(item); return item
        }
        serviceMenuItem = add("", enabled: false)
        modelMenuItem = add("", enabled: false)
        _ = add(L("Settings…"), action: #selector(showConfiguration))
        if let release = updates.available {
            _ = add(L("Update available: {0}…", release.label), action: #selector(showUpdate))
        }
        if serviceRequiresRelaunch { _ = add(ServiceDiagnostics.restartInstruction, enabled: false) }
        if let error = shortcutError { _ = add(error, enabled: false) }
        if !preferences.microphoneAuthorized && !preview {
            _ = add(L("Allow microphone access…"), action: #selector(authorizeMicrophone))
        }
        menu.addItem(.separator())
        _ = add(L("Last result"), action: #selector(showLast), enabled: state.hasResult)
        _ = add(L("Import an audio file…"), action: #selector(importAudio), enabled: state.ready && !state.isBusy)
        _ = add(L("Type a request…"), action: #selector(textRequest), enabled: state.ready && !state.isBusy)
        _ = add(L("Stop request"), action: #selector(stopFromMenu), enabled: state.isBusy)
        let shortcutMenu = NSMenu()
        shortcutMenu.userInterfaceLayoutDirection = menu.userInterfaceLayoutDirection
        for (index, choice) in ShortcutChoice.choices.enumerated() {
            let item = NSMenuItem(title: choice.label, action: #selector(changeShortcut(_:)), keyEquivalent: "")
            item.target = self; item.tag = index; item.state = shortcut.choiceIndex == index ? .on : .off
            item.isEnabled = !state.isBusy && !preview
            shortcutMenu.addItem(item)
        }
        let shortcutItem = NSMenuItem(title: L("Hold to talk"), action: nil, keyEquivalent: "")
        shortcutItem.submenu = shortcutMenu; menu.addItem(shortcutItem)
        _ = add(state.modelMemoryButton, action: #selector(toggleModelMemory), enabled: state.canToggleModelMemory && !preview)
        if !state.ready && !state.modelsChanging && !state.modelsUnloaded && !preview && !serviceRequiresRelaunch { _ = add(L("Retry connection"), action: #selector(retryConnection)) }
        if launcher.logURL != nil { _ = add(L("View service log…"), action: #selector(showServiceLog)) }
        if updates.enabled { _ = add(L("Check for Updates…"), action: #selector(checkForUpdates)) }
        menu.addItem(.separator())
        let quit = add(L("Quit Jais Voice"), action: #selector(quit))
        quit.keyEquivalent = "q"
        statusItem.menu = menu
        refreshMenuStatus()
        StatusItemPresentation.update(statusItem, shortcutTitle: preferences.shortcutLabel)
    }

    private func refreshMenuStatus() {
        serviceMenuItem?.title = L(state.serviceMessage)
        modelMenuItem?.title = L("Model · {0} — {1}", String(describing: state.modelLabel), String(describing: state.modelPhase.label))
        modelMenuItem?.image = NSImage(systemSymbolName: state.modelPhase.symbolName, accessibilityDescription: state.modelPhase.label)
        modelMenuItem?.toolTip = state.modelError
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
    @objc private func showUpdate() { if let release = updates.available { updates.present(release) } }

    @objc private func checkForUpdates() {
        Task { @MainActor in
            let alert = NSAlert()
            do {
                if let release = try await updates.latestNewerRelease() {
                    updateMenu(); updates.present(release); return
                }
                alert.messageText = L("Jais Voice is up to date")
                alert.informativeText = L("You have the latest version ({0}).", UpdateChecker.current.label)
            } catch {
                alert.messageText = L("Could not check for updates")
                alert.informativeText = error.localizedDescription
            }
            NSApplication.shared.activate(ignoringOtherApps: true)
            alert.runModal()
        }
    }

    /// `terminate` waits for shutdown tasks on the main actor (terminateLater).
    /// Called from inside a main-actor task, it would block them: run it from a
    /// run-loop source instead, as a menu action does.
    private static func terminateFromRunLoop() {
        RunLoop.main.perform(inModes: [.default]) { NSApplication.shared.terminate(nil) }
    }

    @objc private func textRequest() {
        guard state.ready, !state.isBusy else { return }
        let alert = NSAlert()
        alert.messageText = L("What would you like to do?")
        alert.informativeText = L("Text goes through the corrector and local agent without using the microphone or speech recognition. macOS commands run directly with your user permissions.")
        alert.addButton(withTitle: L("Send"))
        alert.addButton(withTitle: L("Cancel"))
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 460, height: 60))
        input.placeholderString = L("Open a new note and write hello…")
        input.usesSingleLineMode = false
        input.cell?.wraps = true
        input.setAccessibilityLabel(L("Typed request"))
        alert.accessoryView = input
        NSApplication.shared.activate(ignoringOtherApps: true)
        alert.window.initialFirstResponder = input
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = input.stringValue
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        cancelPendingTurn(); panels.anchorToPointer()
        let id = state.begin(); panels.showResult()
        send(text: text, turnID: id)
    }

    @objc private func importAudio() {
        guard state.ready, !state.isBusy else { return }
        let picker = NSOpenPanel()
        picker.title = L("Import a recording")
        picker.prompt = L("Transcribe")
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
                await self.uploadFile(url, turnID: id)
            }
        }
    }

    private func uploadFile(_ url: URL, turnID: String) async {
        do {
            let data = try await Task.detached(priority: .userInitiated) { try AudioRecorder.importWAV(from: url) }.value
            guard state.turnID == turnID, !state.finished else { return }
            upload(data, turnID: turnID)
        } catch { if state.turnID == turnID { state.fail(error.localizedDescription) } }
    }

    // MARK: Screenshots and smoke tests

    /// Export the settings window, with example models, then quit.
    private func renderSettings() {
        state.ready = true
        state.serviceMessage = "Ready · local models loaded"
        modelStore.show(models: [
            CatalogModel(id: "asr-f5", kind: "asr", display_name: "Jais-ASR · F5", installed: true, download_bytes: 4_780_192_247, disk_bytes: 5_995_121_326),
            CatalogModel(id: "jais-3-0.9b", kind: "llm", display_name: "Jais 3 · 0.9B", size: "0.9B", installed: true, disk_bytes: 2_161_683_689),
            CatalogModel(id: "jais-3-3.7b", kind: "llm", display_name: "Jais 3 · 3.7B", size: "3.7B", installed: true, disk_bytes: 5_395_429_534),
            CatalogModel(id: "jais-3-7b", kind: "llm", display_name: "Jais 3 · 7B", size: "7B", installed: false, download_bytes: 9_582_764_791)
        ])
        latestHealth = Self.exampleHealth
        updateModelState()
        configuration.show()
        if let tab = Self.argument(after: "--settings-tab").flatMap(SettingsTab.init(rawValue:)) { configuration.select(tab) }
        let url = URL(fileURLWithPath: Self.argument(after: "--render-settings")!)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            do { try configuration.renderPreview(to: url); print("Settings exported: \(url.path)") }
            catch { fputs("\(error.localizedDescription)\n", stderr) }
            Self.terminateFromRunLoop()
        }
    }

    private static let exampleHealth = ServiceHealth(status: "ready", message: "", models: [
        "ASR": ModelHealth(status: "ready"),
        "0.9B": ModelHealth(display_name: "Jais 3 · 0.9B", status: "unloaded"),
        "3.7B": ModelHealth(display_name: "Jais 3 · 3.7B", status: "ready"),
        "7B": ModelHealth(display_name: "Jais 3 · 7B", status: "not_installed")
    ])

    /// Export the result bubble for an example turn, then quit.
    private func renderPreview() {
        latestHealth = Self.exampleHealth
        updateModelState()
        let id = state.begin()
        let note = "أبحث عن الموقع الرسمي لمتحف اللوفر.\n\n"
        let answer = "وجدتُ الموقع الرسمي لمتحف اللوفر، وأفتح الصفحة الآن.\n\n[Louvre Museum](https://www.louvre.fr)"
        let events = [
            WireEvent(type: "transcript", turn_id: id, text: "ابحث عن متحف اللوفر في أبوظبي… لا، في باريس. افتح الموقع الرسمي."),
            WireEvent(type: "correction", turn_id: id, text: "ابحث عن متحف اللوفر في باريس. افتح الموقع الرسمي."),
            WireEvent(type: "response_delta", turn_id: id, text: note),
            WireEvent(type: "tool_start", turn_id: id, tool_id: "preview-only", tool_name: "browser_navigate", arguments: .object(["url": .string("https://www.louvre.fr")])),
            WireEvent(type: "tool_end", turn_id: id, tool_id: "preview-only", result: L("Visual example, no browsing performed.")),
            WireEvent(type: "response_delta", turn_id: id, text: answer),
            WireEvent(type: "complete", turn_id: id, text: note + answer)
        ]
        for event in events { state.accept(event) }
        state.showOriginal = true
        panels.showResult()
        let url = URL(fileURLWithPath: Self.argument(after: "--render-preview")!)
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            do { try panels.savePreview(to: url); print("Preview saved: \(url.path)") }
            catch { fputs("\(error.localizedDescription)\n", stderr) }
            Self.terminateFromRunLoop()
        }
    }

    /// `--smoke-text` and `--smoke-file` run one real turn, print the result, then quit.
    private func startSmokeIfNeeded() {
        guard (smokeFile != nil || smokeText != nil), !smokeStarted else { return }
        smokeStarted = true
        let id = state.begin(); panels.anchorToPointer(); panels.showResult()
        if let text = smokeText { send(text: text, turnID: id) }
        else if let file = smokeFile { Task { @MainActor in await uploadFile(file, turnID: id) } }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 300_000_000_000)
            if state.turnID == id && !state.finished {
                cancelPendingTurn(); state.fail(L("The test exceeded five minutes."))
            }
        }
    }

    private func reportSmokeIfNeeded() {
        guard smokeStarted, !smokeReported else { return }
        smokeReported = true
        let result: [String: Any] = ["stage": state.stage, "transcript": state.raw, "correction": state.cleaned, "response": state.response,
                                     "error": state.error ?? "", "tool_count": state.actions.count, "asr_used": smokeText == nil,
                                     "tools": state.actions.map { $0.name + " " + $0.arguments.replacingOccurrences(of: "\n", with: " ") }]
        if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys, .withoutEscapingSlashes]), let text = String(data: data, encoding: .utf8) {
            print("JAIS_SMOKE_RESULT=\(text)"); fflush(stdout)
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            Self.terminateFromRunLoop()
        }
    }
}
