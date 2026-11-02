"""Persistent session state for the ttd TUI.

Sessions come in two flavours:

* **Automatic sessions** are keyed by the SHA-1 hash of the log file's
  absolute path. They are created implicitly the first time a user opens
  a file in the TUI and restored on subsequent opens. This behaviour is
  unchanged from earlier versions.

* **Named sessions** are explicit, user-chosen identifiers. They let a
  user say "open my incident-2024-10-26 session" from any log file, or
  share the same session across teammates via a stable name.

Both flavours are stored in the same directory. A ``NamedSession`` record
is a thin pointer to an automatic session, so no data is duplicated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# XDG helpers
# ---------------------------------------------------------------------------


def _xdg_data_home() -> Path:
    env = os.environ.get("XDG_DATA_HOME")
    if env and Path(env).is_absolute():
        return Path(env)
    return Path.home() / ".local" / "share"


def default_session_dir() -> Path:
    override = os.environ.get("TTD_SESSION_DIR")
    if override:
        return Path(override).expanduser()
    return _xdg_data_home() / "ttd" / "sessions"


# ---------------------------------------------------------------------------
# Session state (unchanged shape, one new optional field)
# ---------------------------------------------------------------------------


SESSION_FORMAT_VERSION = 1


@dataclass
class SessionState:
    """Serializable state of a single ttd replay session."""

    log_file: str
    format_version: int = SESSION_FORMAT_VERSION

    query: str = ""
    level_filter: str = ""
    service_filter: str = ""
    search_term: str = ""

    cursor_row: int = 0
    cursor_trace_id: str = ""
    scroll_offset: int = 0

    detail_panel_visible: bool = True
    anomaly_panel_visible: bool = False
    cluster_errors_only: bool = False

    cluster_query: str = ""

    created_at: str = ""
    updated_at: str = ""

    # Named sessions store their name here so a session file can be
    # discovered by name without consulting the index.
    name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ---------------------------------------------------------------------------
# Named session record
# ---------------------------------------------------------------------------


_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


@dataclass
class NamedSession:
    """A pointer from a human-readable name to an automatic session."""

    name: str
    log_file: str
    session_id: str
    created_at: str = ""
    updated_at: str = ""
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "NamedSession":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


def validate_session_name(name: str) -> None:
    """Raise ``ValueError`` if ``name`` is not a valid session name."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid session name {name!r}. Use letters, digits, "
            "underscores, dots and dashes (1-64 chars), starting with "
            "a letter, digit, or underscore."
        )
    if name in {"index", "default"}:
        raise ValueError(f"Reserved session name: {name!r}")


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Load, save, enumerate, and name sessions.

    The manager is thread-safe: the TUI may save on every keystroke while
    a background watcher reads the index.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_session_dir()
        self._lock = threading.RLock()

    # -- automatic session paths ------------------------------------------

    def _session_id(self, log_file: Path) -> str:
        absolute = str(log_file.resolve())
        return hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:16]

    def session_path(self, log_file: Path) -> Path:
        return self.root / f"{self._session_id(log_file)}.json"

    def metadata_path(self) -> Path:
        return self.root / "index.json"

    def named_index_path(self) -> Path:
        """Return the path of the named-session index."""
        return self.root / "named.json"

    # -- load / save -------------------------------------------------------

    def exists(self, log_file: Path) -> bool:
        return self.session_path(log_file).exists()

    def load(self, log_file: Path) -> SessionState:
        path = self.session_path(log_file)
        if not path.exists():
            return self._fresh_state(log_file)
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._fresh_state(log_file)
        state = SessionState.from_dict(data)
        state.log_file = str(log_file.resolve())
        return state

    def save(self, state: SessionState) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        if not state.created_at:
            state.created_at = now
        state.updated_at = now

        log_path = Path(state.log_file)
        session_file = self.session_path(log_path)
        payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)
        with self._lock:
            session_file.write_text(payload, encoding="utf-8")
            self._update_index(log_path)
        return session_file

    def delete(self, log_file: Path) -> bool:
        path = self.session_path(log_file)
        if not path.exists():
            return False
        with self._lock:
            path.unlink()
            self._update_index(log_file, remove=True)
            # Also remove any named pointers to this log file.
            self._remove_named_for_log(log_file)
        return True

    # -- named sessions ----------------------------------------------------

    def create_named(
        self,
        name: str,
        log_file: Path,
        *,
        description: str = "",
        overwrite: bool = False,
    ) -> NamedSession:
        """Create a named pointer to ``log_file``'s automatic session.

        The automatic session is created on demand so the user can name a
        session before ever opening the file in the TUI.
        """
        validate_session_name(name)
        log_file = Path(log_file).resolve()

        with self._lock:
            named = self._read_named_index()
            if name in named and not overwrite:
                raise ValueError(
                    f"Session {name!r} already exists. "
                    "Use overwrite=True to replace it."
                )

            # Ensure the automatic session exists.
            if not self.exists(log_file):
                self.save(self._fresh_state(log_file))

            state = self.load(log_file)
            state.name = name
            self.save(state)

            now = datetime.now(timezone.utc).isoformat()
            record = NamedSession(
                name=name,
                log_file=str(log_file),
                session_id=self._session_id(log_file),
                created_at=named.get(name, NamedSession("", "", "")).created_at or now,
                updated_at=now,
                description=description,
            )
            named[name] = record
            self._write_named_index(named)
        return record

    def get_named(self, name: str) -> NamedSession | None:
        with self._lock:
            return self._read_named_index().get(name)

    def resolve(self, name: str) -> Path | None:
        """Return the log file associated with a named session, if any."""
        record = self.get_named(name)
        if record is None:
            return None
        return Path(record.log_file)

    def delete_named(self, name: str) -> bool:
        with self._lock:
            named = self._read_named_index()
            record = named.pop(name, None)
            if record is None:
                return False
            self._write_named_index(named)

            # Clear the name from the underlying session state, but keep
            # the automatic session itself intact.
            log_path = Path(record.log_file)
            if self.exists(log_path):
                state = self.load(log_path)
                state.name = ""
                self.save(state)
        return True

    def rename_named(self, old: str, new: str) -> bool:
        """Rename a named session. Returns False if ``old`` does not exist."""
        validate_session_name(new)
        with self._lock:
            named = self._read_named_index()
            if old not in named:
                return False
            if new in named:
                raise ValueError(f"Session {new!r} already exists.")
            record = named.pop(old)
            record.name = new
            record.updated_at = datetime.now(timezone.utc).isoformat()
            named[new] = record
            self._write_named_index(named)

            log_path = Path(record.log_file)
            if self.exists(log_path):
                state = self.load(log_path)
                state.name = new
                self.save(state)
        return True

    def promote(self, log_file: Path, name: str, *, description: str = "") -> NamedSession:
        """Attach a name to an existing automatic session."""
        return self.create_named(name, log_file, description=description, overwrite=False)

    def list_named(self) -> list[NamedSession]:
        with self._lock:
            records = list(self._read_named_index().values())
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records

    # -- listing -----------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """Return metadata about every stored automatic session."""
        with self._lock:
            index = self._read_index()
        sessions = []
        for entry in index.get("sessions", []):
            log_path = Path(entry.get("log_file", ""))
            if log_path.name and self.session_path(log_path).exists():
                sessions.append(entry)
        sessions.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
        return sessions

    def clear_all(self) -> int:
        if not self.root.exists():
            return 0
        removed = 0
        with self._lock:
            for path in self.root.glob("*.json"):
                if path.name in {"index.json", "named.json"}:
                    continue
                path.unlink()
                removed += 1
            self.metadata_path().unlink(missing_ok=True)
            self.named_index_path().unlink(missing_ok=True)
        return removed

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _fresh_state(log_file: Path) -> SessionState:
        return SessionState(log_file=str(log_file.resolve()))

    def _read_index(self) -> dict:
        path = self.metadata_path()
        if not path.exists():
            return {"sessions": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"sessions": []}

    def _update_index(self, log_file: Path, *, remove: bool = False) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index = self._read_index()
        resolved = str(log_file.resolve())
        sessions = [s for s in index.get("sessions", []) if s.get("log_file") != resolved]

        if not remove:
            state = self.load(log_file)
            sessions.append(
                {
                    "log_file": resolved,
                    "session_id": self._session_id(log_file),
                    "created_at": state.created_at,
                    "updated_at": state.updated_at,
                    "query": state.query,
                    "name": state.name,
                }
            )

        index["sessions"] = sessions
        self.metadata_path().write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _read_named_index(self) -> dict[str, NamedSession]:
        path = self.named_index_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {
            name: NamedSession.from_dict(entry)
            for name, entry in raw.items()
            if isinstance(entry, dict)
        }

    def _write_named_index(self, records: dict[str, NamedSession]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {name: asdict(record) for name, record in records.items()}
        self.named_index_path().write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _remove_named_for_log(self, log_file: Path) -> None:
        resolved = str(Path(log_file).resolve())
        named = self._read_named_index()
        changed = False
        for name in list(named.keys()):
            if named[name].log_file == resolved:
                del named[name]
                changed = True
        if changed:
            self._write_named_index(named)