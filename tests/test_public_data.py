import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from backend import public_data, galaxy_runtime
from backend.data_manager import DataManager


def test_gui_runner_uses_working_python(monkeypatch, tmp_path):
    runner = tmp_path / 'run_in_docker.sh'
    runner.touch()
    monkeypatch.setenv('KLINE_GALAXY_RUNTIME', 'docker')
    monkeypatch.setenv('KLINE_GALAXY_RUNNER', str(runner))
    monkeypatch.setattr(galaxy_runtime, 'is_windows', lambda: False)
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    _, _, env = galaxy_runtime.launch_spec()
    assert env['PATH'].split(':')[0] == str(Path(galaxy_runtime.sys.executable).parent)


def test_public_timeout_does_not_write_cache(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('public', 90)
    monkeypatch.setattr(public_data.subprocess, 'run', timeout)
    manager = DataManager(str(tmp_path))
    assert not manager.download_stock_data('000001', source='a_stock_data')
    assert not list(tmp_path.rglob('*.csv'))


def test_skill_uses_separate_cache(tmp_path):
    manager = DataManager(str(tmp_path))
    dates = pd.to_datetime(['2026-09-17'])
    raw = pd.DataFrame({'date': dates, 'open': [10], 'close': [11], 'high': [12], 'low': [9], 'volume': [100]})
    factors = pd.DataFrame({'date': dates, 'factor': [2.]})
    manager._save_bundle_to_cache('000001', raw, factors, source='a_stock_data')
    assert manager._load_cached_kline('000001') is None
    assert manager._load_cached_factor('000001') is None
    assert manager.get_stock_data('000001', source='a_stock_data').close.iloc[0] == 11
    assert manager.get_factor_data('000001', source='a_stock_data').factor.iloc[0] == 2


def test_skill_factor_asof_and_volume_units(monkeypatch):
    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            bars = [['2024-01-02', '10','11','12','9','2',{},'1','3'],
                    ['2024-01-03', '10','11','12','9','2',{},'1','3']]
            return SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {'code': 0, 'data': {'sz000001': {'day': bars}}},
                text='var hfq={"data":[{"d":"2024-01-03","f":"4"},{"d":"2020-01-01","f":"2"}]} /* trailing */')
    monkeypatch.setattr(public_data, 'session', Client)
    bundle = public_data.fetch_skill('000001', '2024-01-01', '2024-01-04')
    assert bundle['factor'].factor.tolist() == [2, 4]
    assert bundle['kline'].volume.tolist() == [200, 200]
    assert bundle['kline'].amount.tolist() == [30000, 30000]


def test_public_session_does_not_mutate_global_proxy(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://invalid:1234')
    monkeypatch.delenv('KLINE_PUBLIC_USE_PROXY', raising=False)
    with public_data.session() as client:
        assert not client.trust_env
    import os
    assert os.environ['HTTPS_PROXY'] == 'http://invalid:1234'


def test_galaxy_factor_cache_is_per_symbol_and_reused(tmp_path, monkeypatch):
    import sys
    from scripts.galaxy_worker import main
    calls = []
    code = '000001.SZ'
    class Base:
        def get_calendar(self): return [20260917, 20260918]
        def get_backward_factor(self, codes, local_path, is_local):
            calls.append(local_path)
            assert codes == [code] and local_path.endswith(code + '/')
            return pd.DataFrame({code: [2., 2.]}, index=pd.to_datetime(['2026-09-17','2026-09-18']))
    monkeypatch.setitem(sys.modules, 'AmazingData', SimpleNamespace(
        login=lambda **kwargs: None, BaseData=Base, MarketData=lambda calendar: None))
    for name in ('AD_USERNAME','AD_PASSWORD','AD_HOST','AD_PORT'):
        monkeypatch.setenv(name, '8600' if name == 'AD_PORT' else 'synthetic')
    monkeypatch.setenv('AD_CACHE_DIR', str(tmp_path))
    request, output = tmp_path/'request.json', tmp_path/'out.json'
    request.write_text(json.dumps({'code':code,'start':'20260917','end':'20260918','periods':[]}))
    main(str(request), str(output))
    assert len(json.loads(output.read_text())['factors']) == 2
    main(str(request), str(output))
    assert len(calls) == 1
    cache = tmp_path/'kline_playground'/code/'verified_factors.json'
    data = json.loads(cache.read_text())
    data['rows'] = data['rows'][:1]
    cache.write_text(json.dumps(data))
    main(str(request), str(output))
    assert len(calls) == 2  # incomplete cached coverage is never treated as ready


@pytest.mark.parametrize('code', ['000001', '000002', '000006'])
def test_public_sync_coexists_with_galaxy_and_other_sources(tmp_path, monkeypatch, code):
    manager = DataManager(str(tmp_path))
    dates = pd.to_datetime(['2026-09-17', '2026-09-18'])
    galaxy = Path(manager._get_galaxy_file(code))
    galaxy.parent.mkdir(parents=True)
    raw = pd.DataFrame({'date': dates, 'open': [10,10], 'close': [11,11],
                        'high': [12,12], 'low': [9,9], 'volume': [100,100], 'amount': [1100,1100]})
    factors = pd.DataFrame({'date': dates, 'factor': [2.,2.]})
    raw.assign(factor=7,source='galaxy').to_csv(galaxy,index=False)
    original = galaxy.read_bytes()
    # Legacy generic archives must remain untouched too.
    legacy = Path(manager.offline_dir)/(code+'.SZ.csv')
    raw.assign(factor=8).to_csv(legacy,index=False)
    old_legacy = legacy.read_bytes()
    monkeypatch.setattr(manager,'_fetch_stock_bundle',lambda *a,**kw: {'kline': raw.assign(source='tencent'), 'factor': factors})
    monkeypatch.setattr(manager,'_fetch_factor_range',lambda *a,**kw: factors)
    for source in ('a_stock_data','akshare'):
        result = manager.sync_offline_data(code,source,'2026-09-17','2026-09-18')
        assert result['success'] and result['fetched_rows']==2
        assert result['rows_before']==0
        assert result['offline_path']==manager._get_public_archive(code,source)
        assert manager.get_factor_data(code,source).factor.tolist()==[2.,2.]
        assert manager.get_stock_data(code,source).close.tolist()==[11,11]
    public_path = Path(manager._get_public_archive(code,'a_stock_data'))
    public_before = public_path.read_bytes()
    again = manager.sync_offline_data(code,'a_stock_data','2026-09-17','2026-09-18')
    assert again['rows_before']==2 and again['fetched_rows']==0
    assert public_path.read_bytes()==public_before
    assert galaxy.read_bytes()==original and legacy.read_bytes()==old_legacy
    assert manager.get_factor_data(code,'galaxy').factor.tolist()==[7,7]


def test_public_archive_discovered_by_offline_blind_pool(tmp_path, monkeypatch):
    manager=DataManager(str(tmp_path))
    path=Path(manager._get_public_archive('000006','a_stock_data'))
    path.parent.mkdir(parents=True)
    pd.DataFrame({'date':['2024-01-02','2024-01-03'],'open':[10,10],'close':[10,10],
                  'high':[11,11],'low':[9,9],'volume':[100,100],'factor':[2,2]}).to_csv(path,index=False)
    assert manager._get_offline_stock_codes()==['000006']
    assert manager._get_offline_file('000006')==str(path)
    assert manager.get_random_stock(date_start='2024-01-02',date_end='2024-01-03',source='offline')[0]=='000006'
