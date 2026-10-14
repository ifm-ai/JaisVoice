---
name: presentations
description: Create a presentation in Keynote, slide by slide so the user sees it being built, with charts, tables and key figures, and save it as PowerPoint (.pptx). Use for requests for a presentation, slides, a deck, Keynote or PowerPoint.
---

# Presentations in Keynote

`deck.sh`, next to this file, builds the deck in Keynote: call it with shell_execute and its full
path, one command per slide. Never write AppleScript, script files or python, and never install
anything: deck.sh does it all.

1. Read the sources with the browser tools. The browser cannot read the text of a PDF: for a PDF
   source, search for an HTML page with the same content and say which source you could not read.
2. Plan every slide first. A good deck mixes slide kinds: a table to compare, a chart for numbers
   that change, a big fact for the one figure to remember, a quote from a source, and bullet slides
   in between. Use only figures from your sources, or label an example as hypothetical.
3. Run one command per slide. Text goes between `<<'EOF'` and `EOF` exactly as written: quotes,
   apostrophes and $ need no escaping. Any slide can end with a line `---` and the speaker notes.

```
zsh /full/path/to/deck.sh new <<'EOF'
Deck title
Subtitle
EOF
```

Bullets: the title, then one short bullet per line (3 to 5).

```
zsh /full/path/to/deck.sh slide <<'EOF'
Slide title
First point
Second point
---
What the speaker says on this slide.
EOF
```

Table: the title, then rows with `|` between the cells; the first row and column are headers.

```
zsh /full/path/to/deck.sh table <<'EOF'
CD vs. variable annuity
| Feature | CD | Variable annuity |
| Insurance | FDIC up to $250,000 | Not FDIC-insured |
EOF
```

Chart: `chart bar`, `chart line` or `chart pie`. The title, a header row of categories, then one
row per series with its numbers (`$`, `%` and thousands commas are fine). For a pie, one row per
slice with one number.

```
zsh /full/path/to/deck.sh chart bar <<'EOF'
Growth of $100,000 (hypothetical, 4% vs. 1.5% net)
| | Year 1 | Year 5 | Year 10 |
| CD | 104,000 | 121,665 | 148,024 |
| Variable annuity | 101,500 | 107,728 | 116,054 |
EOF
```

Big fact: the number or short phrase, then what it means. Quote: the quote, then its source.
Section: one title that opens a new part.

```
zsh /full/path/to/deck.sh fact <<'EOF'
7%
Typical first-year surrender charge (FINRA)
EOF
```

```
zsh /full/path/to/deck.sh quote <<'EOF'
The quoted sentence.
Who said it
EOF
```

```
zsh /full/path/to/deck.sh section <<'EOF'
What the regulators say
EOF
```

4. Save it as PowerPoint in Documents, with the name the user asked for:

```
zsh /full/path/to/deck.sh export "Name.pptx"
```

Each command answers what it did; if one fails, read its message, fix that command and run it again.
