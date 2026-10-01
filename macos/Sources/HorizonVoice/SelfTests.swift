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
            if !condition() { throw VoiceError.message("ÉCHEC : \(name)") }
            passed += 1; print("OK : \(name)")
        }
        do {
            let suite = "horizon-preferences-test-\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suite)!
            defaults.setVolatileDomain([:], forName: UserDefaults.argumentDomain)
            defer { defaults.removePersistentDomain(forName: suite) }
            let preferences = AppPreferences(defaults: defaults)
            let memoryState = TurnState()
            try check(memoryState.canToggleModelMemory, "déchargement autorisé au repos")
            memoryState.begin(recording: true)
            try check(!memoryState.canToggleModelMemory, "déchargement interdit pendant une demande")
            memoryState.cancel(); memoryState.modelsChanging = true
            try check(!memoryState.canToggleModelMemory, "double déchargement interdit")
            memoryState.finishUnloadingModels()
            try check(memoryState.modelsUnloaded && !memoryState.ready && memoryState.canToggleModelMemory, "modèles déchargés rechargeables sans fausse disponibilité")
            try check(memoryState.cleanerModelPhase == .unloaded && memoryState.agentModelPhase == .unloaded && memoryState.turnID == nil, "déchargement réinitialise la session et les états modèles")

            try check(preferences.interfaceLanguage == .french, "interface defaults to French")
            preferences.setASRLanguage("ar")
            preferences.setInterfaceLanguage(.english)
            try check(AppPreferences(defaults: defaults).interfaceLanguage == .english, "interface language persists")
            try check(preferences.asrLanguageCode == "ar", "interface language does not change transcription language")
            preferences.setInterfaceLanguage(.french)
            preferences.setASRLanguage(nil)
            try check(InterfaceLanguage.resolved("unknown") == .french, "unknown interface language falls back to French")
            try check(localized("Réglages…", language: .english) == "Settings…", "English resources are bundled")
            try check(localized("Réglages…", language: .french) == "Réglages…", "French resources are bundled")
            try check(localized("Unknown diagnostic", language: .english) == "Unknown diagnostic", "unknown diagnostics are preserved")
            try check(localized("Maintenez {0}, parlez, puis relâchez.", language: .english, arguments: ["{0} ⌥ Space"]) == "Hold {0} ⌥ Space, speak, then release.", "interpolated content stays literal")
            let frURL = Bundle.main.url(forResource: "Localizable", withExtension: "strings", subdirectory: "fr.lproj")!
            let enURL = Bundle.main.url(forResource: "Localizable", withExtension: "strings", subdirectory: "en.lproj")!
            let frStrings = try PropertyListSerialization.propertyList(from: Data(contentsOf: frURL), format: nil) as! [String: String]
            let enStrings = try PropertyListSerialization.propertyList(from: Data(contentsOf: enURL), format: nil) as! [String: String]
            try check(Set(frStrings.keys) == Set(enStrings.keys) && frStrings.count > 150, "all UI messages have both languages")
            try check(preferences.cleanerModel == "4B" && preferences.agentModel == "4B", "modèle partagé initial : 4B")
            try check(preferences.cleanupGuardEnabled, "garde-fou activé par défaut")
            preferences.setCleanupGuardEnabled(false)
            try check(!AppPreferences(defaults: defaults).cleanupGuardEnabled, "désactivation du garde-fou persistante")
            try check(LocalPipeline.cleanBody("t", options: .init(cleanupGuardEnabled: false))["guard"] as? Bool == false, "désactivation transmise avec le tour")
            preferences.setCleanupGuardEnabled(true)
            try check(AppPreferences(defaults: defaults).cleanupGuardEnabled, "réactivation du garde-fou persistante")
            for size in ["1B", "4B"] {
                preferences.setModelSize(size)
                let restoredModel = AppPreferences(defaults: defaults)
                try check(restoredModel.cleanerModel == size && restoredModel.agentModel == size, "le modèle partagé persiste pour les deux étapes")
                let body = LocalPipeline.cleanBody("t", options: .init(cleanerModel: restoredModel.cleanerModel, agentModel: restoredModel.agentModel))
                try check(body["model_size"] as? String == size && LocalPipeline.agentModelID(restoredModel.agentModel) == "k2-" + size.lowercased(), "le même modèle est transmis aux deux étapes")
            }
            preferences.setModelSize("invalid")
            try check(preferences.cleanerModel == "4B" && preferences.agentModel == "4B", "une préférence invalide revient au modèle partagé par défaut")
            preferences.setModelSize("7B")
            try check(AppPreferences(defaults: defaults).modelSize == "7B" && LocalPipeline.agentModelID("7B") == "k2-7b", "le 7B est sélectionnable et transmis à l’agent")
            preferences.setModelSize("4B")
            try check(ModelLoadPhase(rawValue: "not_installed") == .notInstalled, "un modèle absent est affiché comme non installé")
            let catalogJSON = #"[{"id":"asr-f5","kind":"asr","display_name":"ASR","installed":false,"downloadable":true,"download_bytes":10,"disk_bytes":0,"partial_bytes":0},{"id":"k2-4b","kind":"llm","display_name":"4B","size":"4B","installed":false,"downloadable":false,"disk_bytes":0,"partial_bytes":0},{"id":"k2-7b","kind":"llm","display_name":"7B","size":"7B","installed":false,"downloadable":true,"disk_bytes":0,"partial_bytes":0}]"#
            var catalog = try JSONDecoder().decode([CatalogModel].self, from: Data(catalogJSON.utf8))
            let store = ModelStore()
            try check(store.blockingMessage == nil && store.missingDownloads.isEmpty, "rien n’est bloqué avant la lecture du catalogue")
            store.show(models: catalog)
            try check(store.asr?.memoryKey == "ASR" && catalog[1].memoryKey == "4B", "chaque modèle retrouve son état mémoire")
            try check(store.missingDownloads.map(\.id) == ["asr-f5", "k2-7b"], "au lancement, l’ASR et un modèle de langage publiés sont proposés")
            try check(store.blockingMessage == L("Modèle de transcription non installé : téléchargez-le dans les réglages."), "l’assistant reste bloqué sans modèle de transcription")
            catalog[0] = try JSONDecoder().decode(CatalogModel.self, from: Data(#"{"id":"asr-f5","kind":"asr","display_name":"ASR","installed":true,"downloadable":true}"#.utf8))
            store.show(models: catalog)
            try check(store.blockingMessage == L("Aucun modèle de langage installé : téléchargez-en un dans les réglages.") && store.missingDownloads.map(\.id) == ["k2-7b"], "puis sans modèle de langage")
            catalog = try JSONDecoder().decode([CatalogModel].self, from: Data(catalogJSON.replacingOccurrences(of: #""installed":false"#, with: #""installed":true"#).utf8))
            store.show(models: catalog, downloads: ["k2-7b": DownloadProgress(bytes: 5, total: 10)])
            try check(store.missingDownloads.isEmpty && store.blockingMessage == nil && store.busy, "rien n’est proposé quand les modèles requis sont présents")
            try check(DownloadProgress(bytes: 5, total: 10).fraction == 0.5 && DownloadProgress().fraction == nil, "progression du téléchargement")
            defaults.removeObject(forKey: AppPreferences.modelSizeKey)
            defaults.set("1B", forKey: AppPreferences.agentModelKey)
            defaults.set("4B", forKey: AppPreferences.cleanerModelKey)
            let migrated = AppPreferences(defaults: defaults)
            try check(migrated.modelSize == "1B" && migrated.cleanerModel == migrated.agentModel, "migration vers le précédent choix agent")
            defaults.set("4B", forKey: AppPreferences.agentModelKey)
            try check(AppPreferences(defaults: defaults).modelSize == "1B", "la migration ne remplace pas le choix partagé enregistré")
            try check(preferences.shortcutChoice == 0 && preferences.asrLanguageCode == nil, "réglages initiaux : ⌥ Espace et langue automatique")
            preferences.setASRLanguage(" FR ")
            preferences.setShortcutChoice(2)
            let restored = AppPreferences(defaults: defaults)
            try check(restored.asrLanguageCode == "fr" && restored.shortcutChoice == 2, "langue et raccourci sont conservés après réouverture")
            for language in ASRLanguageChoice.choices {
                preferences.setASRLanguage(language.code)
                try check(preferences.asrLanguage == language.code, "langue acceptée : \(language.label)")
            }
            preferences.setASRLanguage("not-a-language")
            try check(preferences.asrLanguageCode == nil, "une langue inconnue revient à la détection automatique")
            try check(AppPreferences.normalizedShortcutChoice(-1) == 0 && AppPreferences.normalizedShortcutChoice(999) == 0, "un raccourci invalide revient à ⌥ Espace")
            try check(LocalPipeline.asrBody(audio: Data(), language: nil)["language"] == nil, "le mode automatique omet la langue de la transcription")
            try check(LocalPipeline.asrBody(audio: Data(), language: "fr")["language"] as? String == "fr", "le choix français est transmis avec le tour audio")
            try check(LocalPipeline.asrBody(audio: Data(), language: nil)["language"] == nil, "le choix de langue ne fuit pas vers un autre tour")
            try check(AudioRecorder.maximumSeconds == 30, "la durée maximale correspond aux trente secondes acceptées par l’ASR")
            let loadingState = TurnState()
            let selectableHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Prêt","models":{"k2-cleaner":{"model_size":"1B","display_name":"MBZUAI · 1B","status":"ready"},"k2-agent":{"model_size":"4B","display_name":"MBZUAI · 4B","status":"loading"}}}"#.utf8))
            loadingState.updateModelHealth(selectableHealth.selectedModels(cleaner: "4B", agent: "1B"), serviceStatus: "ready")
            try check(loadingState.cleanerModelLabel == "MBZUAI · 4B" && loadingState.cleanerModelPhase == .loading && loadingState.agentModelLabel == "MBZUAI · 1B" && loadingState.agentModelPhase == .ready, "les libellés et états suivent les modèles sélectionnés")
            loadingState.updateModelHealth(selectableHealth.selectedModels(cleaner: "1B", agent: "1B"), serviceStatus: "ready")
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .ready, "les deux rôles peuvent partager un modèle chargé")
            let absentHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Prêt"}"#.utf8))
            loadingState.updateModelHealth(absentHealth.selectedModels(cleaner: "1B", agent: "4B"), serviceStatus: "ready")
            try check(loadingState.cleanerModelPhase == .unavailable && loadingState.agentModelPhase == .unavailable, "un service sans sélection de modèles ne donne pas un faux état prêt")
            let inventoryHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Prêt","models":{"k2-cleaner":{"model_size":"1B","status":"ready"},"k2-agent":{"model_size":"4B","status":"unloaded"}}}"#.utf8))
            loadingState.updateModelInventory(inventoryHealth.models)
            try check(loadingState.modelPhase("1B") == .ready && loadingState.modelPhase("4B") == .unloaded, "états indépendants 1B chargé et 4B déchargé")
            loadingState.updateModelHealth(inventoryHealth.selectedModels(cleaner: "4B", agent: "4B"), serviceStatus: "ready")
            try check(loadingState.agentModelPhase == .unloaded && loadingState.modelPhase("1B") == .ready, "changer la sélection ne change pas la résidence des modèles")
            try check(loadingState.canChangeModel("1B") && loadingState.canChangeModel("4B"), "déchargement et rechargement individuels disponibles au repos")
            loadingState.modelsChanging = true
            try check(!loadingState.canChangeModel("1B") && !loadingState.canChangeModel("4B"), "opérations mémoire concurrentes interdites")
            loadingState.modelsChanging = false
            loadingState.modelsUnavailable()
            try check(loadingState.modelPhase("1B") == .unavailable, "une déconnexion efface les anciens états individuels")
            let partialHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"starting","message":"Chargement du 4B","models":{"k2-cleaner":{"display_name":"1B","status":"ready"},"k2-agent":{"display_name":"4B","status":"loading"}}}"#.utf8))
            loadingState.updateModelHealth(partialHealth.models, serviceStatus: partialHealth.status)
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .loading, "le nettoyage prêt et l’agent en chargement sont distingués")
            let failedHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"error","message":"Erreur 4B","models":{"k2-cleaner":{"display_name":"1B","status":"ready"},"k2-agent":{"display_name":"4B","status":"error","error":"Poids absents"}}}"#.utf8))
            loadingState.updateModelHealth(failedHealth.models, serviceStatus: failedHealth.status)
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .error && loadingState.agentModelError == "Poids absents", "une panne du 4B ne marque pas le 1B chargé comme défaillant")
            loadingState.modelsUnavailable()
            try check(loadingState.cleanerModelPhase == .unavailable && loadingState.agentModelPhase == .unavailable, "une perte de connexion ne conserve pas un faux état prêt")
            loadingState.resetModelsLoading()
            try check(loadingState.cleanerModelPhase == .waiting && loadingState.agentModelPhase == .waiting, "un redémarrage réinitialise les indicateurs des deux modèles")
            loadingState.updateModelHealth(nil, serviceStatus: "ready")
            try check(loadingState.cleanerModelPhase == .ready && loadingState.agentModelPhase == .ready, "les anciens services sans états détaillés restent compatibles")
            let failure = ServiceDiagnostics.exitMessage(code: 1, output: "Model profile: /tmp/profile.json\nProfil de modèles invalide : poids absents.\n")
            try check(failure.contains("Profil de modèles invalide : poids absents."), "l’erreur du superviseur est lisible, pas seulement son code de sortie")
            try check(ServiceDiagnostics.exitMessage(code: 2, output: "").contains("code 2"), "une sortie sans diagnostic conserve le code d’erreur")
            let longMessage = "Profil invalide " + String(repeating: "/long-path", count: 40) + " : index absent"
            let concise = ServiceDiagnostics.concise(longMessage)
            try check(concise.count <= 240 && concise.hasSuffix("index absent"), "un diagnostic long reste lisible et conserve sa cause finale")
            let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("horizon-launch-log-test-\(UUID().uuidString)", isDirectory: true)
            defer { try? FileManager.default.removeItem(at: temporary) }
            let logURL = temporary.appendingPathComponent("supervisor.log")
            let earlierLog = try ServiceLaunchLog(url: logURL)
            try earlierLog.handle.write(contentsOf: Data("Ancienne erreur\n".utf8)); earlierLog.close()
            let latestLog = try ServiceLaunchLog(url: logURL)
            let diagnosticProcess = Process()
            diagnosticProcess.executableURL = URL(fileURLWithPath: "/usr/bin/printf")
            diagnosticProcess.arguments = ["Nouveau lancement\nProfil invalide : tokenizer absent\n"]
            diagnosticProcess.standardOutput = latestLog.handle
            diagnosticProcess.standardError = latestLog.handle
            try diagnosticProcess.run(); diagnosticProcess.waitUntilExit(); latestLog.close()
            try check(diagnosticProcess.terminationStatus == 0, "la sortie d’un processus enfant est enregistrée sans canal de sortie interactif")
            let latestOutput = ServiceLaunchLog.tail(url: logURL, since: latestLog.startOffset)
            try check(latestOutput.contains("tokenizer absent") && !latestOutput.contains("Ancienne erreur"), "le journal ne réutilise pas l’erreur d’un lancement précédent")
            let wholeLog = try String(contentsOf: logURL, encoding: .utf8)
            try check(wholeLog.contains("Ancienne erreur"), "le journal des anciens lancements reste conservé")
            try check(ServiceLaunchLog.tail(url: logURL, since: latestLog.startOffset, maximumBytes: 16).utf8.count <= 16, "la lecture du diagnostic est bornée")
            try check(ServiceDiagnostics.restartInstruction == L("Corrigez le problème, puis quittez et relancez JAIS Voice."), "la récupération exige explicitement un redémarrage de l’application")
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
            try check(state.cleanerModelLabel == L("Correcteur local") && state.agentModelLabel == L("Agent local"), "libellés génériques avant réception du profil des modèles")
            let legacyHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Prêt"}"#.utf8))
            state.updateModelLabels(legacyHealth.models)
            try check(legacyHealth.models == nil && state.cleanerModelLabel == L("Correcteur local"), "compatibilité avec un état de service sans profil des modèles")
            let profileHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Prêt","models":{"k2-cleaner":{"display_name":"  MBZUAI · 1B  ","repo":"mblab/cleaner-1b","quantization":null},"k2-agent":{"display_name":"MBZUAI · 4B","repo":"mblab/agent-4b"}}}"#.utf8))
            state.updateModelLabels(profileHealth.models)
            try check(state.cleanerModelLabel == "MBZUAI · 1B" && state.agentModelLabel == "MBZUAI · 4B", "les libellés des deux modèles suivent le profil actif")
            try check(profileHealth.models?["k2-agent"]?.repo == "mblab/agent-4b", "le dépôt du profil se décode sans imposer son affichage")
            state.begin()
            try check(state.cleanerModelLabel == "MBZUAI · 1B" && state.agentModelLabel == "MBZUAI · 4B", "un nouveau tour conserve les libellés du profil actif")
            let unnamedHealth = try JSONDecoder().decode(ServiceHealth.self, from: Data(#"{"status":"ready","message":"Prêt","models":{"k2-cleaner":{"display_name":"  ","repo":"long/private/path/checkpoint"},"k2-agent":{"repo":"legacy/model"}}}"#.utf8))
            state.updateModelLabels(unnamedHealth.models)
            try check(state.cleanerModelLabel == L("Correcteur local") && state.agentModelLabel == L("Agent local"), "profils sans nom lisible : aucun ancien modèle ni chemin n’est affiché")
            state.stage = "refining"
            try check(state.stageLabel == L("Correction de la transcription…"), "le statut de correction ne présume pas la famille du modèle")
            state.stage = "acting"
            try check(state.stageLabel == L("Préparation de la réponse…"), "le statut de réponse ne présume pas la famille du modèle")
            let oldID = state.begin(); let id = state.begin()
            state.accept(WireEvent(type: "response_delta", turn_id: oldID, text: "ancienne réponse"))
            try check(state.response.isEmpty, "les paquets d’un ancien tour sont ignorés")
            state.accept(WireEvent(type: "response_delta", turn_id: id, text: "bon"))
            state.accept(WireEvent(type: "response_delta", turn_id: id, text: "jour"))
            try check(state.response == "bonjour", "les fragments de réponse sont assemblés")
            state.cancel(); state.accept(WireEvent(type: "complete", turn_id: id, text: "trop tard"))
            try check(state.stage == "cancelled" && state.response == "bonjour", "l’annulation ignore les réponses tardives")
            let toolTurn = state.begin()
            state.accept(WireEvent(type: "tool_start", turn_id: toolTurn, tool_id: "failed-tool", tool_name: "browser_navigate"))
            state.accept(WireEvent(type: "tool_end", turn_id: toolTurn, tool_id: "failed-tool", result: "Page inaccessible", error_code: "tool_error"))
            try check(state.actions.first?.errorCode == "tool_error" && state.actions.first?.finished == true, "un outil terminé en erreur reste identifié comme échec")
            let diff = TranscriptDiff.words(original: "إلى أحمد لا إلى سارة", cleaned: "إلى سارة")
            try check(diff.filter { !$0.removed }.map(\.text).joined(separator: " ") == "إلى سارة", "diff arabe et conservation de la correction")
            try check(diff.contains(DiffWord(text: "أحمد", removed: true)), "les mots supprimés sont identifiés")
            try check(TranscriptDiff.isRTL("ابحث في باريس") && !TranscriptDiff.isRTL("https://www.louvre.fr"), "sens de lecture arabe et URL")
            var clock = DismissalClock(); let start = Date(timeIntervalSince1970: 0)
            clock.reset(at: start)
            _ = clock.tick(at: start.addingTimeInterval(4), paused: false)
            _ = clock.tick(at: start.addingTimeInterval(24), paused: true)
            try check(clock.remaining == 6, "le survol suspend le délai restant")
            try check(clock.tick(at: start.addingTimeInterval(30), paused: false), "fermeture au bout de dix secondes actives")
            try check(!clock.tick(at: start.addingTimeInterval(31), paused: true), "un délai expiré ne ferme jamais une fenêtre en pause")
            let wav = WAVEncoder.encode(samples: [0, 1, -1])
            try check(wav.count == 50 && Array(wav[24..<28]) == [128, 62, 0, 0] && Array(wav[34..<36]) == [16, 0], "WAV mono PCM16 à 16 kHz")
            let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 48_000)!
            buffer.frameLength = 48_000
            for index in 0..<48_000 { buffer.floatChannelData![0][index] = index >= 43_200 ? 0.8 : 0 }
            let converted = try AudioRecorder.convertToWAV(buffer)
            let frames = (converted.count - 44) / 2
            try check(abs(frames - 16_000) <= 4, "conversion 48 → 16 kHz, durée préservée (\(frames) échantillons)")
            let tail = converted.suffix(800)
            let bytes = Array(tail)
            let peak = stride(from: 0, to: bytes.count - 1, by: 2).map { index in
                abs(Int(Int16(bitPattern: UInt16(bytes[index]) | UInt16(bytes[index + 1]) << 8)))
            }.max() ?? 0
            try check(peak > 20_000, "la fin de l’enregistrement reste présente après conversion")
            if let index = CommandLine.arguments.firstIndex(of: "--audio-fixture"), CommandLine.arguments.count > index + 1 {
                let url = URL(fileURLWithPath: CommandLine.arguments[index + 1])
                let file = try AVAudioFile(forReading: url)
                let sourceDuration = Double(file.length) / file.processingFormat.sampleRate
                let imported = try AudioRecorder.importWAV(from: url)
                let seconds = Double(imported.count - 44) / 32_000
                try check(abs(seconds - sourceDuration) < 0.01, "fichier ASR fourni, durée après conversion (\(seconds) s)")
            }
            print("\(passed) vérifications natives réussies.")
            return 0
        } catch {
            fputs("\(error.localizedDescription)\n", stderr)
            return 1
        }
    }
}
