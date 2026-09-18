from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.data_manager import DataManager
from backend import galaxy_data
from backend import galaxy_runtime
from backend.kline_processor_enhanced import KLineProcessorEnhanced
from backend.trade_simulator_enhanced import TradeSimulatorEnhanced


def frame(dates, value=10):
    dates = pd.to_datetime(dates)
    return pd.DataFrame({"date": dates, "open": value, "high": value + 1,
                         "low": value - 1, "close": value, "volume": 10000,
                         "amount": 100000, "factor": 1.0})


def archive(manager, period, data):
    path = Path(manager.offline_dir) / "galaxy" / period / "603938.SH.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    data.to_csv(path, index=False)
    return path


@pytest.fixture
def manager(tmp_path):
    return DataManager(str(tmp_path / "data"))


@pytest.fixture
def histories(manager):
    days = pd.bdate_range("2025-01-01", "2026-09-18")
    archive(manager, "daily", frame(days))
    for period in ("15m", "60m"):
        dates = [day.strftime("%Y-%m-%d") + " " + clock for day in days
                 for clock in galaxy_data.expected_times(period)]
        archive(manager, period, frame(dates))
    return manager


def payload(period="daily", dates=("2026-09-17", "2026-09-18")):
    bars = frame(dates).drop(columns="factor")
    if period != "daily":
        bars["date"] -= pd.Timedelta(minutes=int(period[:-1]))
    bars["date"] = bars.date.astype(str)
    return {"calendar": ["20260917", "20260918"],
            "factors": [["2026-09-17", 2], ["2026-09-18", 2]],
            "periods": {period: {"rows": bars.to_dict("records")}}}


def test_sync_rechecks_internal_holes_without_duplicates(manager, monkeypatch):
    archive(manager, "daily", frame(["2026-09-16", "2026-09-18"]))
    monkeypatch.setattr(galaxy_data, "query_galaxy", lambda *args: payload())
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-18")
    assert result["status"] == "COMPLETE"
    assert result["added_rows"] == 1
    data = manager.get_stock_data("603938", "offline")
    assert len(data) == 3
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-18")
    assert result["added_rows"] == 0


def test_missing_slots_are_partial_and_preserve_other_provider(manager, monkeypatch):
    legacy = Path(manager.offline_dir) / "603938.SH.csv"
    frame(["2026-09-01"]).to_csv(legacy, index=False)
    original = legacy.read_bytes()
    monkeypatch.setattr(galaxy_data, "query_galaxy", lambda *args: payload(dates=["2026-09-17"]))
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-18")
    assert not result["success"]
    assert result["periods"][0]["missing_bars"] == ["2026-09-18 00:00"]
    assert legacy.read_bytes() == original


@pytest.mark.parametrize("error", ["NO_BARS", "GALAXY_QUERY_RuntimeError"])
def test_empty_or_failed_refresh_preserves_file(manager, monkeypatch, error):
    path = archive(manager, "daily", frame(["2026-09-17"]))
    before = path.read_bytes()
    reply = payload()
    reply["periods"]["daily"] = {"error": error}
    monkeypatch.setattr(galaxy_data, "query_galaxy", lambda *args: reply)
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-18", force_full=True)
    assert not result["success"]
    assert not result["local_file_changed"]
    assert path.read_bytes() == before


def test_missing_factor_is_not_filled_with_one(manager, monkeypatch):
    reply = payload()
    reply["factors"] = [["2026-09-17", 2]]
    monkeypatch.setattr(galaxy_data, "query_galaxy", lambda *args: reply)
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-18")
    assert not result["local_file_changed"]
    assert result["periods"][0]["error"] == "MISSING_OR_INVALID_FACTOR"


@pytest.mark.parametrize("period,count", [("15m", 16), ("60m", 4)])
def test_minute_archive_round_trip(manager, monkeypatch, period, count):
    dates = ["2026-09-17 " + clock for clock in galaxy_data.expected_times(period)]
    reply = payload(period, dates)
    reply["calendar"] = ["20260917"]
    monkeypatch.setattr(galaxy_data, "query_galaxy", lambda *args: reply)
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-17", interval=period)
    assert result["success"]
    loaded = manager.get_stock_data("603938", "offline", period)
    assert len(loaded) == count
    assert loaded.date.iloc[-1] == pd.Timestamp("2026-09-17 15:00")
    assert loaded.source_time.iloc[0] == "2026-09-17 09:30:00"
    assert manager.get_training_validation_error("603938", "2026-09-17", "offline", period) is None
    assert manager.get_random_stock(date_start="2026-09-17", date_end="2026-09-17", source="offline", interval=period)[0] == "603938"


def test_no_silent_daily_fallback(manager):
    archive(manager, "daily", frame(["2026-09-17"]))
    assert manager.get_stock_data("603938", "offline", "15m") is None
    with pytest.raises(ValueError, match="分钟"):
        manager.get_stock_data("603938", "akshare", "15m")


@pytest.mark.parametrize("mutation", ["negative", "nan", "duplicate", "out_of_session"])
def test_invalid_provider_data_does_not_replace_archive(manager, monkeypatch, mutation):
    path = archive(manager, "15m", frame(["2026-09-17 09:45"]))
    original = path.read_bytes()
    reply = payload("15m", ["2026-09-17 09:45"])
    rows = reply["periods"]["15m"]["rows"]
    if mutation == "negative":
        rows[0]["close"] = -10
    elif mutation == "nan":
        rows[0]["volume"] = float("nan")
    elif mutation == "duplicate":
        rows.append(rows[0].copy())
    else:
        rows[0]["date"] = "2026-09-17 12:00:00"
    monkeypatch.setattr(galaxy_data, "query_galaxy", lambda *args: reply)
    result = manager.sync_offline_data("603938", "galaxy", "2026-09-17", "2026-09-18", interval="15m", force_full=True)
    assert result["status"] == "FAILED"
    assert path.read_bytes() == original


def test_custom_ma233_available_at_start_and_updates(histories):
    data = histories.get_stock_data("603938", "offline", "15m")
    data["close"] = np.arange(len(data)) + 10.0
    archive(histories, "15m", data.assign(factor=1.0))
    processor = KLineProcessorEnhanced(histories, "603938", "2026-09-15", "offline", "15m")
    ma = processor.get_ma_data([55, 233], "15m")
    assert ma[233][-1]["value"] == pytest.approx(data.close.iloc[processor.start_index-232:processor.start_index+1].mean())
    processor.next_bar()
    assert processor.get_ma_data([233], "15m")[233][-1]["value"] == pytest.approx(ma[233][-1]["value"] + 1)


def test_cross_period_views_do_not_reveal_future(histories):
    processor = KLineProcessorEnhanced(histories, "603938", "2026-09-15", "offline", "15m")
    processor.set_adjustment("dynamic_forward")
    assert processor.full_data.iloc[processor.current_index].date == pd.Timestamp("2026-09-15 09:45")
    daily_before = processor.get_visible_data("daily")
    assert pd.to_datetime(daily_before[-1]["time"], unit="s").date().isoformat() == "2026-09-14"
    assert pd.to_datetime(processor.get_visible_data("60m")[-1]["time"], unit="s") == pd.Timestamp("2026-09-14 15:00")
    # Poison future daily bars/factors. Visible daily bars and MA must remain identical.
    future = processor._context_frames["daily"].date >= pd.Timestamp("2026-09-15")
    processor._context_frames["daily"].loc[future, ["open", "high", "low", "close", "factor"]] = 99999
    assert processor.get_visible_data("daily") == daily_before
    for _ in range(3):
        processor.next_bar()
    assert pd.to_datetime(processor.get_visible_data("60m")[-1]["time"], unit="s") == pd.Timestamp("2026-09-15 10:30")
    assert processor.get_visible_data("daily") == daily_before
    assert processor.get_practice_context()["daily"]["ma233_ready"]


def test_weekly_label_and_ohlc_use_only_known_days(histories):
    processor = KLineProcessorEnhanced(histories, "603938", "2026-09-15", "offline", "15m")
    weekly = processor.get_visible_data("weekly")
    assert pd.to_datetime(weekly[-1]["time"], unit="s") == pd.Timestamp("2026-09-14")
    assert weekly[-1]["volume"] == 10000


def test_tplus_one_and_separate_minute_trade_records(tmp_path):
    simulator = TradeSimulatorEnhanced("test", 100000, "603938", users_dir=str(tmp_path))
    simulator.update_current_price(10, 1)
    assert simulator.buy(1, 10, "2026-09-17")["success"]
    simulator.update_current_price(11, 2)
    assert simulator.buy(1, 11, "2026-09-17")["success"]
    assert len(simulator.trade_history) == 2
    assert not simulator.sell(1, 11, "2026-09-17")["success"]
    assert simulator.sell(1, 11, "2026-09-18")["success"]


def test_api_ma55_preset_and_context(histories, tmp_path, monkeypatch, api_operations):
    from backend import app_enhanced as api
    from backend.user_manager_enhanced import UserManagerEnhanced
    monkeypatch.setattr(api, "data_manager", histories)
    monkeypatch.setattr(api, "users_dir_path", str(tmp_path / "users"))
    monkeypatch.setattr(api, "user_manager", UserManagerEnhanced(str(tmp_path / "users")))
    monkeypatch.setattr(api, "active_trainings", {})
    monkeypatch.setattr(api, "_update_api_info", lambda **kwargs: None)
    client = api.app.test_client()
    response = client.post("/api/training/start", json={"user": "test", "mode": "specified",
                           "stock_code": "603938", "start_date": "2026-09-15", "practice": "ma55", "data_source": "offline"})
    assert response.status_code == 200, response.json
    session = response.json
    assert session["period"] == "15m"
    assert session["adjustment_mode"] == "dynamic_forward"
    url = f'/api/training/{session["id"]}'
    data = client.get(url + "/data?view_period=15m&ma_periods=55,233").json
    assert data["ma_data"]["233"]
    assert data["practice_context"]["daily"]["as_of"].startswith("2026-09-14")
    assert client.post(url + "/next").json["progress"]["current_time"] == "2026-09-15 10:00:00"
    daily = client.get(url + "/data?view_period=daily&ma_periods=55,233").json
    assert len(daily["kline_data"]) == data["practice_context"]["daily"]["bars"]
    assert client.get(url + "/full_data?view_period=60m&ma_periods=55,233").status_code == 200


def test_runtime_timeout_stops_only_its_own_container(tmp_path, monkeypatch):
    import json
    import subprocess
    from types import SimpleNamespace

    monkeypatch.setattr(galaxy_data, "ROOT", tmp_path)
    runner = tmp_path / "runner.sh"
    runner.touch()
    monkeypatch.setattr(galaxy_runtime, "runner_path", lambda: runner)
    monkeypatch.setattr(galaxy_runtime, "is_windows", lambda: False)
    monkeypatch.setenv("KLINE_GALAXY_RUNTIME", "docker")
    command = []
    calls = []

    class Process:
        pid = 999991
        def __init__(self, args, **kwargs):
            command.extend(args)
        def poll(self):
            return None
        def wait(self, timeout):
            if timeout == 5:
                raise subprocess.TimeoutExpired(command, timeout)
            return 0

    def run(args, **kwargs):
        calls.append(args)
        if args[1] == "ps":
            return SimpleNamespace(stdout="ours\nother\n", returncode=0)
        if args[1] == "inspect":
            return SimpleNamespace(stdout=json.dumps(command[1:] if args[2] == "ours" else ["unrelated-worker.py"]), returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    ticks = iter([0, 181])
    monkeypatch.setattr(galaxy_data.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(galaxy_data.subprocess, "Popen", Process)
    monkeypatch.setattr(galaxy_data.subprocess, "run", run)
    killed = []
    monkeypatch.setattr(galaxy_data.os, "killpg", lambda *args: killed.append(args), raising=False)
    with pytest.raises(ValueError, match="超时"):
        galaxy_data.query_galaxy("603938.SH", pd.Timestamp("2026-09-17"), pd.Timestamp("2026-09-18"), ["daily"])
    assert len(killed) == 1
    assert [args[-1] for args in calls if args[1] == "stop"] == ["ours"]


def test_galaxy_source_never_reads_public_archive(manager):
    legacy = Path(manager.offline_dir) / '603938.SH.csv'
    frame(['2026-09-17']).to_csv(legacy, index=False)
    assert manager.get_stock_data('603938', 'galaxy') is None
    assert manager.get_factor_data('603938', 'galaxy') is None


@pytest.mark.parametrize('period', ['daily', '15m', '60m'])
def test_galaxy_training_source_round_trip(histories, period):
    bars = histories.get_stock_data('603938', 'galaxy', period)
    factors = histories.get_factor_data('603938', 'galaxy', period)
    assert len(bars) == len(factors) > 233
    processor = KLineProcessorEnhanced(histories, '603938', '2026-09-15', 'galaxy', period)
    assert processor.get_ma_data([55, 233], view_period=period)[233]
    before = processor.get_progress()['current_bar_id']
    assert processor.next_bar()
    assert processor.get_progress()['current_bar_id'] == before + 1


def test_galaxy_universe_cache_is_source_owned(manager, monkeypatch):
    import json
    calls = []
    def query(*args):
        calls.append(args)
        return {'codes': ['603938.SH', '000001.SZ', '000001.SZ', 'invalid', 'not-a-stock']}
    monkeypatch.setattr(galaxy_data, 'query_galaxy', query)
    assert galaxy_data.galaxy_stock_codes(manager) == ['000001', '603938']
    assert galaxy_data.galaxy_stock_codes(manager) == ['000001', '603938']
    assert len(calls) == 1
    assert calls[0][0] is None
    assert json.loads((Path(manager.data_dir) / 'galaxy_universe.json').read_text())['source'] == 'galaxy'


def test_cached_episode_reuses_three_periods_and_reports_gaps(histories, monkeypatch):
    import json
    for period in galaxy_data.PERIODS:
        path = Path(histories._get_galaxy_file('603938', period)).with_suffix('.receipt.json')
        path.write_text(json.dumps({'status': 'PARTIAL', 'missing_bars': ['2026-07-01'],
                                   'requested_range': {'end': '2099-01-01'}}))
    def no_network(*args):
        pytest.fail('a covered episode must not redownload')
    monkeypatch.setattr(galaxy_data, 'query_galaxy', no_network)
    coverage = galaxy_data.prepare_galaxy_training(histories, '603938', '2026-09-01')
    assert all(item['status'] == 'PARTIAL' and item['missing_count'] == 1 for item in coverage)


def test_failed_galaxy_prepare_does_not_start_using_old_cache(histories, monkeypatch):
    monkeypatch.setattr(galaxy_data, 'sync_galaxy', lambda *args: {
        'periods': [{'period': p, 'status': 'FAILED'} for p in galaxy_data.PERIODS]})
    with pytest.raises(ValueError, match='未使用旧文件'):
        galaxy_data.prepare_galaxy_training(histories, '603938', '2025-02-01')


@pytest.fixture
def blind_client(histories, tmp_path, monkeypatch, api_operations):
    import shutil
    from backend import app_enhanced as api
    from backend import data_manager as dm
    from backend.user_manager_enhanced import UserManagerEnhanced
    for period in galaxy_data.PERIODS:
        shutil.copy(histories._get_galaxy_file('603938', period), histories._get_galaxy_file('000001', period))
    monkeypatch.setattr(dm, 'galaxy_stock_codes', lambda manager: ['603938', '000001'])
    monkeypatch.setattr(dm, 'prepare_galaxy_training', lambda *args: [])
    monkeypatch.setattr(galaxy_data, 'prepare_galaxy_training', lambda *args: [])
    monkeypatch.setattr(dm.random, 'choice', lambda items: items[0])
    monkeypatch.setattr(api, 'data_manager', histories)
    monkeypatch.setattr(api, 'users_dir_path', str(tmp_path / 'users'))
    users = UserManagerEnhanced(str(tmp_path / 'users'))
    users.create_user('blind-test')
    monkeypatch.setattr(api, 'user_manager', users)
    monkeypatch.setattr(api, 'active_trainings', {})
    monkeypatch.setattr(api, '_update_api_info', lambda **kwargs: None)
    return api.app.test_client()


@pytest.mark.parametrize('period', ['daily', '15m', '60m'])
def test_blind_api_changes_stock_and_preserves_source(blind_client, period):
    config = {'user': 'blind-test', 'mode': 'random', 'data_source': 'galaxy', 'period': period,
              'date_start': '2026-09-01', 'date_end': '2026-09-02'}
    first = blind_client.post('/api/training/start', json=config)
    second = blind_client.post('/api/training/start', json=config)
    assert first.status_code == second.status_code == 200, (first.json, second.json)
    assert first.json['stock_code'] != second.json['stock_code']
    assert first.json['id'] != second.json['id']
    assert second.json['data_source'] == 'galaxy'
    assert second.json['period'] == period
    for view in ('daily', '15m', '60m'):
        reply = blind_client.get(f'/api/training/{second.json["id"]}/data?view_period={view}&ma_periods=55,233')
        assert reply.status_code == 200
        assert reply.json['kline_data'] and reply.json['ma_data']['233']


def test_ma55_never_silently_overrides_akshare(blind_client):
    response = blind_client.post('/api/training/start', json={
        'user': 'blind-test', 'mode': 'random', 'practice': 'ma55', 'data_source': 'akshare'})
    assert response.status_code == 400
    assert '请选择银河' in response.json['error']
    response = blind_client.post('/api/training/start', json={
        'user': 'blind-test', 'mode': 'random', 'practice': 'ma55', 'data_source': 'galaxy',
        'date_start': '2026-09-01', 'date_end': '2026-09-02'})
    assert response.status_code == 200, response.json
    assert response.json['data_source'] == 'galaxy'
    assert response.json['period'] == '15m'


def test_offline_pool_excludes_previous_stock(histories, monkeypatch):
    import shutil
    from backend import data_manager as dm
    shutil.copy(histories._get_galaxy_file('603938'), histories._get_galaxy_file('000001'))
    monkeypatch.setattr(dm.random, 'choice', lambda items: items[0])
    code, _ = histories.get_random_stock(date_start='2026-09-01', date_end='2026-09-02',
                                         source='offline', exclude_code='000001')
    assert code == '603938'


def test_worker_reuses_full_calendar_for_factors(tmp_path, monkeypatch):
    import json
    import sys
    from types import SimpleNamespace
    from scripts import galaxy_worker
    calls = []
    full_calendar = [20130104, 20260917, 20260918]
    class Base:
        def get_calendar(self):
            calls.append('calendar_network')
            return full_calendar
        def get_backward_factor(self, codes, **kwargs):
            assert self.get_calendar(symbol='SH') == full_calendar
            return pd.DataFrame({codes[0]: [1., 2.]}, index=pd.to_datetime(['2026-09-17', '2026-09-18']))
    monkeypatch.setitem(sys.modules, 'AmazingData', SimpleNamespace(
        login=lambda **kwargs: None, BaseData=Base, MarketData=lambda days: None))
    for name in ('AD_USERNAME', 'AD_PASSWORD', 'AD_HOST'):
        monkeypatch.setenv(name, 'synthetic')
    monkeypatch.setenv('AD_PORT', '8600')
    monkeypatch.setenv('AD_CACHE_DIR', str(tmp_path / 'cache'))
    request, output = tmp_path / 'request.json', tmp_path / 'output.json'
    request.write_text(json.dumps({'code': '000001.SZ', 'start': '20260917', 'end': '20260918', 'periods': []}))
    galaxy_worker.main(str(request), str(output))
    result = json.loads(output.read_text())
    assert 'error' not in result, result
    assert calls == ['calendar_network']
    assert result['sdk_calendar'] == ['20130104', '20260917', '20260918']
    assert result['calendar'] == ['20260917', '20260918']


def test_local_blind_mode_never_calls_provider(blind_client, monkeypatch):
    from backend import data_manager as dm
    def forbidden(*args, **kwargs):
        pytest.fail('cached blind mode must not call any online preparation')
    monkeypatch.setattr(dm, 'galaxy_stock_codes', forbidden)
    monkeypatch.setattr(dm, 'prepare_galaxy_training', forbidden)
    monkeypatch.setattr(galaxy_data, 'query_galaxy', forbidden)
    response = blind_client.post('/api/training/start', json={
        'user': 'blind-test', 'mode': 'random', 'practice': 'ma55', 'data_source': 'galaxy',
        'date_start': '2026-09-01', 'date_end': '2026-09-02'})
    assert response.status_code == 200, response.json
    assert response.json['galaxy_pool_size'] == 2
    data = blind_client.get(f'/api/training/{response.json["id"]}/data?view_period=15m&ma_periods=55,233')
    assert data.status_code == 200
    assert data.json['ma_data']['233']


def test_empty_local_pool_fails_without_network(manager, monkeypatch):
    from backend import data_manager as dm
    monkeypatch.setattr(dm, 'galaxy_stock_codes', lambda *args: pytest.fail('must stay local'))
    with pytest.raises(ValueError, match='本次未联网'):
        manager.get_random_stock(source='galaxy')


def test_cached_dates_require_three_periods_warmup_and_future(histories):
    dates = galaxy_data.cached_training_dates(histories, '603938')
    assert len(dates) > 0
    for period in galaxy_data.PERIODS:
        bars = histories.get_stock_data('603938', 'galaxy', period)
        assert len(bars[bars.date < dates[0]]) >= 233
        assert len(bars[bars.date >= dates[-1]]) >= 2
    assert dates[-1] < pd.Timestamp('2026-09-18')
    # A rewritten file invalidates the local eligibility cache.
    archive(histories, '60m', frame(['2026-09-01 10:30']))
    assert len(galaxy_data.cached_training_dates(histories, '603938')) == 0


def test_ready_archive_does_not_require_ninety_future_days_or_latest_receipt(histories, monkeypatch):
    import json
    for period in galaxy_data.PERIODS:
        Path(histories._get_galaxy_file('603938', period)).with_suffix('.receipt.json').write_text(json.dumps({
            'status': 'PARTIAL', 'missing_bars': ['2026-07-01'], 'requested_range': {'end': '2026-08-01'}}))
    monkeypatch.setattr(galaxy_data, 'query_galaxy', lambda *args: pytest.fail('already has usable data'))
    coverage = galaxy_data.prepare_galaxy_training(histories, '603938', '2026-09-01')
    assert all(item['status'] == 'PARTIAL' for item in coverage)


def test_full_market_download_requires_explicit_choice(histories, monkeypatch):
    from backend import data_manager as dm
    calls = []
    monkeypatch.setattr(dm, 'galaxy_stock_codes', lambda *args: ['603938'])
    monkeypatch.setattr(dm, 'prepare_galaxy_training', lambda *args: calls.append('prepare'))
    code, _ = histories.get_random_stock(source='galaxy', date_start='2026-09-01', date_end='2026-09-02',
                                         allow_download=True)
    assert code == '603938' and calls == ['prepare']


def test_cached_stock_name_does_not_fetch_missing_public_list(manager, monkeypatch):
    import json
    Path(manager.data_dir, 'stock_names.json').write_text(json.dumps({'000001': '平安银行'}))
    monkeypatch.setattr(manager, 'load_stock_list', lambda: pytest.fail('name lookup must stay local'))
    assert manager.get_stock_name('000001') == '平安银行'
