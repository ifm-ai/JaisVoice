---
name: presentations
description: Create a presentation in Keynote with osascript so the user sees it being built, and save it as PowerPoint (.pptx) if asked. Use for requests for a presentation, slides, a deck, Keynote or PowerPoint.
---

# Presentations in Keynote

Keynote is scripted with AppleScript through shell_execute. Run osascript with a quoted heredoc, and
build the deck in several commands, a slide or a few at a time, so the user sees it appear on screen.

```
osascript <<'EOF'
tell application "Keynote"
activate
set d to make new document with properties {document theme:theme 1}
end tell
EOF
```

- A new document needs a theme (`theme N`); without one Keynote waits for the user to pick it.
- Add a slide with `make new slide` in `front document`, with `{base layout:slide layout N}`.
  Layout names depend on the system language; in the standard themes layout 1 is a title slide and
  layout 4 a title with bullets.
- Fill a slide with `object text` of its `default title item` and `default body item` (one line per
  bullet), and `presenter notes`. Empty placeholders stay visible: go through the slide's text items
  from the last to the first and delete those whose `object text` is "" (a `whose` filter fails).
  `set current slide to s` shows the slide just added.
- Inside AppleScript strings, a double quote must be written `\"`. Keep the lines of the heredoc,
  and the closing `EOF`, without indentation.
- Save as PowerPoint with `export front document to POSIX file ((POSIX path of (path to documents
  folder)) & "Name.pptx") as Microsoft PowerPoint`.
- Read each result; if a command fails, fix that step rather than starting the deck again.
