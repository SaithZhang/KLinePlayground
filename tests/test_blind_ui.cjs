// Run with node --test tests/test_blind_ui.cjs; no browser or new dependencies.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

function page() {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {
            textContent: '', innerHTML: '', style: {}, value: '', children: [], attributes: {},
            classList: { toggle() {}, add() {}, remove() {} },
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; },
            appendChild(child) { this.children.push(child); },
        });
        return elements.get(id);
    };
    const context = vm.createContext({
        localStorage: { getItem: () => null }, console,
        document: { addEventListener() {}, getElementById: element, createElement: () => element(Math.random()), querySelectorAll: () => [] },
        fetch: async () => ({ ok: true, json: async () => [{ action: 'buy', trade_date: '2026-09-01',
            bar_id: 1, quantity: 1, price: 10, net_amount: 1000 }] }),
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js/main_enhanced.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../frontend/js/data_status.js'), 'utf8'), context);
    return { context, element, run: script => vm.runInContext(script, context) };
}

test('blind identity, chart time and trade date stay hidden until explicitly revealed', async () => {
    const { run, element } = page();
    run(`currentTraining = {id:'test', mode:'random', stock_code:'000001', stockName:'平安银行', identityRevealed:false}; updateBlindControls();`);
    assert.equal(element('stock-name').textContent, '盲盒股票');
    for (const period of ['daily', '15m', '60m']) {
        run(`currentPeriod = '${period}';`);
        assert.equal(run('formatChartTime(1788226200)'), '日期已隐藏');
    }
    await run('updateTradeHistory()');
    assert.match(element('trade-history').children[0].innerHTML, /日期已隐藏/);
    assert.doesNotMatch(element('trade-history').children[0].innerHTML, /2026-09-01/);
    run('currentTraining.identityRevealed = true; updateBlindControls();');
    assert.equal(element('stock-name').textContent, '平安银行');
    assert.match(run('formatChartTime(1788226200)'), /2026\/9\/1/);
    assert.equal(element('blind-reveal-btn').attributes['aria-pressed'], 'true');
    run('currentTraining.identityRevealed = false; updateBlindControls();');
    assert.equal(element('stock-name').textContent, '盲盒股票');
});

test('partial coverage and single-stock offline pool are visible without revealing identity', () => {
    const { run, element } = page();
    run(`currentTraining = {mode:'random', coverage:[{period:'daily',status:'PARTIAL',missing_count:1}], offline_pool_size:1}; updateBlindControls();`);
    assert.match(element('training-data-notice').textContent, /PARTIAL/);
    assert.match(element('training-data-notice').textContent, /仅 1 只/);
});

test('download failures and unfinished stocks cannot look like a completed batch', () => {
    const { run, element } = page();
    run(`renderSyncJob({id:'batch', status:'FAILED', config:{source:'galaxy',scope:'all',interval:'all',start_date:'2026-09-17',end_date:'2026-09-17'},
        total:100,processed:3,complete:0,partial:0,failed:3,unprocessed:97,stage:'FACTORS',elapsed_seconds:60,stage_seconds:20,
        records:[{stock_code:'000001',status:'FAILED',error:'NO_BARS'}],error:'连续3只请求失败'});`);
    assert.match(element('sync-progress-text').textContent, /失败 3.*未处理 97/);
    assert.doesNotMatch(element('sync-progress-text').textContent, /全部完整/);
    assert.match(element('sync-result').textContent, /读取复权因子.*60 秒/);
    assert.match(element('sync-result').textContent, /NO_BARS/);
    assert.equal(element('confirm-sync-btn').disabled, false);
    assert.equal(element('sync-report-link').href, '/api/data/jobs/batch?full=1');
});

test('background training surfaces the provider phase and only accepts successful completion', async () => {
    const { run, context, element } = page();
    context.fetch = async () => ({ok:true,json:async () => ({stage:'FACTORS',status:'FAILED',elapsed_seconds:12,stage_seconds:9,error:'银河查询 FACTORS 超时'})});
    await assert.rejects(run("waitForTrainingJob('training')"), /FACTORS 超时/);
    assert.equal(element('loading-title').textContent, '读取复权因子');
    assert.match(element('loading-detail').textContent, /12 秒.*9 秒/);
});
