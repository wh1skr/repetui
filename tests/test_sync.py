import pickle
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import anki.collection
import pytest
from anki.sync_pb2 import SyncCollectionResponse, SyncStatusResponse

from repetui.config import ProfilePaths
from repetui.sync import (
    SyncStatus,
    _auth,
    _profile_data,
    failed_sync_outcome,
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
