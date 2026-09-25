"""Collection ownership across startup, sync, recovery and shutdown.

The UI reserves a sync until its result is dismissed. Blocking transfers run
off-thread; shutdown never waits for a network transfer or reopens afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Event, Lock
from typing import Protocol

from .backend import BackendError
from .config import ProfilePaths
from .sync import (
    FullSyncDirection,
    SyncOutcome,
    SyncStatus,
    failed_sync_outcome,
    full_sync_profile,
    sync_profile,
)


class CollectionBackend(Protocol):
    @property
    def is_open(self) -> bool: ...

    def open(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class SyncRunResult:
    outcome: SyncOutcome
    reopen_error: str | None = None


class CollectionLifecycle:
    """Serialize ownership while leaving transfer and UI policy injectable."""

    def __init__(
        self,
        backend: CollectionBackend,
        profile: ProfilePaths,
        syncer: Callable[[ProfilePaths], SyncOutcome] = sync_profile,
        full_syncer: Callable[[ProfilePaths, FullSyncDirection], SyncOutcome] = full_sync_profile,
    ) -> None:
        self._backend = backend
        self._profile = profile
        self._syncer = syncer
        self._full_syncer = full_syncer
        self._lock = Lock()
        self._transfer = Lock()
        self._stopped = Event()
        self._busy = False

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def busy(self) -> bool:
        return self._busy

    def open(self, cancelled: Callable[[], bool] = lambda: False) -> bool:
        """Acquire for startup/recovery, unless cancelled or reserved by sync."""
        with self._lock:
            if self.stopped or self._busy or cancelled():
                return False
            self._backend.open()
            if self.stopped or cancelled():
                self._backend.close()
                return False
            return True

    def begin_sync(self) -> bool:
        """Reserve ownership before the UI yields to mount its sync popup."""
        with self._lock:
            if self.stopped or self._busy:
                return False
            self._busy = True
            return True

    def finish_sync(self) -> None:
        """Release the UI reservation after the worker and popup finish."""
        with self._lock:
            if self._transfer.locked():
                return
            self._busy = False

    def sync(self, direction: FullSyncDirection | None = None) -> SyncRunResult:
        """Close, transfer and reopen once; never transfer after failed close."""
        if not self._transfer.acquire(blocking=False):
            return SyncRunResult(SyncOutcome(SyncStatus.FAILED, "Sync already running."))
        try:
            with self._lock:
                if self.stopped or not self._busy:
                    return SyncRunResult(SyncOutcome(SyncStatus.FAILED, "Sync cancelled."))
                try:
                    self._backend.close()
                except Exception as exc:
                    return SyncRunResult(
                        SyncOutcome(SyncStatus.COLLECTION_UNAVAILABLE, str(exc)),
                        None if self._backend.is_open else str(exc),
                    )
            try:
                outcome = (
                    self._syncer(self._profile)
                    if direction is None
                    else self._full_syncer(self._profile, direction)
                )
            except Exception as exc:
                outcome = failed_sync_outcome(exc)
            reopen_error = None
            with self._lock:
                if not self.stopped:
                    try:
                        self._backend.open()
                    except Exception as exc:
                        reopen_error = str(exc)
            return SyncRunResult(outcome, reopen_error)
        finally:
            self._transfer.release()

    def shutdown(self) -> None:
        """Stop acquisition permanently and best-effort release local ownership."""
        self._stopped.set()
        self.release()

    def release(self) -> None:
        """Best-effort release after cancelled acquisition or final shutdown."""
        # The transfer owns its own connection; do not wait for the network.
        with self._lock, suppress(BackendError):
            self._backend.close()
