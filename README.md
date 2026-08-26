# repetui

`repetui` is a small, unofficial terminal interface for reviewing an existing
Anki collection.

> Early development: the interface and installation flow may change before the
> first public release.

See the [changelog](CHANGELOG.md) for brief release updates.

## MVP

- Browse decks and current due counts in panes as small as roughly 40×6.
- Review cards with Anki's Again, Hard, Good, and Easy ratings.
- Show, fold, or hide answer sections per card template.
- Scroll long cards without a mouse.
- Synchronize review progress with AnkiWeb.

Card creation, editing, statistics, and media playback are deliberately outside
the first release.

## Terminal-first interface

The deck list is a persistent compact tree. Parent decks start collapsed; use
`Tab` to expand or collapse the selected parent, `j`/`k` to move, and `Enter`
to review either a parent or leaf. Each row keeps its total due count and the
familiar coloured New/Learning/Review split. As a pane narrows, the split
disappears before the total so the selected deck identity remains useful.

During review, `Enter` reveals a card and then answers Good. Use `1`–`4` for
Again, Hard, Good, and Easy; `j`/`k` and `g`/`G` scroll long cards. Press `?` on
a card for its full-screen template settings. Every answer section is shown by
default, and each section can be changed to show, fold, or hide. Press `?` from
the deck list for the complete keyboard reference.

repetui translates rendered Anki HTML into terminal-native text rather than
running a browser. It preserves ordered text, headings, ruby readings, lists,
tables, code, math labels, and media references where possible. Unknown markup
falls back to its visible text without truncating it. Template JavaScript,
typed-answer grading, CSS layout, and media playback are not executed.

## Development

Anki Desktop must already be installed, signed in, and synchronized once. Close
Anki Desktop before opening `repetui`, because both applications use the same
collection database. Press `s` from the deck list or review to sync; progress is
shown only as a temporary status message.

Install the current development release directly from GitHub:

```bash
uv tool install git+https://github.com/wh1skr/repetui
repetui
```

For local development:

```bash
uv sync --extra dev
uv run repetui
```

If more than one Anki profile exists:

```bash
uv run repetui --profile PROFILE_NAME
```

Inside the application, press `?` for help. During review it opens the current
template's section settings and keyboard reference.

## Licence and relationship to Anki

repetui is an independent, unofficial project and is not endorsed by Ankitects.
It uses Anki's AGPL-licensed backend and is therefore distributed under
AGPL-3.0-or-later. The artwork and interface are original to repetui.
