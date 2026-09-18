# 银河离线数据与 55 战法训练

## 运行

### macOS：保留现有 Docker 结构

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-web.txt
./scripts/setup_galaxy.sh
./start.sh
```

浏览器打开 `http://127.0.0.1:8766`。`Ctrl+C` 停止；用 `KLINE_PORT` 修改端口。
原 PyWebView 入口保留，可按原 README 安装完整桌面依赖后运行。

银河使用本机 `~/.codex/skills/ad_api/scripts/run_in_docker.sh`，凭据仍由 skill 从
`~/.config/amazingdata/.env` 读取。项目不存凭据。Docker Desktop 需运行。
`KLINE_GALAXY_RUNNER` 可覆盖 skill 路径；`AD_DOCKER_IMAGE` 可覆盖镜像。
项目镜像 `kline-galaxy:1.1.9-tables` 基于已有 QEMU 镜像，仅补充 SDK 复权缓存必需的
`tables==3.10.2`，不修改其他项目的镜像或全局配置。基础镜像需按 ad-api skill 预先配置。

### Windows：原生 SDK，无需 Docker

准备 64 位 Python（默认 3.12）及银河官方 `AmazingData` / `tgw` wheel。
`AmazingData` wheel 的 `cp312` / `cp313` 必须与 Python 版本一致；不要安装 PyPI 上的同名旧包。
官方包位置：https://gitee.com/cgs2026/xysz/tree/master/xysz/xysz_tools 。
在项目根目录的 `.env` 放置自己的 `AD_USERNAME`、`AD_PASSWORD`、`AD_HOST`、`AD_PORT`，
也可用 `-EnvFile` 引用已有凭据文件。不要将真实凭据写入命令行、文档或提交到 Git。

```powershell
# 将占位路径替换为本机银河官方 wheel；只修改本项目的 .venv。
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_galaxy.ps1 `
  -AmazingDataWheel 'C:\sdk\AmazingData-1.1.8-cp312-none-any.whl' `
  -TgwWheel 'C:\sdk\tgw-1.0.8.7-py3-none-any.whl'
.\start.cmd
```

如果已有安装完 SDK、pandas 和 tables 的 Python，可改用
`scripts/setup_galaxy.ps1 -SdkPython 'C:\sdk-env\Scripts\python.exe' -EnvFile 'C:\private\galaxy.env'`；
此模式只读取外部 SDK 环境，不安装或升级它的包。初始化会做 SDK 导入检查，不登录、不取数。
本机解释器和凭据文件路径保存在 Git 忽略的 `.runtime/galaxy-native.json`，密码不写入此文件。
已有完整桌面环境仍使用 `.\start.cmd`；`.\stop.cmd` / `.\restart.cmd` / `.\status.cmd` 管理桌面进程。
只运行同一套 Web UI 可用 `.\start-web.cmd`，默认 `http://127.0.0.1:8766`，`Ctrl+C` 停止。

### 双平台约定

| 设置 | 行为 |
| --- | --- |
| `KLINE_GALAXY_RUNTIME=auto`（默认） | Windows 原生；macOS/Linux 沿用 Docker skill |
| `KLINE_GALAXY_RUNTIME=native` | 使用兼容本机的 Python SDK；Windows 不调用 shell 或 Docker |
| `KLINE_GALAXY_PYTHON` | 覆盖原生 SDK 解释器；默认使用应用当前 Python |
| `KLINE_GALAXY_ENV_FILE` | 原生凭据路径；优先于本机配置，其次项目 `.env`、`~/.config/amazingdata/.env` |
| `KLINE_GALAXY_RUNNER` / `AD_DOCKER_IMAGE` | 仅用于现有 macOS/Linux Docker 运行路径 |

进程中的 `AD_*` 认证变量优先于凭据文件；兼容 `AD_USER` 用户名别名。
从其他项目凭据文件只读取上述四项认证配置；默认原生缓存位于 `data/galaxy_sdk_cache`，
不复用其他项目的缓存。需自定义缓存时单独设置进程环境变量 `AD_CACHE_DIR`。
两端共用 `scripts/galaxy_worker.py`、补数校验、MA55 训练与归档格式；JSON/回执统一 UTF-8。
数据源的 `LOCAL_READY` 仅代表本机配置/依赖可用，不代表登录成功或历史数据完整。
Docker 的 `RUNNER_FOUND` 仅代表 skill 入口存在，容器/连接仍在补数时验证。
Windows 子进程隐藏控制台，超时终止该次原生 worker 及其虚拟环境启动进程；
`stop.cmd` 结束已核验的桌面进程树（包括进行中的补数）；Mac 保留进程组与请求级容器清理。
`.env`、`.env.*`（示例除外）、`.runtime/`、数据/缓存/用户记录均被 Git 忽略。

## 补数与开局

1. 创建/选择本地练习用户，打开「数据更新器」。
2. 输入代码、日期，选择「银河 AmazingData（补数）」及「55 战法三周期」。
3. 数据源按日线、15分、60分保存；完成后核对各周期状态及缺口。
4. 新建训练，选择「55 战法（离线 · 15分推进）」，设置股票和训练起始日期。
5. 用「下一根」按 15 分钟推进；可切换 15分、60分、日线、周线对照。

推荐先用已验证样本 `603938`，训练日期 `2026-09-15`。这是有来源的历史练习例子，
不是收益验证或无偏样本。要练习其他股票，在 UI 补相应数据即可。
希望开局即有 MA233，应在训练日前下载至少 233 根各周期已完成 K 线（通常约一年日线）。
周 MA233 需要更长历史；不足时不伪造数值，面板会显示日/60分/15分指标预热情况。

## MA 与回放约定

- 原项目已经支持最多 6 条自定义 MA；仍在「设置 → 均线周期」添加/删除，支持 55、233。
- 55 预设仅在当前训练加载 MA5/10/20/55/233，不覆盖用户保存的自由训练设置。
- 修复历史只截取 80 根导致 MA233 空白，以及逐根更新遗漏自定义 MA 的问题。
- 新增均线沿用原设置，但以行内数字输入代替嵌入浏览器不支持的 `prompt()`。
- 55 预设使用动态前复权；三周期共用一个回放时钟。早盘只展示前一已完成日线，
  60 分钟 K 线也必须到其结束时间后才可见。周线按已知日线合成，当前周可能未完成。
- 视图切换不改变交易执行周期，55 预设成交始终基于当前 15 分钟 K 线。
  模拟交易保留 T+1；同日不同分钟的交易记录分开。
- 这是原项目的人工收盘价练习模拟器，未增加下一根成交模型或完整企业行动持仓处理；
  跨除权日的盈亏不应当作精确回测结果。

## 规则边界

清单来自本机已有 `quant-engine-lab/docs/evidence/STG-0002-notion.md`，记录版本为
2026-09-15 的「STG-0002｜55线二买」补充规则。
来源页面：https://app.notion.com/p/3d044bc3d1cd81918a4aef227b3a3bf0

练习顺序：日线 MA5/10/20 趋势及 MA55 位置；量能相对自身均量；
下切 15 分 MA55 观察下击/回踩与收复；检查上级别趋势、MACD 衰减；另行核对大盘/板块环境。
MA233 是上级别 MA55 的近似参照，不能当作精确等价关系。
不增加固定量比、涨幅或 D+N 门槛，不自动打买卖分数，不调用 AI 或自动交易。
清单是每局的人工观察提示，勾选状态不作为自动信号或历史策略结果。

## 数据与缺口

银河文件位于 `data/a_market_offline/galaxy/{daily,15m,60m}/代码.交易所.csv`。
CSV 保存不复权 OHLC、成交量（股）、成交额、按交易日对齐的后复权因子和来源。
银河分钟线的 `kline_time` 标记开始时间；导入后 `source_time` 保留原值，`date` 加上周期长度，
转为上海时间的 K 线结束时刻。午间分段按 09:30/13:00 开始，不跨午休合并。
训练读取优先使用同股票同周期的银河归档；旧日线包保留，分钟数据不回退成日线。
不同来源禁止拼接。所选区间重查，修复中间缺口后同源合并；默认保留区间外历史。
勾选全量覆盖才重建该周期文件。失败、空响应或缺少因子时保留原 CSV。

每次保存 `.receipt.json`，记录请求区间、实际根数、交易日历预期根数、所有缺失时段、
错误、文件 SHA256。日线每交易日 1 根、15 分 16 根、60 分 4 根；午休不计入。
停牌、未上市与上游缺失都保留为未返回时段，状态 `PARTIAL`，不补零、不声称完整。
查询只接受已收盘日期，当日 16:00 后可补当日；每阶段最多 180 秒、整次最多 480 秒，不自动重试。
成功获取的银河交易日历缓存在 `data/galaxy_calendar.json`，只在已验证日期范围内复用。
各周期查询结果分别写入临时检查点；后续周期超时会保留已完成周期，并明确标记失败周期。
全市场按钮沿用原项目股票列表与串行补数，耗时可能较长；本次验收仅下载样本股票。

## 检查

```sh
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q
node --check frontend/js/main_enhanced.js
```

Windows 对应使用 `.venv\Scripts\python.exe -m pytest -q`；首次先安装 `pytest`。

## 2026-09-19 Windows 验收

- 基于 Mac 提交 `4144cf9`，Windows Python 3.12.10、AmazingData 1.1.8、tgw 1.0.8.7、tables 3.11.1。
- 32 项测试通过，包含真实 Windows 虚拟环境进程树退出、原生子进程通信、中文/空格路径、
  UTF-8、配置优先级与脱敏、超时部分结果保留，以及模拟 macOS 的 Docker 入口/容器隔离。
- 使用 Windows 桌面后端真实调用银河：`603938` / `2026-09-17`，日线 1 根、15 分钟 16 根、
  60 分钟 4 根，三周期均 `COMPLETE`，耗时约 80 秒。这是小范围接入验证，非大批量性能验证。
- 原生 PyWebView 窗口及银河补数选择器渲染正常；停止后应用/SDK 残留进程为 0；
  重新启动后 `/api/health` 和 `/api/data/sources` 正常，银河为 `native`。
- 官方 Windows wheel 安装到项目 `.venv`；凭据只在 Git 忽略的本地 `.env`，
  对全部已跟踪和可提交新增文件进行凭据匹配检查通过。
- `start.sh`、`scripts/setup_galaxy.sh`、`scripts/Dockerfile.galaxy` 保持原样。
  本轮没有 Mac 实机，Mac 的真实 Docker 启动仍需在 Mac 上复验。

## 2026-09-18 验收记录

- 18 项测试通过，覆盖补缺去重、错误/空响应/因子缺失不覆盖旧文件、分钟时间转换、
  MA233 预热与更新、跨周期可见时间、随机分钟日期边界、T+1、同日分笔记录和超时容器隔离。
- 真实银河样本 603938：日线 416 根（2025-01-02 至 2026-09-18），
  15 分钟 1,264 根、60 分钟 316 根（2026-06-01 至 2026-09-18）。
  分钟请求区间完整；银河日线缺 2026-07-01，整体仍为 `PARTIAL`，未合成或补零。
- 浏览器实测 2026-09-15 09:45 开局：15 分钟已知 1,201 根、60 分钟 300 根、
  日线 412 根，三个周期 MA55/MA233 均可用。60 分钟与日线只到前一交易日；
  推进至 10:30 后才展示当天首根 60 分钟线。
- 09:45 → 10:00 逐根推进：MA55 48.1801818 → 48.2603636；
  MA233 44.5227039 → 44.5648927，时间戳同步更新。
- 已验证设置中添加并保存 MA55/MA233，以及日线/60分/15分切换后的实际图表渲染。
  全量过去数据用于指标预热，视窗默认展示最近 80 根；主图驱动附图时间轴，
  避免切换周期时旧附图范围将主图推到数据外。
