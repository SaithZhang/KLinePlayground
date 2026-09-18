from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.data_manager import DataManager
from backend import galaxy_data
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


def test_api_ma55_preset_and_context(histories, tmp_path, monkeypatch):
    from backend import app_enhanced as api
    from backend.user_manager_enhanced import UserManagerEnhanced
    monkeypatch.setattr(api, "data_manager", histories)
    monkeypatch.setattr(api, "users_dir_path", str(tmp_path / "users"))
    monkeypatch.setattr(api, "user_manager", UserManagerEnhanced(str(tmp_path / "users")))
    monkeypatch.setattr(api, "active_trainings", {})
    monkeypatch.setattr(api, "_update_api_info", lambda **kwargs: None)
    client = api.app.test_client()
    response = client.post("/api/training/start", json={"user": "test", "mode": "specified",
                           "stock_code": "603938", "start_date": "2026-09-15", "practice": "ma55"})
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
    monkeypatch.setattr(galaxy_data, "runner_path", lambda: runner)
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
    monkeypatch.setattr(galaxy_data.os, "killpg", lambda *args: killed.append(args))
    with pytest.raises(ValueError, match="超时"):
        galaxy_data.query_galaxy("603938.SH", pd.Timestamp("2026-09-17"), pd.Timestamp("2026-09-18"), ["daily"])
    assert len(killed) == 1
    assert [args[-1] for args in calls if args[1] == "stop"] == ["ours"]
