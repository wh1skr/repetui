# repetui

`repetui` is a small, unofficial terminal interface for reviewing an existing
Anki collection.

> Early development: the interface and installation flow may change before the
> first public release.

## MVP

- Browse decks and current due counts.
- Review cards with Anki's Again, Hard, Good, and Easy ratings.
- Show, fold, or hide answer sections per card template.
- Scroll long cards without a mouse.
- Synchronize review progress with AnkiWeb.

Card creation, editing, statistics, and media playback are deliberately outside
the first release.

## Development

Anki Desktop must already be installed, signed in, and synchronized once. Close
Anki Desktop before opening `repetui`, because both applications use the same
collection database.

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
