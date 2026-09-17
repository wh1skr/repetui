# repetui

## Product

**repetui** is an unofficial terminal user interface for reviewing an existing
Anki collection without leaving the terminal.

The name combines **repetition** with **TUI**. It is written as `repetui` in
product text, repository names, packages, and commands.

## First public release

The first release proves one complete daily-study loop:

1. View decks and their due counts.
2. Choose a deck and review its due cards.
3. Reveal each answer and rate it using Anki's review choices.
4. Synchronize the resulting review progress.

The first release does not create or edit notes, manage card templates, show
statistics, or attempt to reproduce the full Anki desktop application.

## Product language

- **Collection**: the user's existing Anki study data.
- **Deck**: a selectable group of cards within the collection.
- **Due count**: the number of cards currently available to study.
- **Review session**: the focused loop for one selected deck.
- **Reveal**: changing the current card from its question to its answer.
- **Rating**: the user's Again, Hard, Good, or Easy response after revealing.
- **Sync**: reconciling local study progress with the user's Anki account.

## Identity boundary

repetui must have its own visual language and original ASCII artwork. It may
describe itself as compatible with Anki, but must not present itself as an
official Anki product or reuse Anki's logo as its own identity.

## Design language

repetui is a simple, functional terminal tool with a small human touch. Its
interface should feel calm and intentional rather than decorative or sterile.

- Information and the current review action take visual priority.
- Space and restrained borders provide structure without crowding the card.
- Colour communicates state; it is not general decoration.
- Warm, conversational wording provides personality without becoming cute or
  distracting.
- Original ASCII artwork belongs at meaningful moments such as startup and an
  empty review queue, not throughout the working interface.

### Review Flow

Review is small-pane-first. The prompt starts at the first terminal cell and
the revealed answer follows immediately below it; no border, logo, margin, or
permanent help line competes with card content. Ratings consume one row only
after reveal.

- Card content always wraps and remains scrollable.
- Ruby and Japanese bracket furigana are metadata on the base text, revealed
  on terminal mouse hover. `r` opens a keyboard reading list without exposing
  hidden or unrevealed answer sections.
- Metadata disappears in this order as width shrinks: deck, template name,
  then total due. The coloured split stays on row one whenever it physically
  fits, and prompt content then wraps below without truncation.
- Every detected answer section is shown by default.
- A user may explicitly show, fold, or hide sections for one stable Anki note
  type and card template.
- Opening a fold during a review is temporary and never changes its saved mode.
- If a script-heavy card cannot be separated confidently, review falls back to
  source fields that are demonstrably present on the rendered card. A one-time
  setup assigns ordered Prompt, Answer, Auto, or Ignore roles without executing
  card JavaScript or adding note-type-specific rules. Auto fields appear only
  when their content is detected on that card, allowing optional fields to
  evolve without another configuration pass. Field roles and order update an
  unsaved preview through the same presentation and Flow composition modules
  as review. Narrow panes toggle between fields and preview; wide panes show
  both. A separate explicit Suggest layout action compares several cards of
  the same template and proposes only field roles supported by repeated
  rendered-side evidence. Repeated instructions remain Auto when a varying
  prompt is available; separately styled prompt and answer fields stay distinct
  only with side-specific markup evidence. Ambiguous fields remain Auto, and
  low-confidence results do not overwrite the draft. A narrow, non-executing
  HTML/CSS subset preserves inline underlines in the terminal without trying
  to reproduce Anki's full browser styling. Save applies the draft; Escape
  discards it.

### Settings

`?` opens one full-screen, small-pane-safe settings surface from decks or
review. Help and profile-scoped review controls are always available. Sections
configures the active card template during review and otherwise explains that
a card must be opened first. Cards with usable source fields expose an Edit
fields row, providing a manual escape hatch even when automatic detection is
not needed or cannot recognize the template; the mapping persists per note
type and card template. Controls also exposes a semantic Action feedback
duration for successful review operations. Errors keep a readable duration
regardless of that choice. Enter on successful feedback continues the
underlying primary review action once; Escape dismisses without forwarding.
`h`/`l` and `Tab` move between tabs.

### Deck Tree

The main menu is a compact tree rather than a flat deck path list. Parent decks
start collapsed for each profile, and expanded parent IDs persist by stable
Anki deck ID. `Tab` toggles the selected parent without moving the selection;
leaf decks give only a brief selection flash. Depth uses a spaced `>` trail,
with `▸` and `▾` indicating collapsed and expanded parents. Counts degrade
before deck identity when the terminal narrows.
