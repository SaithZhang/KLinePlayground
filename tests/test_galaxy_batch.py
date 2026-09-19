"""Real worker protocol with a synthetic SDK; no remote connections or credentials."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from backend import galaxy_data
from scripts import galaxy_worker


def test_batch_reuses_login_market_and_waits_for_final_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / 'project with spaces'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    events = root / 'events.jsonl'
    wrapper = f'''
import sys, types, runpy, json, time
import pandas as pd
from pathlib import Path
log=Path({str(events)!r})
def event(name):
    with log.open('a') as f: f.write(json.dumps(name)+'\\n')
class Base:
    def get_code_list(self, **kwargs): return ['000001.SZ','000002.SZ']
    def get_calendar(self): return [20260917]
    def get_backward_factor(self, codes, **kwargs):
        return pd.DataFrame({{codes[0]:[2.]}}, index=pd.to_datetime(['2026-09-17']))
class Market:
    def __init__(self, calendar): event('market')
    def query_kline(self, codes, period, **kwargs):
        event(codes[0]+':'+period)
        if period=='60m': time.sleep(.4)
        if codes[0]=='000002.SZ': raise ValueError('synthetic provider failure')
        return {{codes[0]:pd.DataFrame([{{'code':codes[0],'kline_time':'2026-09-17','open':10,'close':11,'high':12,'low':9,'volume':100,'amount':1100}}])}}
period=types.SimpleNamespace(**{{k:types.SimpleNamespace(value=v) for k,v in [('day','daily'),('min15','15m'),('min60','60m')]}})
sys.modules['AmazingData']=types.SimpleNamespace(login=lambda **kwargs:event('login'),BaseData=Base,MarketData=Market,constant=types.SimpleNamespace(Period=period))
runpy.run_path({galaxy_worker.__file__!r},run_name='__main__')
'''
    (scripts/'galaxy_worker.py').write_text(wrapper)
    monkeypatch.setattr(galaxy_data, 'ROOT', root)
    env = dict(__import__('os').environ, AD_USERNAME='test', AD_PASSWORD='test', AD_HOST='invalid', AD_PORT='8600', AD_CACHE_DIR=str(root/'cache'))
    monkeypatch.setattr(galaxy_data.galaxy_runtime, 'launch_spec', lambda: ('native',sys.executable,env))
    day = pd.Timestamp('2026-09-17')
    with galaxy_data.galaxy_batch() as batch:
        assert batch.run is None  # entering a batch does not contact the provider
        universe=galaxy_data.query_galaxy(None,day,day,[])
        assert len(universe['codes'])==2
        pid=batch.run.pid
        for code in ('000001.SZ','000002.SZ','000001.SZ'):
            result=galaxy_data.query_galaxy(code,day,day,['daily','15m','60m'])
            assert set(result['periods'])=={'daily','15m','60m'}
            assert batch.run.pid==pid and batch.run.poll() is None
            assert ('error' in result['periods']['daily'])==(code=='000002.SZ')
    assert batch.run.poll() is not None
    assert not batch.directory.exists()
    lines=[json.loads(line) for line in events.read_text().splitlines()]
    assert lines.count('login')==1
    assert lines.count('market')==1
    assert getattr(galaxy_data._batch_local,'session',None) is None


def test_batch_exception_cleans_worker_without_restarting(tmp_path, monkeypatch):
    script=tmp_path/'scripts'/'galaxy_worker.py'
    script.parent.mkdir()
    script.write_text('import time\ntime.sleep(60)\n')
    monkeypatch.setattr(galaxy_data,'ROOT',tmp_path)
    monkeypatch.setattr(galaxy_data.galaxy_runtime,'launch_spec',lambda:('native',sys.executable,{}))
    with pytest.raises(ValueError,match='abort'):
        with galaxy_data.galaxy_batch() as batch:
            batch.start()
            raise ValueError('abort')
    assert batch.run.poll() is not None
    assert not batch.directory.exists()


def test_dead_batch_worker_is_never_relaunched():
    batch=galaxy_data.GalaxyBatch()
    batch.run=SimpleNamespace(poll=lambda:69)
    with pytest.raises(ValueError,match='退出'):
        batch.start()
    assert batch.failed
    with pytest.raises(ValueError,match='未自动重启'):
        batch.start()


def test_sync_job_stops_unprocessed_stocks_when_worker_dies(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from backend.data_jobs import DataJobs
    import time
    batch=SimpleNamespace(failed=False)
    closed=[]
    @contextmanager
    def context():
        try: yield batch
        finally: closed.append(True)
    monkeypatch.setattr(galaxy_data,'galaxy_batch',context)
    def sync(*args):
        batch.failed=True
        raise ValueError('runtime failed')
    manager=SimpleNamespace(get_stock_universe=lambda *a,**k:['000001','000002','000006'],sync_offline_data=sync)
    jobs=DataJobs(tmp_path/'jobs')
    try:
        config={'source':'galaxy','scope':'all'}
        ident=jobs.start('sync',config,lambda ident:jobs.run_sync(ident,manager,config))
        deadline=time.monotonic()+5
        while jobs.get(ident)['status']=='RUNNING' and time.monotonic()<deadline:
            time.sleep(.01)
        result=jobs.get(ident)
        assert result['status']=='FAILED'
        assert result['processed']==1 and result['unprocessed']==2
        assert closed==[True]
    finally: jobs.close()
