"""Local shared-storage protocol tests; external SMB/NFS not simulated."""
from datetime import datetime, timedelta, timezone

from ttd.distributed.coordinator import DistributedCoordinator, WorkerIdentity
from ttd.distributed.fetcher import DistributedFetcher
from ttd.models import LogEvent


def test_distributed_resume_and_merge(tmp_path):
    start = datetime(2024,10,26,tzinfo=timezone.utc)
    end = start + timedelta(seconds=30)
    a = DistributedCoordinator(tmp_path,'case-1',worker=WorkerIdentity('a','host-a',1))
    manifest = a.initialize_job(query='{app="test"}', start=start,end=end,mode='single',chunk_seconds=10)
    assert manifest.total_chunks == 3
    b = DistributedCoordinator(tmp_path,'case-1',worker=WorkerIdentity('b','host-b',2))
    b.join()
    one = a.next_chunk()
    assert one is not None
    assert a.renew_lease(one)
    class FakeClient:
        def stream_range(self, query, *, start, end, max_events=None):
            return [LogEvent.from_raw({'timestamp':(start+timedelta(seconds=1)).isoformat(),'message':start.isoformat()})]
    a.complete_chunk(one, list(FakeClient().stream_range('x',start=datetime.fromisoformat(one.start),end=datetime.fromisoformat(one.end))))
    result = DistributedFetcher(b,FakeClient(), '{app="test"}').run()
    assert result['chunks_processed'] == 2
    assert b.progress()['completed'] == 3
    merged = b.merge_results()
    assert len(merged.read_text(encoding='utf-8').splitlines()) == 3
    assert len(b.merge_results().read_text(encoding='utf-8').splitlines()) == 3
