# 插件开发

QQOps 的插件体系与 AstrBot 对齐:一个目录一个插件, `metadata.json` 声明元信息,
`_conf_schema.json` 声明配置(同时是控制台表单), `__init__.py` 写代码。

最小可用的例子是内置插件 [`plugins/reminders`](https://github.com/mayuxiao2021/qqops/tree/master/qqops/plugins/reminders),
建议边看边改。

## 目录结构

```
qqops/plugins/myplugin/
├── metadata.json        # id / 名称 / 版本 / 描述 / 优先级 / 是否默认启用
├── _conf_schema.json    # 配置项(类型/默认值/中文说明/hint), 控制台据此渲染表单
└── __init__.py          # 插件实现
```

数据文件写到 `data/plugin_data/<插件id>/`, **不要写进程序目录**。

## metadata.json

```json
{
  "id": "myplugin",
  "name": "我的插件",
  "version": "1.0.0",
  "author": "you",
  "desc": "一句话说明它做什么",
  "priority": 50,
  "enabled_by_default": true,
  "depends": []
}
```

## 插件骨架

```python
from qqops.framework.api import Plugin, register, route, command, task, on_stage, on_event


@register(id="myplugin", name="我的插件", version="1.0.0", desc="示例")
class MyPlugin(Plugin):

    def initialize(self, ctx):
        """插件加载时调用一次。拿配置用 self.cfg, 记日志用 self.info/warn/error。"""
        self.info("就绪")

    # 控制台接口: 自动出现在「运维 → 自检」的路由清单里
    @route("/api/myplugin/ping")
    def api_ping(self, req, ctx):
        return {"ok": True, "cfg": self.cfg}

    # 具名命令: 控制台/CLI 可调用
    @command("myplugin.hello", help="打个招呼")
    def cmd_hello(self, args=None, ctx=None):
        return {"hello": (args or {}).get("name") or "world"}

    # 定时任务(默认在调度进程角色里跑)
    @task("myplugin_tick", every=300, run_on_start=True)
    def tick(self, ctx):
        ...

    # 事件订阅(采集进程角色): 消息进来就会收到
    @on_event("message.received")
    def on_msg(self, event, ctx):
        ...

    # 管线阶段: 想插在"落库之后、富化之前"这类位置就用它(order 越小越先跑)
    @on_stage("myplugin_scan", order=45)
    def scan(self, event, ctx):
        if event.kind != "message.received":
            return None
        return {"seen": 1}
```

## 钩子一览

| 装饰器 | 角色 | 说明 |
|---|---|---|
| `@register` | — | 注册插件(必需) |
| `@route(path, methods=("GET",))` | console | 控制台接口, 处理器签名 `(req, ctx) -> dict` |
| `@command(name, help="")` | console | 具名命令, 控制台与 CLI 可调 |
| `@task(name, every=秒, cron="分 时 日 月 周", run_on_start=False)` | tasks | 定时任务 |
| `@on_event(kind, priority=100)` | ingest | 订阅事件总线(`kind="*"` 订阅全部) |
| `@on_stage(name, order, critical=False)` | ingest | 在管线上插一个阶段 |
| `@page({"id", "title", "icon", "order"})` | console | 往控制台导航加一个页面 |

角色(role)决定这个处理器在哪种进程里生效:`ingest`(采集)/`tasks`(调度)/`console`(控制台)。
单进程模式下三种角色都在同一个进程里。

## 拿得到什么

| 想做的事 | 用法 |
|---|---|
| 配置 | `self.cfg`(该插件的配置字典, 按 schema 校验后的值) |
| 日志 | `self.info/warn/error/debug(msg)` → 写 `data/logs/plugins.log` 并进控制台日志流 |
| 数据库 | `self.ctx.store`(`messages()/events()/memory_*` 等, 见 `qqops/core.py`) |
| 全局配置 | `self.ctx.cfg` |
| 发 QQ 消息 | `core.napcat_from_config(self.ctx.cfg)` → `send_text/send_forward/send_markdown/...` |
| 记账 | `from qqops.framework import errors; errors.record(...)` |
| 事件 | `self.ctx.publish(Event("myplugin.something", {...}))` |

## 三条红线(CI 会挡)

1. **不引入第三方依赖** —— 核心必须只用标准库;确实需要就做成可选依赖 + 优雅降级;
2. **异常必须被兜住** —— 循环/线程/请求/钩子里都要有出口(记账 + 日志 + 降级),
   不能让一个插件的异常把进程带走。事件总线与管线本来就会 `guard`, 但你的后台线程不会;
3. **不提交隐私** —— 见 [CONTRIBUTING](https://github.com/mayuxiao2021/qqops/blob/master/CONTRIBUTING.md) 与
   [安全与隐私](/guide/security)。

## 调试与测试

```bash
python3 tests/smoke.py           # 内置插件在这个套件里都有测试(照抄那几段最快)
node tools/console_probe.js      # 改了控制台页面/表单就跑它
python3 tools/check_secrets.py   # 提交前
```

插件状态与配置在控制台「插件」页:启用/停用、改配置、重载、看命令与定时任务清单。
热重载会用「停用→重新导入」的方式替换, 所以别在模块级保存跨重载状态。

## 参考

- 内置插件:[`reminders`](https://github.com/mayuxiao2021/qqops/tree/master/qqops/plugins/reminders)(最小完整例子)、
  [`assistant`](https://github.com/mayuxiao2021/qqops/tree/master/qqops/plugins/assistant)(有接口/会话/降级)
- 框架实现:[`framework/plugin.py`](https://github.com/mayuxiao2021/qqops/blob/master/qqops/framework/plugin.py)、
  [`framework/bus.py`](https://github.com/mayuxiao2021/qqops/blob/master/qqops/framework/bus.py)、
  [`framework/pipeline.py`](https://github.com/mayuxiao2021/qqops/blob/master/qqops/framework/pipeline.py)
- 设计背景:[架构总览](/ARCHITECTURE)
