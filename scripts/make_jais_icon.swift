import AppKit

// Place the original Jais mark on a macOS icon tile without altering its artwork.
let resources = URL(fileURLWithPath: CommandLine.arguments[1])
let iconset = URL(fileURLWithPath: CommandLine.arguments[2])
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)
guard let mark = NSImage(contentsOf: resources.appendingPathComponent("JAISMark.png")) else { fatalError("Jais mark missing") }
for size in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let n = CGFloat(pixels)
        NSColor(srgbRed: 242/255.0, green: 247/255.0, blue: 242/255.0, alpha: 1).setFill()
        NSBezierPath(roundedRect: NSRect(x: n*0.05, y: n*0.05, width: n*0.9, height: n*0.9), xRadius: n*0.20, yRadius: n*0.20).fill()
        let width = n*0.66
        let height = width * mark.size.height / mark.size.width
        mark.draw(in: NSRect(x: (n-width)/2, y: (n-height)/2, width: width, height: height), from: .zero, operation: .sourceOver, fraction: 1)
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff), let data = rep.representation(using: .png, properties: [:]) else { fatalError("Icon render failed") }
        let suffix = scale == 2 ? "@2x" : ""
        try data.write(to: iconset.appendingPathComponent("icon_\(size)x\(size)\(suffix).png"))
    }
}
