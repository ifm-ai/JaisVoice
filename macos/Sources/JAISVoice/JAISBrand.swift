import AppKit
import SwiftUI

/// Brand colors sampled from the supplied, unmodified JAIS artwork.
enum JAISBrand {
    static let green = Color(red: 36 / 255.0, green: 211 / 255.0, blue: 101 / 255.0)
    static let forest = Color(red: 0, green: 69 / 255.0, blue: 57 / 255.0)
    static let background = Color(nsColor: backgroundNS)
    static let backgroundNS = NSColor(srgbRed: 6 / 255.0, green: 35 / 255.0, blue: 30 / 255.0, alpha: 1)
    static let paper = Color(red: 242 / 255.0, green: 247 / 255.0, blue: 242 / 255.0)

    static func image(_ name: String) -> NSImage? {
        guard let url = Bundle.main.url(forResource: name, withExtension: "png") else { return nil }
        return NSImage(contentsOf: url)
    }
}

struct JAISArtwork: View {
    var fullLogo = false
    var body: some View {
        if let image = JAISBrand.image(fullLogo ? "JAISLogo" : "JAISMark") {
            Image(nsImage: image).resizable().interpolation(.high).scaledToFit()
                .accessibilityLabel("JAIS")
        } else {
            Text("JAIS").font(.system(size: 12, weight: .bold)).foregroundStyle(JAISBrand.green)
        }
    }
}
