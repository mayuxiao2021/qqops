# QQOps 架构规划(v3)

> 目标:把 QQOps 从"一组各自为战的脚本"重构成**可扩展、可容错、可观测**的运维平台,
> 结构上对齐 AstrBot(事件总线 + 阶段化管线 + 插件体系 + 集中配置 + 控制台),
> 但保持 QQOps 的立身之本:**纯标准库、零第三方依赖、能在未 root 的 Termux 上跑**。

---

## 1. 设计原则

| 原则 | 含义 | 反面案例(我们踩过的) |
|---|---|---|
| 纯标准库 | 只用 Python 标准库,可选功能懒加载 | 装不上依赖 = 整个程序不可用 |
| 单进程可跑、多进程可扩 | 一个进程能跑全部;也可拆成 4 个服务分进程 | 一个模块崩了整套挂掉 |
| 失败必须可见 | 任何异常都要有出口:账本 + 日志 + 控制台告警 | `pgrep` 命中就报"已启动",其实秒退 |
| 失败必须可隔离 | 一个插件/阶段/请求的异常不能影响其它 | 坏插件导致控制台白屏 |
| 配置即文档 | 配置 schema 同时是默认值、校验规则、控制台表单 | 手写 DEFAULT_CONFIG,改一处漏三处 |
| 迁移不停机 | 新老结构并行(strangler),每步都能跑测试、能部署 | 大爆炸重写导致线上失联 |

---

## 2. 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│  界面层  console/          控制台(HTTP API + 单页前端)        │
│           ├ router.py      路由注册表(插件可挂页面/API)        │
│           ├ pages/         各页面 API                          │
│           └ static/        前端(app.js / app.css)              │
├──────────────────────────────────────────────────────────────┤
│  应用层  app.py             生命周期编排(≈ AstrBotCoreLifecycle)│
│           启动顺序: 配置 → 日志 → 数据库 → 插件 → 管线 → 适配器 │
│           → 控制台 → 监督;支持 reload/restart/优雅退出          │
├──────────────────────────────────────────────────────────────┤
│  插件层  framework/plugin.py + plugins/                        │
│           内置插件 & 第三方插件,统一 @register + 生命周期       │
├──────────────────────────────────────────────────────────────┤
│  管线层  framework/pipeline.py                                 │
│           事件按阶段流动,阶段可开关、可插拔、可短路              │
├──────────────────────────────────────────────────────────────┤
│  适配层  adapters/onebot.py   NapCat/OneBot v11(可扩展其它平台) │
├──────────────────────────────────────────────────────────────┤
│  服务层  core/services.py     进程管理/端口/健康/看门狗          │
└──────────────────────────────────────────────────────────────┘
    基础层  core/{paths,config,store,state,log,errors}.py
```

### 2.1 与 AstrBot 的对齐

| AstrBot | QQOps v3 | 说明 |
|---|---|---|
| `core/core_lifecycle.py` | `app.py` | 启动/停止/重载编排,任务统一包裹 |
| `core/event_bus.py` | `framework/bus.py` | 线程安全队列 + 工作线程 + 异常回收(QQOps 是同步多线程,不引 asyncio) |
| `core/pipeline/{stage,scheduler,stage_order}.py` | `framework/pipeline.py` | Stage 基类 + 有序调度 + 短路 |
| `core/star/{star,star_manager,context,config,filter,register}` | `framework/{plugin,manager,registry,context}.py` | 插件基类/管理器/注册表/上下文 |
| `api/` | `framework/api.py` | 插件唯一可见的公开面(其它内部模块不保证稳定) |
| `core/platform/` | `adapters/` | 消息平台适配器 |
| `core/provider/` | `framework/llm.py` | LLM 供应商抽象(DeepSeek 为默认实现) |
| `core/config/{default,astrbot_config}.py` | `core/config.py` + `framework/schema.py` | schema 即默认值 + 校验 + 迁移 + 快照 |
| `core/log.py` `LogBroker` | `framework/log.py` | 环形缓冲 + 控制台实时日志流 |
| `core/db/` | `core/store.py` | SQLite(+FTS5),WAL,迁移 |
| `core/exceptions.py` `process_restart.py` | `framework/errors.py` + `core/services.py` | 异常体系 + 崩溃账本 + 服务监督 |
| `builtin_stars/` | `plugins/` | 内置插件 |
| `dashboard/` + `StarMetadata.pages` | `console/` + 插件 `@page` | 插件可往控制台加页面 |
| `data/plugins.json`, `data/config/<id>_config.json` | 同左 | 插件启用状态 + 每插件配置 |

**刻意不照搬的部分**:asyncio/pydantic/loguru/SQLModel/pip 依赖自动安装 —— QQOps 面向的是
"零依赖、能离线跑"的场景,用 `threading` + `sqlite3` + `logging` 达到同样效果。

---

## 3. 事件与管线

### 3.1 事件模型

```python
Event(kind="message.received", payload={...}, source="onebot", ts=..., id="evt-...", session="group:123")
```
| kind | 触发者 | 典型消费者 |
|---|---|---|
| `message.received` | 适配器(OneBot 上报) | ingest 阶段、归档插件、提醒插件 |
| `message.batch` | 采集器批量落库后 | 富化(媒体/链接/文件) |
| `task.due` | 调度器 | 总结插件、维护任务 |
| `report.ready` | 总结插件 | 推送插件、控制台 |
| `service.changed` | 服务管理 | 看门狗、仪表盘 |
| `error.raised` | 容错内核 | 崩溃账本、告警插件 |
| `plugin.loaded` / `plugin.failed` | 插件管理器 | 控制台、告警 |
| `plan.reminder` | 计划/提醒插件 | 推送插件 |

### 3.2 阶段(顺序即 `order`,可开关、可短路)

| # | 阶段 | 职责 | 失败语义 |
|---|---|---|---|
| 10 | `ingest_filter` | 群白名单/消息类型/私聊过滤 | 丢弃事件(记 debug) |
| 20 | `persist` | 消息/群落库(FTS 索引) | 重试 3 次 → 记账,事件继续 |
| 30 | `archive` | 事件/记忆/链接抽取落库 | 记账,不影响主流程 |
| 40 | `enrich` | 媒体抓取+多模态描述、链接抓取、文件抽取 | 记账,标记"待补" |
| 50 | `summarize` | 生成总结报告(定时/手动) | 重试 → 降级为统计报告 |
| 60 | `notify` | 推送到 QQ / 控制台通知 | 记账,失败不影响报告落盘 |

约定:**阶段之间用事件/上下文传递**,阶段内异常一律被 `guard` 捕获并记账,
管线**永不因单个阶段异常而中断**;`Stage.critical=True` 的阶段(如 `persist`)失败会导致
该事件被移入 `data/failed/`(可重放),而不是静默丢失。

---

## 4. 插件体系

### 4.1 目录结构(与 AstrBot 一致)

```
plugins/<plugin_id>/
  metadata.json        # 名称/显示名/版本/作者/描述/仓库/优先级/依赖
  _conf_schema.json    # 配置 schema → 自动生成控制台表单 + 校验
  __init__.py          # 导出 Plugin 子类
  plugin.py            # 实现(可选,便于大插件拆分)
  tests/               # 插件自带测试(可选)
  static/              # 插件自带前端资源(可选)
```
运行时数据:`data/plugin_data/<plugin_id>/`;配置:`data/config/<plugin_id>_config.json`;
启用状态:`data/plugins.json`。

### 4.2 插件 API(`framework/api.py`,唯一稳定面)

```python
from qqops.framework.api import Plugin, register, on_event, on_stage, task, command, route, page

@register(id="reminders", name="截止提醒", version="1.0.0", author="qqops",
          desc="从群消息里识别时间点, 到点提醒", priority=20)
class RemindersPlugin(Plugin):
    def initialize(self, ctx):        # ctx 提供 cfg/log/store/bus/state/http/napcat/console
        ...
    def terminate(self):
        ...

    @on_event("message.received", priority=30)
    def scan(self, event, ctx): ...

    @task("reminder_tick", every=60)
    def tick(self, ctx): ...

    @command("reminders.due", help="列出即将到期的提醒")
    def due(self, ctx, args): ...

    @route("/api/reminders/list")
    def api_list(self, req, ctx): ...

    @page({"id": "reminders", "title": "提醒", "icon": "⏰"})
    def page_data(self, req, ctx): ...
```

### 4.3 生命周期与隔离

1. **发现**:扫描 `plugins/*/metadata.json`(内置插件与第三方插件同等待遇)
2. **加载**:`importlib` 导入 → 收集 `@register` 的类 → 实例化 → `initialize(ctx)`
   - 任何一步异常:**只标记该插件失败**,写崩溃账本,发 `plugin.failed`,**不影响其它插件**
3. **启用/停用**:写入 `data/plugins.json`,停用时调用 `terminate()` 并注销其 handler/路由
4. **热重载**:监视插件目录 mtime,`reload(plugin_id)` = 注销 → 重新导入 → 重新注册
5. **配置**:`_conf_schema.json` 驱动:类型/默认值/说明/`hint`/`enum`/`min-max`/`secret`;
   控制台表单由 schema 生成;后端校验失败回退默认值并记账

### 4.4 内置插件(首批)

| 插件 | 职责 | 迁移自 |
|---|---|---|
| `collector` | OneBot 适配器 + ingest 阶段 + persist | `collector.py` |
| `summarizer` | 总结任务 + 报告 + 推送 | `summarizer.py`/`extract.py`/`render.py` |
| `scheduler` | 定时计划(灵活模式)+ 维护任务 | `scheduler.py` |
| `watchdog` | 服务监督(退避/熔断) | `watchdog.py` |
| `media` | 媒体抓取 + 多模态描述 | `media.py` |
| `links` | 链接抓取 + 摘要 | `linkfetch.py` |
| `reminders` | 截止提醒(新功能,验证插件体系) | — |
| `archive` | 事件/记忆/链接归档浏览 | 部分 `core.Store` |

---

## 5. 容错模型(本次重点)

### 5.1 三层隔离

| 层 | 手段 | 效果 |
|---|---|---|
| 进程级 | 服务监督 + 健康检查 + **指数退避** + 崩溃循环熔断 | 进程被杀/假活 → 自动拉起;反复崩溃 → 停止重启并告警,不再雪崩 |
| 事件级 | `EventBus` 对每个 handler 独立 `guard` | 一个插件抛异常,其它插件照常收到事件 |
| 阶段级 | `PipelineScheduler` 对每个阶段 `guard` + `critical` 语义 | 富化/推送失败不影响落库 |
| 请求级 | 控制台每个请求 `guard` + `request-id` + 500 JSON | 单请求异常不会让控制台白屏或进程退出 |

### 5.2 崩溃账本 `framework/errors.py`

- 位置:`data/crashes.jsonl`(追加写,自动轮转),字段:`ts/component/kind/exc/message/traceback(截断)/extra`
- 去重与限流:同一 `component+exc` 在窗口内只记首末次 + 计数,避免日志爆炸
- 出口:`/api/ops/crashes`(控制台可视化)、`qqops doctor` 摘要、可选 QQ 告警
- 清理:控制台一键清空;保留最近 N 条

### 5.3 退避与熔断 `Backoff`

```
attempt: 1 → 2 → 3 → 4 ...      delay: 5s → 15s → 45s → 2m → 5m(上限)
窗口内崩溃次数 ≥ 5 → crash_loop: 停止自动重启, 账本 + 告警 + 控制台红色横幅
人工"重置退避"或成功运行 ≥ 10 分钟 → 退避清零
```

### 5.4 降级矩阵

| 失败点 | 降级策略 |
|---|---|
| LLM 无 key / 401 | 只出统计报告(已有) |
| LLM 超时 / 5xx | 指数退避重试 3 次 → 用部分结果成文 → 标注"降级" |
| LLM 返回非 JSON | 修复重试(提取花括号)→ 仍失败 → 存原文并标注 |
| NapCat 不可用 | 报告仅落盘不推送;控制台提示"推送待恢复",恢复后补推 |
| 图片/媒体抓取失败 | 记录 `pending` 标记,后台任务补抓(最多 N 次) |
| 链接抓取失败 | 只存 URL 与标题占位 |
| 导出 xlsx/docx 失败 | 回退 md(纯文本一定有) |
| SQLite 写失败(busy) | `busy_timeout` + 3 次重试 → 记账 + 队列缓存 |
| 配置非法 | 回退默认值 + 校验提示(不阻塞启动) |
| 插件导入失败 | 标记失败 + 账本 + 控制台可见 + 其它插件照常 |
| 控制台接口异常 | 500 JSON + request-id;前端错误边界降级为"该卡片不可用" |

### 5.5 数据与运行环境保护

- SQLite:**WAL** + `busy_timeout=5000` + `synchronous=NORMAL`;启动时 `PRAGMA integrity_check`(quick)
- 损坏自愈:integrity 失败 → 提示 + 从最近备份恢复(需确认);备份保留策略沿用
- 磁盘水位:可用空间 < 500MB → 控制台告警 + 暂停媒体补抓
- 全局钩子:`sys.excepthook` / `threading.excepthook` / `SIGTERM/SIGINT` → 账本 + 优雅退出
- 时钟异常:检测到时间回拨(常见于手机)时`,记录并在调度器上做保护

---

## 6. 配置体系

- **单一来源**:每个模块的配置由 `schema` 声明(类型/默认值/说明/hint/依赖),schema 即默认值
- **分层**:全局 `data/config.json`(核心)+ 每插件 `data/config/<id>_config.json`
- **校验**:类型转换失败/越界/枚举非法 → 回退默认值 + 返回告警列表(控制台展示)
- **迁移**:旧键自动迁移(如 `schedule.hours` → `summarizer.window_hours`),迁移记录写入日志
- **快照**:每次保存前写 `data/config.bak-<ts>`,控制台可回滚最近 5 份
- **密钥**:schema 中 `secret: true` 的字段在 API 返回时掩码,前端只显示"已配置/未配置"

---

## 7. 日志与可观测

| 能力 | 实现 |
|---|---|
| 分级日志 | 文件 `data/logs/<模块>.log`,按大小轮转(保留 3 份) |
| 实时流 | `framework/log.py` `LogBroker`:内存环形缓冲(2000 条)+ 游标拉取 `/api/logs?since=<seq>`,控制台"运维→日志"页自动滚动 |
| 请求追踪 | 每个控制台请求分配 `rid`,异常时写入响应与账本,便于对号入座 |
| 指标 | 每服务:启动时间/重启次数/崩溃次数/最近错误;每插件:调用次数/失败次数/耗时 |
| 诊断 | `qqops doctor`(环境/配置/存储/服务/适配器/模型/保活)+ 控制台"一键诊断" |

---

## 8. 控制台信息架构

详见 `docs/CONSOLE.md`。要点:

- **9 个一级页面**:仪表盘 / 消息 / 总结 / 任务 / 插件 / 数据 / 运维 / 设置 / 帮助
- **插件可扩展**:`@page` 注册新页面,`@route` 注册 API(AstrBot 的 `StarMetadata.pages` 思路)
- **前端三态**:加载 / 空 / 错误,任何接口失败只影响对应卡片
- **危险操作**:二次确认 + 操作后 toast + 自动刷新
- **移动端优先**:手机浏览器是主场景,单手可操作

---

## 9. 目标目录结构(v3.0 完成后)

```
qqops/
  app.py                    # 生命周期编排
  framework/                # 框架(内核)
    api.py  bus.py  event.py  plugin.py  manager.py  registry.py
    pipeline.py  context.py  schema.py  errors.py  log.py  llm.py  web.py
  plugins/                  # 内置插件(等价 builtin_stars)
    collector/ summarizer/ scheduler/ watchdog/ media/ links/ reminders/ archive/
  adapters/onebot.py        # 消息平台适配器
  console/                  # 控制台(等价 dashboard)
    router.py  pages/  static/
  core/                     # 基础层(由 core.py 逐步拆分)
    paths.py  config.py  store.py  state.py  services.py  render.py
  tools.py  svc.py  cli.py
data/
  config.json  config/  plugins.json  plugin_data/  crashes.jsonl
  messages.db  logs/  reports/  backups/  run/  media/  failed/
```

---

## 10. 迁移策略(strangler,不停机)

| 步骤 | 内容 | 验收 |
|---|---|---|
| S1 ✅ | 容错内核 + 五层隔离接入现有四个服务 | PC/真机测试全绿;注入异常不崩 |
| S2 ✅ | 框架骨架(bus/pipeline/plugin/registry/context/log)+ 控制台路由注册 | 插件可加载/启停/热重载;日志流可用 |
| S3 ✅ | 首批插件(reminders 新功能 + 现有能力包装) | 插件页可见可配;提醒端到端可用 |
| S4 | `core.py` 拆分为 `core/` 包(paths/config/store/state/services),`__init__` 兼容转发 | 测试不变;导入路径零改动 |
| S5 | collector 变适配器 + 管线阶段化(ingest/persist/enrich 分离) | 消息延迟不升;失败可重放 |
| S6 | summarizer/extract/render 插件化 + LLM 供应商抽象 | 报告产物不变;降级可测 |
| S7 | 前端组件化(三态/表格/表单渲染器)+ 全页 schema 驱动表单 | 无重复代码;新增配置零前端改动 |

每一步都遵守:**先 PC 测试 → 再真机测试 → 再部署 → 观察 24h**;任何一步不达标就回滚(有 git 标签)。

---

## 11. 路线图

**v3.0(本轮)**
- 容错内核(账本/守卫/重试/退避/熔断)+ 全局钩子
- 事件总线 + 阶段化管线 + 插件体系 + 日志广播
- 控制台路由注册 + 仪表盘/插件/运维(崩溃账本+日志流)+ 三态与错误边界
- 首批内置插件:reminders(新)+ 现有能力包装
- 测试 ≥ 140 项;真机验收含"故障注入"

**v3.1**
- `core.py` 拆分、collector 适配器化、插件市场式安装(从 URL/压缩包导入插件)

**v3.2**
- LLM 供应商抽象(多 provider/多模型)、函数调用工具(把 QQOps 数据暴露给 LLM)、
  报告模板系统、WebSocket 推送替代轮询

**v3.3**
- 多平台适配器(Telegram/Discord 只读采集)、多实例管理、权限与多用户控制台

---

## 12. 质量门禁

1. `python tests/smoke.py` 全绿(PC + 真机各跑一遍)
2. 故障注入必须通过:坏插件、坏配置、端口占用、强杀进程、LLM 报错、DB 忙
3. 打包(`python pack.py`)后校验:包内容 == 工作区 == 已提交代码
4. 控制台在手机浏览器上可用(无横向滚动、按钮可点、接口失败有提示)
5. 每个改动都要有对应测试;CHANGELOG 记录"改了什么/为什么/怎么验证"
