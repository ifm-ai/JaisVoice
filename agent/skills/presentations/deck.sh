#!/bin/zsh -f
# Build a Keynote deck one step at a time, so the user sees it appear. Text comes on standard
# input through a quoted heredoc, so quotes, apostrophes and $ need no escaping:
#   deck.sh new       title, then the subtitle
#   deck.sh slide     title, one bullet per line
#   deck.sh chart bar|line|pie   title, a header row of categories, one row per series: | name | 1 | 2 |
#   deck.sh table     title, then rows: | cell | cell |
#   deck.sh fact      the big number or phrase, then what it means
#   deck.sh quote     the quote, then who said it
#   deck.sh section   the section title
#   deck.sh export "Name.pptx"   (saved as PowerPoint in ~/Documents)
# Every slide may end with a line --- and the speaker notes.
set -u
setopt extended_glob
action=${1:-}
kind=${2:-bar}

usage='Usage: deck.sh new|slide|chart bar|chart line|chart pie|table|fact|quote|section (text on standard input), or deck.sh export "Name.pptx"'
case $action in
  new|slide|chart|table|fact|quote|section) ;;
  export)
    [[ ${2:-} == *.pptx ]] || { print -u2 'Give a file name ending in .pptx, for example: deck.sh export "Name.pptx"'; exit 1; }
    osascript - "$2" <<'APPLESCRIPT'
on run {fileName}
  set target to (POSIX path of (path to documents folder)) & fileName
  tell application id "com.apple.Keynote"
    if (count of documents) is 0 then error "No deck is open."
    export front document to POSIX file target as Microsoft PowerPoint
    return "Saved " & (count of slides of front document) & " slides as " & target
  end tell
end run
APPLESCRIPT
    exit ;;
  *) print -u2 $usage; exit 1 ;;
esac
[[ $action == chart && $kind != (bar|line|pie) ]] && { print -u2 "Chart type: bar, line or pie."; exit 1; }

# Title, content lines and speaker notes.
lines=("${(@f)$(cat)}")
title=${lines[1]:-}
content=(); spoken=(); in_notes=0
for line in "${(@)lines[2,-1]}"; do
  # The notes separator, also when the model decorates it (#---#, **---**).
  if (( ! in_notes )) && [[ $line == [[:space:]#*_]#---##[[:space:]#*_]# ]]; then in_notes=1
  elif (( in_notes )); then spoken+=("$line")
  elif [[ -n ${line//[[:space:]]/} ]]; then content+=("$line")
  fi
done
notes=${(F)spoken}
[[ -n ${title//[[:space:]]/} ]] || { print -u2 "The first line must be the title."; exit 1; }

# A row "| a | b |" becomes tab-separated cells; a markdown rule row "|---|---|" is skipped.
cells() {
  local row=${1##[[:space:]]#\|}; row=${row%%\|[[:space:]]#}
  local -a parts=("${(@s:|:)row}")
  local out=() part
  for part in "${parts[@]}"; do part=${part##[[:space:]]#}; out+=("${part%%[[:space:]]#}"); done
  print -r -- "${(pj:\t:)out}"
}
rows=()
if [[ $action == (chart|table) ]]; then
  for line in "${content[@]}"; do
    [[ $line == [[:space:]\|:-]# ]] && continue
    [[ $line == *\|* ]] || { print -u2 "Write each row with | between the cells, for example: | CD | 104000 | 121665 |"; exit 1; }
    rows+=("$(cells "$line")")
  done
  (( ${#rows} >= 2 )) || { print -u2 "Give a header row and at least one data row, with | between the cells."; exit 1; }
fi
if [[ $action == chart ]]; then
  header_cells=("${(@ps:\t:)rows[1]}")
  for (( i = 2; i <= ${#rows}; i++ )); do
    values=("${(@ps:\t:)rows[i]}")
    (( ${#values} == ${#header_cells} )) || { print -u2 "Row ${i} has $(( ${#values} - 1 )) numbers but the header row has $(( ${#header_cells} - 1 )) categories: give one number per category."; exit 1; }
    for value in "${(@)values[2,-1]}"; do
      number=${value//[\$€£%,[:space:]]/}
      [[ $number == (-|)[0-9]#(.[0-9]##|)  && -n $number ]] || { print -u2 "Not a number in row ${i}: $value"; exit 1; }
    done
  done
fi
# Keynote draws the bullets: a marker the model wrote would show twice.
bullets=()
for line in "${content[@]}"; do bullets+=("${line##[[:space:]]#[•*-][[:space:]]#}"); done
body=${(F)bullets}
[[ $action == (chart|table) ]] && body=${(F)rows}

osascript - "$action" "$kind" "$title" "$body" "$notes" <<'APPLESCRIPT'
on split(theText, delimiter)
  set saved to AppleScript's text item delimiters
  set AppleScript's text item delimiters to delimiter
  set parts to text items of theText
  set AppleScript's text item delimiters to saved
  return parts
end split

-- "$1,234.5" or "7%" as a number, whatever the decimal separator of the system language.
on number_of(value)
  set whole to 0
  set fraction to 0
  set scale to 1
  set negative to false
  set inFraction to false
  repeat with ch in (characters of value)
    set ch to ch as text
    if ch is "-" then
      set negative to true
    else if ch is "." then
      set inFraction to true
    else if ch is in "0123456789" then
      if inFraction then
        set fraction to fraction * 10 + (ch as integer)
        set scale to scale * 10
      else
        set whole to whole * 10 + (ch as integer)
      end if
    end if
  end repeat
  set amount to whole + fraction / scale
  if negative then set amount to -amount
  return amount
end number_of

-- Fill the placeholder text items, then remove the ones left empty.
on fill(s, mainText, secondText)
  tell application id "com.apple.Keynote"
    tell s
      if title showing then set object text of default title item to mainText
      if body showing and secondText is not missing value then
        if title showing then
          set object text of default body item to secondText
        else
          set object text of default body item to mainText
          repeat with ti in text items
            try
              if object text of ti is "" and width of ti > 0 then
                set object text of ti to secondText
                -- The theme draws a fact's caption and a quote's source very small.
                if size of object text of ti < 30 then set size of object text of ti to 30
                exit repeat
              end if
            end try
          end repeat
        end if
      end if
      repeat with i from (count of text items) to 1 by -1
        try
          if object text of text item i is "" then delete text item i
        end try
      end repeat
    end tell
  end tell
end fill

on run {action, kind, slideTitle, body, notes}
  set seriesColors to {{71, 159, 248}, {129, 214, 83}, {146, 146, 146}, {239, 189, 64}}
  tell application id "com.apple.Keynote"
    if action is "new" then
      activate
      try
        set d to make new document with properties {document theme:theme 14, width:1920, height:1080}
      on error
        set d to make new document with properties {document theme:theme 1, width:1920, height:1080}
      end try
      my fill(slide 1 of d, slideTitle, body)
      return "New deck created with its title slide: " & slideTitle
    end if
    if (count of documents) is 0 then error "No deck is open: run deck.sh new first."
    set d to front document
    set W to width of d
    set H to height of d
    -- Theme 14's layouts: 4 title and bullets, 9 section, 10 title only, 13 big fact, 14 quote.
    set layoutNumber to 4
    if action is "section" then set layoutNumber to 9
    if action is in {"chart", "table"} then set layoutNumber to 10
    if action is "fact" then set layoutNumber to 13
    if action is "quote" then set layoutNumber to 14
    tell d to set s to make new slide with properties {base layout:slide layout layoutNumber}
    if action is in {"slide", "fact", "quote"} then
      my fill(s, slideTitle, body)
    else
      my fill(s, slideTitle, missing value)
    end if
    set contentTop to H * 0.2
    try
      set t to default title item of s
      if title showing of s then set contentTop to (item 2 of (position of t as list)) + (height of t) + 30
    end try
    try
    if action is in {"chart", "table"} then
      set rowTexts to my split(body, linefeed)
      set headerCells to my split(item 1 of rowTexts, tab)
    end if
    if action is "chart" then
      set categories to rest of headerCells
      set seriesNames to {}
      set seriesValues to {}
      repeat with r in rest of rowTexts
        set cellsOfRow to my split(r, tab)
        set end of seriesNames to item 1 of cellsOfRow
        set rowNumbers to {}
        repeat with v in rest of cellsOfRow
          set end of rowNumbers to my number_of(v as text)
        end repeat
        set end of seriesValues to rowNumbers
      end repeat
      -- One row per year and a column per product: series and categories are swapped.
      if (count of seriesNames) > 4 and (count of categories) ≤ 4 then
        set swappedValues to {}
        repeat with k from 1 to count of categories
          set valuesOfCategory to {}
          repeat with r from 1 to count of seriesNames
            set end of valuesOfCategory to item k of item r of seriesValues
          end repeat
          set end of swappedValues to valuesOfCategory
        end repeat
        set {seriesNames, categories, seriesValues} to {categories, seriesNames, swappedValues}
      end if
      tell s
        if kind is "line" then
          add chart row names seriesNames column names categories data seriesValues type line_2d group by chart row
        else if kind is "pie" then
          add chart row names seriesNames column names categories data seriesValues type pie_2d group by chart row
        else
          add chart row names seriesNames column names categories data seriesValues type vertical_bar_2d group by chart row
        end if
      end tell
      set c to chart 1 of s
      -- The category labels sit below the chart's frame: leave them room above the legend.
      set legendTop to H - H * 0.1
      set chartHeight to legendTop - contentTop - 90
      if kind is "pie" then
        set position of c to {(W - chartHeight) / 2, contentTop}
        set width of c to chartHeight
      else
        set position of c to {W * 0.11, contentTop}
        set width of c to W * 0.82
      end if
      set height of c to chartHeight
      -- Keynote's AppleScript cannot show a legendText: write one in the series seriesColors.
      set legendText to ""
      repeat with n in seriesNames
        set legendText to legendText & "●  " & n & "        "
      end repeat
      tell s to set lg to make new text item with properties {object text:legendText}
      set size of object text of lg to 34
      set width of lg to W * 0.82
      set position of lg to {W * 0.09, legendTop}
      set charPos to 1
      repeat with i from 1 to count of seriesNames
        set {cr, cg, cb} to item (((i - 1) mod 4) + 1) of seriesColors
        set color of character charPos of object text of lg to {cr * 257, cg * 257, cb * 257}
        set charPos to charPos + 3 + (length of (item i of seriesNames)) + 8
      end repeat
    else if action is "table" then
      set rowCount to count of rowTexts
      set columnCount to count of headerCells
      tell s to set tb to make new table with properties {row count:rowCount, column count:columnCount, header row count:1, header column count:1}
      repeat with r from 1 to rowCount
        set rowCells to my split(item r of rowTexts, tab)
        repeat with k from 1 to columnCount
          set cellText to ""
          if k ≤ (count of rowCells) then set cellText to item k of rowCells
          set value of cell k of row r of tb to cellText
          set font size of cell k of row r of tb to 30
        end repeat
      end repeat
      set position of tb to {W * 0.06, contentTop}
      set width of tb to W * 0.88
    end if
    on error message
      -- Never leave an empty slide behind: remove it and report why.
      delete s
      error message
    end try
    set presenter notes of s to notes
    set current slide of d to s
    set summary to "Slide " & (count of slides of d) & " added (" & action & "): " & slideTitle
    -- The model reads this summary after every slide: remind it of the charts and tables a deck needs.
    if (count of slides of d) ≥ 3 then
      set chartCount to 0
      set tableCount to 0
      repeat with oneSlide in slides of d
        set chartCount to chartCount + (count of charts of oneSlide)
        set tableCount to tableCount + (count of tables of oneSlide)
      end repeat
      if chartCount is 0 then set summary to summary & linefeed & "This deck has no chart yet: add one with deck.sh chart bar, line or pie for the figures from your sources."
      if tableCount is 0 then set summary to summary & linefeed & "This deck has no table yet: add one with deck.sh table to compare the options side by side."
    end if
    return summary
  end tell
end run
APPLESCRIPT
