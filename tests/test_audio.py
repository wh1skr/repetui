import asyncio
import sys

import pytest

from repetui.audio import CardAudioPlayer


def fake_player(tmp_path):
    script = tmp_path / "player.py"
    script.write_text(
        """import os
import pathlib
import signal
import sys
import time

clip = pathlib.Path(sys.argv[1])
log = pathlib.Path(os.environ['REPETUI_TEST_AUDIO_LOG'])

def record(event):
    with log.open('a') as output:
        output.write(f'{event} {clip.name}\\n')

if clip.name.startswith('hold'):
    def finish(*_):
        record('stop')
        sys.exit(0)
    signal.signal(signal.SIGTERM, finish)
record('start')
if clip.name.startswith('hold'):
    time.sleep(10)
elif clip.name.startswith('fail'):
    sys.exit(2)
"""
    )
    return (sys.executable, str(script))


async def wait_for_log(log, expected):
    async with asyncio.timeout(2):
        while not log.exists() or expected not in log.read_text():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_clips_play_in_order_and_missing_or_failed_clips_do_not_stop_review(
    tmp_path, monkeypatch
) -> None:
    log = tmp_path / "play.log"
    monkeypatch.setenv("REPETUI_TEST_AUDIO_LOG", str(log))
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    failed = tmp_path / "fail.wav"
    for clip in (first, second, failed):
        clip.touch()
    errors = []
    player = CardAudioPlayer(errors.append, command=fake_player(tmp_path))

    player.play((first, tmp_path / "missing.wav", failed, second))
    assert player._task is not None
    await player._task

    assert log.read_text().splitlines() == [
        "start first.wav", "start fail.wav", "start second.wav"
    ]
    assert errors == [
        "Audio file missing: missing.wav",
        "Could not play audio (format or output): fail.wav",
    ]


@pytest.mark.asyncio
async def test_replay_and_card_change_stop_old_process_before_starting_new(
    tmp_path, monkeypatch
) -> None:
    log = tmp_path / "play.log"
    monkeypatch.setenv("REPETUI_TEST_AUDIO_LOG", str(log))
    hold = tmp_path / "hold.wav"
    next_clip = tmp_path / "next.wav"
    hold.touch()
    next_clip.touch()
    player = CardAudioPlayer(lambda message: pytest.fail(message), command=fake_player(tmp_path))

    player.play((hold,))
    await wait_for_log(log, "start hold.wav")
    player.play((hold,))
    await wait_for_log(log, "stop hold.wav\nstart hold.wav")
    player.play((next_clip,))
    assert player._task is not None
    await player._task

    assert log.read_text().splitlines() == [
        "start hold.wav", "stop hold.wav",
        "start hold.wav", "stop hold.wav",
        "start next.wav",
    ]


@pytest.mark.asyncio
async def test_unavailable_player_is_a_nonfatal_error(tmp_path) -> None:
    clip = tmp_path / "clip.wav"
    clip.touch()
    errors = []
    player = CardAudioPlayer(errors.append, command=("/no/such/player",))

    player.play((clip,))
    assert player._task is not None
    await player._task

    assert errors == ["Could not start audio playback: clip.wav"]


@pytest.mark.asyncio
async def test_close_reaps_running_player_before_returning(tmp_path, monkeypatch) -> None:
    log = tmp_path / "play.log"
    monkeypatch.setenv("REPETUI_TEST_AUDIO_LOG", str(log))
    hold = tmp_path / "hold.wav"
    hold.touch()
    player = CardAudioPlayer(lambda message: pytest.fail(message), command=fake_player(tmp_path))

    player.play((hold,))
    await wait_for_log(log, "start hold.wav")
    await player.close()

    assert log.read_text().splitlines() == ["start hold.wav", "stop hold.wav"]
    assert player._process is None


@pytest.mark.asyncio
async def test_close_reaps_process_even_if_spawn_has_not_returned(tmp_path, monkeypatch) -> None:
    log = tmp_path / "play.log"
    monkeypatch.setenv("REPETUI_TEST_AUDIO_LOG", str(log))
    hold = tmp_path / "hold.wav"
    hold.touch()
    spawned = asyncio.Event()
    spawned_processes = []
    native_spawn = asyncio.create_subprocess_exec

    async def slow_spawn(*args, **kwargs):
        process = await native_spawn(*args, **kwargs)
        spawned_processes.append(process)
        spawned.set()
        await asyncio.sleep(0.1)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    player = CardAudioPlayer(lambda message: pytest.fail(message), command=fake_player(tmp_path))

    player.play((hold,))
    await asyncio.wait_for(spawned.wait(), timeout=2)
    await player.close()

    assert player._process is None
    assert not player._tasks
    assert spawned_processes[0].returncode is not None
