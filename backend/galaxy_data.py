"""Shared Galaxy archive contract for Windows native and macOS Docker workers."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time

import numpy as np
import pandas as pd

from backend import galaxy_runtime
from backend.data_jobs import report_progress

PERIODS = ("daily", "15m", "60m")
ROOT = Path(__file__).resolve().parents[1]
SYNC_LOCK = threading.Lock()


def stop_runtime(run, mode, command, env):
    if galaxy_runtime.is_windows():
        galaxy_runtime.stop_windows_process(run)
    else:
        try:
            os.killpg(run.pid, signal.SIGTERM)
            run.wait(timeout=10)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(run.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            run.wait(timeout=5)
    if mode == "docker":
        # Keep the Mac skill's isolation: only stop a container with this exact request.
        ids = subprocess.run(["docker", "ps", "-q", "--filter", "ancestor=" + env["AD_DOCKER_IMAGE"]],
                             capture_output=True, text=True, timeout=10).stdout.split()
        for container_id in ids:
            inspected = subprocess.run(["docker", "inspect", container_id, "--format", "{{json .Config.Cmd}}"],
                                       capture_output=True, text=True, timeout=10)
            if inspected.returncode == 0 and json.loads(inspected.stdout) == command[1:]:
                subprocess.run(["docker", "stop", "-t", "3", container_id],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def query_galaxy(code, start, end, periods):
    report_progress('RUNTIME')
    mode, executable, env = galaxy_runtime.launch_spec()
    runtime = ROOT / "data" / "galaxy_requests"
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as tmp:
        request, output = Path(tmp) / "request.json", Path(tmp) / "output.json"
        body = {"code": code, "start": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"), "periods": periods}
        calendar_path = ROOT / "data" / "galaxy_calendar.json"
        if calendar_path.is_file():
            calendar = json.loads(calendar_path.read_text(encoding="utf-8"))
            if (calendar.get("full") and calendar.get("as_of") == pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
                    and calendar["start"] <= body["start"] and calendar["end"] >= body["end"]):
                body["calendar"] = [int(day) for day in calendar["dates"]]
        request.write_text(json.dumps(body), encoding="utf-8")
        command = [executable, str(ROOT / "scripts" / "galaxy_worker.py"), str(request), str(output)]
        try:
            run = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, **galaxy_runtime.process_options())
        except OSError:
            raise ValueError("银河运行环境启动失败，请检查原生 Python 或 Docker skill 配置") from None
        started = last_progress = time.monotonic()
        stage = "RUNTIME"
        stage_path = Path(str(output) + ".stage")
        try:
            # QEMU initialization, factors and each period can each take over a minute.
            # Bound both a stalled phase and the whole request; never retry automatically.
            while run.poll() is None:
                try:
                    run.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    observed_stage = stage_path.read_text(encoding="utf-8") if stage_path.exists() else stage
                    if observed_stage and observed_stage != stage:
                        stage, last_progress = observed_stage, time.monotonic()
                        report_progress(stage)
                    if time.monotonic() - last_progress > 180 or time.monotonic() - started > 480:
                        raise
        except subprocess.TimeoutExpired:
            stop_runtime(run, mode, command, env)
            if output.is_file():
                partial = json.loads(output.read_text(encoding="utf-8"))
                if partial.get("periods") and not partial.get("error"):
                    for period in periods:
                        partial["periods"].setdefault(period, {"error": "GALAXY_TIMEOUT_" + stage})
                    return partial
            raise ValueError(f"银河查询 {stage} 超时（单阶段 180 秒/整次 480 秒）；本地文件未改动，请缩短区间后手动重试") from None
        if run.returncode or not output.is_file():
            stage = stage_path.read_text(encoding="utf-8") if stage_path.is_file() else stage
            raise ValueError(f"银河运行失败（阶段 {stage}，退出码 {run.returncode}）；请检查原生 SDK/Python 或 Docker skill")
        result = json.loads(output.read_text(encoding="utf-8"))
        if result.get("error"):
            report_progress('SOURCE_ERROR', error_code=result['error'], error_location=result.get('error_location'))
            raise ValueError(result["error"] + "；本地文件未改动")
        if result.get("sdk_calendar"):
            dates = result["sdk_calendar"]
            calendar_path.write_text(json.dumps({"start": min(dates), "end": max(dates), "dates": dates,
                "source": "galaxy", "full": True,
                "as_of": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")}), encoding="utf-8")
        elif result.get("calendar") and "calendar" not in body:
            calendar_path.write_text(json.dumps({"start": body["start"], "end": body["end"],
                                                "dates": result["calendar"], "source": "galaxy"}), encoding="utf-8")
        return result


def validate_rows(rows, factors, start, end, period, timestamp_semantics="start"):
    frame = pd.DataFrame(rows)
    columns = ["open", "high", "low", "close", "volume", "amount"]
    if not {"date", *columns}.issubset(frame.columns):
        raise ValueError("INVALID_COLUMNS")
    frame["date"] = pd.to_datetime(frame["date"])
    if frame["date"].dt.tz is not None:
        frame["date"] = frame["date"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)
    if period != "daily":
        if timestamp_semantics != "start":
            raise ValueError("UNEXPECTED_GALAXY_TIMESTAMP_SEMANTICS")
        frame["source_time"] = frame.date
        frame["date"] += pd.Timedelta(minutes=int(period[:-1]))
    frame = frame[(frame.date >= start) & (frame.date < end + pd.Timedelta(days=1))].copy()
    if frame.empty:
        raise ValueError("NO_BARS_IN_RANGE")
    if frame.date.duplicated().any() or not np.isfinite(frame[columns].to_numpy(dtype=float)).all():
        raise ValueError("INVALID_BARS")
    if ((frame[["open", "high", "low", "close"]] <= 0).any().any()
            or (frame[["volume", "amount"]] < 0).any().any()
            or (frame.high < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("INVALID_OHLCV")
    if period != "daily":
        slots = expected_times(period)
        if not frame.date.dt.strftime("%H:%M").isin(slots).all():
            raise ValueError("INVALID_SESSION_TIME")
    else:
        frame["date"] = frame.date.dt.normalize()
    factor_frame = pd.DataFrame(factors, columns=["day", "factor"])
    factor_frame["day"] = pd.to_datetime(factor_frame.day).dt.normalize()
    factor_frame = factor_frame.drop_duplicates("day", keep="last").set_index("day")
    frame["factor"] = frame.date.dt.normalize().map(factor_frame.factor)
    if frame.factor.isna().any() or not np.isfinite(frame.factor).all() or (frame.factor <= 0).any():
        raise ValueError("MISSING_OR_INVALID_FACTOR")
    return frame.sort_values("date").reset_index(drop=True)


def galaxy_stock_codes(manager):
    """Galaxy's current A-share universe, cached daily; never use a public-source list."""
    path = Path(manager.data_dir) / "galaxy_universe.json"
    today = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
    with SYNC_LOCK:
        cached = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if cached.get("as_of") != today:
            report_progress('UNIVERSE')
            result = query_galaxy(None, pd.Timestamp(today), pd.Timestamp(today), [])
            codes = sorted({code.split(".")[0] for code in result.get("codes", [])
                            if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code)})
            if not codes:
                raise ValueError("银河股票列表为空，未切换到其他数据源")
            cached = {"source": "galaxy", "as_of": today, "codes": codes}
            path.write_text(json.dumps(cached), encoding="utf-8")
    return cached["codes"]


def training_coverage(manager, stock_code):
    coverage = []
    for period in PERIODS:
        path = Path(manager._get_galaxy_file(stock_code, period)).with_suffix(".receipt.json")
        receipt = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        coverage.append({"period": period, "status": receipt.get("status", "UNKNOWN"),
                         "missing_count": len(receipt.get("missing_bars", []))})
    return coverage


def cached_training_dates(manager, stock_code):
    """Eligible local trading days, memoized until any of the three files changes."""
    paths = [Path(manager._get_galaxy_file(stock_code, period)) for period in PERIODS]
    if not all(path.is_file() for path in paths):
        return pd.DatetimeIndex([])
    signature = tuple((path.stat().st_mtime_ns, path.stat().st_size) for path in paths)
    cached = manager._galaxy_training_dates_cache.get(stock_code)
    if cached and cached[0] == signature:
        return cached[1]
    common_days = None
    warmup_end = pd.Timestamp.min
    try:
        for path in paths:
            frame = pd.read_csv(path, usecols=["date", "factor"], parse_dates=["date"])
            if (len(frame) < 235 or frame.date.isna().any() or frame.date.duplicated().any()
                    or not np.isfinite(frame.factor).all() or (frame.factor <= 0).any()):
                return pd.DatetimeIndex([])
            dates = pd.DatetimeIndex(frame.date).sort_values()
            warmup_end = max(warmup_end, dates[232].normalize())
            days = dates.normalize().unique()
            common_days = days if common_days is None else common_days.intersection(days)
    except (ValueError, OSError, TypeError):
        return pd.DatetimeIndex([])
    # At least 233 completed historical bars and another shared trading day ahead.
    eligible = common_days.sort_values()[:-1]
    eligible = eligible[eligible > warmup_end]
    manager._galaxy_training_dates_cache[stock_code] = (signature, eligible)
    return eligible


def prepare_galaxy_training(manager, stock_code, start_date):
    """Download one bounded episode with MA233 history and all three chart periods."""
    start = pd.Timestamp(start_date).normalize()
    report_progress('CACHE_CHECK', stock_code=stock_code)
    now = pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None)
    latest = pd.offsets.BDay().rollback(now.normalize() - pd.Timedelta(days=int(now.hour < 16)))
    if start < pd.Timestamp("2015-01-01") or start >= latest:
        raise ValueError("银河训练起点须在 2015-01-01 之后，且早于最近已收盘日期")
    end = min(start + pd.Timedelta(days=90), latest)
    ready = True
    coverage = []
    for period in PERIODS:
        frame = manager.get_stock_data(stock_code, source="galaxy", interval=period)
        path = manager._get_galaxy_file(stock_code, period)
        receipt_path = Path(path).with_suffix(".receipt.json")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
        if (frame is None or len(frame[frame.date < start]) < 233
                or len(frame[frame.date >= start]) < 2):
            ready = False
        coverage.append({"period": period, "status": receipt.get("status", "UNKNOWN"),
                         "missing_count": len(receipt.get("missing_bars", []))})
    if not ready:
        report_progress('CACHE_MISS', stock_code=stock_code, reason='三周期历史或后续行情不足，开始下载')
        result = sync_galaxy(manager, stock_code, str((start - pd.Timedelta(days=550)).date()),
                             str(end.date()), "all")
        coverage = [{"period": item["period"], "status": item["status"],
                     "missing_count": len(item.get("missing_bars", []))} for item in result["periods"]]
        if any(item["status"] == "FAILED" for item in coverage):
            raise ValueError("银河三周期准备失败，未使用旧文件或其他数据源开局；请在补数面板检查后重试")
    else:
        report_progress('CACHE_HIT')
    # Do not start an empty/brand-new listing episode or invent an MA warmup.
    for period in PERIODS:
        frame = manager.get_stock_data(stock_code, source="galaxy", interval=period)
        if frame is None or len(frame[frame.date < start]) < 233 or len(frame[frame.date >= start]) < 2:
            raise ValueError("抽取区间的银河历史不足 MA233 或后续训练，请重新抽取或调整日期范围")
    return coverage


def expected_times(period):
    if period == "daily":
        return ["00:00"]
    if period == "60m":
        return ["10:30", "11:30", "14:00", "15:00"]
    return [f"{minute // 60:02d}:{minute % 60:02d}" for start in (570, 780)
            for minute in range(start + 15, start + 121, 15)]


def sync_galaxy(manager, stock_code, start_date, end_date, interval="daily", force_full=False):
    if not re.fullmatch(r"\d{6}(?:\.(?:SH|SZ|BJ))?", stock_code.upper()):
        raise ValueError("股票代码应为 6 位数字，可带 .SH/.SZ/.BJ")
    if interval not in (*PERIODS, "all"):
        raise ValueError("银河支持日线、15 分钟、60 分钟或三周期一起补数")
    start, end = manager._normalize_sync_range(start_date, end_date)
    if start < pd.Timestamp("2013-01-01"):
        raise ValueError("银河历史数据起点为 2013-01-01，请调整开始日期")
    today = pd.Timestamp.now(tz="Asia/Shanghai").tz_localize(None).normalize()
    # Download only completed trading days; today's intraday partial bars are not a training archive.
    latest = today if pd.Timestamp.now(tz="Asia/Shanghai").hour >= 16 else today - pd.Timedelta(days=1)
    if end > latest or end < start:
        raise ValueError("请使用已收盘日期（当日 16:00 后可补数），结束日期不得早于开始日期")
    code = manager._normalize_stock_code(stock_code)
    symbol = manager._format_xt_code(code)
    periods = list(PERIODS) if interval == "all" else [interval]
    results = []
    report_progress('WAITING', stock_code=stock_code)
    with SYNC_LOCK:
        payload = query_galaxy(symbol, start, end, periods)
        for period in periods:
            report_progress('VALIDATE_' + period)
            directory = Path(manager.offline_dir) / "galaxy" / period
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{symbol}.csv"
            result = {"period": period, "source": "galaxy", "offline_path": str(path),
                      "requested_range": {"start": str(start.date()), "end": str(end.date())},
                      "local_file_changed": False, "added_rows": 0, "fetched_rows": 0}
            try:
                bundle = payload.get("periods", {}).get(period, {})
                if bundle.get("error"):
                    raise ValueError(bundle["error"])
                frame = validate_rows(bundle.get("rows", []), payload.get("factors", []), start, end, period,
                                      bundle.get("timestamp_semantics", "start"))
                calendar = pd.to_datetime(payload["calendar"], format="%Y%m%d")
                expected = {day.strftime("%Y-%m-%d") + " " + time for day in calendar for time in expected_times(period)}
                if not expected:
                    raise ValueError("EMPTY_TRADING_CALENDAR")
                observed = set(frame.date.dt.strftime("%Y-%m-%d %H:%M"))
                missing = sorted(expected - observed)
                before = pd.read_csv(path, parse_dates=["date"]) if path.exists() else None
                previous_rows = len(before) if before is not None else 0
                # Only this provider's unadjusted series is merged; no public/Galaxy price splicing.
                merged = frame if force_full or before is None else pd.concat([before, frame], ignore_index=True)
                merged = merged.sort_values("date").drop_duplicates("date", keep="last")
                merged["source"] = "galaxy"
                merged["volume_unit"] = "shares"
                merged["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
                temp = path.with_suffix(".csv.tmp")
                merged.to_csv(temp, index=False)
                os.replace(temp, path)
                result.update(status="PARTIAL" if missing else "COMPLETE", success=not missing,
                              rows_before=previous_rows, rows_after=len(merged), total_rows=len(merged),
                              fetched_rows=len(frame), added_rows=max(0, len(merged) - previous_rows),
                              expected_bars=len(expected), observed_bars=len(observed), missing_bars=missing,
                              timestamp_semantics="daily_date" if period == "daily" else "bar_end_Asia/Shanghai",
                              gap_reason="未返回的交易时段，可能停牌/未上市/源缺失；未补零" if missing else None,
                              local_file_changed=True, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                              range_after={"start": str(merged.date.min()), "end": str(merged.date.max())})
            except (ValueError, KeyError, TypeError) as exc:
                result.update(status="FAILED", success=False, error=str(exc))
            receipt = path.with_suffix(".receipt.json")
            receipt_temp = receipt.with_suffix('.json.tmp')
            receipt_temp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(receipt_temp, receipt)
            results.append(result)
            report_progress('SAVED_' + period, period=period, period_status=result['status'],
                            fetched_rows=result['fetched_rows'], missing_count=len(result.get('missing_bars', [])))
        manager._offline_stock_codes_cache = None
        manager._offline_date_range_cache.clear()
    success = all(item["success"] for item in results)
    status = "COMPLETE" if success else ("FAILED" if all(item["status"] == "FAILED" for item in results) else "PARTIAL")
    return {"success": success, "status": status,
            "message": "银河补数完成" if success else "银河补数存在缺口，请查看各周期结果",
            "stock_code": code, "source": "galaxy", "periods": results,
            "fetched_rows": sum(item["fetched_rows"] for item in results),
            "added_rows": sum(item["added_rows"] for item in results),
            "local_file_changed": any(item["local_file_changed"] for item in results)}
