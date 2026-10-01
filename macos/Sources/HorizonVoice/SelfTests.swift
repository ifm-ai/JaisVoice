import AppKit
import Foundation
import AVFoundation

@MainActor enum SelfTests {
    static func runDismissal() -> Int32 {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let state = TurnState()
        let panels = FloatingPanels(state: state, preview: false, onStop: {})
        state.onBegin = { panels.cancelDismissal() }
        state.onFinish = { panels.scheduleHide() }
        func advance(_ seconds: TimeInterval) {
            RunLoop.main.run(until: Date().addingTimeInterval(seconds))
        }
        func check(_ condition: Bool, _ message: String) {
            if !condition { fputs("FAIL: \(message)\n", stderr); exit(1) }
            print("OK: \(message)"); fflush(stdout)
        }
        let old = state.begin()
        state.accept(WireEvent(type: "complete", turn_id: old, text: "Old reply"))
        panels.showResult()
        advance(1)
        let current = state.begin(continuingConversation: true)
        panels.showResult()
        // An erroneous schedule request while busy must not arm a timer.
        panels.scheduleHide()
        for stage in ["transcribing", "refining", "acting", "acting"] {
            state.stage = stage
            advance(3)
            check(panels.result.isVisible, "Window stays visible during \(stage)")
        }
        state.accept(WireEvent(type: "tool_end", turn_id: current, tool_id: "tool"))
        state.accept(WireEvent(type: "response_delta", turn_id: current, text: "Still generating"))
        state.accept(WireEvent(type: "complete", turn_id: old, text: "Stale completion"))
        check(!state.finished && panels.result.isVisible, "Tool/stream/stale completion do not finish current turn")
        state.accept(WireEvent(type: "complete", turn_id: current, text: "Final reply"))
        // Ignore pointer/focus so this test measures only the final countdown.
        state.hovered = false
        panels.result.resignKey()
        advance(2)
        check(panels.result.isVisible, "Full delay starts after completion")
        advance(9)
        check(!panels.result.isVisible, "Window closes ten seconds after completion")
        let question = state.begin()
        state.accept(WireEvent(type: "clarification_request", turn_id: question, text: "Which city?"))
        panels.showResult()
        state.hovered = false
        panels.result.resignKey()
        advance(11)
        check(panels.result.isVisible, "Clarification remains visible past normal dismissal delay")
        panels.hide()
        return 0
    }

    static func run() -> Int32 {
        var passed = 0
        func check(_ condition: @autoclosure () -> Bool, _ name: String) throws {
            if !condition() { throw VoiceError.message("FAILED: \(name)") }
            passed += 1; print("OK: \(name)")
        }
        do {
            let suite = "horizon-preferences-test-\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suite)!
            defaults.setVolatileDomain([:], forName: UserDefaults.argumentDomain)
            defer { defaults.removePersistentDomain(forName: suite) }
            let preferences = AppPreferences(defaults: defaults)
            let memoryState = TurnState()
            try check(memoryState.canToggleModelMemory, "unloading allowed when idle")
            memoryState.begin(recording: true)
            try check(!memoryState.canToggleModelMemory, "unloading forbidden during a request")
            memoryState.cancel(); memoryState.modelsChanging = true
            try check(!memoryState.canToggleModelMemory, "double unloading forbidden")
            memoryState.finishUnloadingModels()
            try check(memoryState.modelsUnloaded && !memoryState.ready && memoryState.canToggleModelMemory, "unloaded models can be reloaded without false readiness")
            try check(memoryState.cleanerModelPhase == .unloaded && memoryState.agentModelPhase == .unloaded && memoryState.turnID == nil, "unloading resets the session and model states")

            try check(preferences.interfaceLanguage == .english, "interface defaults to English")
            preferences.setASRLanguage("ar")
            preferences.setInterfaceLanguage(.french)
            try check(AppPreferences(defaults: defaults).interfaceLanguage == .french, "interface language persists")
            try check(preferences.asrLanguageCode == "ar", "interface language does not change transcription language")
            preferences.setInterfaceLanguage(.english)
            preferences.setASRLanguage(nil)
            try check(InterfaceLanguage.resolved("unknown") == .english, "unknown interface language falls back to English")
            try check(localized("Settings…", language: .english) == "Settings…", "English resources are bundled")
            try check(localized("Settings…", language: .french) == "Réglages…", "French resources are bundled")
            try check(localized("Unknown diagnostic", language: .english) == "Unknown diagnostic", "unknown diagnostics are preserved")
            try check(localized("Hold {0}, speak, then release.", language: .english, arguments: ["{0} ⌥ Space"]) == "Hold {0} ⌥ Space, speak, then release.", "interpolated content stays literal")
            let frURL = Bundle.main.url(forResource: "Localizable", withExtension: "strings", subdirectory: "fr.lproj")!
            let enURL = Bundle.main.url(forResource: "Localizable", withExtension: "strings", subdirectory: "en.lproj")!
            let frStrings = try PropertyListSerialization.propertyList(from: Data(contentsOf: frURL), format: nil) as! [String: String]
            let enStrings = try PropertyListSerialization.propertyList(from: Data(contentsOf: enURL), format: nil) as! [String: String]
            try check(Set(frStrings.keys) == Set(enStrings.keys) && frStrings.count > 150, "all UI messages have both languages")
            try check(preferences.cleanerModel == "3.7B" && preferences.agentModel == "3.7B", "initial shared model: 3.7B")
            try check(preferences.cleanupGuardEnabled, "cleanup guard enabled by default")
            preferences.setCleanupGuardEnabled(false)
            try check(!AppPreferences(defaults: defaults).cleanupGuardEnabled, "disabling the cleanup guard persists")
            try check(LocalPipeline.cleanBody("t", options: .init(cleanupGuardEnabled: false))["guard"] as? Bool == false, "disabled guard is sent with the turn")
            preferences.setCleanupGuardEnabled(true)
            try check(AppPreferences.normalizedModel("1B", fallback: "7B") == "0.9B" && AppPreferences.normalizedModel("4B", fallback: "7B") == "3.7B", "sizes from before Jais 3 are migrated")
            try check(preferences.actionPermission == .ask, "actions on this Mac: ask by default")
            preferences.setActionPermission(.always)
            try check(AppPreferences(defaults: defaults).actionPermission == .always, "action setting persists")
            preferences.setActionPermission(.ask)
            let action = ActionRequest(title: "macOS command", message: #"{"reason":"Ouvrir Safari","details":"open -a Safari"}"#)
            try check(action == ActionRequest(tool: "macOS command", reason: "Ouvrir Safari", details: "open -a Safari"), "action request parsed from the extension")
            try check(ActionRequest(title: "Commande", message: "ls").details == "ls", "non-JSON message shown as is")
            try check(AppPreferences(defaults: defaults).cleanupGuardEnabled, "re-enabling the cleanup guard persists")
            for size in ["0.9B", "3.7B"] {
                preferences.setModelSize(size)
                let restoredModel = AppPreferences(defaults: defaults)
                try check(restoredModel.cleanerModel == size && restoredModel.agentModel == size, "the shared model persists for both stages")
                let body = LocalPipeline.cleanBody("t", options: .init(cleanerModel: restoredModel.cleanerModel, agentModel: restoredModel.agentModel))
                try check(body["model_size"] as? String == size && LocalPipeline.agentModelID(restoredModel.agentModel) == "k2-" + size.lowercased(), "the same model is sent to both stages")
            }
            preferences.setModelSize("invalid")
            try check(preferences.cleanerModel == "3.7B" && preferences.agentModel == "3.7B", "an invalid preference falls back to the default shared model")
            preferences.setModelSize("7B")
            try check(AppPreferences(defaults: defaults).modelSize == "7B" && LocalPipeline.agentModelID("7B") == "k2-7b", "7B is selectable and sent to the agent")
            preferences.setModelSize("3.7B")
            try check(ModelLoadPhase(rawValue: "not_installed") == .notInstalled, "a missing model is shown as not installed")
            let catalogJSON = #"[{"id":"asr-f5","kind":"asr","display_name":"ASR","installed":false,"downloadable":true,"download_bytes":10,"disk_bytes":0,"partial_bytes":0},{"id":"k2-3.7b","kind":"llm","display_name":"3.7B","size":"3.7B","installed":false,"downloadable":false,"disk_bytes":0,"partial_bytes":0},{"id":"k2-7b","kind":"llm","display_name":"7B","size":"7B","installed":false,"downloadable":true,"disk_bytes":0,"partial_bytes":0}]"#
            var catalog = try JSONDecoder().decode([CatalogModel].self, from: Data(catalogJSON.utf8))
            let store = ModelStore()
            try check(store.blockingMessage == nil && store.missingDownloads.isEmpty, "nothing is blocked before the catalog is read")
            store.show(models: catalog)
            try check(store.asr?.memoryKey == "ASR" && catalog[1].memoryKey == "3.7B", "each model maps to its memory state")
            try check(store.missingDownloads.map(\.id) == ["asr-f5", "k2-7b"], "at launch, published ASR and language models are offered")
            try check(store.blockingMessage == L("Transcription model not installed: download it in Settings."), "the assistant stays blocked without a transcription model")
            catalog[0] = try JSONDecoder().decode(CatalogModel.self, from: Data(#"{"id":"asr-f5","kind":"asr","display_name":"ASR","installed":true,"downloadable":true}"#.utf8))
            store.show(models: catalog)
            try check(store.blockingMessage == L("No language model installed: download one in Settings.") && store.missingDownloads.map(\.id) == ["k2-7b"], "then without a language model")
            catalog = try JSONDecoder().decode([CatalogModel].self, from: Data(catalogJSON.replacingOccurrences(of: #""installed":false"#, with: #""installed":true"#).utf8))
            store.show(models: catalog, downloads: ["k2-7b": DownloadProgress(bytes: 5, total: 10)])
            try check(store.missingDownloads.isEmpty && store.blockingMessage == nil && store.busy, "nothing is offered when required models are present")
            try check(DownloadProgress(bytes: 5, total: 10).fraction == 0.5 && DownloadProgress().fraction == nil, "download progress")
            defaults.removeObject(forKey: AppPreferences.modelSizeKey)
            defaults.set("0.9B", forKey: AppPreferences.agentModelKey)
            defaults.set("3.7B", forKey: AppPreferences.cleanerModelKey)
            let migrated = AppPreferences(defaults: defaults)
            try check(migrated.modelSize == "0.9B" && migrated.cleanerModel == migrated.agentModel, "migration from the previous agent choice")
            defaults.set("3.7B", forKey: AppPreferences.agentModelKey)
            try check(AppPreferences(defaults: defaults).modelSize == "0.9B", "migration does not replace the saved shared choice")
            try check(preferences.shortcutChoice == 0 && preferences.asrLanguageCode == nil, "initial settings: ⌥ Space and automatic language")
            preferences.setASRLanguage(" FR ")
            preferences.setShortcutChoice(2)
            let restored = AppPreferences(defaults: defaults)
            try check(restored.asrLanguageCode == "fr" && restored.shortcutChoice == 2, "language and shortcut persist after reopening")
            for language in ASRLanguageChoice.choices {
                preferences.setASRLanguage(language.code)
                try check(preferences.asrLanguage == language.code, "accepted language: \(language.label)")
            }
            preferences.setASRLanguage("not-a-language")
            try check(preferences.asrLanguageCode == nil, "an unknown language falls back to automatic detection")
            try check(AppPreferences.normalizedShortcutChoice(-1) == 0 && AppPreferences.normalizedShortcutChoice(999) == 0, "an invalid shortcut falls back to ⌥ Space")
            try check(LocalPipeline.asrBody(audio: Data(), language: nil)["language"] == nil, "automatic mode omits the transcription language")
            try check(LocalPipeline.asrBody(audio: Data(), language: "fr")["language"] as? String == "fr", "the French choice is sent with the audio turn")
            try check(LocalPipeline.asrBody(audio: Data(), language: nil)["language"] == nil, "the language choice does not leak into another turn")
            try check(AudioRecorder.maximumSeconds == 30, "the maximum duration matches the 30 seconds the ASR accepts")
            let loadingState = TurnState()
            let selectableHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready","models":{"k2-cleaner":{"model_size":"0.9B","display_name":"MBZUAI · 0.9B","status":"ready"},"k2-agent":{"model_size":"3.7B","display_name":"MBZUAI · 3.7B","status":"loading"}}}"#.utf8))
            loadingState.updateModelHealth(selectableHealth.selectedModels(cleaner: "3.7B", agent: "0.9B"), serviceStatus: "ready")
            try check(loadingState.cleanerModelLabel == "MBZUAI · 3.7B" && loadingState.cleanerModelPhase == .loading && loadingState.agentModelLabel == "MBZUAI · 0.9B" && loadingState.agentModelPhase == .ready, "labels and states follow the selected models")
            loadingState.updateModelHealth(selectableHealth.selectedModels(cleaner: "0.9B", agent: "0.9B"), serviceStatus: "ready")
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .ready, "both roles can share a loaded model")
            let absentHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready"}"#.utf8))
            loadingState.updateModelHealth(absentHealth.selectedModels(cleaner: "0.9B", agent: "3.7B"), serviceStatus: "ready")
            try check(loadingState.cleanerModelPhase == .unavailable && loadingState.agentModelPhase == .unavailable, "a service without selected models does not report false readiness")
            let inventoryHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready","models":{"k2-cleaner":{"model_size":"0.9B","status":"ready"},"k2-agent":{"model_size":"3.7B","status":"unloaded"}}}"#.utf8))
            loadingState.updateModelInventory(inventoryHealth.models)
            try check(loadingState.modelPhase("0.9B") == .ready && loadingState.modelPhase("3.7B") == .unloaded, "independent states: 0.9B loaded, 3.7B unloaded")
            loadingState.updateModelHealth(inventoryHealth.selectedModels(cleaner: "3.7B", agent: "3.7B"), serviceStatus: "ready")
            try check(loadingState.agentModelPhase == .unloaded && loadingState.modelPhase("0.9B") == .ready, "changing the selection does not change which models are resident")
            try check(loadingState.canChangeModel("0.9B") && loadingState.canChangeModel("3.7B"), "individual unload and reload available when idle")
            loadingState.modelsChanging = true
            try check(!loadingState.canChangeModel("0.9B") && !loadingState.canChangeModel("3.7B"), "concurrent memory operations forbidden")
            loadingState.modelsChanging = false
            loadingState.modelsUnavailable()
            try check(loadingState.modelPhase("0.9B") == .unavailable, "a disconnection clears previous individual states")
            let partialHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"starting","message":"Loading 3.7B","models":{"k2-cleaner":{"display_name":"0.9B","status":"ready"},"k2-agent":{"display_name":"3.7B","status":"loading"}}}"#.utf8))
            loadingState.updateModelHealth(partialHealth.models, serviceStatus: partialHealth.status)
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .loading, "a ready cleaner and a loading agent are distinguished")
            let failedHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"error","message":"3.7B error","models":{"k2-cleaner":{"display_name":"0.9B","status":"ready"},"k2-agent":{"display_name":"3.7B","status":"error","error":"Missing weights"}}}"#.utf8))
            loadingState.updateModelHealth(failedHealth.models, serviceStatus: failedHealth.status)
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .error && loadingState.agentModelError == "Missing weights", "a 3.7B failure does not mark the loaded 0.9B as failed")
            loadingState.modelsUnavailable()
            try check(loadingState.cleanerModelPhase == .unavailable && loadingState.agentModelPhase == .unavailable, "a lost connection does not keep a false ready state")
            loadingState.resetModelsLoading()
            try check(loadingState.cleanerModelPhase == .waiting && loadingState.agentModelPhase == .waiting, "a restart resets both model indicators")
            loadingState.updateModelHealth(nil, serviceStatus: "ready")
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .ready, "older services without detailed states stay compatible")
            let failure = ServiceDiagnostics.exitMessage(code: 1, output: "Model profile: /tmp/profile.json\nInvalid model profile: missing weights.\n")
            try check(failure.contains("Invalid model profile: missing weights."), "the supervisor error is readable, not just its exit code")
            try check(ServiceDiagnostics.exitMessage(code: 2, output: "").contains("code 2"), "an exit without diagnostics keeps the error code")
            let longMessage = "Invalid profile " + String(repeating: "/long-path", count: 40) + ": missing index"
            let concise = ServiceDiagnostics.concise(longMessage)
            try check(concise.count <= 240 && concise.hasSuffix("missing index"), "a long diagnostic stays readable and keeps its final cause")
            let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("horizon-launch-log-test-\(UUID().uuidString)", isDirectory: true)
            defer { try? FileManager.default.removeItem(at: temporary) }
            let logURL = temporary.appendingPathComponent("supervisor.log")
            let earlierLog = try ServiceLaunchLog(url: logURL)
            try earlierLog.handle.write(contentsOf: Data("Earlier error\n".utf8)); earlierLog.close()
            let latestLog = try ServiceLaunchLog(url: logURL)
            let diagnosticProcess = Process()
            diagnosticProcess.executableURL = URL(fileURLWithPath: "/usr/bin/printf")
            diagnosticProcess.arguments = ["New launch\nInvalid profile: missing tokenizer\n"]
            diagnosticProcess.standardOutput = latestLog.handle
            diagnosticProcess.standardError = latestLog.handle
            try diagnosticProcess.run(); diagnosticProcess.waitUntilExit(); latestLog.close()
            try check(diagnosticProcess.terminationStatus == 0, "child process output is logged without an interactive output channel")
            let latestOutput = ServiceLaunchLog.tail(url: logURL, since: latestLog.startOffset)
            try check(latestOutput.contains("missing tokenizer") && !latestOutput.contains("Earlier error"), "the log does not reuse an error from a previous launch")
            let wholeLog = try String(contentsOf: logURL, encoding: .utf8)
            try check(wholeLog.contains("Earlier error"), "the log of previous launches is kept")
            try check(ServiceLaunchLog.tail(url: logURL, since: latestLog.startOffset, maximumBytes: 16).utf8.count <= 16, "reading the diagnostic is bounded")
            try check(ServiceDiagnostics.restartInstruction == L("Resolve the issue, then quit and reopen JAIS Voice."), "recovery explicitly requires restarting the app")
            let clarification = TurnState()
            let questionTurn = clarification.begin()
            let questionConversation = clarification.conversationID
            clarification.accept(WireEvent(type: "clarification_request", turn_id: questionTurn, text: "Which city?"))
            try check(clarification.awaitingClarification && clarification.finished && !clarification.isBusy, "clarification waits without blocking dictation")
            clarification.accept(WireEvent(type: "complete", turn_id: questionTurn, text: "late completion"))
            try check(clarification.response == "Which city?", "late completion cannot overwrite waiting question")
            clarification.begin(recording: true, continuingConversation: true)
            try check(!clarification.awaitingClarification && clarification.recording && clarification.conversationID == questionConversation, "dictated answer continues clarification conversation")
            try check(clarification.history.last?.response == "Which city?", "question remains visible in conversation history")
            let conversation = TurnState()
            let first = conversation.begin()
            let conversationID = conversation.conversationID
            conversation.accept(WireEvent(type: "correction", turn_id: first, text: "Remember Paris"))
            conversation.accept(WireEvent(type: "complete", turn_id: first, text: "Paris remembered"))
            let followup = conversation.begin(recording: true, continuingConversation: true)
            try check(followup != first && conversation.conversationID == conversationID, "follow-up has a new turn in the same conversation")
            try check(conversation.history.count == 1 && conversation.history[0].prompt == "Remember Paris" && conversation.history[0].response == "Paris remembered", "follow-up preserves the previous exchange")
            try check(conversation.cleaned.isEmpty && conversation.response.isEmpty && conversation.recording, "follow-up records and cleans a fresh prompt")
            conversation.accept(WireEvent(type: "response_delta", turn_id: first, text: "stale"))
            try check(conversation.response.isEmpty, "previous turn cannot write into follow-up")
            conversation.fail("Microphone error")
            conversation.begin(continuingConversation: true)
            try check(conversation.history.count == 1 && conversation.conversationID == conversationID, "empty failed recording preserves conversation without adding a message")
            try check(LocalPipeline.TurnOptions(conversationID: conversation.conversationID).conversationID == conversationID, "conversation identity travels with each recording")
            conversation.begin()
            try check(conversation.history.isEmpty && conversation.conversationID != conversationID, "new conversation clears history and changes identity")
            let state = TurnState()
            try check(state.cleanerModelLabel == L("Local corrector") && state.agentModelLabel == L("Local agent"), "generic labels before the model profile arrives")
            let legacyHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready"}"#.utf8))
            state.updateModelLabels(legacyHealth.models)
            try check(legacyHealth.models == nil && state.cleanerModelLabel == L("Local corrector"), "compatible with a service state without a model profile")
            let profileHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready","models":{"k2-cleaner":{"display_name":"  Jais 3 · 0.9B  ","repo":"IFM/Jais-3-0.9B","quantization":null},"k2-agent":{"display_name":"Jais 3 · 3.7B","repo":"IFM/Jais-3-3.7B"}}}"#.utf8))
            state.updateModelLabels(profileHealth.models)
            try check(state.cleanerModelLabel == "Jais 3 · 0.9B" && state.agentModelLabel == "Jais 3 · 3.7B", "both model labels follow the active profile")
            try check(profileHealth.models?["k2-agent"]?.repo == "IFM/Jais-3-3.7B", "the profile repository decodes without being displayed")
            state.begin()
            try check(state.cleanerModelLabel == "Jais 3 · 0.9B" && state.agentModelLabel == "Jais 3 · 3.7B", "a new turn keeps the active profile labels")
            let unnamedHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready","models":{"k2-cleaner":{"display_name":"  ","repo":"long/private/path/checkpoint"},"k2-agent":{"repo":"legacy/model"}}}"#.utf8))
            state.updateModelLabels(unnamedHealth.models)
            try check(state.cleanerModelLabel == L("Local corrector") && state.agentModelLabel == L("Local agent"), "profiles without a readable name: no old model or path is shown")
            state.stage = "refining"
            try check(state.stageLabel == L("Correcting transcript…"), "the correction status does not assume the model family")
            state.stage = "acting"
            try check(state.stageLabel == L("Preparing response…"), "the response status does not assume the model family")
            let oldID = state.begin(); let id = state.begin()
            state.accept(WireEvent(type: "response_delta", turn_id: oldID, text: "old response"))
            try check(state.response.isEmpty, "packets from an old turn are ignored")
            state.accept(WireEvent(type: "response_delta", turn_id: id, text: "hel"))
            state.accept(WireEvent(type: "response_delta", turn_id: id, text: "lo"))
            try check(state.response == "hello", "response fragments are assembled")
            state.cancel(); state.accept(WireEvent(type: "complete", turn_id: id, text: "too late"))
            try check(state.stage == "cancelled" && state.response == "hello", "cancellation ignores late responses")
            let toolTurn = state.begin()
            state.accept(WireEvent(type: "tool_start", turn_id: toolTurn, tool_id: "failed-tool", tool_name: "browser_navigate"))
            state.accept(WireEvent(type: "tool_end", turn_id: toolTurn, tool_id: "failed-tool", result: "Page inaccessible", error_code: "tool_error"))
            try check(state.actions.first?.errorCode == "tool_error" && state.actions.first?.finished == true, "a tool that ended in error stays marked as failed")
            let diff = TranscriptDiff.words(original: "إلى أحمد لا إلى سارة", cleaned: "إلى سارة")
            try check(diff.filter { !$0.removed }.map(\.text).joined(separator: " ") == "إلى سارة", "Arabic diff keeps the correction")
            try check(diff.contains(DiffWord(text: "أحمد", removed: true)), "removed words are identified")
            try check(TranscriptDiff.isRTL("ابحث في باريس") && !TranscriptDiff.isRTL("https://www.louvre.fr"), "Arabic and URL reading direction")
            var clock = DismissalClock(); let start = Date(timeIntervalSince1970: 0)
            clock.reset(at: start)
            _ = clock.tick(at: start.addingTimeInterval(4), paused: false)
            _ = clock.tick(at: start.addingTimeInterval(24), paused: true)
            try check(clock.remaining == 6, "hovering pauses the remaining delay")
            try check(clock.tick(at: start.addingTimeInterval(30), paused: false), "closes after ten active seconds")
            try check(!clock.tick(at: start.addingTimeInterval(31), paused: true), "an expired delay never closes a paused window")
            let wav = WAVEncoder.encode(samples: [0, 1, -1])
            try check(wav.count == 50 && Array(wav[24..<28]) == [128, 62, 0, 0] && Array(wav[34..<36]) == [16, 0], "mono PCM16 WAV at 16 kHz")
            let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 48_000)!
            buffer.frameLength = 48_000
            for index in 0..<48_000 { buffer.floatChannelData![0][index] = index >= 43_200 ? 0.8 : 0 }
            let converted = try AudioRecorder.convertToWAV(buffer)
            let frames = (converted.count - 44) / 2
            try check(abs(frames - 16_000) <= 4, "48 → 16 kHz conversion preserves duration (\(frames) samples)")
            let tail = converted.suffix(800)
            let bytes = Array(tail)
            let peak = stride(from: 0, to: bytes.count - 1, by: 2).map { index in
                abs(Int(Int16(bitPattern: UInt16(bytes[index]) | UInt16(bytes[index + 1]) << 8)))
            }.max() ?? 0
            try check(peak > 20_000, "the end of the recording survives conversion")
            if let index = CommandLine.arguments.firstIndex(of: "--audio-fixture"), CommandLine.arguments.count > index + 1 {
                let url = URL(fileURLWithPath: CommandLine.arguments[index + 1])
                let file = try AVAudioFile(forReading: url)
                let sourceDuration = Double(file.length) / file.processingFormat.sampleRate
                let imported = try AudioRecorder.importWAV(from: url)
                let seconds = Double(imported.count - 44) / 32_000
                try check(abs(seconds - sourceDuration) < 0.01, "provided ASR file, duration after conversion (\(seconds) s)")
            }
            print("\(passed) native checks passed.")
            return 0
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            return 1
        }
    }
}
