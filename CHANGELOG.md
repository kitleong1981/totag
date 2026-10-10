# ToTag Changelog

## v3.6.0 — 2026-10-10

### Context-hint history chips
- Previously used context hints now appear as clickable chips under the
  Context Hint field (stored in browser localStorage, most recent 15).
  Clicking a chip appends it to the field; each chip can be removed with ×.
- Multi-term entries are split into individual chips.

### Star-button enable logic
- The generate (★) button is now disabled only when every selected file is
  already in an active generation job. Previously any overlap disabled it,
  which could lock the button while a long queue was still running even when
  the selection contained fresh files.

### Queued-file indicator
- Thumbnails for files waiting in an active generation job now show a
  `QUEUED` badge with a dashed border, distinct from the spinner on the file
  currently being processed.

## v3.5.0
- Adobe Stock CSV export.

## v3.4.1
- Upload jobs back to serial (one at a time).
