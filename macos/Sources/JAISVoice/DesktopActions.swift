import AppKit
import ApplicationServices
import Carbon
import ScreenCaptureKit
import Vision

/// Key names and combinations, typed the way the current keyboard layout types them.
enum DesktopKeys {
    struct Combo: Equatable {
        let code: CGKeyCode
        let flags: CGEventFlags
        let label: String
    }

    static let named: [String: CGKeyCode] = [
        "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
        "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
        "left": 123, "right": 124, "down": 125, "up": 126,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    ]
    static let modifiers: [String: CGEventFlags] = [
        "cmd": .maskCommand, "command": .maskCommand, "⌘": .maskCommand, "shift": .maskShift, "⇧": .maskShift,
        "alt": .maskAlternate, "option": .maskAlternate, "opt": .maskAlternate, "⌥": .maskAlternate,
        "ctrl": .maskControl, "control": .maskControl, "⌃": .maskControl, "fn": .maskSecondaryFn,
    ]

    /// "cmd+shift+n", "return", "down down return": at most ten combinations.
    /// `lookup` gives a character's key code and whether it needs Shift.
    static func parse(_ text: String, lookup: (Character) -> (CGKeyCode, Bool)?) throws -> [Combo] {
        // A run of digits and symbols ("128*47=") is one key per character. Words are not:
        // typed as single letters, they would trigger an app's one-key shortcuts.
        let combos = text.split(whereSeparator: { $0 == " " || $0 == "," }).flatMap { token -> [String] in
            let word = String(token)
            let run = word.count > 1 && !word.contains("+") && !word.contains(where: \.isLetter)
            return run ? word.map(String.init) : [word]
        }
        guard !combos.isEmpty, combos.count <= 30 else { throw VoiceError.message("Give one to thirty keys, such as \"cmd+n\", \"return\" or \"down down return\".") }
        return try combos.map { combo in
            var flags: CGEventFlags = []
            var key: (CGKeyCode, Bool)?
            var keyName = ""
            // "+" is a key too: "+", "cmd++".
            var parts = combo.split(separator: "+").map { $0.lowercased() }
            if combo.hasSuffix("+") { parts.append("+") }
            for part in parts {
                if let modifier = modifiers[part] { flags.insert(modifier); continue }
                guard key == nil else { throw VoiceError.message("\"\(combo)\" has more than one key.") }
                keyName = part
                if let code = named[part] { key = (code, false) }
                else if part.count == 1, let character = part.first { key = lookup(character) }
                guard key != nil else { throw VoiceError.message("Unknown key \"\(part)\". Use a character or: " + named.keys.sorted().joined(separator: ", ") + ".") }
            }
            guard let (code, shift) = key else { throw VoiceError.message("\"\(combo)\" has no key besides modifiers.") }
            if shift { flags.insert(.maskShift) }
            return Combo(code: code, flags: flags, label: combo.lowercased() == keyName ? keyName : combo.lowercased())
        }
    }

    /// The key that types `character` on the current layout (AZERTY, QWERTY…).
    static func layoutKey(_ character: Character) -> (CGKeyCode, Bool)? {
        guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let property = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) else { return nil }
        let data = Unmanaged<CFData>.fromOpaque(property).takeUnretainedValue()
        guard let bytes = CFDataGetBytePtr(data) else { return nil }
        let layout = UnsafeRawPointer(bytes).assumingMemoryBound(to: UCKeyboardLayout.self)
        for shift in [false, true] {
            for code in 0..<128 {
                var deadKeys: UInt32 = 0, length = 0
                var characters = [UniChar](repeating: 0, count: 4)
                let state = UInt32(shift ? (shiftKey >> 8) & 0xFF : 0)
                let status = UCKeyTranslate(layout, UInt16(code), UInt16(kUCKeyActionDown), state, UInt32(LMGetKbdType()),
                                            OptionBits(kUCKeyTranslateNoDeadKeysBit), &deadKeys, 4, &length, &characters)
                if status == noErr, length == 1, String(utf16CodeUnits: characters, count: 1) == String(character) {
                    return (CGKeyCode(code), shift)
                }
            }
        }
        return nil
    }

    static func post(_ combo: Combo, to pid: pid_t) {
        let source = CGEventSource(stateID: .privateState)
        for down in [true, false] {
            guard let event = CGEvent(keyboardEventSource: source, virtualKey: combo.code, keyDown: down) else { continue }
            event.flags = combo.flags
            event.postToPid(pid)
            usleep(8_000)
        }
    }

    /// Types text as keyboard input to one app only, in short chunks.
    static func type(_ text: String, to pid: pid_t) {
        let source = CGEventSource(stateID: .privateState)
        let units = Array(text.utf16)
        var start = 0
        while start < units.count {
            var end = min(start + 16, units.count)
            if end < units.count, UTF16.isLeadSurrogate(units[end - 1]) { end -= 1 }
            var chunk = Array(units[start..<end])
            for down in [true, false] {
                guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else { continue }
                event.keyboardSetUnicodeString(stringLength: chunk.count, unicodeString: &chunk)
                event.postToPid(pid)
            }
            usleep(12_000)
            start = end
        }
    }
}

/// Clicks, typing, keys, menus and scrolling on the app a ref belongs to.
@MainActor enum DesktopActions {
    static func describe(_ info: AXInfo) -> String {
        let kind = AXTreeWalker.kind(info) ?? "element"
        let label = info.label.isEmpty ? (info.role == "AXStaticText" ? info.valueText ?? "" : "") : info.label
        return label.isEmpty ? kind : kind + " " + DesktopText.quoted(label, 60)
    }

    static func target(_ name: String, app: DesktopApp, refs: DesktopRefs) throws -> DesktopRef {
        guard let ref = refs[name] else {
            if name.range(of: #"^[ewo][0-9]+$"#, options: .regularExpression) == nil {
                throw VoiceError.message("\"\(name)\" is not a ref: refs look like e12 and come from desktop_snapshot. "
                    + "To choose a menu item, use desktop_menu with its path, for example \"File > New\".")
            }
            throw VoiceError.message("Unknown ref \"\(name)\". Take a new desktop_snapshot of \(app.name) and use one of its refs.")
        }
        guard ref.pid == app.pid else { throw VoiceError.message("Ref \(name) belongs to another app, not \(app.name).") }
        return ref
    }

    static func liveInfo(_ element: AXUIElement) throws -> AXInfo {
        let info = AXInfo(element)
        guard !info.role.isEmpty else { throw VoiceError.message("This element no longer exists. Take a new desktop_snapshot.") }
        return info
    }

    static func focusedApplicationPID() -> pid_t? {
        AXUIElementCreateSystemWide().element(kAXFocusedApplicationAttribute)?.pid
    }

    /// Bring the app and, when given, its window to the front; true once it is frontmost.
    @discardableResult static func bringToFront(_ pid: pid_t, window: AXUIElement?) -> Bool {
        let app = DesktopReader.application(pid)
        if let window {
            if window.bool(kAXMinimizedAttribute) == true { window.set(kAXMinimizedAttribute, kCFBooleanFalse) }
            window.set(kAXMainAttribute, kCFBooleanTrue)
            window.perform(kAXRaiseAction)
        }
        guard focusedApplicationPID() != pid else { return true }
        app.set(kAXFrontmostAttribute, kCFBooleanTrue)
        for _ in 0..<20 {
            if focusedApplicationPID() == pid {
                // An app that just came forward drops the first keys it receives.
                Thread.sleep(forTimeInterval: 0.3)
                return true
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        return focusedApplicationPID() == pid
    }

    /// The process whose window is at this point of the screen.
    static func owner(at point: CGPoint) -> pid_t? {
        var element: AXUIElement?
        guard AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &element) == .success else { return nil }
        return element?.pid
    }

    /// A real mouse click, only where the target app's own window is under the pointer.
    static func pointerClick(_ point: CGPoint, pid: pid_t, window: AXUIElement?, right: Bool, count: Int) throws {
        bringToFront(pid, window: window)
        Thread.sleep(forTimeInterval: 0.15)
        guard let owner = owner(at: point) else { throw VoiceError.message("Nothing is on screen at this place. Scroll it into view, then try again.") }
        guard owner == pid else {
            let name = DesktopApps.running(pid: owner)?.name ?? "another window"
            throw VoiceError.message("\(name) covers this place, so nothing was clicked.")
        }
        let (downType, upType): (CGEventType, CGEventType) = right ? (.rightMouseDown, .rightMouseUp) : (.leftMouseDown, .leftMouseUp)
        let button: CGMouseButton = right ? .right : .left
        CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
        usleep(30_000)
        for click in 1...max(1, count) {
            for type in [downType, upType] {
                guard let event = CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: point, mouseButton: button) else { continue }
                event.setIntegerValueField(.mouseEventClickState, value: Int64(click))
                event.post(tap: .cghidEventTap)
                usleep(20_000)
            }
        }
    }

    static func center(_ element: AXUIElement) throws -> CGPoint {
        guard let frame = element.frame, frame.width > 0, frame.height > 0 else {
            throw VoiceError.message("This element has no place on screen. Scroll it into view, then try again.")
        }
        return CGPoint(x: frame.midX, y: frame.midY)
    }

    static func click(app: DesktopApp, ref name: String, right: Bool, double: Bool, refs: DesktopRefs) throws -> String {
        let ref = try target(name, app: app, refs: refs)
        switch ref.target {
        case .point(let window, let offset, let label):
            guard let frame = window.frame else { throw VoiceError.message("That window is closed. Take a new desktop_snapshot.") }
            try pointerClick(CGPoint(x: frame.minX + offset.x, y: frame.minY + offset.y), pid: ref.pid, window: window, right: right, count: double ? 2 : 1)
            return (double ? "Double-clicked " : right ? "Right-clicked " : "Clicked ") + DesktopText.quoted(label, 60) + "."
        case .element(let element):
            let info = try liveInfo(element)
            let name = describe(info)
            guard info.enabled else { throw VoiceError.message("The \(name) is disabled right now.") }
            if ["AXWindow", "AXSheet"].contains(info.role) {
                bringToFront(ref.pid, window: info.role == "AXWindow" ? element : element.element(kAXWindowAttribute))
                return "Brought the \(name) to the front."
            }
            let actions = element.actionNames
            let window = element.element(kAXWindowAttribute)
            if right {
                if actions.contains("AXShowMenu"), element.perform("AXShowMenu") == .success { return "Opened the context menu of the \(name)." }
                try pointerClick(try center(element), pid: ref.pid, window: window, right: true, count: 1)
                return "Right-clicked the \(name)."
            }
            if double {
                if actions.contains("AXOpen"), element.perform("AXOpen") == .success { return "Opened the \(name)." }
                try pointerClick(try center(element), pid: ref.pid, window: window, right: false, count: 2)
                return "Double-clicked the \(name)."
            }
            for action in ["AXPress", "AXConfirm", "AXPick"] where actions.contains(action) {
                let result = element.perform(action)
                // A button that opens a modal dialog answers only once it closes.
                if result == .success || result == .cannotComplete { return "Clicked the \(name)." }
            }
            if ["AXRow", "AXCell"].contains(info.role), element.set(kAXSelectedAttribute, kCFBooleanTrue) == .success { return "Selected the \(name)." }
            if ["AXTextField", "AXTextArea", "AXComboBox"].contains(info.role), element.set(kAXFocusedAttribute, kCFBooleanTrue) == .success {
                return "Put the cursor in the \(name)."
            }
            // A click in the middle of an area would land on whatever control is there.
            guard !areaRoles.contains(info.role) else {
                throw VoiceError.message("The \(name) is an area, not a control: click one of the elements inside it.")
            }
            try pointerClick(try center(element), pid: ref.pid, window: window, right: false, count: 1)
            return "Clicked the \(name)."
        }
    }

    static let textRoles: Set<String> = ["AXTextField", "AXTextArea", "AXComboBox"]
    static let areaRoles: Set<String> = ["AXScrollArea", "AXSplitGroup", "AXWebArea", "AXTable", "AXOutline", "AXList", "AXToolbar",
                                         "AXTabGroup", "AXBrowser", "AXLayoutArea", "AXGroup", "AXApplication"]

    /// `mode` is "replace", "append" or nil; nil never replaces a document that already has text.
    static func type(app: DesktopApp, ref name: String, text: String, mode: String?, submit: Bool, refs: DesktopRefs) throws -> String {
        let append = mode == "append"
        guard !text.isEmpty || submit, text.utf8.count <= 4000, !text.contains("\0") else {
            throw VoiceError.message("Give the text to type (at most 4000 UTF-8 bytes).")
        }
        let ref = try target(name, app: app, refs: refs)
        guard case .element(let element) = ref.target else { throw VoiceError.message("Type into a text field ref from desktop_snapshot, not into text read from the screen.") }
        let info = try liveInfo(element)
        let fieldName = describe(info)
        guard !info.isSecure else { throw VoiceError.message("The assistant never fills password fields. Ask the user to type it.") }
        let pid = ref.pid
        guard textRoles.contains(info.role) || element.isSettable(kAXValueAttribute) || element.isSettable(kAXSelectedTextAttribute) else {
            throw VoiceError.message("The \(fieldName) is not a text field. Pick a text field from the snapshot; "
                + "for an app without one (a calculator), press keys with desktop_press_key, for example \"128*47=\".")
        }
        guard info.enabled else { throw VoiceError.message("The \(fieldName) is disabled right now.") }
        if mode == nil, info.role == "AXTextArea", !text.isEmpty, !(info.valueText ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw VoiceError.message("The \(fieldName) already contains text: " + DesktopText.quoted(info.valueText ?? "", 160)
                + ". Nothing was typed. Set mode to \"append\" to add to it, or to \"replace\" only if the user asked to replace it; "
                + "for a new document, use desktop_menu (for example \"File > New\").")
        }
        let frontmost = bringToFront(pid, window: element.element(kAXWindowAttribute))
        element.set(kAXFocusedAttribute, kCFBooleanTrue)
        Thread.sleep(forTimeInterval: 0.12)
        let focused = DesktopReader.application(pid).element(kAXFocusedUIElementAttribute).map { CFEqual($0, element) } ?? false
        let before = info.valueText ?? ""
        let length = (before as NSString).length
        let squeeze = { (value: String) in value.filter { !$0.isWhitespace } }
        var message = ""
        if !text.isEmpty && !(!append && before == text) {
            /// Select what the typing replaces, or move to the end, then type for real.
            func keyboard() {
                var range = CFRange(location: append ? length : 0, length: append ? 0 : length)
                if let value = AXValueCreate(.cfRange, &range), element.set(kAXSelectedTextRangeAttribute, value) != .success, !append,
                   let selectAll = try? DesktopKeys.parse("cmd+a", lookup: DesktopKeys.layoutKey) {
                    selectAll.forEach { DesktopKeys.post($0, to: pid) }
                }
                DesktopKeys.type(text, to: pid)
            }
            /// The same through accessibility, for an app that cannot come to the front.
            func accessibility() {
                if append {
                    var range = CFRange(location: length, length: 0)
                    if let value = AXValueCreate(.cfRange, &range), element.set(kAXSelectedTextRangeAttribute, value) == .success {
                        element.set(kAXSelectedTextAttribute, text as CFString)
                    }
                } else {
                    element.set(kAXValueAttribute, text as CFString)
                }
            }
            // Some apps ignore one of the two ways without saying so: check, then try the other.
            let attempts: [() -> Void] = frontmost && focused ? [keyboard, accessibility] : [accessibility, keyboard]
            var changed = false
            for attempt in attempts where !changed {
                attempt()
                Thread.sleep(forTimeInterval: 0.2)
                changed = (AXInfo(element).valueText ?? "") != before
            }
            guard changed else {
                throw VoiceError.message("\(app.name) did not accept the text: the \(fieldName) did not change, so nothing was typed. Do not claim it was.")
            }
            let now = AXInfo(element).valueText ?? ""
            message = squeeze(now).contains(squeeze(text)) ? "Typed into the \(fieldName)."
                : "Warning: the text did not arrive exactly as given in the \(fieldName). Do not say it is done: fix it, or tell the user to check it."
            message += " It now contains: " + DesktopText.quoted(now, 300) + "."
        } else if !text.isEmpty {
            message = "The \(fieldName) already contains this text."
        }
        if submit {
            try DesktopKeys.parse("return", lookup: DesktopKeys.layoutKey).forEach { DesktopKeys.post($0, to: pid) }
            message += " Pressed Return."
        }
        return message.trimmingCharacters(in: .whitespaces)
    }

    static func pressKeys(app: DesktopApp, keys: String) throws -> String {
        guard let pid = app.pid else { throw VoiceError.message("\(app.name) is not running.") }
        let combos = try DesktopKeys.parse(keys, lookup: DesktopKeys.layoutKey)
        let window = DesktopReader.application(pid).element(kAXFocusedWindowAttribute)
        // Keys go to the app's process even when it cannot come to the front.
        bringToFront(pid, window: window)
        for combo in combos {
            DesktopKeys.post(combo, to: pid)
            usleep(60_000)
        }
        return "Pressed " + combos.map(\.label).joined(separator: " ") + " in \(app.name)."

    }

    // MARK: Menus

    static func menuTitle(_ item: AXUIElement) -> String { item.string(kAXTitleAttribute) ?? "" }

    static func menuKey(_ title: String) -> String {
        var key = DesktopApps.normalized(title)
        while key.hasSuffix("…") || key.hasSuffix(".") { key.removeLast() }
        return key.trimmingCharacters(in: .whitespaces)
    }

    /// Exact title first, then a unique prefix, then a unique substring.
    static func match(_ wanted: String, in titles: [String]) -> Int? {
        let key = menuKey(wanted)
        let keys = titles.map(menuKey)
        if let exact = keys.firstIndex(of: key) { return exact }
        for test in [{ (candidate: String) in candidate.hasPrefix(key) }, { (candidate: String) in candidate.contains(key) }] {
            let matches = keys.indices.filter { !keys[$0].isEmpty && test(keys[$0]) }
            if matches.count == 1 { return matches[0] }
        }
        return nil
    }

    static func submenu(_ item: AXUIElement) -> AXUIElement? {
        item.elements(kAXChildrenAttribute, limit: 4).first { AXInfo($0).role == "AXMenu" }
    }

    static func shortcut(_ item: AXUIElement) -> String {
        guard let key = item.string(kAXMenuItemCmdCharAttribute), !key.isEmpty else { return "" }
        let modifiers = (item.value(kAXMenuItemCmdModifiersAttribute) as? NSNumber)?.intValue ?? 0
        // kAXMenuItemModifier: 1 shift, 2 option, 4 control, 8 no command.
        var text = ""
        if modifiers & 4 != 0 { text += "⌃" }
        if modifiers & 2 != 0 { text += "⌥" }
        if modifiers & 1 != 0 { text += "⇧" }
        if modifiers & 8 == 0 { text += "⌘" }
        return text + key
    }

    static func listing(_ items: [AXUIElement], trail: [String]) -> String {
        let lines = items.compactMap { item -> String? in
            let title = menuTitle(item)
            guard !title.isEmpty else { return nil }
            var line = "- " + DesktopText.quoted(title, 80)
            if submenu(item) != nil { line += " ▸" }
            let key = shortcut(item)
            if !key.isEmpty { line += " (\(key))" }
            if item.bool(kAXEnabledAttribute) == false { line += " [disabled]" }
            if let mark = item.string(kAXMenuItemMarkCharAttribute), !mark.isEmpty { line += " [checked]" }
            return line
        }
        let place = trail.isEmpty ? "Menus" : "Menu " + trail.joined(separator: " > ")
        let example = (trail + [items.lazy.map(menuTitle).first(where: { !$0.isEmpty }) ?? "…"]).joined(separator: " > ")
        return place + ":\n" + lines.joined(separator: "\n") + "\nChoose an item with desktop_menu and its full path, for example \"\(example)\"."
    }

    /// Lists a menu, or chooses its item: "File", "File > New Note", "Format > Font > Bold".
    static func menu(app: DesktopApp, path: String) throws -> (text: String, chose: Bool) {
        guard let pid = app.pid else { throw VoiceError.message("\(app.name) is not running.") }
        guard let bar = DesktopReader.application(pid).element(kAXMenuBarAttribute) else { throw VoiceError.message("\(app.name) has no menu bar.") }
        // The first menu is Apple's (restart, shut down, force quit): never the app's to use.
        var items = Array(bar.elements(kAXChildrenAttribute, limit: 30).dropFirst())
        let parts = path.split(whereSeparator: { $0 == ">" || $0 == "›" || $0 == "→" }).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        var trail: [String] = []
        for (index, part) in parts.enumerated() {
            let titles = items.map(menuTitle)
            guard let position = match(part, in: titles) else {
                throw VoiceError.message("No menu item \"\(part)\" in " + (trail.isEmpty ? "the menu bar of \(app.name)" : trail.joined(separator: " > "))
                    + ". Available: " + titles.filter { !$0.isEmpty }.prefix(40).map { DesktopText.quoted($0, 50) }.joined(separator: ", ") + ".")
            }
            let item = items[position]
            trail.append(titles[position])
            let children = submenu(item)
            if index == parts.count - 1 {
                if let children { return (listing(children.elements(kAXChildrenAttribute, limit: 80), trail: trail), false) }
                guard item.bool(kAXEnabledAttribute) != false else { throw VoiceError.message("\"\(trail.joined(separator: " > "))\" is disabled right now.") }
                let result = item.perform(kAXPressAction)
                guard result == .success || result == .cannotComplete else {
                    throw VoiceError.message("\(app.name) did not perform \"\(trail.joined(separator: " > "))\".")
                }
                return ("Chose " + trail.joined(separator: " > ") + " in \(app.name).", true)
            }
            guard let children else { throw VoiceError.message("\"\(titles[position])\" has no submenu.") }
            items = children.elements(kAXChildrenAttribute, limit: 80)
        }
        return (listing(items, trail: trail), false)
    }

    // MARK: Scrolling

    static func scrollArea(near element: AXUIElement) -> AXUIElement? {
        var current: AXUIElement? = element
        for _ in 0..<20 {
            guard let node = current else { return nil }
            if AXInfo(node).role == "AXScrollArea" { return node }
            current = node.element(kAXParentAttribute)
        }
        return nil
    }

    /// The largest scroll area of a window: the content, not a sidebar.
    static func largestScrollArea(in root: AXUIElement) -> AXUIElement? {
        var best: (AXUIElement, CGFloat)?
        var queue: [(AXUIElement, Int)] = [(root, 0)], visited = 0
        while !queue.isEmpty, visited < 400 {
            let (node, depth) = queue.removeFirst(); visited += 1
            let info = AXInfo(node)
            if info.role == "AXScrollArea", let frame = info.frame, frame.width * frame.height > (best?.1 ?? 0) { best = (node, frame.width * frame.height) }
            if depth < 7 { queue += node.elements(kAXChildrenAttribute, limit: 40).map { ($0, depth + 1) } }
        }
        return best?.0
    }

    static func scroll(app: DesktopApp, ref name: String?, direction: String, pages: Double, refs: DesktopRefs) throws -> String {
        guard let pid = app.pid else { throw VoiceError.message("\(app.name) is not running.") }
        let vertical: Bool, sign: Double
        switch direction.lowercased() {
        case "down": (vertical, sign) = (true, 1)
        case "up": (vertical, sign) = (true, -1)
        case "right": (vertical, sign) = (false, 1)
        case "left": (vertical, sign) = (false, -1)
        default: throw VoiceError.message("Direction must be up, down, left or right.")
        }
        let amount = min(10, max(0.25, pages))
        var area: AXUIElement?
        if let name, !name.isEmpty {
            guard case .element(let element) = try target(name, app: app, refs: refs).target else { throw VoiceError.message("Scroll with an element ref.") }
            area = scrollArea(near: element)
        } else {
            area = largestScrollArea(in: try DesktopReader.window(DesktopReader.application(pid), pid: pid, query: nil, refs: refs))
        }
        guard let area else { throw VoiceError.message("Nothing scrolls there.") }
        if let bar = area.element(vertical ? kAXVerticalScrollBarAttribute : kAXHorizontalScrollBarAttribute),
           let current = (bar.value(kAXValueAttribute) as? NSNumber)?.doubleValue, bar.isSettable(kAXValueAttribute),
           let visible = area.frame, let content = area.elements(kAXContentsAttribute, limit: 1).first?.frame ?? area.elements(kAXChildrenAttribute, limit: 1).first?.frame {
            let shown = vertical ? visible.height : visible.width, total = vertical ? content.height : content.width
            guard total > shown + 1 else { return "This area shows everything already; there is nothing to scroll." }
            let target = min(1, max(0, current + sign * amount * 0.9 * shown / (total - shown)))
            bar.set(kAXValueAttribute, target as CFNumber)
            return "Scrolled \(direction) (now at \(Int((target * 100).rounded())) %)."
        }
        let point = try center(area)
        let lines = Int32(sign * amount * 12)
        guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .line, wheelCount: 2, wheel1: vertical ? -lines : 0, wheel2: vertical ? 0 : -lines, wheel3: 0) else {
            throw VoiceError.message("Scrolling failed.")
        }
        event.location = point
        event.postToPid(pid)
        return "Scrolled \(direction)."
    }

    // MARK: Opening apps

    static func open(app: DesktopApp, windowQuery: String?, refs: DesktopRefs) async throws -> DesktopApp {
        var pid = app.pid
        // Like `open -a`: it relaunches an app macOS terminated while idle, and asks a
        // running app without windows to reopen one.
        if let url = app.url {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            let opened = try await NSWorkspace.shared.openApplication(at: url, configuration: configuration).processIdentifier
            // LaunchServices may answer -1 for an app it thought idle-terminated (TextEdit).
            pid = opened > 0 ? opened : DesktopApps.running().first(where: { $0.bundleID != nil && $0.bundleID == app.bundleID })?.pid ?? app.pid
        }
        guard let pid, pid > 0 else { throw VoiceError.message("\(app.name) did not start.") }
        let element = DesktopReader.application(pid)
        for _ in 0..<40 where DesktopReader.windows(element).isEmpty {
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        let window = DesktopReader.windows(element).isEmpty ? nil : try DesktopReader.window(element, pid: pid, query: windowQuery, refs: refs)
        bringToFront(pid, window: window)
        return DesktopApps.running(pid: pid) ?? DesktopApp(name: app.name, bundleID: app.bundleID, pid: pid, url: app.url, regular: app.regular)
    }

    // MARK: Text from pixels

    /// The window's visible text, read by Vision from a capture of the window alone.
    static func recognizeText(app: DesktopApp, window: AXUIElement, refs: DesktopRefs) async throws -> String {
        guard let pid = app.pid, let frame = window.frame else { throw VoiceError.message("This window is not on screen.") }
        guard CGPreflightScreenCaptureAccess() else {
            throw VoiceError.message("Reading text from pixels needs the Screen Recording permission for JAIS Voice (Settings > Assistant).")
        }
        let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
        let candidates = content.windows.filter { $0.owningApplication?.processID == pid && $0.windowLayer == 0 && $0.frame.width > 20 }
        func distance(_ other: CGRect) -> CGFloat { abs(other.minX - frame.minX) + abs(other.minY - frame.minY) + abs(other.width - frame.width) + abs(other.height - frame.height) }
        guard let captured = candidates.min(by: { distance($0.frame) < distance($1.frame) }) else { throw VoiceError.message("The window could not be captured.") }
        let scale = NSScreen.screens.map(\.backingScaleFactor).max() ?? 2
        let configuration = SCStreamConfiguration()
        configuration.width = Int(captured.frame.width * scale); configuration.height = Int(captured.frame.height * scale)
        configuration.showsCursor = false
        configuration.ignoreShadowsSingleWindow = true
        let image = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(desktopIndependentWindow: captured), configuration: configuration)
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.automaticallyDetectsLanguage = true
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        let observations = (request.results ?? []).compactMap { observation -> (String, CGRect)? in
            guard let text = observation.topCandidates(1).first?.string, !text.trimmingCharacters(in: .whitespaces).isEmpty else { return nil }
            return (text, observation.boundingBox)
        }.sorted { lhs, rhs in
            abs(lhs.1.midY - rhs.1.midY) > 0.01 ? lhs.1.midY > rhs.1.midY : lhs.1.minX < rhs.1.minX
        }
        guard !observations.isEmpty else { return "No text is visible in this window." }
        let lines = observations.prefix(150).map { text, box -> String in
            // Vision boxes are normalized with a bottom-left origin; refs keep a window offset.
            let offset = CGPoint(x: captured.frame.minX - frame.minX + box.midX * captured.frame.width,
                                 y: captured.frame.minY - frame.minY + (1 - box.midY) * captured.frame.height)
            return "- text " + DesktopText.quoted(text, 160) + " [ref=\(refs.name(forPoint: offset, in: window, pid: pid, label: text))]"
        }
        return "Text read from the screen (clickable refs):\n" + lines.joined(separator: "\n")
            + (observations.count > 150 ? "\n… \(observations.count - 150) more lines." : "")
    }
}
