"""Session and checkpoint durability tests."""
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ttd.checkpoint import CheckpointState, CheckpointStore, CheckpointTracker
from ttd.session import SessionManager, SessionState


def test_named_session(tmp_path):
    mgr = SessionManager(root=tmp_path)
    logfile = tmp_path / "sample.jsonl"
    logfile.write_text('', encoding='utf-8')
    mgr.save(SessionState(log_file=str(logfile), query='level=ERROR'))
    named = mgr.create_named('incident-a', logfile)
    assert named.name == 'incident-a'
    assert mgr.get_named('incident-a') is not None
    assert mgr.load(logfile).query == 'level=ERROR'
    assert mgr.delete_named('incident-a')


def test_checkpoint_resume(tmp_path):
    start = datetime(2024,1,1,tzinfo=timezone.utc)
    midpoint = start + timedelta(minutes=5)
    end = midpoint + timedelta(minutes=5)
    state = CheckpointState('case-a', '{app="api"}', start.isoformat(), end.isoformat(), 'single')
    store = CheckpointStore(root=tmp_path)
    tracker = CheckpointTracker(store, state)
    tracker.record_chunk_complete(start, midpoint, 42)
    tracker.flush()
    loaded = store.load('case-a')
    assert loaded is not None
    assert loaded.remaining_gaps() == [(midpoint,end)]
    tracker.mark_complete()
