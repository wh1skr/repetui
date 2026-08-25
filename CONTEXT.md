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
- Metadata disappears in this order as width shrinks: deck, split counts,
  total due, then template name. Prompt content then wraps without truncation.
- Every detected answer section is shown by default.
- A user may explicitly show, fold, or hide sections for one stable Anki note
  type and card template.
- Opening a fold during a review is temporary and never changes its saved mode.
