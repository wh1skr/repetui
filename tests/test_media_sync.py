from pathlib import Path
from threading import Event
from types import SimpleNamespace
from weakref import ref

import pytest
from anki._backend import RustBackend, Translations
from anki.sync_pb2 import MediaSyncProgress, MediaSyncStatusResponse

from repetui.config import ProfilePaths
from repetui.media_sync import MediaSyncSnapshot, MediaSyncStatus, MediaSyncTask
from repetui.sync import SyncStatus


@pytest.mark.parametrize("language", ["en", "ja", "de", "ar"])
def test_download_detection_uses_native_localized_direction_counts(language):
    backend = RustBackend(langs=[language])
    translations = Translations(ref(backend))
    for uploaded, downloaded in ((0, 0), (12, 0), (0, 1), (12, 1234)):
        added = translations.sync_media_added_count(up=str(uploaded), down=str(downloaded))
        snapshot = MediaSyncSnapshot(MediaSyncStatus.ACTIVE, added=added)
        assert snapshot.has_downloads is (downloaded > 0)
    assert not MediaSyncSnapshot(MediaSyncStatus.STARTING).has_downloads
    assert not MediaSyncSnapshot(MediaSyncStatus.ACTIVE, added="Added: 12").has_downloads
    assert MediaSyncSnapshot(MediaSyncStatus.COMPLETE, added="Added: 0↑ 1↓").has_downloads


class BackgroundCollection:
    def __init__(self):
        self.started = Event()
        self.finish = Event()
        self.abort_calls = 0
        self.status_calls = 0
        self.fail = False

    def sync_media(self, auth):
        self.auth = auth
        self.started.set()

    def media_sync_status(self):
        self.status_calls += 1
        if self.fail:
            raise ConnectionError("private background connection failure")
        return MediaSyncStatusResponse(
            active=not self.finish.is_set(),
            progress=MediaSyncProgress(added="12", checked="50", removed="1"),
        )

    def abort_media_sync(self):
        self.abort_calls += 1
        self.finish.set()

    def card_count(self):
        return 17


def task(collection, report=lambda *_: None, auth_factory=lambda _: object()):
    profile = ProfilePaths(Path("/tmp"), "fixture", Path("/tmp/unused.anki2"))
    return MediaSyncTask(collection, profile, report, auth_factory=auth_factory, poll_interval=0.01)


def test_background_media_reports_counts_while_collection_remains_usable():
    collection = BackgroundCollection()
    reports = []
    progress_ready = Event()

    def report(worker, snapshot):
        reports.append(snapshot)
        if snapshot.status is MediaSyncStatus.ACTIVE:
            progress_ready.set()

    worker = task(collection, report)
    try:
        worker.start()
        assert progress_ready.wait(1)
        assert worker.active
        assert collection.card_count() == 17
        assert worker.snapshot.added == "12"
        assert worker.snapshot.checked == "50"
        collection.finish.set()
        assert worker.done.wait(1)
        assert worker.snapshot.status is MediaSyncStatus.COMPLETE
        assert not worker.active
        assert collection.abort_calls == 0
    finally:
        worker.cancel()
        assert worker.done.wait(1)


def test_delayed_media_failure_preserves_counts_and_does_not_claim_complete():
    collection = BackgroundCollection()
    ready = Event()
    worker = task(collection, lambda *_: ready.set())
    try:
        worker.start()
        assert ready.wait(1)
        collection.fail = True
        assert worker.done.wait(1)
        assert worker.snapshot.status is MediaSyncStatus.FAILED
        assert worker.snapshot.failure is SyncStatus.OFFLINE
        assert worker.snapshot.added == "12"
        assert worker.snapshot.checked == "50"
        assert not worker.active
    finally:
        worker.cancel()


def test_cancel_during_auth_prevents_late_transfer_start():
    collection = BackgroundCollection()
    auth_started, release_auth = Event(), Event()

    def auth(_):
        auth_started.set()
        assert release_auth.wait(1)
        return object()

    worker = task(collection, auth_factory=auth)
    worker.start()
    try:
        assert auth_started.wait(1)
        worker.cancel()
    finally:
        release_auth.set()
        assert worker.done.wait(1)
    assert not collection.started.is_set()
    assert worker.snapshot.status is MediaSyncStatus.CANCELLED


def test_cancel_interrupts_active_media_without_waiting_for_network():
    collection = BackgroundCollection()
    worker = task(collection)
    worker.start()
    assert collection.started.wait(1)
    worker.cancel()
    assert worker.done.wait(1)
    assert collection.abort_calls >= 1
    assert worker.snapshot.status is MediaSyncStatus.CANCELLED


def test_media_uses_endpoint_resolved_by_collection_sync():
    collection = BackgroundCollection()
    collection.finish.set()
    auth = SimpleNamespace(endpoint="http://old.invalid/")
    profile = ProfilePaths(Path("/tmp"), "fixture", Path("/tmp/unused.anki2"))
    worker = MediaSyncTask(
        collection, profile, lambda *_: None, auth_factory=lambda _: auth,
        endpoint="http://redirected.invalid/",
    )
    worker.start()
    assert worker.done.wait(1)
    assert collection.auth.endpoint == "http://redirected.invalid/"
    assert worker.snapshot.status is MediaSyncStatus.COMPLETE


def test_cancelled_old_task_cannot_abort_its_replacement_transfer():
    collection = BackgroundCollection()
    auth_started, release_auth = Event(), Event()

    def held_auth(_):
        auth_started.set()
        assert release_auth.wait(1)
        return object()

    old = task(collection, auth_factory=held_auth)
    new = task(collection)
    old.start()
    try:
        assert auth_started.wait(1)
        old.cancel()
        new.start()
        assert collection.started.wait(1)
        release_auth.set()
        assert old.done.wait(1)
        assert collection.abort_calls == 0
        assert new.active
    finally:
        release_auth.set()
        new.cancel()
        assert old.done.wait(1)
        assert new.done.wait(1)
