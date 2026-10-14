---
name: presentations
description: Create a presentation in Keynote, slide by slide so the user sees it being built, and save it as PowerPoint (.pptx). Use for requests for a presentation, slides, a deck, Keynote or PowerPoint.
---

# Presentations in Keynote

`deck.sh`, next to this file, builds the deck in Keynote: call it with shell_execute and its full
path, one command per step. Never write AppleScript, script files or python, and never install
anything: deck.sh does it all.

1. Read the sources with the browser tools. The browser cannot read the text of a PDF: for a PDF
   source, search for an HTML page with the same content and say which source you could not read.
2. Plan every slide, then create the deck: first line the title, next line the subtitle.

```
zsh /full/path/to/deck.sh new <<'EOF'
Deck title
Subtitle
EOF
```

3. Add each slide with its own command: first line the title, then one bullet per line, then a
   line `---` and the speaker notes. Keep 3 to 5 short bullets; details go in the notes.

```
zsh /full/path/to/deck.sh slide <<'EOF'
Slide title
First point
Second point
---
What the speaker says on this slide.
EOF
```

4. Save it as PowerPoint in Documents, with the name the user asked for:

```
zsh /full/path/to/deck.sh export "Name.pptx"
```

Text goes between the `<<'EOF'` and `EOF` lines exactly as written: quotes, apostrophes and $ need
no escaping. Each command answers what it did; if one fails, read its message, fix that command
and run it again.
