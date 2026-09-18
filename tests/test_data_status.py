import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from backend.data_jobs import DataJobs, report_progress
from backend.data_inventory import inventory
from test_galaxy_training import manager, histories, frame, archive, blind_client


@pytest.fixture
def jobs(tmp_path):
    store = DataJobs(tmp_path / 'operations')
    yield store
    store.close()


def finished(jobs, ident):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = jobs.get(ident)
        if result['status'] != 'RUNNING':
            return result
        time.sleep(.01)
    pytest.fail('job did not finish')


def test_galaxy_universe_never_falls_back_to_public_or_local(manager, monkeypatch):
    from backend import data_manager as dm
    monkeypatch.setattr(dm, 'galaxy_stock_codes', lambda _: ['000001', '600000', '920001'])
    monkeypatch.setattr(manager, 'load_stock_list', lambda: pytest.fail('must use Galaxy universe'))
    assert manager.get_stock_universe('all', source='galaxy') == ['000001', '600000', '920001']
    assert manager.get_stock_universe('sh', source='galaxy') == ['600000']
    assert manager.get_stock_universe('sz', source='galaxy') == ['000001']


def test_failed_public_universe_does_not_masquerade_as_full_market(histories, monkeypatch):
    monkeypatch.setattr(histories, 'load_stock_list', lambda: False)
    with pytest.raises(ValueError, match='不会将本地少量股票冒充全市场'):
        histories.get_stock_universe('all')


def test_inventory_explains_exclusion_and_actual_file_range(histories, monkeypatch):
    import shutil
    for period in ('daily', '15m', '60m'):
        shutil.copy(histories._get_galaxy_file('603938', period), histories._get_galaxy_file('000001', period))
    Path(histories._get_galaxy_file('000001', '60m')).unlink()
    monkeypatch.setattr(histories, 'load_stock_list', lambda: pytest.fail('inventory must be local'))
    result = inventory(histories, '2026-09-01', '2026-09-02')
    assert (result['downloaded_stocks'], result['three_period_stocks'], result['eligible_stocks']) == (2, 1, 1)
    missing = next(row for row in result['rows'] if row['stock_code'] == '000001')
    assert missing['reason_code'] == 'MISSING_PERIOD' and '60m' in missing['reason']
    assert result['period_counts']['daily'] == 2
    outside = inventory(histories, '2024-01-01', '2024-12-31')
    assert outside['eligible_stocks'] == 0
    existing = next(row for row in outside['rows'] if row['stock_code'] == '603938')
    assert existing['reason_code'] == 'DATE_RANGE'
    assert '2025-01-01' in existing['periods']['daily']['start']


def test_inventory_failure_receipt_is_not_downloaded_data(manager):
    path = Path(manager._get_galaxy_file('603938')).with_suffix('.receipt.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'status': 'FAILED', 'error': 'NO_BARS'}))
    result = inventory(manager, '2026-01-01', '2026-09-01')
    assert result['downloaded_stocks'] == result['eligible_stocks'] == 0
    assert result['rows'][0]['periods']['daily']['error'] == 'NO_BARS'


def test_batch_keeps_complete_partial_failed_and_unprocessed_distinct(jobs):
    replies = iter([{'success': True}, {'status': 'PARTIAL', 'periods': [{'period': 'daily', 'status': 'PARTIAL', 'missing_bars': ['20260101']}]},
                    {'status': 'FAILED', 'error': 'NO_BARS'}])
    manager = SimpleNamespace(get_stock_universe=lambda *args, **kwargs: ['000001', '000002', '000003'],
                              sync_offline_data=lambda *args: next(replies))
    config = {'scope': 'all', 'source': 'galaxy', 'interval': 'all'}
    ident = jobs.start('sync', config, lambda ident: jobs.run_sync(ident, manager, config))
    result = finished(jobs, ident)
    assert result['status'] == 'PARTIAL'
    assert (result['complete'], result['partial'], result['failed'], result['unprocessed']) == (1, 1, 1, 0)
    assert len(json.loads((jobs.directory / (ident + '.json')).read_text())['records']) == 3
    assert (jobs.directory.parent / 'logs' / 'operations.jsonl').is_file()


def test_all_failed_stops_without_claiming_completion(jobs):
    manager = SimpleNamespace(get_stock_universe=lambda *args, **kwargs: ['1', '2', '3', '4', '5'],
                              sync_offline_data=lambda *args: {'status': 'FAILED', 'error': 'NO_BARS'})
    config = {'scope': 'all', 'source': 'galaxy'}
    ident = jobs.start('sync', config, lambda ident: jobs.run_sync(ident, manager, config))
    result = finished(jobs, ident)
    assert result['status'] == 'FAILED'
    assert result['processed'] == result['failed'] == 3
    assert result['unprocessed'] == 2
    assert '连续3只' in result['error']


def test_stages_visible_during_work_and_stop_preserves_remaining(jobs):
    entered, release = threading.Event(), threading.Event()
    def sync(*args):
        report_progress('FACTORS', stock_code='000001', password='must-not-persist')
        entered.set()
        release.wait(3)
        return {'success': True}
    manager = SimpleNamespace(get_stock_universe=lambda *args, **kwargs: ['000001', '000002'], sync_offline_data=sync)
    config = {'scope': 'all', 'source': 'galaxy'}
    ident = jobs.start('sync', config, lambda ident: jobs.run_sync(ident, manager, config))
    try:
        assert entered.wait(2)
        assert jobs.get(ident)['stage'] == 'FACTORS'
        with pytest.raises(ValueError, match='已有补数任务'):
            jobs.start('sync', config, lambda _: None)
        jobs.stop(ident)
    finally:
        release.set()
    result = finished(jobs, ident)
    assert result['status'] == 'STOPPED'
    assert result['processed'] == 1 and result['unprocessed'] == 1
    for path in jobs.directory.parent.rglob('*'):
        if path.is_file():
            assert 'must-not-persist' not in path.read_text()


def test_restart_marks_unfinished_job_interrupted_without_replaying(tmp_path):
    path = tmp_path / 'operations'; path.mkdir()
    (path / 'saved.json').write_text(json.dumps(dict(id='saved', kind='sync', status='RUNNING',
        config={}, records=[], total=3, processed=1, started_at=time.time(), stage_started_at=time.time())))
    store = DataJobs(path)
    try:
        result = store.get('saved')
        assert result['status'] == 'INTERRUPTED' and result['unprocessed'] == 2
        assert '未自动重试' in result['error']
    finally:
        store.close()


def test_training_progress_hides_stock_but_local_diagnostic_log_keeps_it(jobs):
    entered, release = threading.Event(), threading.Event()
    def work(_):
        report_progress('CACHE_MISS', stock_code='603938')
        entered.set(); release.wait(2)
        return {'id': 'training'}
    ident = jobs.start('training', {}, work)
    try:
        assert entered.wait(1)
        assert 'stock_code' not in jobs.get(ident)
    finally:
        release.set()
    assert finished(jobs, ident)['result']['id'] == 'training'
    assert '603938' in (jobs.directory.parent / 'logs' / 'operations.jsonl').read_text()


@pytest.mark.parametrize('period', ['daily', '15m', '60m'])
def test_background_training_returns_usable_session(blind_client, jobs, monkeypatch, period):
    from backend import app_enhanced as api
    monkeypatch.setattr(api, 'data_jobs', jobs)
    response = blind_client.post('/api/training/start', json={
        'user': 'blind-test', 'mode': 'random', 'data_source': 'galaxy', 'period': period,
        'background': True, 'date_start': '2026-09-01', 'date_end': '2026-09-02'})
    assert response.status_code == 202
    ident = response.json['job_id']
    job = finished(jobs, ident)
    assert job['status'] == 'COMPLETE', job
    result = blind_client.get(f'/api/training/{job["result"]["id"]}/data?view_period={period}&ma_periods=55,233')
    assert result.status_code == 200 and result.json['kline_data']
    assert blind_client.get(f'/api/data/jobs/{ident}').json['result']['id'] == job['result']['id']


def test_job_api_validates_and_persists_only_approved_fields(blind_client, jobs, monkeypatch):
    from backend import app_enhanced as api
    monkeypatch.setattr(api, 'data_jobs', jobs)
    monkeypatch.setattr(api.data_manager, 'sync_offline_data', lambda *args: {'status': 'COMPLETE'})
    response = blind_client.post('/api/data/jobs', json={
        'source': 'galaxy', 'stock_code': '000001', 'start_date': '2026-09-17', 'end_date': '2026-09-17',
        'password': 'must-not-save-this'})
    assert response.status_code == 202
    job = finished(jobs, response.json['job_id'])
    assert job['status'] == 'COMPLETE' and job['config']['scope'] == 'single'
    assert job['config']['interval'] == 'daily'
    assert 'password' not in job['config']
    assert blind_client.get('/api/data/jobs').json['jobs'][0]['id'] == job['id']
    assert len(blind_client.get(f'/api/data/jobs/{job["id"]}?full=1').json['records']) == 1
    assert blind_client.post('/api/data/jobs', json={'source': 'akshare', 'interval': '15m', 'stock_code': '000001'}).status_code == 400
    assert blind_client.get('/api/data/jobs/absent').status_code == 404
    coverage = blind_client.get('/api/data/inventory?date_start=2026-09-01&date_end=2026-09-02')
    assert coverage.status_code == 200 and coverage.json['eligible_stocks'] == 2


def test_fast_local_training_logs_selection_and_duration_without_user_credentials(blind_client, jobs, monkeypatch):
    from backend import app_enhanced as api
    monkeypatch.setattr(api, 'data_jobs', jobs)
    response = blind_client.post('/api/training/start', json={
        'user': 'blind-test', 'mode': 'random', 'data_source': 'galaxy', 'period': 'daily',
        'date_start': '2026-09-01', 'date_end': '2026-09-02', 'password': 'do-not-log'})
    assert response.status_code == 200
    log_text = (jobs.directory.parent / 'logs' / 'operations.jsonl').read_text()
    events = [json.loads(line) for line in log_text.splitlines()]
    result = next(event for event in events if event['event'] == 'TRAINING_RESULT')
    assert result['pool_size'] == 2 and result['stock_code'] == response.json['stock_code']
    assert result['elapsed_seconds'] >= 0
    assert 'do-not-log' not in log_text and 'blind-test' not in log_text
