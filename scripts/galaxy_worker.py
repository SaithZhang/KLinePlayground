"""One-shot SDK query; run only via the user's ad-api skill runtime."""
import json
import os
from pathlib import Path
import sys


def main(request_path, output_path):
    request = json.loads(Path(request_path).read_text())
    result = {"periods": {}}
    def progress(stage):
        Path(output_path + ".stage").write_text(stage)
    def checkpoint():
        temporary = Path(output_path + ".tmp")
        temporary.write_text(json.dumps(result, allow_nan=False))
        temporary.replace(output_path)
    progress("IMPORT")
    stage = "IMPORT"
    try:
        import AmazingData as ad
        import pandas as pd

        stage = "LOGIN"
        progress(stage)
        ad.login(username=os.environ["AD_USERNAME"], password=os.environ["AD_PASSWORD"],
                 host=os.environ["AD_HOST"], port=int(os.environ["AD_PORT"]))
        base = ad.BaseData()
        stage = "CALENDAR"
        progress(stage)
        calendar = request.get("calendar")
        if calendar is None:
            calendar = base.get_calendar()
        if calendar is None or len(calendar) == 0:
            raise ValueError("empty trading calendar")
        market = ad.MarketData(calendar)
        code = request["code"]
        start, end = request["start"], request["end"]
        result["calendar"] = [str(d)[:10].replace("-", "") for d in calendar
                              if start <= str(d)[:10].replace("-", "") <= end]
        stage = "FACTORS"
        progress(stage)
        factors = base.get_backward_factor([code], local_path=os.environ["AD_CACHE_DIR"], is_local=False)
        if code not in factors.columns:
            raise ValueError("missing factors")
        result["factors"] = [[str(d)[:10], float(v)] for d, v in factors[code].items()
                             if start <= str(d)[:10].replace("-", "") <= end and pd.notna(v)]
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
                    records.append({"date": stamp, **{key: float(row[key]) for key in
                                    ("open", "high", "low", "close", "volume", "amount")}})
                result["periods"][period] = {"rows": records, "timestamp_semantics": "start"}
            except Exception as exc:
                result["periods"][period] = {"error": "GALAXY_QUERY_" + type(exc).__name__}
            checkpoint()
    except Exception as exc:
        result["error"] = "GALAXY_" + stage + "_" + type(exc).__name__
        if isinstance(exc, ImportError):
            result["dependency_error"] = str(exc)
    progress("SAVE")
    checkpoint()


if __name__ == "__main__":
    # SDK/native login logs can contain credentials. Never forward them to the UI.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        main(sys.argv[1], sys.argv[2])
