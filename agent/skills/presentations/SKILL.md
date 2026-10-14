---
name: presentations
description: Create a presentation in Keynote with osascript so the user sees it being built, and save it as PowerPoint (.pptx) if asked. Use for requests for a presentation, slides, a deck, Keynote or PowerPoint.
---

# Presentations in Keynote

Build the deck in Keynote with shell_execute and osascript. The user watches it appear: plan the
slides first, then create one document and add the slides one command at a time. Keynote has all
that is needed: never install packages, never use python-pptx and never write script files.

## 1. Sources

Read the web sources with the browser tools. The browser cannot read the text of a PDF: for a
PDF source, search for an HTML page with the same content, and say which source you could not read.

## 2. New document and title slide

Run this once, with your title and subtitle. Do not create test documents: Keynote saves each
new document.

```
osascript <<'EOF'
tell application "Keynote"
activate
set d to make new document with properties {document theme:theme 14}
tell d
set object text of default title item of slide 1 to "Deck title"
set object text of default body item of slide 1 to "Subtitle"
end tell
end tell
EOF
```

## 3. One command per slide

Copy this command for each slide and change only the texts. Layout 4 is a title with bullets.

```
osascript <<'EOF'
tell application "Keynote"
tell front document
set s to make new slide with properties {base layout:slide layout 4}
set object text of default title item of s to "Slide title"
set object text of default body item of s to "First point
Second point
Third point"
set presenter notes of s to "What the speaker says on this slide."
repeat with i from (count of text items of s) to 1 by -1
if object text of text item i of s is "" then delete text item i of s
end repeat
set current slide to s
end tell
end tell
EOF
```

- Text is set with `object text`; `text` and `value` cannot be set and fail.
- One bullet per line; inside the quotes, write `\"` for a double quote.
- Nothing after the closing `EOF`, no `echo` inside the heredoc, no indentation.
- Slide layouts belong to the document: use them inside `tell front document`.

## 4. Save as PowerPoint

```
osascript <<'EOF'
tell application "Keynote"
export front document to POSIX file ((POSIX path of (path to documents folder)) & "Name.pptx") as Microsoft PowerPoint
end tell
EOF
```

If a command fails, read the error, fix that slide's command and run it again; keep the same document.
