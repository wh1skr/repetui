"""Linux/WSL collection-owner verification and opt-in instance shutdown.

Never guess an owner by name. Unsupported or inaccessible ownership is a
manual-retry case. Only force termination uses a signal, pinned with a pidfd.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import stat
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread


@dataclass(frozen=True)
class CollectionOwner:
    pid: int
    started: str
    application: str
    executable: str
    command: tuple[str, ...]


def _identity(pid: int) -> CollectionOwner | None:
    if pid == os.getpid() or pid <= 0:
        return None
    proc = Path("/proc") / str(pid)
    if proc.stat().st_uid != os.getuid():
        return None
    command = tuple(part.decode() for part in (proc / "cmdline").read_bytes().split(b"\0") if part)
    if not command:
        return None
    executable = str((proc / "exe").resolve(strict=True))
    entry = Path(command[0]).name
    if Path(executable).name.startswith("python"):
        arguments = list(command[1:])
        while arguments and arguments[0] in {"-u", "-B", "-I", "-E", "-s", "-S"}:
            arguments.pop(0)
        if len(arguments) > 1 and arguments[0] == "-m":
            entry = arguments[1]
        elif arguments and not arguments[0].startswith("-"):
            entry = Path(arguments[0]).name
        else:
            return None
    elif Path(executable).name not in {"anki", "repetui"}:
        return None
    else:
        entry = Path(executable).name
    if entry not in {"repetui", "anki", "aqt"}:
        return None
    started = (proc / "stat").read_text().rpartition(")")[2].split()[19]
    return CollectionOwner(pid, started, "repetui" if entry == "repetui" else "Anki Desktop",
                           executable, command)


def find_owner(collection: Path) -> CollectionOwner | None:
    """Return only one same-user recognized process holding this exact file's lock."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        info = collection.stat()
        resource = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
        pids = set()
        for line in Path("/proc/locks").read_text().splitlines():
            parts = line.split()
            if len(parts) < 8 or "->" in parts:
                continue
            major, minor, inode = parts[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == resource:
                pids.add(int(parts[4]))
        if len(pids) != 1:
            return None
        return _identity(pids.pop())
    except (OSError, ValueError, IndexError, UnicodeError):
        return None


def _control_folder() -> Path:
    folder = Path(tempfile.gettempdir()) / f"repetui-control-{os.getuid()}"
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Unsafe instance-control directory.")
    return folder


def _endpoint(pid: int, started: str) -> Path:
    return _control_folder() / f"{pid}-{started}.sock"


def request_close(collection: Path, owner: CollectionOwner) -> bool:
    """Request cooperative closure; False means unsupported, busy or inaccessible."""
    if find_owner(collection) != owner or owner.application != "repetui":
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.5)
            client.connect(str(_endpoint(owner.pid, owner.started)))
            if find_owner(collection) != owner:
                return False
            client.sendall(b"close\n")
            return client.recv(32) == b"requested\n"
    except OSError:
        return False


def force_close(collection: Path, owner: CollectionOwner) -> bool:
    """Terminate only a freshly verified owner; caller must obtain confirmation."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        # A pidfd binds the signal to this process, not a subsequently reused PID.
        fd = os.pidfd_open(owner.pid)
        try:
            if find_owner(collection) != owner:
                return False
            signal.pidfd_send_signal(fd, signal.SIGKILL)
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


class InstanceControl:
    """Private, same-user cooperative-close endpoint for a running repetui."""

    def __init__(self, request: Callable[[], None]) -> None:
        self.request = request
        self.stop = Event()
        self.server: socket.socket | None = None
        self.thread: Thread | None = None
        self.path: Path | None = None

    def start(self) -> None:
        if not sys.platform.startswith("linux") or self.server is not None:
            return
        try:
            started = Path(f"/proc/{os.getpid()}/stat").read_text().rpartition(")")[2].split()[19]
            self.path = _endpoint(os.getpid(), started)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(self.path))
                server.listen(1)
                server.settimeout(0.2)
            except OSError:
                server.close()
                self.path = None
                return
            self.server = server
            self.thread = Thread(target=self._serve, daemon=True, name="repetui-instance-control")
            self.thread.start()
        except OSError:
            self.path = None

    def _serve(self) -> None:
        assert self.server is not None
        while not self.stop.is_set():
            try:
                client, _ = self.server.accept()
                with client:
                    client.settimeout(0.5)
                    if client.recv(32) == b"close\n":
                        self.request()
                        client.sendall(b"requested\n")
            except OSError:
                continue

    def close(self) -> None:
        self.stop.set()
        if self.server is not None:
            self.server.close()
        if self.thread is not None:
            self.thread.join(timeout=1)
        if self.path is not None:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
