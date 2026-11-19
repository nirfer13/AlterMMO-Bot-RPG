"""Small file-backed state (cooldown lists, shrine modifiers, exp summary).

The legacy bot kept this state in files in the repository root and rewrote them
in place with plain ``open(..., "w")``.  A crash or power loss in the middle of
a write left an empty/half-written file, and the files constantly dirtied the
git working tree.

v2 keeps the same file names and formats (so existing files can simply be
copied over) but stores them in a ``data/`` directory outside version control
and writes them atomically: write to a temp file, fsync, then ``os.replace``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)


#: How often a save tries to put the new file in place.
_REPLACE_ATTEMPTS = 6


def _replace(source: str, target: Path) -> None:
    """``os.replace`` that waits out a reader.

    On Windows a file somebody has open at this very moment (the backup run
    from ``backup_now.bat``, an antivirus scan) cannot be replaced: the call
    fails with PermissionError and the save would be lost.  Such a reader is
    done within milliseconds, so the replace is simply tried again.
    """
    for attempt in range(1, _REPLACE_ATTEMPTS + 1):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS:
                raise
            time.sleep(0.02 * attempt)


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so that readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class JsonFile:
    """A JSON document on disk with a safe default.

    ``default`` is a zero-argument callable returning a fresh default value; it
    is used when the file is missing, empty or corrupt (which is logged).
    """

    def __init__(self, path: Path, default: Callable[[], Any]) -> None:
        self.path = Path(path)
        self._default = default

    def load(self) -> Any:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._default()
        except OSError:
            log.exception("Cannot read %s; using defaults.", self.path)
            return self._default()
        if not raw.strip():
            return self._default()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.error("File %s is corrupt (%d bytes); using defaults.", self.path, len(raw))
            return self._default()

    def save(self, value: Any) -> None:
        atomic_write_text(self.path, json.dumps(value, ensure_ascii=False, indent=4))

    def reset(self) -> Any:
        value = self._default()
        self.save(value)
        return value


class IdListFile:
    """A text file with one Discord id per line (the legacy cooldown format)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> list[str]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            log.exception("Cannot read %s; treating it as empty.", self.path)
            return []
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def _write(self, ids: Iterable[str]) -> None:
        lines = list(ids)
        atomic_write_text(self.path, "".join(f"{line}\n" for line in lines))

    def all(self) -> list[str]:
        return self._read()

    def contains(self, user_id: int | str) -> bool:
        return str(user_id) in self._read()

    def count(self, user_id: int | str) -> int:
        """How many times the id is listed (some cooldowns allow N uses)."""
        return self._read().count(str(user_id))

    def add(self, user_id: int | str) -> None:
        self._write([*self._read(), str(user_id)])

    def remove(self, user_ids: Iterable[int | str], *, all_occurrences: bool = True) -> int:
        """Remove ids; returns how many lines were dropped."""
        targets = {str(uid) for uid in user_ids}
        current = self._read()
        if all_occurrences:
            kept = [line for line in current if line not in targets]
        else:
            kept = []
            pending = set(targets)
            for line in current:
                if line in pending:
                    pending.discard(line)
                    continue
                kept.append(line)
        if len(kept) != len(current):
            self._write(kept)
        return len(current) - len(kept)

    def clear(self) -> None:
        self._write([])


def migrate_legacy_file(name: str, data_dir: Path, legacy_dirs: Iterable[Path]) -> bool:
    """Copy a legacy state file into ``data_dir`` the first time v2 starts.

    Nothing is ever overwritten: if ``data_dir/name`` exists it wins.  The
    source is only read, never changed.  Returns True when a file was copied.
    """
    target = data_dir / name
    if target.exists():
        return False
    for legacy_dir in legacy_dirs:
        source = Path(legacy_dir) / name
        if source.is_file():
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            log.info("Migrated state file %s -> %s", source, target)
            return True
    return False
