#!/bin/zsh -f
# Build a Keynote deck one step at a time, so the user sees it appear.
#   deck.sh new     <<'EOF'  title, then subtitle lines                 EOF
#   deck.sh slide   <<'EOF'  title, then one bullet per line, then ---, then speaker notes  EOF
#   deck.sh export  "Name.pptx"   (saved as PowerPoint in ~/Documents)
# Text comes on standard input through a quoted heredoc, so quotes, apostrophes and $ need no escaping.
set -u
setopt extended_glob
action=${1:-}

case $action in
  new|slide)
    lines=("${(@f)$(cat)}")
    title=${lines[1]:-}
    bullets=(); spoken=(); in_notes=0
    for line in "${(@)lines[2,-1]}"; do
      if (( ! in_notes )) && [[ $line == --- ]]; then in_notes=1
      elif (( in_notes )); then spoken+=("$line")
      # Keynote draws the bullets: a marker the model wrote would show twice.
      elif [[ -n ${line//[[:space:]]/} ]]; then bullets+=("${line##[[:space:]]#[•*-][[:space:]]#}")
      fi
    done
    body=${(F)bullets}; notes=${(F)spoken}
    [[ -n ${title//[[:space:]]/} ]] || { print -u2 "The first line must be the title."; exit 1; }
    ;;
  export)
    name=${2:-}
    [[ $name == *.pptx ]] || { print -u2 'Give a file name ending in .pptx, for example: deck.sh export "Name.pptx"'; exit 1; }
    ;;
  *)
    print -u2 "Usage: deck.sh new|slide (text on standard input) or deck.sh export \"Name.pptx\""; exit 1 ;;
esac

case $action in
  new) osascript - "$title" "$body" <<'APPLESCRIPT'
on run {deckTitle, subtitle}
  tell application id "com.apple.Keynote"
    activate
    try
      set d to make new document with properties {document theme:theme 14}
    on error
      set d to make new document with properties {document theme:theme 1}
    end try
    tell d
      set object text of default title item of slide 1 to deckTitle
      set object text of default body item of slide 1 to subtitle
    end tell
  end tell
  return "New deck created with its title slide: " & deckTitle
end run
APPLESCRIPT
  ;;
  slide) osascript - "$title" "$body" "$notes" <<'APPLESCRIPT'
on run {slideTitle, bullets, notes}
  tell application id "com.apple.Keynote"
    if (count of documents) is 0 then error "No deck is open: run deck.sh new first."
    tell front document
      set s to make new slide with properties {base layout:slide layout 4}
      set object text of default title item of s to slideTitle
      set object text of default body item of s to bullets
      set presenter notes of s to notes
      repeat with i from (count of text items of s) to 1 by -1
        if object text of text item i of s is "" then delete text item i of s
      end repeat
      set current slide to s
      return "Slide " & (count of slides) & " added: " & slideTitle
    end tell
  end tell
end run
APPLESCRIPT
  ;;
  export) osascript - "$name" <<'APPLESCRIPT'
on run {fileName}
  set target to (POSIX path of (path to documents folder)) & fileName
  tell application id "com.apple.Keynote"
    if (count of documents) is 0 then error "No deck is open."
    export front document to POSIX file target as Microsoft PowerPoint
    return "Saved " & (count of slides of front document) & " slides as " & target
  end tell
end run
APPLESCRIPT
  ;;
esac
