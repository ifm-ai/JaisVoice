---
name: presentations
description: Create a presentation in Keynote, slide by slide so the user sees it being built, with charts, tables and key figures, and save it as PowerPoint (.pptx). Use for requests for a presentation, slides, a deck, Keynote or PowerPoint.
---

# Presentations in Keynote

`deck.sh`, next to this file, builds the deck in Keynote: call it with shell_execute and its full
path in double quotes (the path may contain spaces), one command per slide. Never write AppleScript, script files or python, and never install
anything: deck.sh does it all.

## 1. Sources

Read the sources with the browser tools. The browser cannot read the text of a PDF: for a PDF
source, search for an HTML page with the same content and say which source you could not read.

## 2. Plan the deck: charts and tables first

A deck of bullet slides alone is not acceptable. Before the first command, write the plan: one line
per slide with its kind. A deck of 6 slides or more has at least:

- one **table**, wherever two or more options are compared (features, costs, penalties, risks);
- one **chart**, wherever numbers change over time or split into shares (growth, fees, returns);
- one **fact**, for the single figure the audience must remember.

Bullet slides fill the rest; a quote or a section slide may help. For 10 slides, for example:
title, bullets (the situation), table (comparison), chart (growth over years), fact (key figure),
table (penalties or costs), bullets, bullets, quote or bullets, bullets (recommendation).

Use only figures from your sources; when you compute an example, say "hypothetical" in the title.

## 3. Build it: one command per slide

Text goes between `<<'EOF'` and `EOF` exactly as written: quotes, apostrophes and $ need no
escaping. Every slide can end with a line `---` and the speaker notes. Each command answers what
it did and reminds you while the deck still lacks a chart or a table.

### Title (first)

```
zsh "/full/path/to/deck.sh" new <<'EOF'
Deck title
Subtitle
EOF
```

### Table

The title, then one row per line with `|` between the cells. The first row holds the column
headers and the first column the row labels. Keep cells short (a few words); 3 to 7 rows.

```
zsh "/full/path/to/deck.sh" table <<'EOF'
CD vs. variable annuity
| Feature | CD | Variable annuity |
| Insurance | FDIC up to $250,000 | Not FDIC-insured |
| Return | Fixed rate | Tied to the market |
| Exit cost | A few months of interest | Surrender charge for years |
---
What the speaker says about this comparison.
EOF
```

### Chart

`chart bar` compares amounts, `chart line` shows a change over time, `chart pie` splits a whole.
The title, then a header row with the categories (for example years), then one row per series
(for example each product) with one number per category. Numbers may carry `$`, `%` and thousands
commas. Give 3 to 8 categories and 1 to 4 series; a legend is added with the series names.

```
zsh "/full/path/to/deck.sh" chart bar <<'EOF'
Growth of $100,000 (hypothetical: 4% vs. 1.5% after fees)
| | Year 1 | Year 3 | Year 5 | Year 10 |
| CD | 104,000 | 112,486 | 121,665 | 148,024 |
| Variable annuity | 101,500 | 104,568 | 107,728 | 116,054 |
EOF
```

A pie: one row per slice, one number each.

```
zsh "/full/path/to/deck.sh" chart pie <<'EOF'
Yearly costs of a variable annuity (hypothetical)
| | Share of yearly cost |
| Mortality and expense | 1.25 |
| Fund expenses | 0.8 |
| Riders | 0.5 |
EOF
```

### Fact

The number alone on the first line (such as 7% or $7,000), then one line saying what it means.

```
zsh "/full/path/to/deck.sh" fact <<'EOF'
7%
Typical first-year surrender charge (FINRA)
EOF
```

### Bullets, quote and section

Bullets: the title, then 3 to 5 short bullets, one per line. Quote: the quote, then its source.
Section: one title that opens a new part.

```
zsh "/full/path/to/deck.sh" slide <<'EOF'
Slide title
First point
Second point
---
What the speaker says on this slide.
EOF
```

```
zsh "/full/path/to/deck.sh" quote <<'EOF'
The quoted sentence.
Who said it
EOF
```

```
zsh "/full/path/to/deck.sh" section <<'EOF'
What the regulators say
EOF
```

## 4. Save as PowerPoint

Once every slide is added, export to Documents with the name the user asked for:

```
zsh "/full/path/to/deck.sh" export "Name.pptx"
```

If a command fails, read its message, fix that command and run it again; keep the same deck.
