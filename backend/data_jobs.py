"""Observable one-shot work inside the existing Flask process; JSON receipts survive reloads."""
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
import traceback
import uuid

_context = threading.local()


def report_progress(stage, **fields):
    callback = getattr(_context, 'callback', None)
    if callback:
        callback(stage=stage, **fields)


@contextmanager
def progress_reporter(callback):
    previous = getattr(_context, 'callback', None)
    _context.callback = callback
    try:
        yield
    finally:
        _context.callback = previous


class DataJobs:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs = {}
        log_dir = self.directory.parent / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.Logger('kline-operations-' + uuid.uuid4().hex)
        handler = RotatingFileHandler(log_dir / 'operations.jsonl', maxBytes=5_000_000, backupCount=4, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        self.logger.addHandler(handler)
        for path in self.directory.glob('*.json'):
            try:
                item = json.loads(path.read_text(encoding='utf-8'))
                if item['status'] == 'RUNNING':
                    item.update(status='INTERRUPTED', error='应用已重启，任务中断；未自动重试。', finished_at=time.time())
                    self._save(item)
                self.jobs[item['id']] = item
            except (ValueError, KeyError, OSError):
                continue

    def _save(self, item):
        path = self.directory / (item['id'] + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    def _log(self, ident, event, **fields):
        allowed = {'stage', 'stock_code', 'period', 'period_status', 'fetched_rows', 'missing_count',
                   'status', 'processed', 'total', 'complete', 'partial', 'failed', 'error_code', 'error_location',
                   'source', 'mode', 'sector', 'date_start', 'date_end', 'start_date', 'allow_download',
                   'elapsed_seconds', 'pool_size'}
        self.logger.info(json.dumps({'time': time.time(), 'job_id': ident, 'event': event,
                                    **{key: value for key, value in fields.items() if key in allowed}}, ensure_ascii=False))

    def start(self, kind, config, work):
        with self.lock:
            if kind == 'sync' and any(j['kind'] == kind and j['status'] == 'RUNNING' for j in self.jobs.values()):
                raise ValueError('已有补数任务运行中，请查看进度或停止后再开始。')
            ident = uuid.uuid4().hex
            item = dict(id=ident, kind=kind, config=config, status='RUNNING', stage='PREPARE',
                        started_at=time.time(), stage_started_at=time.time(), total=0, processed=0,
                        complete=0, partial=0, failed=0, records=[], stop_requested=False)
            self.jobs[ident] = item
            self._save(item)
            self._log(ident, 'START', status='RUNNING')
        thread = threading.Thread(target=self._run, args=(ident, work), daemon=True)
        thread.start()
        return ident

    def _update(self, ident, stage=None, **fields):
        allowed = {'stock_code', 'period', 'period_status', 'fetched_rows', 'missing_count', 'reason',
                   'status', 'processed', 'total', 'complete', 'partial', 'failed', 'error_code', 'error_location',
                   'error', 'result', 'finished_at', 'stop_requested'}
        fields = {key: value for key, value in fields.items() if key in allowed}
        with self.lock:
            item = self.jobs[ident]
            self._log(ident, 'PROGRESS', stage=stage, **fields)
            # A blind training progress response must not disclose the selected stock/date.
            if item['kind'] == 'training':
                fields.pop('stock_code', None)
            if stage and stage != item['stage']:
                item.update(stage=stage, stage_started_at=time.time())
            item.update(fields)
            self._save(item)

    def _run(self, ident, work):
        try:
            with progress_reporter(lambda **fields: self._update(ident, **fields)):
                result = work(ident)
            with self.lock:
                item = self.jobs[ident]
                status = 'COMPLETE'
                if item['kind'] == 'sync':
                    if item['stop_requested']:
                        status = 'STOPPED'
                    elif item['processed'] < item['total'] or item['partial'] or item['failed']:
                        status = 'PARTIAL' if item['complete'] or item['partial'] else 'FAILED'
                self._update(ident, status=status, result=result, finished_at=time.time())
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else f'处理失败（{type(exc).__name__}），请检查本地数据与运行环境。'
            self._log(ident, 'ERROR', error_code=type(exc).__name__, error_location=[
                f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(exc.__traceback__)])
            self._update(ident, status='FAILED', error=error, finished_at=time.time())

    def close(self):
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)

    def get(self, ident, full=False):
        with self.lock:
            item = deepcopy(self.jobs[ident])
        now = item.get('finished_at', time.time())
        item['elapsed_seconds'] = round(now - item['started_at'])
        item['stage_seconds'] = max(0, round(now - item['stage_started_at']))
        item['unprocessed'] = max(0, item['total'] - item['processed'])
        if not full:
            item['records'] = item['records'][-10:]
        return item

    def history(self):
        with self.lock:
            ids = sorted((j['id'] for j in self.jobs.values() if j['kind'] == 'sync'),
                         key=lambda ident: self.jobs[ident]['started_at'], reverse=True)[:20]
        return [self.get(ident) for ident in ids]

    def stop(self, ident):
        with self.lock:
            if self.jobs[ident]['kind'] != 'sync':
                raise ValueError('此任务不支持批次停止')
            self._update(ident, stop_requested=True)

    def run_sync(self, ident, manager, config):
        report_progress('UNIVERSE')
        scope = config.get('scope', 'single')
        codes = [config['stock_code']] if scope == 'single' else manager.get_stock_universe(scope, source=config['source'])
        if not codes:
            raise ValueError('股票列表为空，未开始补数。')
        report_progress('PREPARE', total=len(codes))
        consecutive_failures = 0
        for code in codes:
            with self.lock:
                if self.jobs[ident]['stop_requested']:
                    break
            stock_started = time.monotonic()
            report_progress('FETCH', stock_code=code)
            try:
                result = manager.sync_offline_data(code, config['source'], config.get('start_date'),
                    config.get('end_date'), config.get('force_full', False), config.get('interval', 'daily'))
                status = result.get('status') or ('COMPLETE' if result.get('success') is not False else 'PARTIAL')
                if status not in ('COMPLETE', 'PARTIAL', 'FAILED'):
                    status = 'FAILED'
                record = dict(stock_code=code, status=status, added_rows=result.get('added_rows', 0),
                              error=result.get('error'), message=result.get('message'),
                              periods=[{k: p.get(k) for k in ('period', 'status', 'error', 'fetched_rows', 'expected_bars')}
                                       | {'missing_count': len(p.get('missing_bars', []))} for p in result.get('periods', [])])
            except Exception as exc:
                record = dict(stock_code=code, status='FAILED', error=str(exc) if isinstance(exc, ValueError)
                              else f'数据源请求失败（{type(exc).__name__}）')
                self._log(ident, 'STOCK_ERROR', stock_code=code, error_code=type(exc).__name__, error_location=[
                    f'{Path(frame.filename).name}:{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(exc.__traceback__)])
            record['elapsed_seconds'] = round(time.monotonic() - stock_started, 2)
            with self.lock:
                item = self.jobs[ident]
                item['records'].append(record)
                item['processed'] += 1
                item[record['status'].lower() if record['status'] != 'COMPLETE' else 'complete'] += 1
                self._save(item)
                self._log(ident, 'STOCK_RESULT', stock_code=code, status=record['status'],
                          processed=item['processed'], total=item['total'])
            consecutive_failures = consecutive_failures + 1 if record['status'] == 'FAILED' else 0
            if consecutive_failures >= 3:
                self._update(ident, error='连续3只请求失败，已停止本批，剩余股票未请求。请查看失败原因后重试。')
                break
        return {'message': '批次已结束，请按完整、缺口、失败和未处理数量核对。'}
