from pathlib import Path
from threading import Event, Thread

import pytest

from repetui.backend import AnkiBackend, BackendError
from repetui.config import ProfilePaths
from repetui.lifecycle import CollectionLifecycle
from repetui.sync import FullSyncDirection, SyncOutcome, SyncStatus


class Collection:
    def __init__(self):
        self.is_open = False
        self.events = []
        self.fail_close = False
        self.fail_open = False

    def open(self):
        self.events.append("open")
        if self.fail_open:
            raise BackendError("reopen failed")
        self.is_open = True

    def close(self):
        self.events.append("close")
        if self.fail_close:
            raise BackendError("close failed")
        self.is_open = False


def lifecycle(backend, syncer, full_syncer=None):
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/unused.anki2"))
    return CollectionLifecycle(backend, profile, syncer, full_syncer or syncer)


@pytest.mark.parametrize("direction", [None, *FullSyncDirection])
def test_sync_owns_close_transfer_reopen_and_reservation(direction):
    backend = Collection()

    def transfer(_profile, *choice):
        assert not backend.is_open
        assert choice == (() if direction is None else (direction,))
        backend.events.append("transfer")
        assert not owner.open()
        assert not owner.begin_sync()
        return SyncOutcome(SyncStatus.SYNCED)

    owner = lifecycle(backend, transfer)
    assert owner.open()
    assert owner.begin_sync()
    assert owner.sync(direction).outcome.ok
    assert backend.events == ["open", "close", "transfer", "open"]
    assert owner.busy  # Success display still owns interaction until dismissed.
    owner.finish_sync()
    assert not owner.busy
    assert owner.begin_sync()


def test_failed_close_never_transfers_or_reacquires_owned_collection():
    backend = Collection()
    owner = lifecycle(backend, lambda _: pytest.fail("must not transfer"))
    owner.open()
    backend.fail_close = True
    owner.begin_sync()
    result = owner.sync()
    assert result.outcome.status is SyncStatus.COLLECTION_UNAVAILABLE
    assert result.reopen_error is None
    assert backend.events == ["open", "close"]
    assert backend.is_open
    owner.finish_sync()
    backend.fail_close = False
    owner.shutdown()
    assert not backend.is_open


def test_transfer_exception_still_reopens_and_reports_original_failure():
    backend = Collection()

    def transfer(_):
        raise ConnectionError("offline")

    owner = lifecycle(backend, transfer)
    owner.open()
    owner.begin_sync()
    result = owner.sync()
    assert result.outcome.status is SyncStatus.OFFLINE
    assert result.reopen_error is None
    assert backend.is_open


def test_reopen_failure_preserves_transfer_outcome():
    backend = Collection()

    def transfer(_):
        backend.fail_open = True
        return SyncOutcome(SyncStatus.SYNCED)

    owner = lifecycle(backend, transfer)
    owner.open()
    owner.begin_sync()
    result = owner.sync()
    assert result.outcome.ok
    assert result.reopen_error == "reopen failed"
    assert not backend.is_open


def test_shutdown_during_transfer_is_nonblocking_and_prevents_reopen():
    backend = Collection()
    entered, release = Event(), Event()
    results = []

    def transfer(_):
        entered.set()
        assert release.wait(2)
        return SyncOutcome(SyncStatus.SYNCED)

    owner = lifecycle(backend, transfer)
    owner.open()
    owner.begin_sync()
    worker = Thread(target=lambda: results.append(owner.sync()))
    worker.start()
    try:
        assert entered.wait(1)
        assert owner.sync().outcome.status is SyncStatus.FAILED
        owner.finish_sync()
        assert owner.busy  # Cannot relinquish ownership during transfer.
        owner.shutdown()
        assert owner.stopped
        assert not owner.open()
        assert not owner.begin_sync()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert results[0].outcome.ok
    assert backend.events.count("open") == 1
    assert not backend.is_open


def test_shutdown_before_worker_starts_prevents_transfer():
    backend = Collection()
    owner = lifecycle(backend, lambda _: pytest.fail("must not transfer"))
    owner.open()
    owner.begin_sync()
    owner.shutdown()
    assert owner.sync().outcome.status is SyncStatus.FAILED
    assert not backend.is_open


def test_cancelled_acquisition_can_be_retried():
    backend = Collection()
    owner = lifecycle(backend, lambda _: SyncOutcome(SyncStatus.SYNCED))
    assert not owner.open(lambda: True)
    assert backend.events == []
    # Cancellation can arrive while native open is running.
    assert not owner.open(lambda: backend.is_open)
    assert not backend.is_open
    assert owner.open()
    owner.release()
    assert not backend.is_open
    assert owner.open()


def test_native_collection_can_transfer_ownership_to_worker_and_back(tmp_path):
    path = tmp_path / "collection.anki2"
    backend = AnkiBackend(path)
    profile = ProfilePaths(tmp_path, "test", path)

    def transfer(_):
        # Exercise the same separate-connection ownership as sync without a network.
        other = AnkiBackend(path)
        try:
            other.open()
            assert other.decks()
        finally:
            other.close()
        return SyncOutcome(SyncStatus.UP_TO_DATE)

    owner = CollectionLifecycle(backend, profile, transfer)
    results = []
    try:
        assert owner.open()
        assert owner.begin_sync()
        worker = Thread(target=lambda: results.append(owner.sync()))
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        assert results[0].outcome.ok
        assert results[0].reopen_error is None
        owner.finish_sync()
        assert backend.decks()
    finally:
        owner.shutdown()
    assert not backend.is_open
