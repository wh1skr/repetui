# Changelog

Brief user-visible updates to repetui are recorded here.

## Unreleased

## 0.1.7 - 2026-09-25

Maintenance release: bug fixes and internal cleanup.

- Fixed review durations recorded as nearly zero by starting Anki's timer when
  a card is presented.
- Fixed sync skipping media when cards were already up to date. A failed
  collection close now blocks transfer, and reopen failures are shown clearly.
- Fixed a crash when full sync removes the active deck; review returns to the
  refreshed deck list.
- Kept active layout, section, add-on, and deck expansion settings unchanged
  when saving fails. Layout, section, and deck errors have retryable UI feedback.
- Preserved underlines and furigana through repeated text, whitespace
  normalization, compact and multiline cards. `r` now toggles bracketed
  readings inline on the current card.
- Consolidated collection lifecycle, card-text transformations, and preference
  persistence in focused modules. No preference migration is needed.

## 0.1.6b - 2026-09-16

- Prevented review-screen timers and resizes from reading the Anki collection
  while sync has it closed.
- Prevented internal widget names from flashing while deck and settings rows
  rebuild, including immediately after sync.
- Returned directly to the refreshed deck list after the completion celebration,
  avoiding a second finished-review screen.
- Added profile-specific Instant, Brief, Normal, and Relaxed feedback timing,
  with Enter able to continue immediately after successful review actions.

## 0.1.6a - 2026-09-15

- Added adaptive field profiles for script-heavy card templates, including a
  one-time 40×6-safe setup and persistent per-template field ordering.

## 0.1.3 - 2026-08-27

- Unified Help, Controls, and Sections in one settings screen available from decks and review.
- Refreshed deck due counts immediately after syncing from an active or completed review.
- Kept the coloured review counts stationary while revealing scrollable answers.

## 0.1.2 - 2026-08-26

- Replaced generic sync notifications with a compact, terminal-native status popup.
- Added undo, bury, suspend, and flag controls to the review flow.
- Kept due counts pinned to the first review row and underlined the current card's queue.

## 0.1.1 - 2026-08-26

- Added a compact, collapsible deck tree whose state persists per Anki profile.
- Added the current version to the deck screen and terminal title.

## 0.1.0 - 2026-08-25

- Shipped the first small-pane review MVP.
