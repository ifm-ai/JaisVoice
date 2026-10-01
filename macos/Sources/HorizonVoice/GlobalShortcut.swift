import AppKit
import Carbon

struct ShortcutChoice: Equatable {
    let label: String
    let modifiers: UInt32
    static var choices: [Self] { [
        ShortcutChoice(label: L("⌥ Space"), modifiers: UInt32(optionKey)),
        ShortcutChoice(label: L("⌃ ⌥ Space"), modifiers: UInt32(controlKey | optionKey)),
        ShortcutChoice(label: L("⌃ ⇧ Space"), modifiers: UInt32(controlKey | shiftKey)),
        ShortcutChoice(label: L("⌘ ⇧ Space"), modifiers: UInt32(cmdKey | shiftKey))
    ] }
}

@MainActor final class GlobalShortcut {
    private var hotKey: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private var isDown = false
    private var modifierTimer: Timer?
    var onDown: (() -> Void)?
    var onUp: (() -> Void)?
    private(set) var choiceIndex = 0

    func register(choice index: Int) throws {
        if hotKey != nil { unregister() }
        choiceIndex = max(0, min(index, ShortcutChoice.choices.count - 1))
        var types = [
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
            EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))
        ]
        let handlerStatus = InstallEventHandler(GetApplicationEventTarget(), { _, event, context in
            guard let context, let event else { return OSStatus(eventNotHandledErr) }
            let shortcut = Unmanaged<GlobalShortcut>.fromOpaque(context).takeUnretainedValue()
            let kind = GetEventKind(event)
            MainActor.assumeIsolated { shortcut.handle(kind: kind) }
            return noErr
        }, types.count, &types, Unmanaged.passUnretained(self).toOpaque(), &handler)
        guard handlerStatus == noErr else { throw VoiceError.message(L("The global shortcut could not be registered.")) }
        let id = EventHotKeyID(signature: OSType(0x48565F31), id: 1)
        let status = RegisterEventHotKey(UInt32(kVK_Space), ShortcutChoice.choices[choiceIndex].modifiers, id, GetApplicationEventTarget(), 0, &hotKey)
        guard status == noErr else {
            unregister()
            throw VoiceError.message(L("This shortcut is already in use. Choose another combination in the JAIS Voice menu."))
        }
        UserDefaults.standard.set(choiceIndex, forKey: "shortcutChoice")
    }

    private func handle(kind: UInt32) {
        if kind == kEventHotKeyPressed && !isDown {
            isDown = true; onDown?()
            // Carbon handles Space-up. Also end if a modifier is released first,
            // using the system modifier snapshot (no global key event tap).
            modifierTimer = Timer.scheduledTimer(withTimeInterval: 0.03, repeats: true) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.isDown else { return }
                    let required = ShortcutChoice.choices[self.choiceIndex].modifiers
                    if GetCurrentKeyModifiers() & required != required { self.handle(kind: UInt32(kEventHotKeyReleased)) }
                }
            }
        } else if kind == kEventHotKeyReleased && isDown {
            isDown = false; modifierTimer?.invalidate(); modifierTimer = nil; onUp?()
        }
    }

    func unregister() {
        modifierTimer?.invalidate(); modifierTimer = nil
        if isDown { isDown = false; onUp?() }
        if let key = hotKey { UnregisterEventHotKey(key) }
        if let handler { RemoveEventHandler(handler) }
        hotKey = nil; handler = nil
    }
}
