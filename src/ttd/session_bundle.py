"""Export and import ttd sessions as portable bundles.

A bundle is a zip archive containing:

* ``manifest.json`` — bundle format version, creation time, source info.
* ``session.json`` — the full ``SessionState``.
* ``log-snapshot.jsonl`` — an optional snapshot of the log slice the
  session was built around (only included if ``--with-log`` is passed).

The bundle is designed to be safe to email or commit to a repo: it
contains no secrets unless the log itself does.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .session import SessionManager, SessionState


BUNDLE_FORMAT = "ttd-session-bundle"
BUNDLE_VERSION = 1


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_session(
    output: Path,
    *,
    session_name: str | None = None,
    log_file: Path | None = None,
    include_log: bool = False,
    log_max_events: int = 5_000,
) -> Path:
    """Export a session to a zip bundle.

    Either ``session_name`` or ``log_file`` must be provided. If both are
    given, ``session_name`` wins and ``log_file`` is ignored.
    """
    manager = SessionManager()

    if session_name:
        record = manager.get_named(session_name)
        if record is None:
            raise ValueError(f"Session {session_name!r} not found")
        log_file = Path(record.log_file)
        state = manager.load(log_file)
    else:
        if log_file is None:
            raise ValueError("Either session_name or log_file is required")
        state = manager.load(log_file)

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session_name": state.name or session_name or "",
        "log_file": state.log_file,
        "includes_log": include_log,
    }

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr(
            "session.json",
            json.dumps(asdict(state), indent=2, ensure_ascii=False),
        )

        if include_log and log_file is not None and log_file.exists():
            snapshot = _snapshot_log(log_file, log_max_events)
            archive.writestr("log-snapshot.jsonl", snapshot)

    return output


def _snapshot_log(log_file: Path, max_events: int) -> str:
    """Return up to ``max_events`` lines from the tail of a log file."""
    lines: list[str] = []
    with log_file.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            lines.append(line)
            if len(lines) > max_events * 2:
                lines = lines[-max_events:]
    return "".join(lines[-max_events:])


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_session(
    bundle: Path,
    *,
    target_log: Path | None = None,
    overwrite: bool = False,
) -> SessionState:
    """Restore a session from a bundle.

    If the bundle contains a log snapshot and no ``target_log`` is given,
    the snapshot is written next to the bundle and used as the log file.
    Otherwise the original log path stored in the session is used.
    """
    bundle = Path(bundle)
    if not bundle.exists():
        raise FileNotFoundError(bundle)

    with zipfile.ZipFile(bundle, "r") as archive:
        try:
            manifest = json.loads(archive.read("manifest.json"))
            session_data = json.loads(archive.read("session.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid session bundle: {exc}") from exc

        if manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError(f"Unknown bundle format: {manifest.get('format')}")

        state = SessionState.from_dict(session_data)

        # If a snapshot is present, extract it next to the bundle.
        if "log-snapshot.jsonl" in archive.namelist():
            if target_log is None:
                target_log = bundle.with_suffix(".jsonl")
            target_log = Path(target_log)
            target_log.parent.mkdir(parents=True, exist_ok=True)
            target_log.write_bytes(archive.read("log-snapshot.jsonl"))
            state.log_file = str(target_log.resolve())
        elif target_log is not None:
            state.log_file = str(Path(target_log).resolve())

    manager = SessionManager()
    if manager.exists(Path(state.log_file)) and not overwrite:
        # Keep the imported state but do not clobber existing work unless
        # the caller explicitly asked for it.
        pass
    manager.save(state)

    if state.name:
        try:
            manager.create_named(
                state.name,
                Path(state.log_file),
                overwrite=overwrite,
            )
        except ValueError:
            # The name may already be taken by another session; that is
            # not fatal, the session state is still saved.
            pass

    return state


def describe_bundle(bundle: Path) -> dict:
    """Read the manifest of a bundle without restoring it."""
    bundle = Path(bundle)
    if not bundle.exists():
        raise FileNotFoundError(bundle)
    with zipfile.ZipFile(bundle, "r") as archive:
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid session bundle: {exc}") from exc
    return manifest