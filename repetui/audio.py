"""Sequential, cancellable playback of recorded card sound."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path


def available_player() -> tuple[str, ...]:
    """Choose a quiet command that exits after one clip."""
    if player := shutil.which("mpv"):
        return (player, "--no-video", "--no-terminal", "--really-quiet", "--")
    if player := shutil.which("ffplay"):
        return (player, "-nodisp", "-autoexit", "-loglevel", "error", "-i")
    if player := shutil.which("paplay"):
        return (player, "--")
    return ()


class CardAudioPlayer:
    """Own the process for the visible card side; a new play stops the old one."""

    def __init__(
        self,
        report_error: Callable[[str], None],
        *,
        command: Sequence[str] | None = None,
    ) -> None:
        self._report_error = report_error
        self._command = tuple(command) if command is not None else available_player()
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._process: asyncio.subprocess.Process | None = None
        self._terminating: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    def play(self, paths: Sequence[Path]) -> None:
        self.stop()
        if paths:
            self._task = asyncio.create_task(self._play(tuple(paths)))
            self._tasks.add(self._task)
            self._task.add_done_callback(self._tasks.discard)

    def stop(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._terminating = self._process
            with suppress(ProcessLookupError):
                self._process.terminate()
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def close(self) -> None:
        """Stop and reap every playback process before its event loop exits."""
        self.stop()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _reap_cancelled(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None and self._terminating is not process:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    async def _play(self, paths: tuple[Path, ...]) -> None:
        async with self._lock:
            if not self._command:
                self._report_error("Audio player unavailable. Install mpv or ffplay.")
                return
            for path in paths:
                try:
                    available = path.is_file()
                except OSError:
                    available = False
                if not available:
                    self._report_error(f"Audio file missing: {path.name}")
                    continue
                starting = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *self._command,
                        str(path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                )
                try:
                    process = await asyncio.shield(starting)
                except asyncio.CancelledError:
                    try:
                        process = await starting
                    except OSError:
                        pass
                    else:
                        await self._reap_cancelled(process)
                    raise
                except OSError:
                    self._report_error(f"Could not start audio playback: {path.name}")
                    continue
                self._process = process
                try:
                    result = await process.wait()
                except asyncio.CancelledError:
                    await self._reap_cancelled(process)
                    raise
                finally:
                    if self._process is process:
                        self._process = None
                    if self._terminating is process:
                        self._terminating = None
                if result:
                    self._report_error(f"Could not play audio (format or output): {path.name}")
