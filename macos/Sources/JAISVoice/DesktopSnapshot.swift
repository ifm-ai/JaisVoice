import AppKit
import ApplicationServices

/// Failure-tolerant reads and writes on one accessibility element.
extension AXUIElement {
    func value(_ attribute: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(self, attribute as CFString, &value) == .success ? value : nil
    }
    func string(_ attribute: String) -> String? { AXInfo.text(value(attribute)) }
    func bool(_ attribute: String) -> Bool? { value(attribute) as? Bool }
    func element(_ attribute: String) -> AXUIElement? {
        guard let value = value(attribute), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
        return (value as! AXUIElement)
    }
    /// At most `limit` elements of an array attribute, without copying the rest.
    func elements(_ attribute: String, limit: Int = 500) -> [AXUIElement] {
        var values: CFArray?
        guard AXUIElementCopyAttributeValues(self, attribute as CFString, 0, limit, &values) == .success,
              let array = values as? [AnyObject] else { return [] }
        return array.compactMap { CFGetTypeID($0) == AXUIElementGetTypeID() ? ($0 as! AXUIElement) : nil }
    }
    func count(_ attribute: String) -> Int {
        var count: CFIndex = 0
        return AXUIElementGetAttributeValueCount(self, attribute as CFString, &count) == .success ? count : 0
    }
    var pid: pid_t? {
        var pid: pid_t = 0
        return AXUIElementGetPid(self, &pid) == .success ? pid : nil
    }
    var actionNames: [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(self, &names) == .success else { return [] }
        return names as? [String] ?? []
    }
    @discardableResult func perform(_ action: String) -> AXError { AXUIElementPerformAction(self, action as CFString) }
    @discardableResult func set(_ attribute: String, _ value: CFTypeRef) -> AXError { AXUIElementSetAttributeValue(self, attribute as CFString, value) }
    func isSettable(_ attribute: String) -> Bool {
        var settable: DarwinBoolean = false
        return AXUIElementIsAttributeSettable(self, attribute as CFString, &settable) == .success && settable.boolValue
    }
    var frame: CGRect? {
        guard let position = value(kAXPositionAttribute), let size = value(kAXSizeAttribute) else { return nil }
        var point = CGPoint.zero, extent = CGSize.zero
        guard AXValueGetValue(position as! AXValue, .cgPoint, &point), AXValueGetValue(size as! AXValue, .cgSize, &extent) else { return nil }
        return CGRect(origin: point, size: extent)
    }
}

/// Accessibility elements compare by identity with CFEqual, not by pointer.
struct AXKey: Hashable {
    let element: AXUIElement
    static func == (lhs: AXKey, rhs: AXKey) -> Bool { CFEqual(lhs.element, rhs.element) }
    func hash(into hasher: inout Hasher) { hasher.combine(CFHash(element)) }
}

/// What a ref points at: an element, or a point in a window found by OCR.
enum DesktopTarget {
    case element(AXUIElement)
    case point(window: AXUIElement, offset: CGPoint, label: String)
}

struct DesktopRef {
    let pid: pid_t
    let target: DesktopTarget
}

/// Refs stay valid and never change meaning during a request: the same element
/// keeps its ref across snapshots, and a ref name is never reused.
@MainActor final class DesktopRefs {
    private var byName: [String: DesktopRef] = [:]
    private var byElement: [AXKey: String] = [:]
    private var counter = 0

    subscript(name: String) -> DesktopRef? { byName[name.trimmingCharacters(in: .whitespaces).lowercased()] }

    func name(for element: AXUIElement, pid: pid_t, prefix: String = "e") -> String {
        let key = AXKey(element: element)
        if let name = byElement[key], name.hasPrefix(prefix) { return name }
        counter += 1
        let name = prefix + String(counter)
        byName[name] = DesktopRef(pid: pid, target: .element(element)); byElement[key] = name
        return name
    }

    func name(forPoint offset: CGPoint, in window: AXUIElement, pid: pid_t, label: String) -> String {
        counter += 1
        let name = "o" + String(counter)
        byName[name] = DesktopRef(pid: pid, target: .point(window: window, offset: offset, label: label))
        return name
    }

    func reset() { byName = [:]; byElement = [:]; counter = 0 }
}

/// The attributes of one element, read in a single round trip.
struct AXInfo {
    var role = "", subrole = "", title = "", description = "", placeholder = ""
    var value: AnyObject?
    var enabled = true, focused = false, selected = false
    var expanded: Bool?
    var frame: CGRect?

    static let names = [kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute,
                        "AXPlaceholderValue", kAXEnabledAttribute, kAXFocusedAttribute, kAXSelectedAttribute, kAXExpandedAttribute,
                        kAXPositionAttribute, kAXSizeAttribute]

    init(_ element: AXUIElement) {
        var raw: CFArray?
        guard AXUIElementCopyMultipleAttributeValues(element, Self.names as CFArray, AXCopyMultipleAttributeOptions(rawValue: 0), &raw) == .success,
              let values = raw as? [AnyObject], values.count == Self.names.count else { return }
        func get(_ index: Int) -> AnyObject? {
            let value = values[index]
            if CFGetTypeID(value) == AXValueGetTypeID(), AXValueGetType(value as! AXValue) == .axError { return nil }
            return value
        }
        role = get(0) as? String ?? ""
        subrole = get(1) as? String ?? ""
        title = Self.text(get(2)) ?? ""
        description = Self.text(get(3)) ?? ""
        value = get(4)
        placeholder = Self.text(get(5)) ?? ""
        enabled = get(6) as? Bool ?? true
        focused = get(7) as? Bool ?? false
        selected = get(8) as? Bool ?? false
        expanded = get(9) as? Bool
        if let position = get(10), let size = get(11), CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() {
            var point = CGPoint.zero, extent = CGSize.zero
            if AXValueGetValue(position as! AXValue, .cgPoint, &point), AXValueGetValue(size as! AXValue, .cgSize, &extent) {
                frame = CGRect(origin: point, size: extent)
            }
        }
    }

    static func text(_ value: AnyObject?) -> String? {
        switch value {
        case let string as String: return string
        case let attributed as NSAttributedString: return attributed.string
        case let number as NSNumber where CFGetTypeID(number) != CFBooleanGetTypeID(): return number.stringValue
        default: return nil
        }
    }

    var valueText: String? { Self.text(value) }
    var isSecure: Bool { subrole == "AXSecureTextField" }
    /// The element's own name; static text carries its words in its value.
    var label: String {
        for candidate in [title, description] where !candidate.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return candidate }
        return ""
    }
}

enum DesktopText {
    /// One line, at most `limit` characters, quotes escaped.
    static func clip(_ text: String, _ limit: Int) -> String {
        var line = text.replacingOccurrences(of: "\r\n", with: " ").replacingOccurrences(of: "\n", with: " ").replacingOccurrences(of: "\t", with: " ")
        while line.contains("  ") { line = line.replacingOccurrences(of: "  ", with: " ") }
        line = line.trimmingCharacters(in: .whitespaces)
        if line.count > limit { line = String(line.prefix(limit)) + "…" }
        return line
    }
    static func quoted(_ text: String, _ limit: Int = 120) -> String {
        "\"" + clip(text, limit).replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}

/// One rendered element: a YAML-like line, as Playwright shows web pages.
struct SnapshotItem {
    var depth: Int
    var line: String
    /// Normalized words of the element, for desktop_find.
    var words: String
    var ref: String?
    /// The nearest ancestor with a ref, for text found by desktop_find.
    var owner: String?
}

/// Walks one window's accessibility tree into compact lines with refs.
@MainActor final class AXTreeWalker {
    static let skippedRoles: Set<String> = ["AXScrollBar", "AXSplitter", "AXValueIndicator", "AXGrowArea", "AXRuler", "AXRulerMarker",
                                            "AXBusyIndicator", "AXMenuBar", "AXHelpTag", "AXColumn", "AXHandle"]
    static let skippedSubroles: Set<String> = ["AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton"]
    static let transparentRoles: Set<String> = ["AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXLayoutItem", "AXCell", "AXUnknown", "AXMatte"]
    /// Shown only when labeled; otherwise their children are shown in their place.
    static let transparentWhenUnlabeled: Set<String> = ["AXGroup", "AXList", "AXRadioGroup"]
    static let actionableRoles: Set<String> = ["AXButton", "AXMenuButton", "AXPopUpButton", "AXCheckBox", "AXRadioButton", "AXLink", "AXMenuItem",
                                               "AXTextField", "AXTextArea", "AXComboBox", "AXSlider", "AXIncrementor", "AXRow",
                                               "AXDisclosureTriangle", "AXColorWell", "AXDateField", "AXTimeField"]
    /// Roles that act only when their app says so (custom controls, Finder icons).
    static let probedRoles: Set<String> = ["AXGroup", "AXImage", "AXHeading"]
    static let pressActions: Set<String> = ["AXPress", "AXConfirm", "AXPick", "AXOpen", "AXShowMenu", "AXIncrement"]
    /// Their own words are their content: their children are not listed again.
    static let leafRoles: Set<String> = ["AXButton", "AXLink", "AXHeading", "AXMenuItem", "AXRadioButton", "AXCheckBox", "AXDisclosureTriangle", "AXImage"]
    static let maxChildren = 60

    let pid: pid_t
    let refs: DesktopRefs
    let maxNodes: Int
    let maxChars: Int?
    let deadline: Date
    private(set) var items: [SnapshotItem] = []
    private(set) var truncated = false
    private var visited = 0
    private var chars = 0
    /// Some trees loop back on themselves (a locked screen returns the app as its own child).
    private var seen: Set<AXKey> = []

    init(pid: pid_t, refs: DesktopRefs, maxNodes: Int, maxChars: Int?, seconds: TimeInterval) {
        self.pid = pid; self.refs = refs; self.maxNodes = maxNodes; self.maxChars = maxChars
        deadline = Date().addingTimeInterval(seconds)
    }

    static func kind(_ info: AXInfo) -> String? {
        switch info.role {
        case "AXWindow": return ["AXDialog", "AXSystemDialog", "AXFloatingWindow"].contains(info.subrole) ? "dialog" : "window"
        case "AXSheet": return "sheet"
        case "AXButton": return "button"
        case "AXMenuButton": return "menu button"
        case "AXPopUpButton": return "pop-up button"
        case "AXCheckBox": return info.subrole == "AXSwitch" ? "switch" : (info.subrole == "AXToggle" ? "toggle" : "checkbox")
        case "AXRadioButton": return info.subrole == "AXTabButton" ? "tab" : "radio button"
        case "AXTextField":
            switch info.subrole {
            case "AXSearchField": return "search field"
            case "AXSecureTextField": return "password field"
            default: return "text field"
            }
        case "AXTextArea": return "text area"
        case "AXComboBox": return "combo box"
        case "AXStaticText": return "text"
        case "AXLink": return "link"
        case "AXImage": return "image"
        case "AXHeading": return "heading"
        case "AXList": return "list"
        case "AXTable": return "table"
        case "AXOutline": return "outline"
        case "AXBrowser": return "column browser"
        case "AXRow": return "row"
        case "AXTabGroup": return "tabs"
        case "AXToolbar": return "toolbar"
        case "AXSlider": return "slider"
        case "AXIncrementor": return "stepper"
        case "AXProgressIndicator": return "progress"
        case "AXLevelIndicator": return "level"
        case "AXWebArea": return "web page"
        case "AXMenu": return "menu"
        case "AXMenuItem": return "menu item"
        case "AXDisclosureTriangle": return "disclosure"
        case "AXColorWell": return "color well"
        case "AXDateField", "AXTimeField": return "date field"
        case "AXRadioGroup": return "radio group"
        case "AXGroup":
            switch info.subrole {
            case "AXLandmarkNavigation": return "navigation"
            case "AXApplicationDialog", "AXApplicationAlertDialog": return "dialog"
            default: return "group"
            }
        default:
            if transparentRoles.contains(info.role) { return nil }
            return info.role.hasPrefix("AX") ? String(info.role.dropFirst(2)).lowercased() : info.role.lowercased()
        }
    }

    private var exhausted: Bool {
        if visited >= maxNodes || Date() > deadline { truncated = true }
        if let maxChars, chars >= maxChars { truncated = true }
        return truncated
    }

    private func append(_ depth: Int, _ line: String, words: String, ref: String?, owner: String?) {
        items.append(SnapshotItem(depth: depth, line: line, words: DesktopApps.normalized(words), ref: ref, owner: owner))
        chars += depth * 2 + line.count + 1
    }

    /// Words of an element's descendants: the label of a button made of text, a row's cells.
    /// An icon's description ("clock", "document page") names the element only when it has no text.
    private func descendantText(_ element: AXUIElement) -> (texts: [String], controls: [(AXUIElement, AXInfo)]) {
        var texts: [String] = [], icons: [String] = [], controls: [(AXUIElement, AXInfo)] = []
        func visit(_ element: AXUIElement, depth: Int) {
            guard depth < 5, texts.joined().count < 240, visited < maxNodes else { return }
            for child in element.elements(kAXChildrenAttribute, limit: 20) {
                visited += 1
                let info = AXInfo(child)
                if Self.skippedRoles.contains(info.role) { continue }
                // A checkbox or button inside a row stays reachable on its own line.
                if Self.actionableRoles.contains(info.role) && info.role != "AXRow" { controls.append((child, info)); continue }
                let words = info.role == "AXStaticText" ? info.valueText ?? info.label : ""
                if !words.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    texts.append(DesktopText.clip(words, 120))
                } else if info.role == "AXImage", !info.label.isEmpty {
                    icons.append(DesktopText.clip(info.label, 60))
                }
                visit(child, depth: depth + 1)
            }
        }
        visit(element, depth: 0)
        return (texts.isEmpty ? Array(icons.prefix(2)) : texts, controls)
    }

    /// The line for one element, without indentation.
    private func line(kind: String, info: AXInfo, label: String, ref: String?, summary: String? = nil) -> String {
        var line = "- " + kind
        if !label.isEmpty { line += " " + DesktopText.quoted(label) }
        if let ref { line += " [ref=\(ref)]" }
        if !info.enabled { line += " [disabled]" }
        if info.focused { line += " [focused]" }
        if info.selected { line += " [selected]" }
        if let expanded = info.expanded, ["AXDisclosureTriangle", "AXRow", "AXMenuButton", "AXPopUpButton", "AXComboBox", "AXButton"].contains(info.role) {
            line += expanded ? " [expanded]" : ""
        }
        if ["AXCheckBox", "AXRadioButton"].contains(info.role), let number = info.value as? NSNumber {
            line += number.intValue == 1 ? " [checked]" : (number.intValue == 2 ? " [mixed]" : "")
        }
        if info.isSecure {
            line += ": [hidden]"
        } else if let summary, !summary.isEmpty {
            line += ": " + DesktopText.clip(summary, 220)
        } else if ["AXTextField", "AXTextArea", "AXComboBox", "AXPopUpButton", "AXSlider", "AXIncrementor", "AXProgressIndicator",
                   "AXLevelIndicator", "AXDateField", "AXTimeField", "AXMenuButton"].contains(info.role), let value = info.valueText, !value.isEmpty, value != label {
            line += ": " + DesktopText.clip(value, info.role == "AXTextArea" ? 400 : 160)
        }
        if label.isEmpty, !info.placeholder.isEmpty, (info.valueText ?? "").isEmpty { line += " (placeholder " + DesktopText.quoted(info.placeholder, 60) + ")" }
        return line
    }

    func walk(_ element: AXUIElement, depth: Int = 0, parentLabel: String = "", owner: String? = nil) {
        guard !exhausted, seen.insert(AXKey(element: element)).inserted else { return }
        visited += 1
        let info = AXInfo(element)
        if Self.skippedRoles.contains(info.role) || Self.skippedSubroles.contains(info.subrole) || info.role == "AXApplication" { return }
        if let frame = info.frame, frame.width < 1 || frame.height < 1, !["AXWindow", "AXMenu", "AXMenuItem"].contains(info.role) { return }
        var label = info.label
        if label.isEmpty, ["AXTextField", "AXTextArea", "AXComboBox", "AXPopUpButton", "AXSlider", "AXCheckBox"].contains(info.role),
           let title = element.element(kAXTitleUIElementAttribute) {
            label = AXInfo.text(title.value(kAXValueAttribute)) ?? AXInfo.text(title.value(kAXTitleAttribute)) ?? ""
        }
        if info.role == "AXStaticText" {
            let text = info.valueText ?? label
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, DesktopText.clip(text, 300) != DesktopText.clip(parentLabel, 300) else { return }
            append(depth, "- text: " + DesktopText.clip(text, 300), words: text, ref: nil, owner: owner)
            return
        }
        guard let kind = Self.kind(info), !(Self.transparentWhenUnlabeled.contains(info.role) && label.isEmpty && info.subrole != "AXLandmarkNavigation") else {
            walkChildren(element, depth: depth, parentLabel: parentLabel, owner: owner)
            return
        }
        var ref: String?
        if Self.actionableRoles.contains(info.role) || (Self.probedRoles.contains(info.role) && element.actionNames.contains(where: Self.pressActions.contains)) {
            ref = refs.name(for: element, pid: pid)
        }
        if info.role == "AXRow" || (Self.leafRoles.contains(info.role) && label.isEmpty) {
            let (texts, controls) = descendantText(element)
            if info.role == "AXImage" && label.isEmpty && texts.isEmpty && ref == nil { return }
            let summary = texts.joined(separator: " · ")
            append(depth, line(kind: kind, info: info, label: info.role == "AXRow" ? label : (label.isEmpty ? summary : label), ref: ref,
                               summary: info.role == "AXRow" ? summary : nil),
                   words: label + " " + summary, ref: ref, owner: owner)
            for (control, controlInfo) in controls.prefix(6) where !exhausted {
                let controlRef = refs.name(for: control, pid: pid)
                let controlKind = Self.kind(controlInfo) ?? "control"
                append(depth + 1, line(kind: controlKind, info: controlInfo, label: controlInfo.label, ref: controlRef),
                       words: controlInfo.label + " " + (controlInfo.valueText ?? ""), ref: controlRef, owner: ref ?? owner)
            }
            return
        }
        append(depth, line(kind: kind, info: info, label: label, ref: ref), words: label + " " + (info.isSecure ? "" : info.valueText ?? ""), ref: ref, owner: owner)
        if Self.leafRoles.contains(info.role) {
            // A pop-up menu opened from a button is its only child worth listing.
            for child in element.elements(kAXChildrenAttribute, limit: 4) where AXInfo(child).role == "AXMenu" {
                walk(child, depth: depth + 1, parentLabel: label, owner: ref ?? owner)
            }
            return
        }
        walkChildren(element, depth: depth + 1, parentLabel: label, owner: ref ?? owner)
    }

    private func walkChildren(_ element: AXUIElement, depth: Int, parentLabel: String, owner: String?) {
        let total = element.count(kAXChildrenAttribute)
        guard total > 0 else { return }
        let children = element.elements(kAXChildrenAttribute, limit: Self.maxChildren)
        for child in children {
            guard !exhausted else { return }
            walk(child, depth: depth, parentLabel: parentLabel, owner: owner)
        }
        if total > children.count, !exhausted {
            append(depth, "- … \(total - children.count) more items (desktop_scroll or desktop_find reach them)", words: "", ref: nil, owner: owner)
        }
    }

    var rendered: String {
        items.map { String(repeating: "  ", count: $0.depth) + $0.line }.joined(separator: "\n")
    }
}

/// Windows, snapshots and search for one app, through the accessibility API.
@MainActor enum DesktopReader {
    static func application(_ pid: pid_t, timeout: Float = 2) -> AXUIElement {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, timeout)
        return app
    }

    static func windows(_ app: AXUIElement) -> [AXUIElement] { app.elements(kAXWindowsAttribute, limit: 30) }

    /// The window named by its ref ("w3") or a part of its title; the focused one by default.
    static func window(_ app: AXUIElement, pid: pid_t, query: String?, refs: DesktopRefs) throws -> AXUIElement {
        let all = windows(app)
        if let query = query?.trimmingCharacters(in: .whitespacesAndNewlines), !query.isEmpty {
            if let ref = refs[query], case .element(let window) = ref.target {
                guard ref.pid == pid else { throw VoiceError.message("Window \(query) belongs to another app.") }
                return window
            }
            let wanted = DesktopApps.normalized(query)
            if let match = all.first(where: { DesktopApps.normalized($0.string(kAXTitleAttribute) ?? "").contains(wanted) }) { return match }
            let titles = all.compactMap { $0.string(kAXTitleAttribute) }.filter { !$0.isEmpty }
            throw VoiceError.message("No window matches \"\(query)\"." + (titles.isEmpty ? "" : " Windows: " + titles.map { DesktopText.quoted($0, 60) }.joined(separator: ", ") + "."))
        }
        let isWindow = { (element: AXUIElement) in AXInfo(element).role == "AXWindow" }
        if let focused = app.element(kAXFocusedWindowAttribute), isWindow(focused) { return focused }
        if let main = app.element(kAXMainWindowAttribute), isWindow(main) { return main }
        if let first = all.first(where: isWindow) { return first }
        throw VoiceError.message("This app has no open window. Use desktop_open_app or desktop_menu (for example \"File > New\").")
    }

    /// Chromium and Electron build their accessibility tree only when asked.
    static func enableWebAccessibility(_ app: AXUIElement) {
        app.set("AXManualAccessibility", kCFBooleanTrue)
    }

    static func snapshot(app: DesktopApp, windowQuery: String?, refs: DesktopRefs, maxChars: Int = 7000) throws -> (text: String, window: AXUIElement, itemCount: Int) {
        guard let pid = app.pid else { throw VoiceError.message("\(app.name) is not running. Use desktop_open_app first.") }
        let element = application(pid)
        enableWebAccessibility(element)
        let window = try self.window(element, pid: pid, query: windowQuery, refs: refs)
        var walker = render(window, app: element, pid: pid, refs: refs, maxChars: maxChars)
        if walker.items.count < 4 {
            // A web view that just enabled accessibility fills its tree a moment later.
            Thread.sleep(forTimeInterval: 0.4)
            walker = render(window, app: element, pid: pid, refs: refs, maxChars: maxChars)
        }
        var header = "App: \(app.name)"
        let windowRef = refs.name(for: window, pid: pid, prefix: "w")
        header += " · window " + DesktopText.quoted(window.string(kAXTitleAttribute) ?? "", 80) + " [ref=\(windowRef)]"
        var text = header + "\n" + walker.rendered
        if walker.truncated {
            text += "\n[Snapshot truncated: use desktop_find to look for a word, or desktop_scroll.]"
        }
        return (text, window, walker.items.count)
    }

    private static func render(_ window: AXUIElement, app: AXUIElement, pid: pid_t, refs: DesktopRefs, maxChars: Int) -> AXTreeWalker {
        let walker = AXTreeWalker(pid: pid, refs: refs, maxNodes: 1800, maxChars: maxChars, seconds: 4)
        // A context menu opened by the app is a child of the app, not of the window.
        for menu in app.elements(kAXChildrenAttribute, limit: 40) where AXInfo(menu).role == "AXMenu" {
            walker.walk(menu)
        }
        walker.walk(window)
        return walker
    }

    static func find(app: DesktopApp, windowQuery: String?, text: String, refs: DesktopRefs) throws -> String {
        guard let pid = app.pid else { throw VoiceError.message("\(app.name) is not running. Use desktop_open_app first.") }
        let wanted = DesktopApps.normalized(text)
        guard !wanted.isEmpty else { throw VoiceError.message("Give the words to look for.") }
        let element = application(pid)
        enableWebAccessibility(element)
        let window = try self.window(element, pid: pid, query: windowQuery, refs: refs)
        let walker = AXTreeWalker(pid: pid, refs: refs, maxNodes: 6000, maxChars: nil, seconds: 8)
        walker.walk(window)
        let matches = walker.items.filter { $0.words.contains(wanted) }
        guard !matches.isEmpty else {
            return "Nothing matching \"\(text)\" in \(app.name)." + (walker.truncated ? " The window is large and was only partly searched." : "")
        }
        var lines = matches.prefix(30).map { item -> String in
            var line = item.line
            if item.ref == nil, let owner = item.owner { line += " (inside [ref=\(owner)])" }
            return line
        }
        if matches.count > 30 { lines.append("… \(matches.count - 30) more matches.") }
        return "Matches for \"\(text)\" in \(app.name):\n" + lines.joined(separator: "\n")
    }

    /// Apps with windows, front to back, and what JAIS Voice may do with each.
    static func listWindows(refs: DesktopRefs) -> String {
        let apps = DesktopApps.running()
        var order: [pid_t] = []
        if let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {
            for info in windows {
                if let pid = info[kCGWindowOwnerPID as String] as? pid_t, (info[kCGWindowLayer as String] as? Int) == 0, !order.contains(pid) { order.append(pid) }
            }
        }
        let frontmost = NSWorkspace.shared.frontmostApplication?.processIdentifier
        let visible = apps.filter { $0.regular && $0.bundleID != DesktopPolicy.ownBundleID }
            .sorted { lhs, rhs in
                let left = order.firstIndex(of: lhs.pid!) ?? Int.max, right = order.firstIndex(of: rhs.pid!) ?? Int.max
                return left != right ? left < right : lhs.name.localizedCaseInsensitiveCompare(rhs.name) == .orderedAscending
            }
        var lines: [String] = [], withoutWindows: [String] = [], blocked: [String] = []
        for app in visible {
            guard let pid = app.pid else { continue }
            if app.tier == .blocked { blocked.append(app.name); continue }
            let element = application(pid, timeout: 0.5)
            let windows = windows(element)
            guard !windows.isEmpty else { withoutWindows.append(app.name); continue }
            var line = "- app " + DesktopText.quoted(app.name, 60)
            if pid == frontmost { line += " [frontmost]" }
            line += app.tier == .read ? " [read-only]" : ""
            lines.append(line)
            let focused = element.element(kAXFocusedWindowAttribute).map(AXKey.init(element:))
            for window in windows.prefix(8) {
                var entry = "  - window " + DesktopText.quoted(window.string(kAXTitleAttribute) ?? "", 80) + " [ref=\(refs.name(for: window, pid: pid, prefix: "w"))]"
                if window.bool(kAXMinimizedAttribute) == true { entry += " [minimized]" }
                if let focused, AXKey(element: window) == focused { entry += " [focused]" }
                lines.append(entry)
            }
            if windows.count > 8 { lines.append("  - … \(windows.count - 8) more windows") }
        }
        var text = lines.isEmpty ? "No app window is open." : "Open windows, front to back:\n" + lines.joined(separator: "\n")
        if !withoutWindows.isEmpty { text += "\nRunning without windows: " + withoutWindows.joined(separator: ", ") }
        if !blocked.isEmpty { text += "\nNot accessible to JAIS Voice: " + blocked.joined(separator: ", ") }
        return text
    }
}
