# Internal ownership rules

## Collection lifecycle

`CollectionLifecycle` owns startup/recovery acquisition, sync reservation,
close/transfer/reopen ordering, and shutdown. Its collection adapter is
`AnkiBackend`; sync callbacks are injected (native Anki in production, controlled
transfers in tests). It has no Textual dependency.

- Reserve sync before yielding to mount the popup. Keep the reservation until
  the result is dismissed, including a full-sync confirmation/retry.
- A failed close prevents transfer. Never reacquire a collection still owned.
- Only one transfer can run. Reopen after transfer success or failure unless shutting
  down. Transfer outcome and reopen failure are separate results.
- Shutdown does not wait for a network transfer; that transfer owns a separate
  connection and must close it itself. No acquisition is allowed after shutdown.
- Screens own interaction, explicit destructive-sync consent and refreshing
  their view from current backend state, not locks or connection ordering.

`sync.py` continues to own authentication, backups, AnkiWeb calls and transfer
connection cleanup. `backend.py` continues to own Anki review semantics.

## Card text

`card_text.py` transforms Rich Text with annotations attached. Normalization,
placeholder substitution, slicing and reading extraction preserve source
positions; no text search is used to recover annotation positions.

`presentation.py` interprets safe HTML and detects semantic sections/field roles.
It exports immutable presentation values. Text searches there identify content,
not annotations. `flow.py` applies layout policy and inserts visible readings
before measuring widths. Review and preview share these modules. No JavaScript
is executed and no second renderer or note-type-specific rule is introduced.

## Preferences

Every JSON preference setter enters the private `_edit` transaction: deep-copy
the document, edit the draft, atomically replace the file, then publish the draft
as active state. An exception leaves active and saved values unchanged. This is
one internal rule behind the existing Preferences interface, not a new storage
framework. Callers report write failures and may retry.

## Verification

Tests cross module interfaces: lifecycle with a fake collection and controlled
transfers; card presentation and Flow with source fixtures; Preferences with
temporary files and failed filesystem writes. Textual tests cover visible
interaction at 40×6. None of these tests needs a live profile or AnkiWeb account.
