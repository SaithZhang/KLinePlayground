"""Bounded public daily data, using the a-stock-data skill's Tencent/Sina routes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import requests


def session():
    client = requests.Session()
    # Only this provider session bypasses macOS/environment proxies.
    client.trust_env = os.environ.get('KLINE_PUBLIC_USE_PROXY') == '1'
    client.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn/'})
    return client


def fetch_skill(code, start, end):
    symbol = ('sh' if code.startswith(('6',)) else 'bj' if code.startswith(('4', '8', '92')) else 'sz') + code
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    rows = []
    with session() as client:
        for year in range(start.year, end.year + 1):
            response = client.get('https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get',
                                  params={'param': f'{symbol},day,{year}-01-01,{year + 1}-01-01,640,'}, timeout=8)
            response.raise_for_status()
            payload = response.json()
            if payload.get('code') != 0:
                raise ValueError('TENCENT_SOURCE_ERROR')
            bars = payload.get('data', {}).get(symbol, {}).get('day', [])
            for row in bars:
                if len(row) < 9:
                    raise ValueError('TENCENT_MISSING_AMOUNT')
                rows.append(dict(zip(['date', 'open', 'close', 'high', 'low', 'volume', 'amount'],
                                     [row[0], *row[1:6], row[8]])))
        if not rows:
            raise ValueError('TENCENT_NO_BARS')
        frame = pd.DataFrame(rows)
        frame['date'] = pd.to_datetime(frame.date)
        frame = frame[(frame.date >= start) & (frame.date <= end)].drop_duplicates('date').sort_values('date')
        for name in ['open', 'close', 'high', 'low', 'volume', 'amount']:
            frame[name] = pd.to_numeric(frame[name], errors='raise')
        # newfqkline: volume in lots except STAR shares; amount in ten-thousand yuan.
        if not symbol.startswith('sh688'):
            frame['volume'] *= 100
        frame['amount'] *= 10000
        response = client.get(f'https://finance.sina.com.cn/realstock/company/{symbol}/hfq.js', timeout=8)
        response.raise_for_status()
        text = response.text
        if '{' not in text:
            raise ValueError('SINA_NO_FACTORS')
        payload, _ = json.JSONDecoder().raw_decode(text[text.index('{'):])
        factors = pd.DataFrame([{'date': pd.Timestamp(item['d']), 'factor': float(item['f'])}
                                for item in payload.get('data', [])])
        if factors.empty or frame.empty:
            raise ValueError('PUBLIC_NO_DATA')
        factors = pd.merge_asof(frame[['date']], factors.sort_values('date').drop_duplicates('date'), on='date')
        values = frame[['open', 'close', 'high', 'low', 'volume', 'amount']].to_numpy()
        if (not np.isfinite(values).all() or not np.isfinite(factors.factor).all()
                or (factors.factor <= 0).any() or (frame[['open','close','high','low']] <= 0).any().any()
                or (frame.high < frame[['open','close','low']].max(axis=1)).any()
                or (frame.low > frame[['open','close','high']].min(axis=1)).any()):
            raise ValueError('INVALID_PUBLIC_BARS_OR_FACTORS')
        frame['source'] = 'a_stock_data:tencent'
        frame['volume_unit'] = 'shares'
        factors['source'] = 'a_stock_data:sina_hfq'
        return {'kline': frame, 'factor': factors}


def fetch(source, code, start, end=None):
    """Keep third-party requests and their proxy/timeout policy in a disposable child."""
    request = {'source': source, 'code': code, 'start': start,
               'end': end or pd.Timestamp.now(tz='Asia/Shanghai').strftime('%Y-%m-%d')}
    try:
        run = subprocess.run([sys.executable, '-m', 'backend.public_data'], input=json.dumps(request),
                             capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=90)
    except subprocess.TimeoutExpired:
        raise ValueError('公开源查询超时（90 秒）；请稍后重试') from None
    try:
        result = json.loads(run.stdout)
    except ValueError:
        raise ValueError('公开源运行失败，未写入缓存') from None
    if result.get('error'):
        raise ValueError(result['error'])
    if source == 'universe':
        return pd.DataFrame(result['stocks'])
    bundle = {key: pd.DataFrame(result[key]) for key in ('kline', 'factor')}
    for frame in bundle.values():
        frame['date'] = pd.to_datetime(frame.date)
    return bundle


def main():
    request = json.load(sys.stdin)
    # AKShare uses module-global requests; changing it is safe only in this child.
    original = requests.sessions.Session.request
    def bounded(self, method, url, **kwargs):
        self.trust_env = os.environ.get('KLINE_PUBLIC_USE_PROXY') == '1'
        kwargs['timeout'] = 8
        return original(self, method, url, **kwargs)
    requests.sessions.Session.request = bounded
    try:
        if request['source'] == 'universe':
            import akshare as ak
            stocks = ak.stock_info_a_code_name()
            if stocks.empty:
                raise ValueError('EMPTY_UNIVERSE')
            print(json.dumps({'stocks': stocks.to_dict('records')}))
            return
        if request['source'] == 'akshare':
            try:
                import akshare as ak
                from backend.data_manager import DataManager
                manager = DataManager.__new__(DataManager)
                bundle = manager._fetch_stock_bundle_ak(request['code'], request['start'], request['end'])
                if any(bundle[key] is None or bundle[key].empty for key in ('kline', 'factor')):
                    raise ValueError('AKSHARE_EMPTY')
                bundle['kline']['source'] = 'akshare:eastmoney'
            except Exception:
                bundle = fetch_skill(request['code'], request['start'], request['end'])
                bundle['kline']['source'] = 'akshare:fallback_tencent'
        else:
            bundle = fetch_skill(request['code'], request['start'], request['end'])
        result = {}
        for key, frame in bundle.items():
            frame['date'] = frame.date.dt.strftime('%Y-%m-%d')
            result[key] = frame.to_dict('records')
    except Exception as exc:
        result = {'error': 'PUBLIC_SOURCE_' + type(exc).__name__}
    print(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    # Library diagnostic prints must not corrupt the protocol.
    import contextlib
    import io
    output = sys.stdout
    with contextlib.redirect_stdout(io.StringIO()) as capture:
        main()
    output.write(capture.getvalue().splitlines()[-1])
