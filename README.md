# 🐧 QQOps

面向 **Termux(Android)** 的 QQ 群消息 **采集 / 总结 / 运维控制台**。纯标准库实现,
零 pip 依赖、零前端构建、不占容器 —— 只要 NapCat 在跑,拷目录即可用。

```
QQ ── NapCat(proot 内) ──┬── HTTP 上报 ──▶ QQOps Collector ──▶ SQLite
                          ├── 反向 WS  ──▶ AstrBot(可选, 存档/分析插件)
                          └── HTTP API ◀── QQOps Summarizer(只发你自己)
                                            ▲
                     QQOps Scheduler(每日定点) ── QQOps Web Console(运维界面)
```

## 特性

| 模块 | 能力 |
|---|---|
| **Collector** | 接收 OneBot v11 HTTP 上报;正确处理 `chunked` 请求体;可选 HMAC 签名;按群/类型过滤;落库 + 采集指标 |
| **Summarizer** | 按群/类型取消息 → 大模型总结(OpenAI 兼容)→ Markdown 报告;**统计块不依赖模型**;token/费用记账;推送默认只发私聊 |
| **Scheduler** | 多个每日时间点、当天去重、配置热加载、保留策略清理、每日自动备份、心跳上报 |
| **Web Console** | 概览 KPI + 趋势图、**消息全文检索(FTS5, 命中高亮)**、CSV 导出、群组开关(实时群列表)、任务历史、报告浏览、日志跟踪、配置编辑、服务与**旁路服务(NapCat/AstrBot)启停**、备份/索引重建/自检 |
| **CLI** | `qqops start/stop/restart/status/url/logs/summary/backup/prune/doctor` + `qqops extras` / `qqops napcat{start,stop,restart}` / `qqops astrbot{start,stop,restart}` |

## 快速开始

```bash
# 1) 拷到手机(任选其一)
#    a. 直接解压部署包
tar xzf qqops-1.0.0.tar.gz -C ~ && bash ~/qqops-1.0.0/install.sh
#    b. 或者从已克隆目录安装
bash ~/qqops/install.sh

# 2) 装完会打印控制台地址(含令牌):
#    http://127.0.0.1:8091/?token=xxxxxxxx
```

安装器会自动:检查 `python3`(screen 可选)→ 复制程序 → 生成配置与令牌 → 迁移旧数据(可选)→
注册 `qqops` 命令 → 启动服务 → 跑一次自检。

服务由 `nohup` + `data/run/*.pid` 管理(不依赖 screen 会话,SSH 断线不受影响):
`qqops start|stop|restart|status` 即完整的进程生命周期管理,停止时先 `TERM` 再 `KILL` 兜底,保证端口释放。

### 可选参数

| 参数 | 说明 |
|---|---|
| `--no-start` | 只安装不启动 |
| `--import-db <旧库>` | 迁移旧采集库(`messages.db`) |
| `--import-config <旧配置>` | 迁移旧 `summarize.json`(模型/群/推送) |

## NapCat 侧接线(只做一次)

编辑容器内 `…/app_launcher/napcat/config/onebot11_<QQ>.json`:

```jsonc
"network": {
  // ① 让消息推给采集器(采集口)
  "httpClients": [
    {"enable": true, "name": "qqops", "url": "http://127.0.0.1:8080/",
     "messagePostFormat": "array", "reportSelfMessage": false, "secret": ""}
  ],
  // ② 打开 HTTP API(发送出口 / 实时群列表)
  "httpServers": [
    {"enable": true, "name": "local-api", "host": "127.0.0.1", "port": 3000, "token": "<自定义令牌>"}
  ]
}
```

重启 NapCat(`bash /root/_napcat_run.sh`),然后在控制台「配置」页把 `push.napcat_api_port`
与 `push.token` 填成一致。用 `qqops doctor` 验证:采集口与 NapCat API 都应为 OK。

## 日常使用

```bash
qqops status                        # 进程/端口/消息量/关注的群/定时/全文索引
qqops summary --hours 24            # 立即总结一次(推送到配置目标)
qqops summary --dry-run             # 只组装提示词, 不花钱
qqops logs web                      # 跟踪控制台日志
qqops backup                        # 备份数据库 + 配置
qqops doctor                        # 全链路自检
qqops extras                        # 旁路服务(NapCat/AstrBot)状态
qqops napcat restart                # 重启 NapCat(QQ 接入)
qqops astrbot stop                  # 停掉 AstrBot(存档旁路, QQOps 不受影响)
```

### 全文检索

内置 SQLite **FTS5** 索引, 中文按字匹配、拉丁按前缀匹配, 零额外依赖:

* 控制台「消息」页输入关键词即可, 命中词高亮, 并显示当前检索方式(FTS5 / LIKE / 按时间);
* 中文必须逐字索引 —— `unicode61` 分词器会把整串中文当成一个 token, 所以索引侧在中文字间插空格、
  查询侧用短语匹配(见 `core.fts_text` / `core.fts_query`);
* 索引缺失或落后时启动自动重建; 也可在运维页点「重建全文索引」或 `POST /api/reindex`。

控制台页面:

| 页面 | 用途 |
|---|---|
| 概览 | 消息总量/趋势、服务状态(可一键重启)、NapCat 在线状态、最近任务、立即总结 |
| 消息 | 时间窗 + 群 + 类型 + 关键词筛选, 分页, 点行看详情, 导出 CSV |
| 群组 | **NapCat 实时群列表**(含从未发言的群)、搜索、单个/批量启用总结 |
| 任务 | 每次运行的状态/耗时/token/费用, 手工触发 |
| 报告 | 历史报告列表 + 渲染阅读 |
| 日志 | collector / scheduler / web 三份日志在线 tail |
| 配置 | 全部配置项表单(密钥显示为掩码), 保存即生效 |
| 运维 | 服务启停、备份、CSV 导出、NapCat 信息与**发送测试**、采集指标 |

## 目录结构

```
~/qqops/
├── bin/qqops                 # 控制脚本
├── qqops/
│   ├── core.py               # 配置 / 存储 / NapCat 客户端 / 服务管理 / 日志
│   ├── collector.py          # 采集器(HTTP 上报入口)
│   ├── summarizer.py         # 总结器(LLM + 报告 + 推送)
│   ├── scheduler.py          # 调度器(定时 + 备份 + 清理)
│   ├── webapp.py             # 控制台(JSON API + SPA 外壳)
│   ├── tools.py              # init / doctor / import / backup / prune
│   └── static/               # app.css + app.js(零依赖)
├── tests/smoke.py            # 冒烟测试(unittest)
├── install.sh / uninstall.sh / pack.sh
└── data/                     # ← 全部运行时数据都在这里, 备份只需拷它
    ├── config.json           # 配置(含令牌)
    ├── messages.db           # 消息库(SQLite, WAL)
    ├── run/*.pid             # 服务 PID 文件
    ├── logs/  reports/  backups/
```

## 运维要点

* **备份**: `qqops backup` 或控制台「运维 → 立即备份」;产物在 `data/backups/`。
  调度器每天 03:30 自动备份并按 `retention.backup_keep` 滚动。
* **恢复**: 停止服务 → 用备份的 `messages.db` 覆盖 `data/messages.db` → 启动。
* **升级**: 用新版本覆盖程序目录(`qqops/ bin/ static/ …`), **不要动 `data/`**, 然后 `qqops restart`。
* **保留策略**: 配置页 `retention.message_days > 0` 时每天自动清理超期消息;也可 `qqops prune --days 30`。
* **保活**: 安装器会尝试 `termux-wake-lock`;建议同时关闭电池优化,并参考
  “Android 幽灵进程查杀”的处理(见 `doctor` 输出与本文末)。

## 安全

* 控制台与采集器**默认只监听 127.0.0.1**;要用电脑访问请用 `adb forward tcp:8091 tcp:8091`。
* 令牌鉴权 + HttpOnly 会话 Cookie;配置接口只回传**掩码**密钥,不会把明文发回浏览器。
* 采集器可选 `collector.secret`(HMAC-SHA1 校验 `X-Signature`);如需强制,置 `require_signature=true`。
* 推送默认**只发私聊**;`push.group_id` 非 0 才会发群 —— 想「不在群里发言」就留 0。

## 常见问题

| 现象 | 处理 |
|---|---|
| 控制台打不开 | `qqops status`;`qqops logs web`;确认端口未被占用 |
| 消息不增长 | `qqops doctor` 看采集口;确认 NapCat `httpClients` 指向 8080 且已重启 |
| 总结失败 | 配置页填 `summarizer.api_key`;`qqops logs scheduler` 看错误 |
| 群里收到消息 | 把 `push.group_id` 归零;检查是否有其它插件在发(本程序默认只发自聊) |
| 服务老是被杀 | 关电池优化 + `termux-wake-lock`;必要时用 adb 关闭幽灵进程查杀 |
| 时间不对 | 容器/系统时区;本程序内部强制 `TZ=Asia/Shanghai` |

## 开发

```bash
python3 -m py_compile qqops/*.py        # 语法检查
python3 tests/smoke.py -v               # 冒烟测试(不需要网络)
bash pack.sh                            # 打包 dist/qqops-<ver>.tar.gz
```

## License

MIT
