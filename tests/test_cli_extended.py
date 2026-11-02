"""CLI integration and checkpoint resume behavior without external services."""
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from ttd import cli
from ttd.models import LogEvent


def test_loki_cli_checkpoint_and_resume(monkeypatch, tmp_path):
    monkeypatch.setenv("TTD_CHECKPOINT_DIR", str(tmp_path / 'checkpoints'))
    stamp = datetime(2024,10,26,tzinfo=timezone.utc)
    calls = []
    class MockClient:
        def stream_range(self, expression, *, start, end):
            calls.append((start,end))
            return [LogEvent.from_raw({
                'timestamp': (start+timedelta(seconds=1)).isoformat(),
                'service': 'test',
                'message': 'fetched',
            })]
    monkeypatch.setattr(cli, '_client', lambda *args, **kwargs: MockClient())
    args=['loki','{app="test"}','--start',stamp.isoformat(),
          '--end',(stamp+timedelta(seconds=20)).isoformat(),
          '--output',str(tmp_path/'log.jsonl'), '--checkpoint-name','integration']
    runner = CliRunner()
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.output + repr(first.exception)
    assert len(calls) == 1
    assert (tmp_path / 'log.jsonl').read_text().count('fetched') == 1
    again = runner.invoke(cli.app, args + ['--resume'])
    assert again.exit_code == 0, again.output + repr(again.exception)
    assert len(calls) == 1
    assert (tmp_path / 'log.jsonl').read_text().count('fetched') == 1


def test_validate_alerts_and_sessions(runner=None):
    runner=CliRunner()
    assert runner.invoke(cli.app,['alerts','validate']).exit_code == 0
    assert runner.invoke(cli.app,['sessions','--help']).exit_code == 0
    assert runner.invoke(cli.app,['distributed','--help']).exit_code == 0
