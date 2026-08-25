import pickle
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import anki.collection
import pytest
from anki.sync_pb2 import SyncCollectionResponse, SyncStatusResponse

from repetui.config import ProfilePaths
from repetui.sync import _auth, _profile_data, sync_profile


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


class FakeSyncCollection:
    instances = []
    status_required = SyncStatusResponse.Required.NORMAL_SYNC
    collection_required = SyncCollectionResponse.NORMAL_SYNC

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
    FakeSyncCollection.collection_required = SyncCollectionResponse.NORMAL_SYNC
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    outcome = sync_profile(profile)

    collection = FakeSyncCollection.instances[-1]
    assert outcome.ok is True
    assert collection.media_synced is True
    assert collection.closed is True


def test_required_full_download_is_performed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = profile_with_prefs(tmp_path, {"syncKey": "secret"})
    FakeSyncCollection.instances.clear()
    FakeSyncCollection.status_required = SyncStatusResponse.Required.FULL_SYNC
    FakeSyncCollection.collection_required = SyncCollectionResponse.FULL_DOWNLOAD
    monkeypatch.setattr(anki.collection, "Collection", FakeSyncCollection)

    outcome = sync_profile(profile)

    collection = FakeSyncCollection.instances[-1]
    assert outcome.ok is True
    assert collection.full_sync == (12, False)
    assert collection.media_synced is True
