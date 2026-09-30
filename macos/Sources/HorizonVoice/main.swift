import AppKit

MainActor.assumeIsolated {
    if CommandLine.arguments.contains("--shell-self-test") {
        exit(ShellCommandSelfTests.run())
    }
    if CommandLine.arguments.contains("--dismissal-self-test") {
        exit(SelfTests.runDismissal())
    }
    if CommandLine.arguments.contains("--self-test") {
        exit(SelfTests.run())
    }

    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    withExtendedLifetime(delegate) { app.run() }
}
