#!/usr/bin/env python3
"""Package an app into a drag-to-install disk image.

The window shows the app on the left and an Applications shortcut on the
right, over a background with an arrow. Finder lays the window out through
AppleScript; if Finder cannot be scripted (no Automation permission, headless
session), the image is still produced with Finder's default layout.

    make_dmg.py "outputs/Jais Voice.app" outputs/Jais-Voice-1.0.0.dmg
"""
import argparse
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile

WIDTH, HEIGHT = 640, 400
APP_POSITION, APPLICATIONS_POSITION = (160, 190), (480, 190)
TITLE_BAR = 28  # Finder window bounds include the title bar

# Drawn with AppKit through JavaScript for Automation: no image library needed.
# AppKit's origin is bottom-left, Finder's is top-left.
BACKGROUND_SCRIPT = """
ObjC.import('AppKit');
function run(argv) {
  const W = %(width)d, H = %(height)d, scale = 2;
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, W * scale, H * scale, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
  rep.setSize($.NSMakeSize(W, H));
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext($.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep));
  $.NSColor.whiteColor.setFill;
  $.NSRectFill($.NSMakeRect(0, 0, W, H));
  const ink = $.NSColor.blackColor;
  const y = H - %(arrow_y)d;
  const line = $.NSBezierPath.bezierPath;
  line.moveToPoint($.NSMakePoint(255, y)); line.lineToPoint($.NSMakePoint(372, y));
  line.setLineWidth(5); line.setLineCapStyle(1); ink.setStroke; line.stroke;
  const head = $.NSBezierPath.bezierPath;
  head.moveToPoint($.NSMakePoint(388, y)); head.lineToPoint($.NSMakePoint(366, y + 13));
  head.lineToPoint($.NSMakePoint(366, y - 13)); head.closePath; ink.setFill; head.fill;
  const style = $.NSMutableParagraphStyle.alloc.init; style.setAlignment(1);
  const attributes = $.NSDictionary.dictionaryWithObjectsForKeys(
    $([$.NSFont.fontWithNameSize('HelveticaNeue-Medium', 15), $.NSColor.blackColor, style]),
    $([$.NSFontAttributeName, $.NSForegroundColorAttributeName, $.NSParagraphStyleAttributeName]));
  $(%(caption)s).drawInRectWithAttributes($.NSMakeRect(0, 52, W, 24), attributes);
  $.NSGraphicsContext.restoreGraphicsState;
  rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(argv[0], true);
}
"""

LAYOUT_SCRIPT = """
on run argv
  set volumeName to item 1 of argv
  tell application "Finder"
    tell disk volumeName
      open
      set current view of container window to icon view
      set toolbar visible of container window to false
      set statusbar visible of container window to false
      set pathbar visible of container window to false
      set the bounds of container window to {200, 120, %(right)d, %(bottom)d}
      set viewOptions to the icon view options of container window
      set arrangement of viewOptions to not arranged
      set icon size of viewOptions to 112
      set text size of viewOptions to 13
      set background picture of viewOptions to file ".background:background.png"
      set position of item "%(app)s" of container window to {%(app_x)d, %(app_y)d}
      set position of item "Applications" of container window to {%(apps_x)d, %(apps_y)d}
      set extension hidden of item "%(app)s" to true
      close
      open
      -- Finder stores the size it shows: set it again once the window is reopened.
      set the bounds of container window to {200, 120, %(right)d, %(bottom)d}
      update without registering applications
      delay 2
      close
    end tell
  end tell
end run
"""


def run(*args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def disk_usage(path: Path) -> int:
    return int(subprocess.check_output(["/usr/bin/du", "-sk", str(path)], text=True).split()[0]) * 1024


def make_dmg(app: Path, output: Path, volume: str) -> Path:
    app = app.resolve()
    with tempfile.TemporaryDirectory(prefix="jais-dmg-") as temporary:
        temporary = Path(temporary)
        source = temporary / "source"
        source.mkdir()
        run("/usr/bin/ditto", app, source / app.name)
        (source / "Applications").symlink_to("/Applications")
        (source / ".background").mkdir()
        background = source / ".background/background.png"
        script = BACKGROUND_SCRIPT % {"width": WIDTH, "height": HEIGHT, "arrow_y": APP_POSITION[1],
                                      "caption": '"Drag Jais Voice to Applications"'}
        run("/usr/bin/osascript", "-l", "JavaScript", "-e", script, background)
        # A writable image lets Finder store the window layout in .DS_Store.
        writable = temporary / "layout.dmg"
        size_mb = disk_usage(source) * 11 // 10 // 2**20 + 64
        run("/usr/bin/hdiutil", "create", "-srcfolder", source, "-volname", volume, "-fs", "HFS+",
            "-format", "UDRW", "-size", f"{size_mb}m", writable)
        attached = subprocess.check_output(["/usr/bin/hdiutil", "attach", "-readwrite", "-noverify", "-noautoopen", str(writable)], text=True)
        device = re.search(r"^(/dev/disk\d+)\s", attached, re.M).group(1)
        mount = Path(attached.strip().splitlines()[-1].split("\t")[-1].strip())
        try:
            layout = LAYOUT_SCRIPT % {"app": app.name, "right": 200 + WIDTH, "bottom": 120 + HEIGHT + TITLE_BAR,
                                      "app_x": APP_POSITION[0], "app_y": APP_POSITION[1],
                                      "apps_x": APPLICATIONS_POSITION[0], "apps_y": APPLICATIONS_POSITION[1]}
            try:
                run("/usr/bin/osascript", "-e", layout, mount.name, timeout=120)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                print(f"Finder layout skipped ({error}); the image keeps the default window.", flush=True)
            run("/bin/sync")
        finally:
            # Finder may still hold the volume for a moment after closing its window.
            if subprocess.run(["/usr/bin/hdiutil", "detach", device, "-quiet"]).returncode != 0:
                run("/usr/bin/hdiutil", "detach", device, "-force")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)
        run("/usr/bin/hdiutil", "convert", writable, "-format", "ULFO", "-o", output)
    print(f"Disk image ready: {output}", flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--volume", help="Volume name (default: the app's display name)")
    args = parser.parse_args()
    with (args.app / "Contents/Info.plist").open("rb") as source:
        name = plistlib.load(source).get("CFBundleDisplayName") or args.app.stem
    make_dmg(args.app, args.output, args.volume or name)


if __name__ == "__main__":
    main()
