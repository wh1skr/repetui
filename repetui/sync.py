"""Synchronize through Anki's backend using credentials Anki Desktop created."""

from __future__ import annotations

import contextlib
import io
import pickle
import sqlite3
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from zipfile import ZipFile

from .config import ProfilePaths


class SyncStatus(str, Enum):
    SYNCED = "synced"
    UP_TO_DATE = "up_to_date"
    OFFLINE = "offline"
    AUTH_REQUIRED = "auth_required"
    COLLECTION_UNAVAILABLE = "collection_unavailable"
    FULL_SYNC_REQUIRED = "full_sync_required"
    BACKUP_FAILED = "backup_failed"
    FAILED = "failed"


class FullSyncDirection(str, Enum):
    DOWNLOAD = "download"
    UPLOAD = "upload"


@dataclass(frozen=True)
class SyncOutcome:
    status: SyncStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {SyncStatus.SYNCED, SyncStatus.UP_TO_DATE}


class SyncAuthenticationError(RuntimeError):
    """Anki Desktop has not supplied usable AnkiWeb credentials yet."""


def failed_sync_outcome(error: Exception) -> SyncOutcome:
    """Classify a sync failure once, at the backend boundary."""
    detail = str(error)
    normalized = detail.casefold()
    if isinstance(error, SyncAuthenticationError):
        status = SyncStatus.AUTH_REQUIRED
    elif isinstance(error, (ConnectionError, TimeoutError)) or any(
        clue in normalized
        for clue in (
            "offline",
            "network",
            "connection",
            "timed out",
            "timeout",
            "name or service not known",
            "name resolution",
            "socket",
            "dns",
        )
    ):
        status = SyncStatus.OFFLINE
    elif any(
        clue in normalized
        for clue in (
            "sign in",
            "sync once",
            "one sync",
            "sync key",
            "synckey",
            "authentication",
            "authenticate",
            "auth failed",
            "unauthorized",
            "credentials",
            "not logged in",
            "401",
            "login",
            "prefs21",
        )
    ):
        status = SyncStatus.AUTH_REQUIRED
    elif any(clue in normalized for clue in ("collection", "database", "locked", "reopen")):
        status = SyncStatus.COLLECTION_UNAVAILABLE
    else:
        status = SyncStatus.FAILED
    return SyncOutcome(status, detail)


class _ProfileUnpickler(pickle.Unpickler):
    """Anki profile preferences should contain data, never executable globals."""

    def find_class(self, module: str, name: str) -> object:
        raise pickle.UnpicklingError(f"Unsupported profile value: {module}.{name}")


def _profile_data(profile: ProfilePaths) -> dict:
    prefs = profile.base / "prefs21.db"
    if not prefs.is_file():
        raise SyncAuthenticationError(
            "Open Anki Desktop and sync once before using repetui sync."
        )
    with sqlite3.connect(f"file:{prefs}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT data FROM profiles WHERE name = ?", (profile.name,)
        ).fetchone()
    if row is None:
        raise SyncAuthenticationError(
            f"Anki profile '{profile.name}' was not found in prefs21.db."
        )
    data = _ProfileUnpickler(io.BytesIO(row[0])).load()
    if not isinstance(data, dict):
        raise SyncAuthenticationError("Anki profile preferences had an unexpected format.")
    return data


def _auth(profile: ProfilePaths):
    from anki.sync_pb2 import SyncAuth

    data = _profile_data(profile)
    key = data.get("syncKey")
    if not key:
        raise SyncAuthenticationError(
            "Open Anki Desktop and complete one sync before using repetui sync."
        )

    endpoint = data.get("currentSyncUrl") or data.get("customSyncUrl")
    if not endpoint and data.get("hostNum") is not None:
        endpoint = f"https://sync{data['hostNum']}.ankiweb.net/sync/"
    endpoint = endpoint or "https://sync.ankiweb.net/"

    auth = SyncAuth()
    auth.hkey = key
    auth.endpoint = endpoint.rstrip("/") + "/"
    auth.io_timeout_secs = 30
    return auth


def sync_profile(profile: ProfilePaths) -> SyncOutcome:
    """Sync collection and media, stopping when full sync needs a user's choice."""
    from anki.collection import Collection
    from anki.sync_pb2 import SyncCollectionResponse, SyncStatusResponse

    collection = None
    try:
        auth = _auth(profile)
        collection = Collection(str(profile.collection))
        status = collection.sync_status(auth)
        if status.new_endpoint:
            auth.endpoint = status.new_endpoint.rstrip("/") + "/"
        if status.required == SyncStatusResponse.Required.NO_CHANGES:
            # Collection status does not include pending media transfers.
            collection.sync_media(auth)
            return SyncOutcome(SyncStatus.UP_TO_DATE)

        result = collection.sync_collection(auth, sync_media=False)
        if result.new_endpoint:
            auth.endpoint = result.new_endpoint.rstrip("/") + "/"
        if result.required == SyncCollectionResponse.FULL_SYNC:
            # Both sides contain data. Media sync cannot resolve this choice,
            # and choosing a direction here could discard unsynced reviews.
            return SyncOutcome(
                SyncStatus.FULL_SYNC_REQUIRED,
                "Cards were not synced. Back up both collections before resolving "
                "this profile in Anki. Upload/download replaces one side.",
            )
        if result.required in {
            SyncCollectionResponse.FULL_DOWNLOAD,
            SyncCollectionResponse.FULL_UPLOAD,
        }:
            upload = result.required == SyncCollectionResponse.FULL_UPLOAD
            collection.close_for_full_sync()
            collection.full_upload_or_download(
                auth=auth,
                server_usn=result.server_media_usn,
                upload=upload,
            )
            collection.reopen(after_full_sync=True)
        elif result.required != SyncCollectionResponse.NO_CHANGES:
            return SyncOutcome(
                SyncStatus.FAILED,
                "Collection sync did not complete. Try syncing again.",
            )
        collection.sync_media(auth)
        return SyncOutcome(SyncStatus.SYNCED)
    except Exception as exc:
        return failed_sync_outcome(exc)
    finally:
        if collection is not None:
            with contextlib.suppress(Exception):
                collection.close()


def full_sync_profile(profile: ProfilePaths, direction: FullSyncDirection) -> SyncOutcome:
    """Resolve a user-confirmed replacement, preserving local data first.

    Call only after explicit direction-specific confirmation. A local backup
    does not protect remote-only changes when uploading.
    """
    from anki.collection import Collection
    from anki.sync_pb2 import SyncCollectionResponse, SyncStatusResponse

    if not isinstance(direction, FullSyncDirection):
        return SyncOutcome(SyncStatus.FAILED, "Choose a full-sync direction first.")
    collection = None
    backing_up = False
    try:
        auth = _auth(profile)
        collection = Collection(str(profile.collection))
        backing_up = True
        backups = profile.collection.parent / "backups"
        backups.mkdir(exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="repetui-full-sync-", dir=backups))
        backup = folder / "collection.colpkg"
        # Anki exports a consistent snapshot and closes the collection.
        collection.export_collection_package(str(backup), include_media=False, legacy=False)
        collection.reopen()
        with ZipFile(backup) as archive:
            if not any(name.startswith("collection.anki") for name in archive.namelist()):
                raise ValueError("Backup contains no collection.")
            if archive.testzip() is not None:
                raise ValueError("Backup integrity check failed.")
        backing_up = False

        # Refresh server state and direction permissions; never reuse the modal's
        # old response. If ordinary sync now succeeds, no replacement is needed.
        result = collection.sync_collection(auth, sync_media=False)
        if result.new_endpoint:
            auth.endpoint = result.new_endpoint.rstrip("/") + "/"
        upload = direction is FullSyncDirection.UPLOAD
        allowed = {
            SyncCollectionResponse.FULL_SYNC,
            SyncCollectionResponse.FULL_UPLOAD if upload else SyncCollectionResponse.FULL_DOWNLOAD,
        }
        if result.required in allowed:
            collection.close_for_full_sync()
            collection.full_upload_or_download(
                auth=auth, server_usn=result.server_media_usn, upload=upload
            )
            collection.reopen(after_full_sync=True)
            status = collection.sync_status(auth)
            if status.new_endpoint:
                auth.endpoint = status.new_endpoint.rstrip("/") + "/"
            if status.required != SyncStatusResponse.Required.NO_CHANGES:
                return SyncOutcome(
                    SyncStatus.FAILED, "Full sync still needs attention; retry sync."
                )
        elif result.required != SyncCollectionResponse.NO_CHANGES:
            return SyncOutcome(SyncStatus.FAILED, "Sync state changed; retry and choose again.")
        collection.sync_media(auth)
        return SyncOutcome(SyncStatus.SYNCED, f"Local collection backup: {backup}")
    except Exception as exc:
        if backing_up:
            return SyncOutcome(SyncStatus.BACKUP_FAILED, "Backup failed; no sync data transferred.")
        return failed_sync_outcome(exc)
    finally:
        if collection is not None:
            with contextlib.suppress(Exception):
                collection.close()
