"""Observe Anki's background media transfer on the review-owned collection."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Thread
from typing import Any

from .config import ProfilePaths
from .sync import SyncStatus, _auth, failed_sync_outcome


class MediaSyncStatus(str, Enum):
    STARTING = "starting"
    ACTIVE = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MediaSyncSnapshot:
    status: MediaSyncStatus
    checked: str = ""
    added: str = ""
    removed: str = ""
    failure: SyncStatus | None = None

    @property
    def active(self) -> bool:
        return self.status in {MediaSyncStatus.STARTING, MediaSyncStatus.ACTIVE}


class MediaSyncTask:
    """Keep one captured collection alive; publish counts and terminal state."""

    def __init__(
        self,
        collection: Any,
        profile: ProfilePaths,
        report: Callable[[MediaSyncTask, MediaSyncSnapshot], None],
        *,
        auth_factory: Callable[[ProfilePaths], Any] = _auth,
        poll_interval: float = 0.25,
        endpoint: str | None = None,
    ) -> None:
        self._collection = collection
        self._profile = profile
        self._report = report
        self._auth_factory = auth_factory
        self._poll_interval = poll_interval
        self._endpoint = endpoint
        self._cancelled = Event()
        self._start_lock = Lock()
        self._started = False
        self.done = Event()
        self.snapshot = MediaSyncSnapshot(MediaSyncStatus.STARTING)
        self._thread: Thread | None = None

    @property
    def active(self) -> bool:
        return self.snapshot.active

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name="repetui-media", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """Interrupt media before its owning collection is closed/replaced."""
        with self._start_lock:
            if self.done.is_set():
                return
            self._cancelled.set()
            if self._started:
                with suppress(Exception):
                    self._collection.abort_media_sync()

    def _publish(self, snapshot: MediaSyncSnapshot) -> None:
        if snapshot == self.snapshot:
            return
        self.snapshot = snapshot
        # The UI may already be shutting down. A closed UI must not own media.
        with suppress(RuntimeError):
            self._report(self, snapshot)

    def _run(self) -> None:
        try:
            auth = self._auth_factory(self._profile)
            if self._endpoint is not None:
                auth.endpoint = self._endpoint
            # Native start returns immediately. Serialize it with cancellation
            # so an old task can never start or abort a replacement transfer.
            with self._start_lock:
                if self._cancelled.is_set():
                    return
                self._collection.sync_media(auth)
                self._started = True
            while not self._cancelled.is_set():
                response = self._collection.media_sync_status()
                progress = response.progress
                self._publish(MediaSyncSnapshot(
                    MediaSyncStatus.ACTIVE if response.active else MediaSyncStatus.COMPLETE,
                    checked=progress.checked,
                    added=progress.added,
                    removed=progress.removed,
                ))
                if not response.active:
                    return
                self._cancelled.wait(self._poll_interval)
        except Exception as exc:
            if not self._cancelled.is_set():
                self._publish(MediaSyncSnapshot(
                    MediaSyncStatus.FAILED,
                    checked=self.snapshot.checked,
                    added=self.snapshot.added,
                    removed=self.snapshot.removed,
                    failure=failed_sync_outcome(exc).status,
                ))
        finally:
            if self._cancelled.is_set():
                self._publish(MediaSyncSnapshot(MediaSyncStatus.CANCELLED))
            self.done.set()
