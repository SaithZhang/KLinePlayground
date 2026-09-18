// Data downloads and local coverage share the existing Flask application.
let currentSyncJobId = null;
let syncPollGeneration = 0;
let localInventory = null;
const PERIOD_LABELS = {daily: '日线', '15m': '15分钟', '60m': '60分钟', all: '三周期'};
const JOB_LABELS = {RUNNING: '处理中', COMPLETE: '全部完整', PARTIAL: '存在缺口或未处理项', FAILED: '失败', STOPPED: '已停止', INTERRUPTED: '已中断'};
const STAGE_LABELS = {PREPARE: '准备请求', UNIVERSE: '获取所选数据源的全市场股票名录', FETCH: '请求行情',
    WAITING: '等待银河数据通道', RUNTIME: '启动银河运行环境', IMPORT: '加载银河 SDK', LOGIN: '连接银河',
    CALENDAR: '读取交易日历', FACTORS: '读取复权因子', CACHE_CHECK: '检查本地三周期覆盖',
    CACHE_MISS: '本地历史不足，开始下载', CACHE_HIT: '本地数据已满足，直接读取',
    CALCULATE: '计算指标并创建训练', SAVE: '接收查询结果', SOURCE_ERROR: '数据源返回错误'};

function stageLabel(stage) {
    if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
    for (const [prefix, verb] of [['KLINE_', '下载'], ['VALIDATE_', '校验'], ['SAVED_', '已写入']]) {
        if (stage?.startsWith(prefix)) return verb + (PERIOD_LABELS[stage.slice(prefix.length)] || stage.slice(prefix.length));
    }
    return stage || '准备中';
}

async function readDataJob(ident, full = false) {
    const response = await fetch(`${API_BASE}/data/jobs/${ident}${full ? '?full=1' : ''}`);
    const job = await response.json();
    if (!response.ok) throw new Error(job.error || '读取任务状态失败');
    return job;
}

async function waitForTrainingJob(ident) {
    const progress = document.getElementById('loading-progress');
    progress.classList.remove('hidden');
    progress.removeAttribute('value');
    try {
        while (true) {
            const job = await readDataJob(ident);
            document.getElementById('loading-title').textContent = stageLabel(job.stage);
            document.getElementById('loading-detail').textContent = `已用时 ${job.elapsed_seconds} 秒 · 当前阶段 ${job.stage_seconds} 秒。正在显示真实处理阶段，下载速度取决于银河响应。`;
            if (job.status !== 'RUNNING') {
                if (job.status !== 'COMPLETE') throw new Error(job.error || '训练准备未完成');
                progress.value = 1;
                return job.result;
            }
            await sleep(600);
        }
    } finally {
        progress.classList.add('hidden');
    }
}

function renderSyncJob(job) {
    const running = job.status === 'RUNNING';
    currentSyncJobId = job.id;
    const request = job.config;
    for (const [key, id] of Object.entries({scope:'sync-scope', source:'sync-source', stock_code:'sync-stock-code',
        interval:'sync-interval', start_date:'sync-start-date', end_date:'sync-end-date'})) {
        document.getElementById(id).value = request[key] || '';
    }
    document.getElementById('sync-force-full').checked = !!request.force_full;
    document.getElementById('sync-stock-code-group').classList.toggle('hidden', request.scope !== 'single');
    document.querySelectorAll('#data-sync-modal .form-grid input, #data-sync-modal .form-grid select, #sync-force-full').forEach(input => { input.disabled = running; });
    document.getElementById('confirm-sync-btn').disabled = running;
    document.getElementById('stop-sync-btn').classList.toggle('hidden', !running);
    document.getElementById('stop-sync-btn').disabled = !!job.stop_requested;
    document.getElementById('sync-stage-progress').classList.toggle('hidden', !running);
    const counts = `计划 ${job.total} 只 · 已处理 ${job.processed} · 完整 ${job.complete} · 有缺口 ${job.partial} · 失败 ${job.failed} · 未处理 ${job.unprocessed}`;
    updateSyncProgress(job.processed, job.total, `${JOB_LABELS[job.status]}｜${counts}`);
    const box = document.getElementById('sync-result');
    box.classList.remove('hidden');
    box.textContent = `${request.source === 'galaxy' ? '银河' : request.source} · ${getSyncScopeLabel(request.scope)} · ${PERIOD_LABELS[request.interval]} · ${describeSyncRange(request.start_date, request.end_date)}\n`
        + `${stageLabel(job.stage)}${job.stock_code ? ' · ' + job.stock_code : ''} · 总耗时 ${job.elapsed_seconds} 秒 · 本阶段 ${job.stage_seconds} 秒\n`
        + (job.stop_requested && running ? '已请求停止，当前股票处理完后停止本批。\n' : '')
        + (job.error ? job.error + '\n' : '')
        + job.records.map(record => `${record.stock_code}：${JOB_LABELS[record.status] || record.status}${record.error ? ' · ' + record.error : ''}`
            + (record.periods || []).map(p => `\n  ${PERIOD_LABELS[p.period]}：${JOB_LABELS[p.status] || p.status}，返回 ${p.fetched_rows ?? 0} 根，缺 ${p.missing_count} 根${p.error ? ' · ' + p.error : ''}`).join('')).join('\n');
    const report = document.getElementById('sync-report-link');
    report.classList.remove('hidden');
    report.href = `${API_BASE}/data/jobs/${job.id}?full=1`;
    report.download = `补数记录-${job.id.slice(0, 8)}.json`;
}

async function watchSyncJob(ident) {
    const generation = ++syncPollGeneration;
    try {
        while (generation === syncPollGeneration) {
            const job = await readDataJob(ident);
            if (generation !== syncPollGeneration) return;
            renderSyncJob(job);
            if (job.status !== 'RUNNING') {
                await loadSyncHistory(false);
                return;
            }
            await sleep(1000);
        }
    } catch (error) {
        document.getElementById('sync-result').textContent = `进度连接中断：${error.message}。补数可能仍在运行，请重新打开数据中心查看记录。`;
    }
}

async function loadSyncHistory(restore = true) {
    const response = await fetch(`${API_BASE}/data/jobs`);
    if (!response.ok) return;
    const {jobs} = await response.json();
    const list = document.getElementById('sync-history');
    list.replaceChildren();
    if (!jobs.length) {
        list.textContent = '暂无批次记录。旧版本未保存批次历史，不能据此声称过去补数成功；请查看本地文件覆盖。';
        return;
    }
    for (const job of jobs.slice(0, 8)) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'btn btn-secondary';
        button.textContent = `${new Date(job.started_at * 1000).toLocaleString()} · ${JOB_LABELS[job.status]} · ${job.config.source} ${PERIOD_LABELS[job.config.interval]} · ${job.processed}/${job.total}`;
        button.addEventListener('click', () => watchSyncJob(job.id));
        list.appendChild(button);
    }
    if (restore) await watchSyncJob((jobs.find(job => job.status === 'RUNNING') || jobs[0]).id);
}

async function syncOfflineData() {
    const payload = {
        scope: document.getElementById('sync-scope').value,
        stock_code: document.getElementById('sync-stock-code').value.trim(),
        source: document.getElementById('sync-source').value,
        interval: document.getElementById('sync-interval').value,
        start_date: document.getElementById('sync-start-date').value,
        end_date: document.getElementById('sync-end-date').value,
        force_full: document.getElementById('sync-force-full').checked,
    };
    const box = document.getElementById('sync-result');
    box.classList.remove('hidden');
    box.textContent = '正在创建补数任务；阶段、结果与失败原因将保存到本地。';
    document.getElementById('confirm-sync-btn').disabled = true;
    try {
        const response = await fetch(`${API_BASE}/data/jobs`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || '无法创建任务');
        await watchSyncJob(result.job_id);
    } catch (error) {
        box.textContent = '未开始补数：' + error.message;
        document.getElementById('confirm-sync-btn').disabled = false;
    }
}

async function stopSyncJob() {
    if (!currentSyncJobId) return;
    const response = await fetch(`${API_BASE}/data/jobs/${currentSyncJobId}/stop`, {method: 'POST'});
    if (response.ok) document.getElementById('stop-sync-btn').disabled = true;
}

function inventoryRange() {
    const start = document.getElementById('random-start-date');
    if (start.dataset.initialized) return [start.value, document.getElementById('random-end-date').value];
    const end = new Date(); end.setDate(end.getDate() - 30);
    const first = new Date(end); first.setFullYear(first.getFullYear() - 1);
    return [first.toISOString().slice(0, 10), end.toISOString().slice(0, 10)];
}

async function showInventory() {
    const [start, end] = inventoryRange();
    document.getElementById('inventory-start').value = start;
    document.getElementById('inventory-end').value = end;
    document.getElementById('inventory-modal').classList.remove('hidden');
    await refreshInventory();
}

async function refreshInventory() {
    const summary = document.getElementById('inventory-summary');
    summary.textContent = '正在读取本地文件覆盖，不联网…';
    const params = new URLSearchParams({date_start: document.getElementById('inventory-start').value,
        date_end: document.getElementById('inventory-end').value, sector: document.getElementById('sector-filter').value});
    try {
        const response = await fetch(`${API_BASE}/data/inventory?${params}`);
        localInventory = await response.json();
        if (!response.ok) throw new Error(localInventory.error);
        const data = localInventory;
        summary.textContent = `本机银河行情：已下载 ${data.downloaded_stocks} 只 → 三周期齐备 ${data.three_period_stocks} 只 → 当前范围可训练 ${data.eligible_stocks} 只。`
            + ` 日线 ${data.period_counts.daily} 只 / 15分钟 ${data.period_counts['15m']} 只 / 60分钟 ${data.period_counts['60m']} 只。`
            + ` 其他来源日线文件 ${data.other_daily_files} 只。股票名录 ${data.universe_count} 只只是代码列表，不代表行情已下载。`;
        document.getElementById('inventory-environment').textContent = `当前环境：${data.platform} · 数据目录：${data.data_directory}。本地数据不随 Git 同步到另一台电脑。日志：${data.data_directory}/logs/operations.jsonl`;
        renderInventoryRows();
    } catch (error) { summary.textContent = '无法读取本地覆盖：' + error.message; }
}

function renderInventoryRows() {
    if (!localInventory?.rows) return;
    const query = document.getElementById('inventory-search').value.trim();
    const excludedOnly = document.getElementById('inventory-excluded').checked;
    const rows = localInventory.rows.filter(row => (!query || (row.stock_code + row.name).includes(query)) && (!excludedOnly || !row.eligible));
    const body = document.getElementById('inventory-rows'); body.replaceChildren();
    document.getElementById('inventory-row-count').textContent = `匹配 ${rows.length} 只，显示前 ${Math.min(rows.length, 100)} 只；可按名称或代码搜索。`;
    for (const row of rows.slice(0, 100)) {
        const tr = document.createElement('tr');
        const stock = document.createElement('td'); stock.textContent = `${row.name} ${row.stock_code}`; tr.appendChild(stock);
        for (const period of ['daily', '15m', '60m']) {
            const item = row.periods[period]; const cell = document.createElement('td');
            cell.textContent = item.rows ? `${item.rows} 根\n${item.start.slice(0, 10)} 至 ${item.end.slice(0, 10)}` : '尚无行情文件';
            const details = document.createElement('details'); const label = document.createElement('summary');
            label.textContent = `最近补数：${JOB_LABELS[item.status] || (item.status === 'MISSING' ? '未下载' : '完整性待确认')}${item.missing_count != null ? ` · 缺 ${item.missing_count} 根` : ''}`;
            details.appendChild(label);
            const text = document.createElement('p');
            text.textContent = `${item.requested_range ? '核对区间：' + item.requested_range.start + ' 至 ' + item.requested_range.end : '没有可用回执'}\n`
                + `${item.error || ''}\n${item.gaps?.length ? '缺口示例：' + item.gaps.join('、') : ''}`;
            details.appendChild(text); cell.appendChild(details); tr.appendChild(cell);
        }
        const reason = document.createElement('td'); reason.textContent = row.reason; tr.appendChild(reason); body.appendChild(tr);
    }
}
