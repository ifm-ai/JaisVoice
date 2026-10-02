import AppKit
import Foundation
import AVFoundation

/// `--self-test`: native checks without microphone, services or models.
@MainActor enum SelfTests {
    static func run() -> Int32 {
        var passed = 0
        func check(_ condition: @autoclosure () -> Bool, _ name: String) throws {
            if !condition() { throw VoiceError.message("FAILED: \(name)") }
            passed += 1; print("OK: \(name)")
        }
        do {
            // Preferences
            let suite = "jais-voice-preferences-test-\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suite)!
            defaults.setVolatileDomain([:], forName: UserDefaults.argumentDomain)
            defer { defaults.removePersistentDomain(forName: suite) }
            let preferences = AppPreferences(defaults: defaults)
            try check(preferences.interfaceLanguage == .english, "interface defaults to English")
            try check(preferences.shortcutChoice == 0 && preferences.asrLanguageCode == nil, "initial settings: ⌥ Space and automatic language")
            try check(preferences.modelSize == "3.7B" && preferences.actionPermission == .ask, "defaults: 3.7B, ask before actions")
            preferences.setInterfaceLanguage(.french)
            preferences.setASRLanguage(" FR ")
            preferences.setShortcutChoice(2)
            preferences.setModelSize("7B")
            preferences.setActionPermission(.always)
            let restored = AppPreferences(defaults: defaults)
            try check(restored.interfaceLanguage == .french && restored.asrLanguageCode == "fr" && restored.shortcutChoice == 2
                      && restored.modelSize == "7B" && restored.actionPermission == .always,
                      "settings persist after reopening")
            preferences.setInterfaceLanguage(.english)
            preferences.setModelSize("invalid")
            preferences.setASRLanguage("not-a-language")
            try check(preferences.modelSize == "3.7B" && preferences.asrLanguageCode == nil, "invalid values fall back to the defaults")
            try check(AppPreferences.normalizedShortcutChoice(999) == 0, "an invalid shortcut falls back to ⌥ Space")

            // Localization
            try check(localized("Settings…", language: .french) == "Réglages…", "French resources are bundled")
            try check(localized("Unknown diagnostic", language: .english) == "Unknown diagnostic", "unknown diagnostics are preserved")
            try check(localized("Hold {0}, speak, then release.", language: .english, arguments: ["{0} ⌥ Space"]) == "Hold {0} ⌥ Space, speak, then release.",
                      "interpolated content stays literal")
            let strings = try ["fr", "en"].map { language -> Set<String> in
                let url = Bundle.main.url(forResource: "Localizable", withExtension: "strings", subdirectory: "\(language).lproj")!
                return Set((try PropertyListSerialization.propertyList(from: Data(contentsOf: url), format: nil) as! [String: String]).keys)
            }
            try check(strings[0] == strings[1], "every interface string exists in both languages")

            // Requests sent to the services and the agent
            let options = LocalPipeline.TurnOptions(conversationID: "c", modelSize: "0.9B")
            let body = LocalPipeline.cleanBody("t", options: options)
            try check(body["model_size"] as? String == "0.9B", "cleanup gets the model size")
            try check(LocalPipeline.agentModelID("3.7B") == "jais-3-3.7b", "the agent model ID matches agent/models.json")
            try check(LocalPipeline.asrBody(audio: Data(), language: nil)["language"] == nil
                      && LocalPipeline.asrBody(audio: Data(), language: "fr")["language"] as? String == "fr", "automatic mode omits the language")
            let action = ActionRequest(title: "macOS command", message: #"{"reason":"Open Safari","details":"open -a Safari"}"#)
            try check(action == ActionRequest(tool: "macOS command", reason: "Open Safari", details: "open -a Safari"), "action request parsed from the extension")
            try check(ActionRequest(title: "Command", message: "ls").details == "ls", "a non-JSON message is shown as is")

            // Updates
            let releaseJSON = #"{"tag_name":"v1.2.0-build.15","html_url":"https://github.com/o/r/releases/tag/v1.2.0-build.15","assets":[{"browser_download_url":"https://github.com/o/r/releases/download/v1.2.0-build.15/JAIS-Voice-1.2.0.dmg"}]}"#
            let release = AppRelease.parse(Data(releaseJSON.utf8))
            try check(release?.version == "1.2.0" && release?.build == 15 && release?.downloadURL.pathExtension == "dmg" && release?.label == "1.2.0 (build 15)",
                      "a CI release tag gives the version, build and disk image")
            try check(AppRelease.parse(Data(#"{"tag_name":"nightly","html_url":"https://github.com"}"#.utf8)) == nil, "other tags are ignored")

            // Models
            let catalogJSON = #"[{"id":"asr-f5","kind":"asr","display_name":"ASR","installed":false,"download_bytes":10},{"id":"jais-3-3.7b","kind":"llm","display_name":"3.7B","size":"3.7B","installed":false},{"id":"jais-3-7b","kind":"llm","display_name":"7B","size":"7B","installed":false}]"#
            var catalog = try JSONDecoder().decode([CatalogModel].self, from: Data(catalogJSON.utf8))
            let store = ModelStore()
            try check(store.blockingMessage == nil && store.missingDownloads.isEmpty, "nothing is blocked before the catalog is read")
            store.show(models: catalog)
            try check(store.missingDownloads.map(\.id) == ["asr-f5", "jais-3-3.7b"], "at launch, the ASR and the default size are offered")
            try check(store.blockingMessage == L("Transcription model not installed: download it in Settings."), "blocked without a transcription model")
            catalog = try JSONDecoder().decode([CatalogModel].self, from: Data(catalogJSON.replacingOccurrences(of: #""installed":false"#, with: #""installed":true"#).utf8))
            store.show(models: catalog)
            try check(store.missingDownloads.isEmpty && store.blockingMessage == nil, "nothing is offered when the models are present")
            let started = Date()
            let stalled = DownloadProgress(bytes: 5, total: 10, lastActivity: started)
            try check(stalled.fraction == 0.5 && stalled.isActive(at: started.addingTimeInterval(5)) && !stalled.isActive(at: started.addingTimeInterval(20)),
                      "a download without new bytes for 15 s is shown as waiting")
            var measured = DownloadProgress(total: 100_000_000)
            measured.update(bytes: 40_000_000, at: started)
            measured.update(bytes: 60_000_000, at: started.addingTimeInterval(2))
            try check(abs((measured.speed ?? 0) - 10_000_000) < 1, "download speed over 2 seconds")

            // Model state
            let state = TurnState()
            let health = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Ready","models":{"0.9B":{"display_name":"Jais 3 · 0.9B","status":"ready"},"3.7B":{"display_name":"Jais 3 · 3.7B","status":"error","error":"Missing weights"},"ASR":{"status":"ready"}}}"#.utf8))
            state.updateModels(health, selected: "0.9B")
            try check(state.modelLabel == "Jais 3 · 0.9B" && state.modelPhase == .ready && state.phase(of: "ASR") == .ready, "the selected model's state is shown")
            state.updateModels(health, selected: "3.7B")
            try check(state.modelPhase == .error && state.modelError == "Missing weights" && state.phase(of: "0.9B") == .ready, "a failed size does not mark another as failed")
            try check(state.canChangeModel("0.9B") && state.phase(of: "7B") == .unavailable, "loading allowed when idle; unknown sizes are unavailable")
            state.updateModels(nil, selected: "3.7B")
            try check(state.modelPhase == .unavailable && state.modelInventory.isEmpty, "a lost connection keeps no false ready state")
            state.begin(recording: true)
            try check(!state.canToggleModelMemory, "unloading is forbidden during a request")
            state.cancel()
            state.finishUnloadingModels()
            try check(state.modelsUnloaded && !state.ready && state.canToggleModelMemory && state.turnID == nil, "unloading resets the session and can be undone")

            // Turns and conversations
            let conversation = TurnState()
            let first = conversation.begin()
            let conversationID = conversation.conversationID
            conversation.accept(WireEvent(type: "correction", turn_id: first, text: "Remember Paris"))
            conversation.accept(WireEvent(type: "complete", turn_id: first, text: "Paris remembered"))
            let followup = conversation.begin(recording: true, continuingConversation: true)
            try check(followup != first && conversation.conversationID == conversationID && conversation.history.first?.response == "Paris remembered",
                      "a follow-up continues the conversation and keeps the previous exchange")
            conversation.accept(WireEvent(type: "response_delta", turn_id: first, text: "stale"))
            try check(conversation.response.isEmpty, "a previous turn cannot write into the follow-up")
            conversation.accept(WireEvent(type: "response_delta", turn_id: followup, text: "hel"))
            conversation.accept(WireEvent(type: "response_delta", turn_id: followup, text: "lo"))
            try check(conversation.response == "hello", "response fragments are assembled")
            conversation.cancel(); conversation.accept(WireEvent(type: "complete", turn_id: followup, text: "too late"))
            try check(conversation.stage == "cancelled" && conversation.response == "hello", "cancellation ignores late responses")
            conversation.begin()
            try check(conversation.history.isEmpty && conversation.conversationID != conversationID, "a new conversation clears the history")
            let question = conversation.begin()
            conversation.accept(WireEvent(type: "clarification_request", turn_id: question, text: "Which city?"))
            conversation.accept(WireEvent(type: "complete", turn_id: question, text: "late completion"))
            try check(conversation.awaitingClarification && !conversation.isBusy && conversation.response == "Which city?", "a clarification waits without blocking dictation")
            let toolTurn = conversation.begin()
            conversation.accept(WireEvent(type: "tool_start", turn_id: toolTurn, tool_id: "tool", tool_name: "browser_navigate"))
            conversation.accept(WireEvent(type: "tool_end", turn_id: toolTurn, tool_id: "tool", result: "Page inaccessible", error_code: "tool_error"))
            try check(conversation.actions.first?.errorCode == "tool_error" && conversation.actions.first?.finished == true, "a failed tool stays marked as failed")

            // Diagnostics
            try check(ServiceDiagnostics.exitMessage(code: 1, output: "Starting\nInvalid model catalog.\n").contains("Invalid model catalog."), "the supervisor error is readable")
            let concise = ServiceDiagnostics.concise("Invalid " + String(repeating: "/long-path", count: 40) + ": missing index")
            try check(concise.count <= 240 && concise.hasSuffix("missing index"), "a long diagnostic keeps its final cause")
            let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("jais-voice-log-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: temporary) }
            let logURL = temporary.appendingPathComponent("supervisor.log")
            let earlier = try ServiceLaunchLog(url: logURL)
            try earlier.handle.write(contentsOf: Data("Earlier error\n".utf8)); earlier.close()
            let latest = try ServiceLaunchLog(url: logURL)
            try latest.handle.write(contentsOf: Data("Missing tokenizer\n".utf8)); latest.close()
            let tail = ServiceLaunchLog.tail(url: logURL, since: latest.startOffset)
            try check(tail.contains("Missing tokenizer") && !tail.contains("Earlier error"), "the log tail does not reuse a previous launch's error")

            // Display
            let diff = TranscriptDiff.words(original: "إلى أحمد لا إلى سارة", cleaned: "إلى سارة")
            try check(diff.filter { !$0.removed }.map(\.text).joined(separator: " ") == "إلى سارة" && diff.contains(DiffWord(text: "أحمد", removed: true)),
                      "the Arabic diff shows the removed words")
            try check(TranscriptDiff.isRTL("ابحث في باريس") && !TranscriptDiff.isRTL("https://www.louvre.fr"), "reading direction")
            var clock = DismissalClock(); let start = Date(timeIntervalSince1970: 0)
            clock.reset(at: start)
            _ = clock.tick(at: start.addingTimeInterval(4), paused: false)
            _ = clock.tick(at: start.addingTimeInterval(24), paused: true)
            try check(clock.remaining == 6 && clock.tick(at: start.addingTimeInterval(30), paused: false), "hovering pauses the ten-second dismissal")

            // Audio
            let wav = WAVEncoder.encode(samples: [0, 1, -1])
            try check(wav.count == 50 && Array(wav[24..<28]) == [128, 62, 0, 0] && Array(wav[34..<36]) == [16, 0], "mono PCM16 WAV at 16 kHz")
            let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 48_000)!
            buffer.frameLength = 48_000
            for index in 0..<48_000 { buffer.floatChannelData![0][index] = index >= 43_200 ? 0.8 : 0 }
            let converted = try AudioRecorder.convertToWAV(buffer)
            try check(abs((converted.count - 44) / 2 - 16_000) <= 4, "48 → 16 kHz conversion preserves duration")
            let bytes = Array(converted.suffix(800))
            let peak = stride(from: 0, to: bytes.count - 1, by: 2).map { abs(Int(Int16(bitPattern: UInt16(bytes[$0]) | UInt16(bytes[$0 + 1]) << 8))) }.max() ?? 0
            try check(peak > 20_000, "the end of the recording survives conversion")

            print("\(passed) native checks passed.")
            return 0
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            return 1
        }
    }
}
