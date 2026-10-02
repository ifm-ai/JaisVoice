import AppKit

/// A published release of the app, as tagged by CI: `v<version>-build.<number>`.
struct AppRelease: Equatable {
    let version: String
    let build: Int
    /// The disk image when the release has one, else the release page.
    let downloadURL: URL

    var label: String { "\(version) (build \(build))" }

    /// Read a GitHub "latest release" response.
    static func parse(_ data: Data) -> AppRelease? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tag = object["tag_name"] as? String,
              let match = tag.wholeMatch(of: #/v(.+)-build\.(\d+)/#), let build = Int(match.2),
              let page = (object["html_url"] as? String).flatMap(URL.init(string:)) else { return nil }
        let assets = object["assets"] as? [[String: Any]] ?? []
        let disk = assets.compactMap { $0["browser_download_url"] as? String }.first { $0.hasSuffix(".dmg") }
        return AppRelease(version: String(match.1), build: build, downloadURL: disk.flatMap(URL.init(string:)) ?? page)
    }
}

/// Looks for a newer release at launch, then once a day, and on request.
///
/// Only CI release builds carry a feed (`JAISUpdateFeedURL` in Info.plist) and
/// a build number; local builds never check. The user downloads the disk image
/// and replaces the app: nothing is installed without them.
@MainActor final class UpdateChecker {
    static let feedURL = (Bundle.main.object(forInfoDictionaryKey: "JAISUpdateFeedURL") as? String).flatMap(URL.init(string:))
    static let current = AppRelease(
        version: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
        build: Int(Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "") ?? 0,
        downloadURL: URL(string: "https://github.com/MBZUAI-IFM/JaisVoice/releases")!)
    private static let dismissedKey = "dismissedUpdateBuild"

    /// A newer release found by the last check.
    private(set) var available: AppRelease?
    /// Called when an automatic check finds a newer release.
    var onAvailable: ((AppRelease) -> Void)?
    private var timer: Timer?

    var enabled: Bool { Self.feedURL != nil }

    func startAutomaticChecks() {
        guard enabled else { return }
        Task { await automaticCheck() }
        timer = Timer.scheduledTimer(withTimeInterval: 24 * 3600, repeats: true) { [weak self] _ in
            Task { @MainActor in await self?.automaticCheck() }
        }
    }

    /// Automatic checks stay silent on errors and do not repeat a release the user put off.
    private func automaticCheck() async {
        guard let release = try? await latestNewerRelease(),
              release.build != UserDefaults.standard.integer(forKey: Self.dismissedKey) else { return }
        onAvailable?(release)
    }

    /// The newer release, or nil when this app is up to date.
    func latestNewerRelease() async throws -> AppRelease? {
        guard let url = Self.feedURL else { throw VoiceError.message(L("Update checks are not available in this build.")) }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        let (data, response) = try await URLSession.shared.data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode
        // GitHub hides private repositories, even from members: the app does not sign in.
        if status == 404 { throw VoiceError.message(L("The releases are not public yet. Get new versions from the GitHub releases page.")) }
        guard status == 200, let release = AppRelease.parse(data) else {
            throw VoiceError.message(L("The release server did not answer as expected."))
        }
        available = release.build > Self.current.build ? release : nil
        return available
    }

    /// Offer the release; "Later" keeps automatic checks from showing it again.
    func present(_ release: AppRelease) {
        let alert = NSAlert()
        alert.messageText = L("A new version of JAIS Voice is available")
        alert.informativeText = L("Version {0} is available; you have version {1}.", release.label, Self.current.label) + "\n\n"
            + L("Download the disk image, quit JAIS Voice, then drag the new version onto Applications.")
        alert.addButton(withTitle: L("Download"))
        alert.addButton(withTitle: L("Later"))
        NSApplication.shared.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            NSWorkspace.shared.open(release.downloadURL)
        } else {
            UserDefaults.standard.set(release.build, forKey: Self.dismissedKey)
        }
    }
}
