import AppKit

/// A discoverable status item showing the Jais wordmark.
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
        // The wordmark is the whole item: Jais 3 has no graphic mark.
        button.font = JAISBrand.displayNS(13)
        button.image = nil
        button.imagePosition = .noImage
        button.appearsDisabled = false
        button.toolTip = L("Jais Voice · {0} to talk · Click for settings", String(describing: shortcutTitle))
        button.setAccessibilityLabel("Jais Voice")
        button.setAccessibilityHelp(L("Voice assistant. {0} to talk. Open the menu for settings.", String(describing: shortcutTitle)))
    }
}
