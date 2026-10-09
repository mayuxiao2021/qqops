# CHANGELOG

## [1.1.0] — 2026-10-09

### 新增
- **全文检索(FTS5)**: 消息页支持关键词检索, 中文按字匹配、拉丁按前缀匹配, 命中词高亮,
  并显示当前检索方式(FTS5 / LIKE / 按时间); 运维页新增「重建全文索引」与 `POST /api/reindex`。
  中文需逐字索引(unicode61 会把整串中文当一个 token), 故索引侧插入空格、查询侧用短语。
- **旁路服务管理**: NapCat / AstrBot 可在控制台「运维」页启停, 也可用 CLI:
  `qqops extras`、`qqops napcat {start|stop|restart}`、`qqops astrbot {start|stop|restart}`;
  `POST /api/service` 同样支持这两个名字。AstrBot 停止时会清理 `astrbot.lock`。
- 状态快照新增 `extras` / `fts` 字段; `qqops status` 显示旁路服务与索引状态。

### 修复
- **collector**: `signal.SIGHUP` 在 Windows 上不存在, 直接引用会让进程**启动即崩**(改为按平台取信号)。
- **webapp**: 未鉴权访问 `/api/*` 现在返回 **401 JSON**(此前 302 到登录页, 客户端会把登录页 HTML 当 JSON 解析);
  页面路径仍 302 跳登录。
- **FTS**: 群改名时同步更新索引内的群名副本, 否则按群名会搜到旧名。

### 测试
- 测试由 24 项扩展到 **47 项**: 新增中文/拉丁检索、改名同步、索引重建、旁路服务规格、
  版本号一致性、**纯标准库校验**(解析 AST 断言无第三方依赖)、以及**真实子进程集成测试**
  (采集器收消息落库 + SIGTERM 必须能退出; 控制台 HTTP 契约: 鉴权、302/401、HEAD、缓存头唯一)。
  上述前两个 bug 正是被这套集成测试抓出来的。

## [1.0.1] — 2026-10-09

修复「页面渲染不全 / 状态不全」。

### 修复
- **浏览器缓存旧页面**: HTML 与 JSON 接口原来没有任何 `Cache-Control`, 浏览器会启发式缓存,
  换版本后仍显示旧页面(表现为"渲染不全")。现在 HTML/接口一律 `Cache-Control: no-store`,
  静态资源仍走版本化 URL(`?v=<version>`)。
- **状态不全**: 概览页只有「运行中/已停止」。
  - 服务状态现在带 **PID / 常驻内存 / 运行时长 / 端口**;
  - 新增 **生态端口探测**: NapCat WebUI(6099)、反向 WS(6199)、AstrBot(6185)、存档仪表盘(8090)、NapCat API;
  - 新增 **下次总结时间**(由 `schedule.times` 计算)与剩余分钟数;
  - 新增存储面板: 库大小 / 数据目录 / 报告数 / 备份数 / 磁盘剩余 / 启用群数 / 采集计数器;
  - 顶部新增**常驻状态条**(采集·调度·控制台·NapCat 四灯 + 近24h 条数 + 启用群 + 下次总结),
    每 10 秒独立刷新, 任何页面都能看到。

### 新增
- `GET /api/status`: 轻量完整状态快照(服务/生态端口/采集/调度/存储/消息), 供状态条与自动化使用。
- `core.status_snapshot()` / `core.proc_info()` / `core.next_run_time()` / `core.ecosystem_ports()`。

## [1.0.0] — 2026-10-09

首个可部署版本。把此前散落的脚本(listen.py / summarize.py / panel.py / _sched.py)
重构为一个有安装器、控制台、CLI 与测试的完整程序。

### 新增
- **安装器** `install.sh`:依赖检查、程序复制、配置与令牌生成、旧数据迁移、命令注册、
  服务启动、自动自检;支持 `--no-start` / `--import-db` / `--import-config`。
- **Web Console**(`qqops/webapp.py` + `static/`):SPA 外壳 + JSON API,暗色主题、响应式、
  自动刷新;八个页面(概览/消息/群组/任务/报告/日志/配置/运维)。
- **群组页**:群列表改为调用 NapCat `get_group_list` 的**实时数据**(含从未发言的群),
  支持搜索、单个/批量启用总结。
- **任务页**:每次运行入库(`runs` 表),记录状态、耗时、token 与费用。
- **运维页**:服务启停、立即备份、CSV 导出、NapCat 信息与发送测试、采集指标。
- **CLI** `bin/qqops`:start/stop/restart/status/url/logs/summary/backup/prune/doctor。
- **Scheduler**:多时间点、当天去重、配置热加载、每日自动备份与保留策略清理、心跳。
- **Tools**:`init / doctor / import / backup / prune`。
- **测试** `tests/smoke.py`:24 项冒烟测试(配置/存储/总结/过滤/会话),不依赖网络。
- **打包** `pack.sh` → `dist/qqops-<version>.tar.gz`。

### 工程规范
- 配置:默认值深合并、校验、密钥掩码、损坏自动留证;**原子写**(临时文件 + fsync + rename)。
- 存储:WAL 模式、索引、`runs`/`meta` 表;导出 CSV 带 BOM(Excel 直接打开不乱码)。
- 日志:分级 + 按大小滚动;采集器/调度器/控制台/总结各自独立日志。
- 安全:仅监听 loopback、令牌 + HttpOnly 会话、密钥只回传掩码、可选 HMAC 上报校验。

### 修复(来自历史踩坑)
- **采集器信号死锁**: 原在信号处理器里调用 `srv.shutdown()`, 而 `serve_forever()` 跑在同一线程 →
  进程无法退出、一直占着端口, 重启后新实例绑定失败即退出。改为落指标后 `os._exit(0)`。
- 服务判活模式与启动方式不一致(`collector.py` vs `-m qqops.collector`)→ 会造成「状态显示未运行、按重启却重复启动」。
- 进程管理改为 `nohup` + PID 文件 + TERM/KILL 两段式停止, 不再依赖 screen 会话名(避免 "Session name already in use")。
- 旧配置迁移只写 `summarizer.groups`、未同步群勾选 → 现在同时写 `groups.enabled`, 控制台是唯一事实来源。
- NapCat 用 `Transfer-Encoding: chunked` 上报导致连接错位 → 显式消费请求体 + HTTP/1.0 关闭连接。
- 容器时区为 UTC 导致定时偏移 8 小时 → 全程序强制 `TZ=Asia/Shanghai`。
- 插件配置 BOM 导致写入失败 → 统一 `utf-8-sig` 读取。
