import os
import select
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from repetui.backend import AnkiBackend, BackendError, CollectionInUseError
from repetui.recovery import find_owner, force_close, request_close

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux ownership API")


@contextmanager
def holder(tmp_path, *, name="repetui", cooperative=True):
    directory = tmp_path / f"owner-{name}-{time.monotonic_ns()}"
    directory.mkdir()
    collection = directory / "collection.anki2"
    entry = directory / name
    entry.write_text(
        "from pathlib import Path\n"
        "from threading import Event\n"
        "from anki.collection import Collection\n"
        "from repetui.recovery import InstanceControl\n"
        "import sys\n"
        "stop=Event()\n"
        "col=Collection(sys.argv[1])\n"
        "control=InstanceControl(stop.set)\n"
        + ("control.start()\n" if cooperative else "")
        + "print('READY',flush=True)\n"
        "stop.wait(30)\n"
        "control.close()\n"
        "col.close()\n"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    process = subprocess.Popen(
        [sys.executable, "-u", str(entry), str(collection)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    try:
        output = b""
        deadline = time.monotonic() + 10
        while b"READY\n" not in output:
            assert time.monotonic() < deadline, output.decode(errors="replace")
            if select.select([process.stdout], [], [], 0.1)[0]:
                chunk = os.read(process.stdout.fileno(), 8192)
                assert chunk, output.decode(errors="replace")
                output += chunk
        yield collection, process
    finally:
        if process.poll() is None:
            process.terminate()  # Only this test-owned disposable child.
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()


def test_verified_owner_closes_cooperatively_and_collection_reopens(tmp_path):
    with holder(tmp_path) as (path, process), holder(tmp_path) as (_, unrelated):
        owner = find_owner(path)
        assert owner is not None and owner.pid == process.pid
        backend = AnkiBackend(path)
        with pytest.raises(CollectionInUseError):
            backend.open()
        assert request_close(path, owner)
        assert process.wait(timeout=5) == 0
        assert unrelated.poll() is None
        backend.open()
        assert backend.is_open
        backend.close()


def test_unknown_application_cannot_be_targeted(tmp_path):
    with holder(tmp_path, name="unrelated.py") as (path, process):
        assert find_owner(path) is None
        assert process.poll() is None


def test_force_requires_current_identity_and_exact_collection(tmp_path):
    if not hasattr(os, "pidfd_open"):
        pytest.skip("pidfd required")
    with holder(tmp_path, cooperative=False) as (path, process):
        owner = find_owner(path)
        assert owner is not None
        assert not request_close(path, owner)
        assert not force_close(path, replace(owner, started="stale"))
        assert not force_close(tmp_path / "other.anki2", owner)
        assert process.poll() is None
        assert force_close(path, owner)
        process.wait(timeout=5)
        assert not force_close(path, owner)


def test_unknown_missing_collection_is_manual_retry(tmp_path):
    assert find_owner(tmp_path / "missing.anki2") is None


def test_generic_lock_word_does_not_enable_process_recovery(tmp_path, monkeypatch):
    import anki.collection

    def fail(_path):
        raise RuntimeError("Unable to load clock settings")

    monkeypatch.setattr(anki.collection, "Collection", fail)
    with pytest.raises(BackendError) as error:
        AnkiBackend(tmp_path / "collection.anki2").open()
    assert not isinstance(error.value, CollectionInUseError)


@pytest.mark.asyncio
async def test_native_startup_recovery_reaches_decks_and_preserves_other_owner(tmp_path):
    from repetui.app import DeckScreen, RepetuiApp, StartupRecoveryScreen
    from repetui.config import ProfilePaths
    from repetui.preferences import JsonPreferences

    with holder(tmp_path) as (path, process), holder(tmp_path) as (_, unrelated):
        app = RepetuiApp(
            AnkiBackend(path), ProfilePaths(tmp_path, "disposable", path),
            JsonPreferences(tmp_path / "prefs.json"), add_ons=(),
        )
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.pause(0.3)
            assert isinstance(app.screen, StartupRecoveryScreen)
            assert app.screen.owner.pid == process.pid
            await pilot.press("c")
            await pilot.pause(1.5)
            assert isinstance(app.screen, DeckScreen)
            assert process.poll() == 0
            assert unrelated.poll() is None
