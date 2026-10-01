import Foundation

/// One catalog entry and its state on this Mac, as listed by `scripts/models.py`.
struct CatalogModel: Decodable, Identifiable, Equatable {
    let id: String
    let kind: String
    let display_name: String
    var size: String? = nil
    let installed: Bool
    let downloadable: Bool
    var download_bytes: Int64? = nil
    var disk_bytes: Int64? = nil
    var partial_bytes: Int64? = nil

    var diskBytes: Int64 { disk_bytes ?? 0 }
    var partialBytes: Int64 { partial_bytes ?? 0 }

    var isASR: Bool { kind == "asr" }
    /// Key of this model in service health: its size, or "ASR".
    var memoryKey: String { isASR ? "ASR" : size ?? id }
}

struct DownloadProgress: Equatable {
    var phase = "download"
    var bytes: Int64 = 0
    var total: Int64 = 0
    var fraction: Double? { total > 0 ? min(1, Double(bytes) / Double(total)) : nil }
}

/// Download and delete catalog models with the project's model tool, as a child process.
@MainActor final class ModelStore: ObservableObject {
    @Published private(set) var models: [CatalogModel] = []
    @Published private(set) var downloads: [String: DownloadProgress] = [:]
    @Published private(set) var deleting: Set<String> = []
    @Published var error: String?
    @Published private(set) var loaded = false
    private var processes: [String: Process] = [:]
    /// Called after a model was installed or removed.
    var onChange: ((CatalogModel) -> Void)?

    var asr: CatalogModel? { models.first(where: \.isASR) }
    var languageModels: [CatalogModel] { models.filter { !$0.isASR } }
    var busy: Bool { !downloads.isEmpty || !deleting.isEmpty }

    /// Models the app needs to work and that can be fetched now.
    var missingDownloads: [CatalogModel] {
        var result: [CatalogModel] = []
        if let asr, !asr.installed, asr.downloadable { result.append(asr) }
        if !languageModels.contains(where: \.installed), let model = languageModels.last(where: \.downloadable) { result.append(model) }
        return result
    }

    /// Why the assistant cannot run yet, if a required model is missing.
    var blockingMessage: String? {
        guard loaded else { return nil }
        if let asr, !asr.installed { return L("Transcription model not installed: download it in Settings.") }
        if !languageModels.contains(where: \.installed) { return L("No language model installed: download one in Settings.") }
        return nil
    }

    private static func launcher() throws -> URL {
        if let explicit = ProcessInfo.processInfo.environment["HORIZON_MODELS_LAUNCHER"] { return URL(fileURLWithPath: explicit) }
        guard let resource = Bundle.main.url(forResource: "launch-models", withExtension: nil) else {
            throw VoiceError.message(L("The model management tool is missing. Open the JAIS Voice.app bundle created by the setup script."))
        }
        return resource
    }

    /// Run one command to completion and return its JSON records.
    private static func run(_ arguments: [String]) async throws -> [JSONValue] {
        let executable = try launcher()
        return try await Task.detached(priority: .userInitiated) {
            let task = Process(), output = Pipe()
            task.executableURL = executable
            task.arguments = arguments
            task.standardOutput = output
            task.standardError = FileHandle.nullDevice
            try task.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            task.waitUntilExit()
            var splitter = JSONLineSplitter()
            return splitter.append(data + Data([0x0A])).compactMap { try? JSONDecoder().decode(JSONValue.self, from: $0) }
        }.value
    }

    private static func failure(_ records: [JSONValue]) -> String? {
        records.first(where: { $0["event"]?.string == "error" })?["message"]?.string
    }

    func refresh() async {
        do {
            let records = try await Self.run(["list"])
            if let message = Self.failure(records) { throw VoiceError.message(message) }
            guard let list = records.first(where: { $0["event"]?.string == "catalog" })?["models"] else {
                throw VoiceError.message(L("The model catalog cannot be read."))
            }
            models = try JSONDecoder().decode([CatalogModel].self, from: JSONEncoder().encode(list))
            loaded = true
        } catch {
            self.error = error.localizedDescription
        }
    }

    func download(_ id: String) {
        guard processes[id] == nil, let executable = try? Self.launcher() else { return }
        error = nil
        let task = Process(), output = Pipe()
        task.executableURL = executable
        task.arguments = ["download", id]
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        let reader = ProgressReader()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            let records = reader.records(from: data)
            DispatchQueue.main.async { MainActor.assumeIsolated { records.forEach { self?.receive($0, id: id) } } }
        }
        task.terminationHandler = { [weak self] task in
            let status = task.terminationStatus
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self, self.processes[id] === task else { return }
                    self.processes[id] = nil
                    self.downloads[id] = nil
                    if status != 0 && status != 130 && self.error == nil {
                        self.error = L("The download stopped (code {0}).", String(describing: status))
                    }
                    Task { await self.finished(id) }
                }
            }
        }
        do {
            try task.run()
            processes[id] = task
            downloads[id] = DownloadProgress()
        } catch { self.error = error.localizedDescription }
    }

    private func receive(_ record: JSONValue, id: String) {
        switch record["event"]?.string {
        case "progress":
            var progress = downloads[id] ?? DownloadProgress()
            progress.phase = record["phase"]?.string ?? progress.phase
            if case .number(let bytes)? = record["bytes"] { progress.bytes = Int64(bytes) }
            if case .number(let total)? = record["total"] { progress.total = Int64(total) }
            if processes[id] != nil { downloads[id] = progress }
        case "error":
            if record["code"]?.string == "no_access", let repo = record["repo"]?.string, let folder = record["folder"]?.string {
                error = L("No access to {0} on Hugging Face. Request access on huggingface.co/{0} and sign in with `hf auth login`, or download the repository files yourself into {1}, then click Download again.", repo, folder)
            } else {
                error = record["message"].flatMap(\.string).map { L($0) } ?? L("The download failed.")
            }
        default: break
        }
    }

    private func finished(_ id: String) async {
        await refresh()
        if let model = models.first(where: { $0.id == id }), model.installed { onChange?(model) }
    }

    /// Stop a download; what was fetched is kept and the next download resumes it.
    func cancel(_ id: String) {
        processes[id]?.terminate()
    }

    func delete(_ id: String) async throws {
        deleting.insert(id)
        defer { deleting.remove(id) }
        let records = try await Self.run(["delete", id])
        if let message = Self.failure(records) { throw VoiceError.message(L(message)) }
        await refresh()
        if let model = models.first(where: { $0.id == id }) { onChange?(model) }
    }

    func cancelAll() {
        for process in processes.values where process.isRunning { process.terminate() }
    }

    /// Visual previews and self-tests only: no process is started.
    func show(models: [CatalogModel], downloads: [String: DownloadProgress] = [:]) {
        self.models = models; self.downloads = downloads; loaded = true
    }

    /// Sizes follow the interface language (6 GB / 6 Go), not the macOS locale.
    static func formatBytes(_ bytes: Int64) -> String {
        bytes.formatted(.byteCount(style: .file).locale(Locale(identifier: InterfaceLanguage.current.rawValue)))
    }
}

/// Accessed only from the pipe's serial readability handler.
private final class ProgressReader: @unchecked Sendable {
    private var splitter = JSONLineSplitter()
    func records(from data: Data) -> [JSONValue] {
        splitter.append(data).compactMap { try? JSONDecoder().decode(JSONValue.self, from: $0) }
    }
}
