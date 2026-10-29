"""Read workload-owned files without borrowing the control plane's filesystem authority.

Each component is opened relative to a pinned directory descriptor, with O_NOFOLLOW.
Checking resolve() and subsequently opening a pathname is not a sandbox boundary: the
workload can replace a checked component between those operations. Symlinks, hardlinks,
special files and Git metadata are never read or written, including by read-only model tools.
A walk reports a symlink or special entry as a leaf it never follows or descends: its
existence does not refuse the walk, reading it does.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import stat
import sys
from typing import Iterator

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 20_000
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class RepoFileRefused(ValueError):
    """An unsafe or unbounded repository read; callers must surface this refusal."""


class RepoFileNotWritable(RepoFileRefused):
    """The path is a real repository file or directory that the OS will not let us write
    (EACCES/EPERM): e.g. a template reference copied in read-only. Not a safety refusal — the
    caller may make it writable (workspace_perms.make_writable) and try once more."""


def _parts(raw: str, *, directory: bool = False) -> tuple[str, ...]:
    if directory and raw in ("", ".", "./"):
        return ()
    if not raw or raw.startswith(("/", "~")) or "\\" in raw or "\0" in raw:
        raise RepoFileRefused("absolute or invalid path outside the repository root is refused")
    parts = tuple(p for p in raw.split("/") if p != ".")
    if len(parts) > 128 or len(raw.encode("utf-8")) > 16_384:
        raise RepoFileRefused("repository path exceeds traversal limit")
    if not parts or any(p in ("", "..") or p.casefold() == ".git" for p in parts):
        raise RepoFileRefused("path outside the repository root or Git metadata is refused")
    return parts


def _refused(path: str) -> RepoFileRefused:
    # Do not disclose the resolved target (which may itself name protected state).
    return RepoFileRefused(f"{path!r}: symlink, special file or path outside the repository root refused")


class RepoFiles:
    """A pinned repository root. All reads stay relative to this descriptor, even after rename."""

    def __init__(self, root: Path):
        self._fd = -1
        absolute = os.path.abspath(root)
        # macOS's standard /tmp and /var aliases are OS-owned, not repository components.
        if sys.platform == "darwin":
            for prefix in ("/tmp", "/var"):
                if absolute == prefix or absolute.startswith(prefix + "/"):
                    absolute = "/private" + absolute
                    break
        # Ancestors of the root (e.g. the sandbox's own parent directories) need only be
        # traversed, not listed: O_PATH requires search (x) permission on each component, not
        # read (r) — a root nested under a directory we may not read (but may enter) must still
        # be openable. Only the root itself (opened last, with the real _DIR flags) is ever
        # scanned, so only it needs read permission.
        _TRAVERSE = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        parts = Path(absolute).parts[1:]
        fd = os.open("/", _TRAVERSE)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, _TRAVERSE, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            if parts:
                next_fd = os.open(parts[-1], _DIR, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self._fd, fd = fd, -1
        except OSError as exc:
            raise _refused("repository root") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextlib.contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False):
        fd = os.dup(self._fd)
        try:
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, mode=0o777, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(part, _DIR, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def read_text(self, path: str, *, max_bytes: int = MAX_FILE_BYTES,
                  truncate: bool = False, errors: str = "replace") -> str:
        parts = _parts(path)
        cap = max(0, min(int(max_bytes), MAX_FILE_BYTES))
        try:
            with self._directory(parts[:-1]) as parent:
                fd = os.open(parts[-1], _FILE, dir_fd=parent)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _refused(path)
                if info.st_size > cap and not truncate:
                    raise RepoFileRefused(f"{path!r}: repository file exceeds {cap} bytes")
                data = stream.read(cap + 1)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _refused(path) from exc
        if len(data) > cap:
            if not truncate:
                raise RepoFileRefused(f"{path!r}: repository file exceeds {cap} bytes")
            return data[:cap].decode("utf-8", errors=errors) + "\n...[truncated: repository read cap]"
        return data.decode("utf-8", errors=errors)

    def write_text(self, path: str, content: str) -> bool:
        """The native tool's matching write boundary; never truncate before checking the fd."""
        parts = _parts(path)
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise RepoFileRefused(f"{path!r}: repository file exceeds {MAX_FILE_BYTES} bytes")
        try:
            with self._directory(parts[:-1], create=True) as parent:
                try:
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o666, dir_fd=parent)
                    existed = False
                except FileExistsError:
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                                 os.O_CLOEXEC, dir_fd=parent)
                    existed = True
            with os.fdopen(fd, "wb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _refused(path)
                os.ftruncate(stream.fileno(), 0)
                stream.write(data)
            return existed
        except PermissionError as exc:
            raise RepoFileNotWritable(
                f"{path!r} is not writable (permission denied) — it, or its folder, is "
                "read-only") from exc
        except OSError as exc:
            raise _refused(path) from exc

    def listing(self, path: str = ".", *, skip: set[str] | frozenset[str] = frozenset(),
                surface_special: bool = True) -> list[tuple[str, str]]:
        """One directory's rows as (name, kind), kind "dir", "file" or "special".

        A symlink or special entry (fifo, socket, device) is reported as "special": it is never
        followed or descended, and read_text (O_NOFOLLOW + S_ISREG) still refuses to read it.
        Its mere EXISTENCE does not refuse the listing: a repository that tracks one symlink
        (BOSS's own tests/e2e/stubs/ssh-only/ssh) failed every walk of its tree and so every
        build (crun_0cd67b0a). surface_special=False restores the strict refusal."""
        parts = _parts(path, directory=True)
        try:
            with self._directory(parts) as parent:
                rows: list[tuple[str, str]] = []
                with os.scandir(parent) as entries:
                    for entry in entries:
                        if entry.name in skip or entry.name.casefold() == ".git":
                            continue
                        if len(rows) >= MAX_ENTRIES:
                            raise RepoFileRefused("repository directory exceeds entry limit")
                        mode = entry.stat(follow_symlinks=False).st_mode
                        if stat.S_ISDIR(mode):
                            rows.append((entry.name, "dir"))
                        elif stat.S_ISREG(mode):
                            rows.append((entry.name, "file"))
                        elif surface_special:
                            rows.append((entry.name, "special"))
                        else:
                            raise _refused("/".join((*parts, entry.name)))
                return sorted(rows)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise _refused(path) from exc

    def entries(self, path: str = ".", *, skip: set[str] | frozenset[str] = frozenset(),
                surface_special: bool = True) -> list[tuple[str, bool]]:
        """(name, is_directory) rows; a symlink/special entry is a non-directory leaf."""
        return [(name, kind == "dir") for name, kind in
                self.listing(path, skip=skip, surface_special=surface_special)]

    def walk_kinds(self, path: str = ".", *, skip: set[str] | frozenset[str] = frozenset(),
                   surface_special: bool = True) -> Iterator[tuple[str, str]]:
        """Every non-directory leaf under `path` as (rel, kind), kind "file" or "special".

        Only real directories are descended (each opened O_NOFOLLOW|O_DIRECTORY), so a symlink
        to a directory, inside or outside the root, is a leaf and never a way out."""
        parts = _parts(path, directory=True)
        pending = ["/".join(parts)]
        count = 0
        while pending:
            rel = pending.pop()
            for name, kind in self.listing(rel or ".", skip=skip,
                                           surface_special=surface_special):
                count += 1
                if count > MAX_ENTRIES:
                    raise RepoFileRefused("repository scan exceeds entry limit")
                child = f"{rel}/{name}" if rel else name
                if kind == "dir":
                    pending.append(child)
                else:
                    yield child, kind

    def walk(self, path: str = ".", *, skip: set[str] | frozenset[str] = frozenset(),
             surface_special: bool = True) -> Iterator[str]:
        """Every non-directory leaf under `path`, symlink/special leaves included by name."""
        for rel, _kind in self.walk_kinds(path, skip=skip, surface_special=surface_special):
            yield rel

    def regular_files(self, path: str = ".", *,
                      skip: set[str] | frozenset[str] = frozenset()) -> Iterator[str]:
        """The leaves a scan may READ: regular files only. A symlink/special entry is skipped
        unopened, so a caller that reads what it walks never aborts because one exists. A file
        swapped for a link after this listing is still refused by read_text."""
        for rel, kind in self.walk_kinds(path, skip=skip):
            if kind == "file":
                yield rel
