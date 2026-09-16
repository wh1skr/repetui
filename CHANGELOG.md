# Changelog

Brief user-visible updates to repetui are recorded here.

## Unreleased

## 0.1.6b - 2026-09-16

- Prevented review-screen timers and resizes from reading the Anki collection
  while sync has it closed.
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
