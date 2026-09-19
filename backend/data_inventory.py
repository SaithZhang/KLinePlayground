"""Read local archive evidence without contacting any provider."""
from collections import Counter
import json
from pathlib import Path
import platform

import pandas as pd

from backend.galaxy_data import PERIODS, cached_training_dates


def inventory(manager, date_start, date_end, sector='all'):
    start, end = manager._resolve_training_range(date_start, date_end)
    root = Path(manager.offline_dir) / 'galaxy'
    symbols = sorted({p.stem for period in PERIODS for p in (root / period).glob('*.csv')})
    for period in PERIODS:
        symbols = sorted(set(symbols) | {p.name.removesuffix('.receipt.json') for p in (root / period).glob('*.receipt.json')})
    rows, exclusions = [], Counter()
    counts = dict.fromkeys(PERIODS, 0)
    downloaded = three_periods = eligible_count = 0
    for symbol in symbols:
        code = symbol.split('.')[0]
        periods = {}
        for period in PERIODS:
            path = root / period / (symbol + '.csv')
            receipt_path = path.with_suffix('.receipt.json')
            item = dict(rows=0, status='MISSING', missing_count=None)
            try:
                if receipt_path.is_file():
                    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                    item.update(status=receipt.get('status', 'UNKNOWN'), requested_range=receipt.get('requested_range'),
                                missing_count=len(receipt.get('missing_bars', [])), gaps=receipt.get('missing_bars', [])[:10],
                                error=receipt.get('error'), last_attempt=pd.Timestamp(receipt_path.stat().st_mtime, unit='s').isoformat())
                if path.is_file():
                    frame = pd.read_csv(path, usecols=['date'], parse_dates=['date'])
                    if frame.date.isna().any():
                        raise ValueError('invalid dates')
                    item.update(rows=len(frame), start=str(frame.date.min()), end=str(frame.date.max()))
                    if len(frame):
                        counts[period] += 1
                        if item['status'] == 'MISSING':
                            item['status'] = 'UNKNOWN'
            except (ValueError, OSError, KeyError):
                item.update(status='INVALID', error='文件或回执无法读取')
            periods[period] = item
        has_data = any(p['rows'] for p in periods.values())
        downloaded += int(has_data)
        missing = [p for p in PERIODS if not periods[p]['rows']]
        three_periods += int(not missing)
        dates = cached_training_dates(manager, code) if not missing else pd.DatetimeIndex([])
        usable = dates[(dates >= start) & (dates <= end)]
        reason_code, reason = 'ELIGIBLE', '可训练'
        if code not in manager._filter_stock_codes_by_sector([code], sector):
            reason_code, reason = 'SECTOR', '不在所选板块'
        elif missing:
            reason_code, reason = 'MISSING_PERIOD', '尚缺周期：' + '、'.join(missing)
        elif not len(dates):
            reason_code, reason = 'WARMUP', '三周期共同历史不足 MA233，或没有后续行情；检查文件有效性'
        elif not len(usable):
            reason_code, reason = 'DATE_RANGE', f'可开局日期 {dates[0]:%Y-%m-%d} 至 {dates[-1]:%Y-%m-%d}，与所选范围不重叠'
        eligible_count += int(reason_code == 'ELIGIBLE')
        if has_data and reason_code != 'ELIGIBLE':
            exclusions[reason_code] += 1
        rows.append(dict(stock_code=code, name=manager.get_stock_name(code), periods=periods,
                         eligible=reason_code == 'ELIGIBLE', reason=reason, reason_code=reason_code,
                         training_start=str(dates[0].date()) if len(dates) else None,
                         training_end=str(dates[-1].date()) if len(dates) else None))
    universe_file = Path(manager.data_dir) / 'galaxy_universe.json'
    universe = json.loads(universe_file.read_text(encoding='utf-8')) if universe_file.is_file() else {}
    return dict(platform=platform.system(), data_directory=str(Path(manager.data_dir).resolve()),
                downloaded_stocks=downloaded, three_period_stocks=three_periods, eligible_stocks=eligible_count,
                period_counts=counts, exclusions=dict(exclusions), rows=rows,
                universe_count=len(universe.get('codes', [])), universe_as_of=universe.get('as_of'),
                other_daily_files=(len(list(Path(manager.offline_dir).glob('*.csv'))) +
                    sum(len(list((Path(manager.offline_dir) / source / 'daily').glob('*.csv')))
                        for source in ('a_stock_data', 'akshare', 'xtdata', 'mootdx'))),
                date_start=str(start.date()), date_end=str(end.date()), sector=sector)
