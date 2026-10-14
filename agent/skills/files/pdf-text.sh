#!/bin/zsh -f
# Print the text of a PDF, a few pages at a time (read cannot open PDFs):
#   pdf-text.sh "/path/file.pdf"            pages 1 to 2
#   pdf-text.sh "/path/file.pdf" 3 4        pages 3 to 4
# A command's output is cut after about 6000 characters: read two pages at a time.
set -u
file=${1:-}
[[ -f $file ]] || { print -u2 "No such PDF: $file"; exit 1; }
osascript -l JavaScript - "$file" "${2:-1}" "${3:-}" <<'JAVASCRIPT'
ObjC.import('PDFKit')
function run(argv) {
  const document = $.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]))
  if (document.isNil()) return 'This file could not be opened as a PDF.'
  const count = document.pageCount
  const first = Math.max(1, parseInt(argv[1]) || 1)
  const last = Math.min(count, parseInt(argv[2]) || first + 1)
  const parts = [`${argv[0].split('/').pop()}: ${count} pages; pages ${first} to ${last} below.`]
  for (let number = first; number <= last; number++) {
    const text = ObjC.unwrap(document.pageAtIndex(number - 1).string) || ''
    parts.push(`--- Page ${number} ---`, text.trim() || '(This page has no text layer: it may be an image.)')
  }
  if (last < count) parts.push(`--- More: pdf-text.sh with ${last + 1} ${Math.min(count, last + 2)} ---`)
  return parts.join('\n')
}
JAVASCRIPT
