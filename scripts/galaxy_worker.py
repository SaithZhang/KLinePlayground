"""SDK worker for one request or one bounded download batch; no resident service."""
import json
import math
import os
from pathlib import Path
import sys
import traceback
import time


def main(request_path, output_path, sdk=None):
    sdk = {} if sdk is None else sdk
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    result = {"periods": {}}
    def progress(stage):
        Path(output_path + ".stage").write_text(stage, encoding="utf-8")
    def checkpoint():
        temporary = Path(output_path + ".tmp")
        temporary.write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        temporary.replace(output_path)
    progress("IMPORT")
    stage = "IMPORT"
    try:
        import AmazingData as ad
        import pandas as pd

        Path(os.environ["AD_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
        if "base" not in sdk:
            stage = "LOGIN"
            progress(stage)
            ad.login(username=os.environ["AD_USERNAME"], password=os.environ["AD_PASSWORD"],
                     host=os.environ["AD_HOST"], port=int(os.environ["AD_PORT"]))
            sdk["base"] = ad.BaseData()
        base = sdk["base"]
        if request.get("code") is None:
            stage = "UNIVERSE"
            progress(stage)
            result["codes"] = list(base.get_code_list(security_type="EXTRA_STOCK_A"))
            checkpoint()
            return
        stage = "CALENDAR"
        progress(stage)
        calendar = request.get("calendar")
        if calendar is None:
            calendar = base.get_calendar()
        if calendar is None or len(calendar) == 0:
            raise ValueError("empty trading calendar")
        # get_backward_factor internally calls get_calendar again. Pin this worker
        # to the same complete, verified SH calendar instead of another network read.
        # A clipped episode calendar must never be supplied here: factor history
        # needs the original full calendar to keep its price basis unchanged.
        base.get_calendar = lambda *args, **kwargs: calendar
        result["sdk_calendar"] = [str(d)[:10].replace("-", "") for d in calendar]
        if "market" not in sdk:
            sdk["market"] = ad.MarketData(calendar)
        market = sdk["market"]
        code = request["code"]
        start, end = request["start"], request["end"]
        result["calendar"] = [str(d)[:10].replace("-", "") for d in calendar
                              if start <= str(d)[:10].replace("-", "") <= end]
        stage = "FACTORS"
        progress(stage)
        # SDK refresh unions requested symbols with every symbol in its HDF5 cache.
        # Isolate each symbol so a one-stock episode never refreshes a global universe.
        cache = Path(os.environ["AD_CACHE_DIR"]) / "kline_playground" / code
        cache.mkdir(parents=True, exist_ok=True)
        cache_file = cache / "verified_factors.json"
        cached = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.is_file() else {}
        required = set(result["calendar"])
        cached_rows = cached.get("rows", [])
        covered = {day.replace("-", "") for day, value in cached_rows if math.isfinite(value) and value > 0}
        if required and required <= covered and cached.get("as_of") == pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d"):
            progress("FACTORS_CACHE")
            factor_rows = cached_rows
        else:
            progress("FACTORS_DOWNLOAD")
            factors = base.get_backward_factor([code], local_path=str(cache) + os.sep, is_local=False)
            if code not in factors.columns:
                raise ValueError("missing factors")
            factor_rows = [[str(d)[:10], float(v)] for d, v in factors[code].items() if pd.notna(v)]
            covered = {day.replace("-", "") for day, value in factor_rows if math.isfinite(value) and value > 0}
            if not required <= covered:
                raise ValueError("incomplete factors")
            temporary = cache_file.with_suffix(".tmp")
            temporary.write_text(json.dumps({"as_of": pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d"),
                                             "rows": factor_rows}, allow_nan=False), encoding="utf-8")
            temporary.replace(cache_file)
        result["factors"] = [[day, value] for day, value in factor_rows
                             if start <= day.replace("-", "") <= end]
        for period in request["periods"]:
            progress("KLINE_" + period)
            try:
                value = {"daily": ad.constant.Period.day.value,
                         "15m": ad.constant.Period.min15.value,
                         "60m": ad.constant.Period.min60.value}[period]
                frame = market.query_kline([code], begin_date=int(start), end_date=int(end), period=value).get(code)
                if frame is None or frame.empty:
                    result["periods"][period] = {"error": "NO_BARS"}
                    continue
                if set(frame["code"]) != {code}:
                    raise ValueError("wrong symbol")
                records = []
                for row in frame.to_dict("records"):
                    stamp = str(row["kline_time"])
                    records.append({"date": stamp, **{key: float(row[key]) if math.isfinite(float(row[key])) else None for key in
                                    ("open", "high", "low", "close", "volume", "amount")}})
                result["periods"][period] = {"rows": records, "timestamp_semantics": "start"}
            except Exception as exc:
                result["periods"][period] = {"error": "GALAXY_QUERY_" + type(exc).__name__}
            checkpoint()
    except Exception as exc:
        result["error"] = "GALAXY_" + stage + "_" + type(exc).__name__
        # Structural diagnostics only; SDK exception messages can contain credentials.
        result["error_location"] = [f"{Path(item.filename).name}:{item.name}:{item.lineno}"
                                    for item in traceback.extract_tb(exc.__traceback__)]
        if isinstance(exc, ImportError):
            result["dependency_error"] = str(exc)
    progress("SAVE")
    checkpoint()


def serve(directory):
    """Consume sequential, atomically published requests until this batch ends."""
    root = Path(directory)
    sdk = {}
    idle_since = time.monotonic()
    while root.is_dir() and not (root / "STOP").exists():
        requests = sorted(root.glob("*/request.json"))
        if not requests:
            # An abandoned launcher must not leave a permanent SDK/container behind.
            if time.monotonic() - idle_since > 120:
                return
            time.sleep(0.1)
            continue
        request = requests[0]
        output = request.parent / "output.json"
        main(str(request), str(output), sdk)
        request.unlink()
        output.with_suffix(".done").touch()
        idle_since = time.monotonic()


def check_runtime(output_path):
    """Import native dependencies without logging in or contacting the provider."""
    try:
        import AmazingData as ad
        import pandas
        import tables
        import tgw
        ready = all(hasattr(ad, name) for name in ("login", "BaseData", "MarketData", "constant"))
        result = {"ready": ready}
    except Exception as exc:
        result = {"ready": False, "error": type(exc).__name__}
    Path(output_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    # SDK/native login logs can contain credentials. Never forward them to the UI.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        if sys.argv[1] == "--check":
            check_runtime(sys.argv[2])
        elif sys.argv[1] == "--serve":
            serve(sys.argv[2])
        else:
            main(sys.argv[1], sys.argv[2])
