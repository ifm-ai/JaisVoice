import AppKit

MainActor.assumeIsolated {
    if CommandLine.arguments.contains("--self-test") {
        exit(SelfTests.run())
    }
    if CommandLine.arguments.contains("--desktop-helper") {
        DesktopHelper.runForever()
    }

    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    withExtendedLifetime(delegate) { app.run() }
}
