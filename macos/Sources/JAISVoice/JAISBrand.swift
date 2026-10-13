import AppKit
import SwiftUI

/// Jais 3 identity: black and white, set in a bold neo-grotesque. The wordmark is
/// type only ("Jais 3" over "جيس"); there is no graphic mark.
enum JAISBrand {
    static let ink = Color.black
    static let paper = Color.white
    static let background = Color(nsColor: backgroundNS)
    static let backgroundNS = NSColor.black
    /// Interactive emphasis on the black surfaces: white, like the wordmark.
    static let accent = Color.white
    /// Links stay apart from body text without leaving the greyscale.
    static let link = Color(white: 0.62)

    /// Latin display face. Helvetica Neue ships with macOS, so nothing is bundled.
    static func display(_ size: CGFloat, weight: NSFont.Weight = .bold) -> Font {
        Font(displayNS(size, weight: weight))
    }

    static func displayNS(_ size: CGFloat, weight: NSFont.Weight = .bold) -> NSFont {
        let name: String
        switch weight {
        case .bold, .heavy, .black, .semibold: name = "HelveticaNeue-Bold"
        case .medium: name = "HelveticaNeue-Medium"
        default: name = "HelveticaNeue"
        }
        return NSFont(name: name, size: size) ?? .systemFont(ofSize: size, weight: weight)
    }

    /// Arabic face of the wordmark: the system Arabic (SF Arabic).
    static func arabicNS(_ size: CGFloat) -> NSFont {
        .systemFont(ofSize: size, weight: .heavy)
    }
}

/// The Jais 3 lockup: "Jais 3" with "جيس" beneath, aligned on the right edge.
/// Its layout never mirrors with the interface language.
struct JAISWordmark: View {
    var size: CGFloat = 20
    var color: Color = JAISBrand.paper

    var body: some View {
        VStack(alignment: .trailing, spacing: -size * 0.34) {
            Text("Jais 3").font(JAISBrand.display(size)).tracking(-size * 0.045)
            Text("جيس").font(Font(JAISBrand.arabicNS(size * 1.1)))
        }
        .foregroundStyle(color)
        .fixedSize()
        .environment(\.layoutDirection, .leftToRight)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Jais")
    }
}
