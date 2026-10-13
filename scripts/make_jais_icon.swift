import AppKit

// Draw the Jais 3 wordmark ("Jais 3" over "جيس") in white on a black macOS icon tile.
//   swift scripts/make_jais_icon.swift <iconset dir>
//   iconutil -c icns <iconset dir> -o macos/Resources/JAISVoice.icns
let iconset = URL(fileURLWithPath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

func line(_ text: String, font: NSFont, kern: CGFloat = 0) -> NSAttributedString {
    NSAttributedString(string: text, attributes: [.font: font, .foregroundColor: NSColor.white, .kern: kern])
}

for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let n = CGFloat(pixels)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                                         samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                         bytesPerRow: 0, bitsPerPixel: 0) else { fatalError("Icon render failed") }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
        NSColor.black.setFill()
        NSBezierPath(roundedRect: NSRect(x: n*0.1, y: n*0.1, width: n*0.8, height: n*0.8), xRadius: n*0.18, yRadius: n*0.18).fill()
        let type = n * 0.215
        let latin = line("Jais 3", font: NSFont(name: "HelveticaNeue-Bold", size: type)!, kern: -type * 0.045)
        let arabic = line("جيس", font: .systemFont(ofSize: type * 1.1, weight: .heavy))
        let latinSize = latin.size(), arabicSize = arabic.size()
        let gap = -type * 0.34
        let height = latinSize.height + gap + arabicSize.height
        let right = (n + latinSize.width) / 2
        let bottom = (n - height) / 2
        arabic.draw(at: NSPoint(x: right - arabicSize.width, y: bottom))
        latin.draw(at: NSPoint(x: right - latinSize.width, y: bottom + arabicSize.height + gap))
        NSGraphicsContext.restoreGraphicsState()
        guard let data = rep.representation(using: .png, properties: [:]) else { fatalError("Icon render failed") }
        let suffix = scale == 2 ? "@2x" : ""
        try data.write(to: iconset.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
