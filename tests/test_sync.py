import pickle
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import anki.collection
import pytest
from anki.sync_pb2 import SyncCollectionResponse, SyncStatusResponse

from repetui.config import ProfilePaths
from repetui.sync import (
    FullSyncDirection,
    SyncStatus,
    _auth,
    _profile_data,
    failed_sync_outcome,
    full_sync_profile,
    sync_profile,
)


def profile_with_prefs(tmp_path: Path, data: dict) -> ProfilePaths:
    profile_dir = tmp_path / "whskr"
    profile_dir.mkdir()
    collection = profile_dir / "collection.anki2"
    collection.touch()
    with sqlite3.connect(tmp_path / "prefs21.db") as connection:
        connection.execute("CREATE TABLE profiles (name TEXT PRIMARY KEY, data BLOB)")
        connection.execute(
            "INSERT INTO profiles (name, data) VALUES (?, ?)",
            ("whskr", pickle.dumps(data)),
        )
    return ProfilePaths(base=tmp_path, name="whskr", collection=collection)


def test_reads_only_the_selected_profile(tmp_path: Path) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret", "hostNum": 4})

    assert _profile_data(profile)["syncKey"] == "secret"


def test_builds_auth_using_ankis_current_endpoint(tmp_path: Path) -> None:
    profile = profile_with_prefs(
        tmp_path,
        {"syncKey": "secret", "currentSyncUrl": "https://sync9.example/sync"},
    )

    auth = _auth(profile)

    assert auth.hkey == "secret"
    assert auth.endpoint == "https://sync9.example/sync/"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ConnectionError("host unavailable"), SyncStatus.OFFLINE),
        (RuntimeError("401 unauthorized"), SyncStatus.AUTH_REQUIRED),
        (RuntimeError("collection database locked"), SyncStatus.COLLECTION_UNAVAILABLE),
        (RuntimeError("unexpected detail"), SyncStatus.FAILED),
    ],
)
def test_sync_failures_are_classified_at_the_backend_boundary(
    error: Exception,
    status: SyncStatus,
) -> None:
    outcome = failed_sync_outcome(error)

    assert outcome.status is status
    assert outcome.detail == str(error)


def test_missing_ankiweb_credentials_require_sign_in(tmp_path: Path) -> None:
    profile = profile_with_prefs(tmp_path, {})

    outcome = sync_profile(profile)

    assert outcome.status is SyncStatus.AUTH_REQUIRED


class FakeSyncCollection:
    instances = []
    status_required = SyncStatusResponse.Required.NORMAL_SYNC
    collection_required = SyncCollectionResponse.NO_CHANGES

    def __init__(self, path: str) -> None:
        self.path = path
        self.closed = False
        self.media_synced = False
        self.full_sync = None
        self.instances.append(self)

    def sync_status(self, auth):
        return SimpleNamespace(required=self.status_required, new_endpoint="")

    def sync_collection(self, auth, sync_media: bool):
        assert sync_media is False
        return SimpleNamespace(
            required=self.collection_required,
            new_endpoint="",
            server_media_usn=12,
        )

    def close_for_full_sync(self) -> None:
        pass

    def full_upload_or_download(self, *, auth, server_usn: int, upload: bool) -> None:
        self.full_sync = (server_usn, upload)

    def reopen(self, after_full_sync: bool) -> None:
        assert after_full_sync is True

    def sync_media(self, auth) -> None:
        self.media_synced = True

    def close(self) -> None:
        self.closed = True


def test_normal_sync_includes_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    FakeSyncCollection.instances.clear()
    FakeSyncCollection.status_required = SyncStatusResponse.Required.NORMAL_SYNC
    FakeSyncCollection.collection_required = SyncCollectionResponse.NO_CHANGES
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    outcome = sync_profile(profile)

    collection = FakeSyncCollection.instances[-1]
    assert outcome.ok is True
    assert collection.media_synced is True
    assert collection.closed is True


@pytest.mark.parametrize("required", [SyncCollectionResponse.NORMAL_SYNC, 999])
def test_incomplete_or_unknown_sync_response_is_not_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: int
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(
        FakeSyncCollection, "status_required", SyncStatusResponse.Required.NORMAL_SYNC
    )
    monkeypatch.setattr(FakeSyncCollection, "collection_required", required)
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    outcome = sync_profile(profile)

    assert not outcome.ok
    assert outcome.status is SyncStatus.FAILED
    assert not FakeSyncCollection.instances[-1].media_synced
    assert FakeSyncCollection.instances[-1].closed


class RecoveryCollection(FakeSyncCollection):
    collection_required = SyncCollectionResponse.FULL_SYNC
    status_required = SyncStatusResponse.Required.NO_CHANGES

    def __init__(self, path):
        super().__init__(path)
        self.events = []

    def export_collection_package(self, path, *, include_media, legacy):
        assert not include_media and not legacy
        self.events.append("backup")
        with ZipFile(path, "w") as archive:
            archive.writestr("collection.anki21b", b"synthetic collection snapshot")

    def reopen(self, after_full_sync=False):
        self.events.append("reopen")

    def sync_collection(self, auth, sync_media):
        self.events.append("sync")
        return super().sync_collection(auth, sync_media)

    def full_upload_or_download(self, **kwargs):
        self.events.append("replace")
        super().full_upload_or_download(**kwargs)


@pytest.mark.parametrize("direction", list(FullSyncDirection))
def test_confirmed_full_sync_backs_up_before_replacement(monkeypatch, tmp_path, direction):
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(anki.collection, "Collection", RecoveryCollection)
    outcome = full_sync_profile(profile, direction)
    collection = RecoveryCollection.instances[-1]
    assert outcome.ok
    assert collection.events == ["backup", "reopen", "sync", "replace", "reopen"]
    assert collection.full_sync == (12, direction is FullSyncDirection.UPLOAD)
    assert collection.closed and collection.media_synced
    assert list(profile.collection.parent.glob("backups/repetui-full-sync-*/collection.colpkg"))


@pytest.mark.parametrize("failure", ["export", "invalid_archive"])
def test_backup_failure_prevents_all_sync_transfer(monkeypatch, tmp_path, failure):
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})

    def broken_backup(self, path, **kwargs):
        if failure == "export":
            raise OSError("disk full")
        with ZipFile(path, "w") as archive:
            archive.writestr("not-a-collection", "invalid")

    monkeypatch.setattr(RecoveryCollection, "export_collection_package", broken_backup)
    monkeypatch.setattr(anki.collection, "Collection", RecoveryCollection)
    assert full_sync_profile(profile, FullSyncDirection.DOWNLOAD).status is SyncStatus.BACKUP_FAILED
    collection = RecoveryCollection.instances[-1]
    assert "sync" not in collection.events
    assert collection.full_sync is None and not collection.media_synced
    assert collection.closed


def test_changed_server_direction_does_not_override_confirmation(monkeypatch, tmp_path):
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(
        RecoveryCollection, "collection_required", SyncCollectionResponse.FULL_UPLOAD
    )
    monkeypatch.setattr(anki.collection, "Collection", RecoveryCollection)
    assert not full_sync_profile(profile, FullSyncDirection.DOWNLOAD).ok
    assert RecoveryCollection.instances[-1].full_sync is None


def test_full_sync_pending_verification_is_not_success(monkeypatch, tmp_path):
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(
        RecoveryCollection, "status_required", SyncStatusResponse.Required.NORMAL_SYNC
    )
    monkeypatch.setattr(anki.collection, "Collection", RecoveryCollection)
    assert not full_sync_profile(profile, FullSyncDirection.DOWNLOAD).ok
    assert not RecoveryCollection.instances[-1].media_synced


def test_native_recovery_backup_can_be_restored_without_network(monkeypatch, tmp_path):
    from anki._backend import RustBackend

    profile = profile_with_prefs(tmp_path, {"syncKey": "local-test-only"})
    native = anki.collection.Collection
    original = native(str(profile.collection))
    try:
        note = original.new_note(original.models.by_name("Basic"))
        note["Front"] = "Preserve this local review collection"
        note["Back"] = "Synthetic backup test"
        original.add_note(note, 1)
        note_id = note.id
        original.db.execute(
            "insert into revlog values (?, ?, -1, 3, 1, 0, 2500, 100, 0)",
            12345, note.cards()[0].id,
        )
    finally:
        original.close()

    monkeypatch.setattr(native, "sync_collection", lambda *args, **kwargs: SimpleNamespace(
        required=SyncCollectionResponse.NO_CHANGES, new_endpoint=""
    ))
    monkeypatch.setattr(native, "sync_media", lambda *args: None)
    outcome = full_sync_profile(profile, FullSyncDirection.DOWNLOAD)
    assert outcome.ok, outcome.detail
    backup, = profile.collection.parent.glob("backups/repetui-full-sync-*/collection.colpkg")
    restored_path = tmp_path / "restored.anki2"
    RustBackend().import_collection_package(
        col_path=str(restored_path), backup_path=str(backup),
        media_folder=str(tmp_path / "restored.media"), media_db=str(tmp_path / "restored.media.db"),
    )
    restored = native(str(restored_path))
    try:
        assert restored.get_note(note_id)["Front"] == "Preserve this local review collection"
        assert restored.card_count() == 1
        assert restored.db.scalar("select count(*) from revlog where usn=-1") == 1
    finally:
        restored.close()


def test_failed_full_transfer_keeps_backup_and_closes_collection(monkeypatch, tmp_path):
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})

    def fail_transfer(self, **kwargs):
        raise ConnectionError("connection lost during transfer")

    monkeypatch.setattr(RecoveryCollection, "full_upload_or_download", fail_transfer)
    monkeypatch.setattr(anki.collection, "Collection", RecoveryCollection)
    assert full_sync_profile(profile, FullSyncDirection.DOWNLOAD).status is SyncStatus.OFFLINE
    assert list(profile.collection.parent.glob("backups/repetui-full-sync-*/collection.colpkg"))
    assert RecoveryCollection.instances[-1].closed
    assert not RecoveryCollection.instances[-1].media_synced


@pytest.mark.parametrize("required, upload", [
    (SyncCollectionResponse.FULL_DOWNLOAD, False),
    (SyncCollectionResponse.FULL_UPLOAD, True),
])
def test_unambiguous_full_sync_is_performed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, required: int, upload: bool
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    FakeSyncCollection.instances.clear()
    monkeypatch.setattr(
        FakeSyncCollection, "status_required", SyncStatusResponse.Required.FULL_SYNC
    )
    monkeypatch.setattr(FakeSyncCollection, "collection_required", required)
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    outcome = sync_profile(profile)

    collection = FakeSyncCollection.instances[-1]
    assert outcome.ok is True
    assert collection.full_sync == (12, upload)
    assert collection.media_synced is True


@pytest.mark.parametrize("initial_status", [
    SyncStatusResponse.Required.NORMAL_SYNC,
    SyncStatusResponse.Required.FULL_SYNC,
])
def test_unresolved_full_sync_never_reports_success_or_transfers_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initial_status: int
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(FakeSyncCollection, "status_required", initial_status)
    monkeypatch.setattr(FakeSyncCollection, "collection_required", SyncCollectionResponse.FULL_SYNC)
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    outcome = sync_profile(profile)

    assert outcome.ok is False
    assert outcome.status.value == "full_sync_required"
    assert "not synced" in outcome.detail
    collection = FakeSyncCollection.instances[-1]
    assert collection.media_synced is False
    assert collection.full_sync is None
    assert collection.closed is True


def test_resolved_conflict_can_be_retried_successfully(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(
        FakeSyncCollection, "status_required", SyncStatusResponse.Required.NORMAL_SYNC
    )
    monkeypatch.setattr(FakeSyncCollection, "collection_required", SyncCollectionResponse.FULL_SYNC)
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)
    assert sync_profile(profile).status is SyncStatus.FULL_SYNC_REQUIRED

    monkeypatch.setattr(
        FakeSyncCollection, "collection_required", SyncCollectionResponse.NO_CHANGES
    )
    assert sync_profile(profile).status is SyncStatus.SYNCED
    assert FakeSyncCollection.instances[-1].media_synced
    assert FakeSyncCollection.instances[-1].closed


def test_media_failure_after_collection_sync_does_not_report_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    monkeypatch.setattr(
        FakeSyncCollection, "status_required", SyncStatusResponse.Required.NORMAL_SYNC
    )
    monkeypatch.setattr(
        FakeSyncCollection, "collection_required", SyncCollectionResponse.NO_CHANGES
    )
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    def fail_media(self, auth):
        raise ConnectionError("media connection lost")

    monkeypatch.setattr(FakeSyncCollection, "sync_media", fail_media)
    assert sync_profile(profile).status is SyncStatus.OFFLINE
    assert FakeSyncCollection.instances[-1].closed
