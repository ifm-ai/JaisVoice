import AppKit

/// A discoverable status item, including a text fallback if SF Symbols fail.
/// `isVisible` means registered with macOS, not necessarily unobscured by other
/// menu items. The configuration window remains a separate way into the app.
@MainActor enum StatusItemPresentation {
    static let title = "Jais"
    static let autosaveName = "JAISVoice.MainStatusItem"

    static func makeItem(shortcutTitle: String) -> NSStatusItem {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.autosaveName = autosaveName
        // Do not inherit a saved hidden state at the next explicit app launch.
        // This changes only this application's own item, never global settings.
        item.isVisible = true
        update(item, shortcutTitle: shortcutTitle)
        return item
    }

    static func update(_ item: NSStatusItem, shortcutTitle: String) {
        guard let button = item.button else { return }
        button.title = title
        button.font = .systemFont(ofSize: 12, weight: .semibold)
        button.image = makeIcon()
        button.imagePosition = button.image == nil ? .noImage : .imageLeading
        button.appearsDisabled = false
        button.toolTip = L("Jais Voice · {0} to talk · Click for settings", String(describing: shortcutTitle))
        button.setAccessibilityLabel("Jais Voice")
        button.setAccessibilityHelp(L("Voice assistant. {0} to talk. Open the menu for settings.", String(describing: shortcutTitle)))
    }

    static func makeIcon() -> NSImage? {
        if let image = JAISBrand.image("JAISMark") {
            image.size = NSSize(width: 18, height: 18 * image.size.height / image.size.width)
            // Template rendering follows macOS menu-bar contrast automatically.
            image.isTemplate = true
            return image
        }
        guard let symbol = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: nil) else {
            return nil // The always-present Jais title still identifies the app.
        }
        let image = symbol.withSymbolConfiguration(.init(pointSize: 16, weight: .medium)) ?? symbol
        image.isTemplate = true
        return image
    }

}
