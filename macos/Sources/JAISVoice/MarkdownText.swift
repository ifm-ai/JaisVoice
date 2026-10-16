import SwiftUI

/// A block of an agent reply. Inline styles (bold, links, code) stay in the text
/// and are read by `Markdown.inline`.
indirect enum MarkdownBlock: Equatable {
    case paragraph(String)
    case heading(level: Int, text: String)
    case listItem(marker: String, depth: Int, text: String)
    case code(String)
    case quote([MarkdownBlock])
    case table(header: [String], rows: [[String]])
    case rule
}

/// The Markdown subset language models write: headings, lists, code, quotes, tables and rules.
enum Markdown {
    static func blocks(_ text: String) -> [MarkdownBlock] {
        let lines = text.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n")
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var index = 0
        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))); paragraph = [] }
        }
        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.isEmpty { flush(); index += 1; continue }
            if let fence = fence(trimmed) {
                flush()
                var code: [String] = []
                index += 1
                // A reply still streaming may not have closed its fence yet.
                while index < lines.count, !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(fence) {
                    code.append(lines[index]); index += 1
                }
                blocks.append(.code(code.joined(separator: "\n")))
                index += 1
                continue
            }
            if let heading = heading(trimmed) { flush(); blocks.append(heading); index += 1; continue }
            if isRule(trimmed) { flush(); blocks.append(.rule); index += 1; continue }
            if trimmed.hasPrefix(">") {
                flush()
                var quoted: [String] = []
                while index < lines.count {
                    let next = lines[index].trimmingCharacters(in: .whitespaces)
                    guard next.hasPrefix(">") else { break }
                    let content = next.dropFirst()
                    quoted.append(String(content.hasPrefix(" ") ? content.dropFirst() : content))
                    index += 1
                }
                blocks.append(.quote(self.blocks(quoted.joined(separator: "\n"))))
                continue
            }
            if trimmed.contains("|"), index + 1 < lines.count, isTableDelimiter(lines[index + 1]) {
                flush()
                let header = cells(trimmed)
                var rows: [[String]] = []
                index += 2
                while index < lines.count {
                    let row = lines[index].trimmingCharacters(in: .whitespaces)
                    guard row.contains("|") else { break }
                    let values = cells(row)
                    rows.append(Array((values + Array(repeating: "", count: header.count)).prefix(header.count)))
                    index += 1
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }
            if let item = listItem(line) { flush(); blocks.append(item); index += 1; continue }
            if paragraph.isEmpty, case .listItem(let marker, let depth, let text)? = blocks.last, line.first?.isWhitespace == true {
                // An indented line continues the item above it.
                blocks[blocks.count - 1] = .listItem(marker: marker, depth: depth, text: text + "\n" + trimmed)
                index += 1
                continue
            }
            paragraph.append(trimmed)
            index += 1
        }
        flush()
        return blocks
    }

    /// Inline Markdown with line breaks kept, as a chat reply expects.
    static func inline(_ text: String, size: CGFloat) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace,
                                                              failurePolicy: .returnPartiallyParsedIfPossible)
        guard var result = try? AttributedString(markdown: text, options: options) else { return AttributedString(text) }
        for run in result.runs where run.inlinePresentationIntent?.contains(.code) == true {
            result[run.range].font = .system(size: size * 0.92, design: .monospaced)
            result[run.range].backgroundColor = Color.white.opacity(0.1)
        }
        return result
    }

    private static func fence(_ line: String) -> String? {
        ["```", "~~~"].first { line.hasPrefix($0) }
    }

    private static func heading(_ line: String) -> MarkdownBlock? {
        let level = line.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(level) else { return nil }
        let rest = line.dropFirst(level)
        guard rest.isEmpty || rest.first == " " else { return nil }
        var text = rest.trimmingCharacters(in: .whitespaces)
        // A closing sequence of hashes is decoration.
        while text.hasSuffix("#") { text.removeLast() }
        return .heading(level: level, text: text.trimmingCharacters(in: .whitespaces))
    }

    private static func isRule(_ line: String) -> Bool {
        let compact = line.filter { $0 != " " }
        guard compact.count >= 3, let first = compact.first, "-*_".contains(first) else { return false }
        return compact.allSatisfy { $0 == first }
    }

    private static func listItem(_ line: String) -> MarkdownBlock? {
        let indent = line.prefix(while: { $0 == " " || $0 == "\t" }).reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
        let body = line.drop(while: { $0 == " " || $0 == "\t" })
        let depth = min(3, indent / 2)
        var marker: String
        var rest: Substring
        if let first = body.first, "-*+".contains(first), body.dropFirst().first == " " {
            marker = ["•", "◦", "▪", "▪"][depth]
            rest = body.dropFirst(2)
        } else {
            let digits = body.prefix(while: \.isNumber)
            let after = body.dropFirst(digits.count)
            guard (1...9).contains(digits.count), let delimiter = after.first, ".)".contains(delimiter),
                  after.dropFirst().first == " " else { return nil }
            marker = digits + "."
            rest = after.dropFirst(2)
        }
        var text = rest.trimmingCharacters(in: .whitespaces)
        for (box, symbol) in [("[ ] ", "☐"), ("[x] ", "☑"), ("[X] ", "☑")] where text.hasPrefix(box) {
            marker = symbol; text = String(text.dropFirst(box.count))
        }
        return .listItem(marker: marker, depth: depth, text: text)
    }

    private static func isTableDelimiter(_ line: String) -> Bool {
        let row = cells(line.trimmingCharacters(in: .whitespaces))
        return line.contains("|") && line.contains("-") && !row.isEmpty && row.allSatisfy { cell in
            !cell.isEmpty && cell.allSatisfy { "-:".contains($0) } && cell.contains("-")
        }
    }

    private static func cells(_ row: String) -> [String] {
        var row = Substring(row)
        if row.hasPrefix("|") { row = row.dropFirst() }
        if row.hasSuffix("|") && !row.hasSuffix("\\|") { row = row.dropLast() }
        var cells: [String] = [], current = "", escaped = false
        for character in row {
            if escaped { current.append(character); escaped = false }
            else if character == "\\" { escaped = true; current.append(character) }
            else if character == "|" { cells.append(current); current = "" }
            else { current.append(character) }
        }
        cells.append(current)
        return cells.map { $0.replacingOccurrences(of: "\\|", with: "|").trimmingCharacters(in: .whitespaces) }
    }
}

/// An agent reply rendered as Markdown in the result bubble.
struct MarkdownText: View {
    let text: String
    let size: CGFloat

    var body: some View {
        MarkdownBlocksView(blocks: Markdown.blocks(text), size: size)
            .foregroundStyle(.white.opacity(0.9)).tint(JAISBrand.link)
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .environment(\.layoutDirection, TranscriptDiff.isRTL(text) ? .rightToLeft : .leftToRight)
    }
}

private struct MarkdownBlocksView: View {
    let blocks: [MarkdownBlock]
    let size: CGFloat

    var body: some View {
        VStack(alignment: .leading, spacing: size * 0.6) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in view(for: block) }
        }
    }

    @ViewBuilder private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .paragraph(let text):
            inline(text)
        case .heading(let level, let text):
            inline(text, size: size + CGFloat(max(0, 4 - level)), weight: level <= 2 ? .bold : .semibold)
                .foregroundStyle(.white)
                .padding(.top, level <= 2 ? 4 : 2)
        case .listItem(let marker, let depth, let text):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(marker).font(.system(size: size).monospacedDigit()).foregroundStyle(.white.opacity(0.55))
                inline(text)
            }
            .padding(.leading, CGFloat(depth) * 16)
        case .code(let code):
            Text(code)
                .font(.system(size: size - 3, design: .monospaced)).foregroundStyle(.white.opacity(0.8))
                .lineSpacing(2).fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(9).background(Color.white.opacity(0.05), in: RoundedRectangle(cornerRadius: 9))
                .environment(\.layoutDirection, .leftToRight)
        case .quote(let inner):
            HStack(alignment: .top, spacing: 9) {
                RoundedRectangle(cornerRadius: 1).fill(JAISBrand.accent.opacity(0.6)).frame(width: 2)
                MarkdownBlocksView(blocks: inner, size: size).opacity(0.75)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .table(let header, let rows):
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                GridRow { ForEach(Array(header.enumerated()), id: \.offset) { _, cell in inline(cell, size: size * 0.9, weight: .semibold) } }
                Rectangle().fill(.white.opacity(0.15)).frame(height: 1).gridCellUnsizedAxes(.horizontal)
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    GridRow { ForEach(Array(row.enumerated()), id: \.offset) { _, cell in inline(cell, size: size * 0.9) } }
                }
            }
            .padding(9).background(Color.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
        case .rule:
            Rectangle().fill(.white.opacity(0.15)).frame(height: 1).padding(.vertical, 2)
        }
    }

    private func inline(_ text: String, size: CGFloat? = nil, weight: Font.Weight = .regular) -> some View {
        let size = size ?? self.size
        return Text(Markdown.inline(text, size: size)).font(.system(size: size, weight: weight))
            .lineSpacing(size * 0.28).fixedSize(horizontal: false, vertical: true)
    }
}
