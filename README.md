<div align="center">

# QQOps

**在手机上把 QQ 群消息管起来:采集 · 总结 · 智能查询 · 运维控制台**

纯标准库 Python,零 pip 依赖、零前端构建;NapCat 负责连 QQ,QQOps 负责其余全部。

[![CI](https://github.com/mayuxiao2021/qqops/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/mayuxiao2021/qqops/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-qqops-3c8772.svg)](https://mayuxiao2021.github.io/qqops/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#为什么零依赖)
[![Platform](https://img.shields.io/badge/platform-Termux%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#安装)

[功能](#功能) · [安装](#安装) · [界面](#界面) · [架构](#架构) · [文档](#文档索引) · [安全](#安全) · [贡献](CONTRIBUTING.md)

</div>

---

## 这是什么

QQOps 是一个**跑在自己设备上的 QQ 群消息管家**。它接在 [NapCat](https://github.com/NapNeko/NapCatQQ)(OneBot v11 实现)后面:

- 群里说过的话 → **落进本地 SQLite**,可按群、类型、时间、关键词全文检索;
- 每天定时 → **让模型总结**:事件、待办、链接、共识、每群滚动记忆,产出 Markdown/HTML/Excel/Word 报告;
- 想问什么 → **一句话问记录**("昨天谁提到报告?"),回答必须带来源编号,点开可核对原文;
- 在 QQ 里 → **直接私聊提问**,答案回到聊天窗口(可选群内问答);
- 出问题 → **控制台自己会说话**:服务健康、崩溃账本、实时日志、一键自检。

数据**全部留在本机**:模型只看到你允许它看的那部分记录,推送默认只发给你自己。

## 功能

| 模块 | 能力 |
|---|---|
| **采集** | OneBot v11 HTTP 上报;`chunked` 请求体、HMAC 签名、按群/类型过滤、消息去重、撤回标记、时钟回拨保护 |
| **总结** | 结构化抽取(事件/待办/链接/事实/记忆)→ 报告(Markdown + 自包含 HTML)→ 可选私聊推送;token 与费用记账 |
| **补料** | 图片多模态识别、文档解析(docx/xlsx/pptx/txt,PDF 可选)、链接正文抓取(带 SSRF 防护),都有配额与缓存 |
| **智能查询** | 中文提问先分词再检索 → 只依据检索结果作答 → 强制标注 `[M1]/[E1]` 来源 → 控制台或 **QQ 聊天里**回答;会话落库、可追问、每日额度 |
| **计划与记忆** | 事件/待办去重合并成长期列表;每群滚动记忆让上下文跨天连续;个人档案判定"与我相关" |
| **调度** | 多个每日时间点 / 间隔 / 每周;无新消息跳过;保留策略清理;每日自动备份 |
| **控制台** | 15 个页面:仪表盘、对话、消息(FTS5 高亮)、总结、任务、插件、数据、运维(服务/资源/账本/日志/自检/备份)、设置、帮助;手机底部标签栏可用 |
| **插件体系** | 对齐 AstrBot 的分层:`@register/@route/@command/@task/@on_event/@on_stage`,插件可热重载、可往控制台加页面 |
| **容错** | 崩溃账本、五层隔离(进程/事件/阶段/请求/插件)、指数退避 + 熔断、端口预检与回收、真实健康检查 |
| **运行形态** | 一个进程跑全部(`single`,手机省 ~74% 内存)或拆成四个进程(`multi`,隔离更好);`performance/balanced/saver` 三档功耗 |

## 安装

> 前置:[NapCat](https://github.com/NapNeko/NapCatQQ) 已在跑(手机用 proot 容器或 Linux 直接跑),并能登录你的 QQ。

```bash
# 1) 下载发布包(或 git clone)
tar xzf qqops-<版本>.tar.gz -C ~ && bash ~/qqops-<版本>/install.sh

# 2) 安装器会:检查 python3 → 复制程序 → 生成配置与随机令牌 → 注册 qqops 命令
#    → 启动服务 → 跑一次自检, 最后打印控制台地址(含令牌)

# 3) 把 NapCat 的消息接到采集口(见下), 然后
qqops doctor
```

NapCat 侧只需接线一次(容器内 `…/napcat/config/onebot11_<QQ>.json`):

```jsonc
"network": {
  // ① 消息上报给采集器
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

详细步骤(Android/Termux、Linux、Windows,含保活与排错):**[docs/DEPLOY.md](docs/DEPLOY.md)**

## 界面

控制台是**零依赖单页**(原生 JS + 一个 CSS 文件,无构建工具),默认只监听 `127.0.0.1`:

| 页面 | 用途 |
|---|---|
| 仪表盘 | 服务健康、今日消息量、最近报告、告警、快捷操作 |
| **对话** | 一句话问记录:气泡问答 + 内联来源编号(点开原文)+ 会话管理 + 推荐问题 |
| 消息 | 时间/群/类型/关键词筛选(全文检索,命中高亮)、分页、导出 CSV |
| 总结 | 报告列表、阅读、导出 XLSX/DOCX、推送状态 |
| 任务 | 定时设置(每天定点/间隔/每周)、运行历史、token 与费用、手工触发 |
| 插件 | 内置与第三方插件:启停、配置(带说明的表单)、重载、命令、定时任务 |
| 数据 | 群组、事件/待办、长期记忆、链接、媒体 |
| 运维 | 服务启停、**资源与功耗**(单/多进程、三档、内存)、崩溃账本、实时日志、框架自检、备份 |
| 设置 | 全部配置项(密钥掩码)、令牌、模型、推送、端口 |

手机浏览器打开即可,底部有标签栏;桌面用 `adb forward tcp:8091 tcp:8091` 转发后访问。

## 架构

```
QQ ──▶ NapCat ──HTTP 上报──▶ collector ──▶ SQLite(消息/事件/记忆/会话)
                                  │
                                  ├─ 管线阶段: ingest → persist → archive → enrich → summarize → notify
                                  ├─ 插件:     reminders(截止提醒) · assistant(智能查询)
                                  └─ 事件总线: 插件与框架松耦合, 单插件异常不影响主流程
   scheduler ──▶ 定时总结 / 维护 / 备份          webapp ──▶ 控制台(HTTP API + SPA)
   watchdog/runner ──▶ 进程或线程级监督, 退避重启 + 熔断
```

分层与 AstrBot 对齐(`app.py` 生命周期、`framework/` 骨架、`plugins/` 相当于 builtin_stars、
`console/` 相当于 dashboard)。设计原则、容错模型、迁移路线见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

### 为什么零依赖

手机上的 Python 装包经常失败(缺编译环境、空间紧张),而"装不上依赖 = 整个程序不可用"。
所以核心只用标准库:SQLite(+FTS5)、`http.server`、`urllib`、`threading`。
`tests/smoke.py` 里有一项测试**专门断言模块级不得出现第三方导入**;唯一可选件是 PDF 解析(`pip install pypdf`)。

## 文档

**在线阅读(推荐):** <https://mayuxiao2021.github.io/qqops/> —— 用 VitePress 构建, 带搜索与侧边导航。

### 使用文档

| 文档 | 内容 |
|---|---|
| [快速开始](docs/guide/quickstart.md) | 10 分钟跑起来:装、接线、第一次总结 |
| [控制台](docs/guide/console.md) | 15 个页面分别做什么、怎么用 |
| [消息与检索](docs/guide/messages.md) | 采集范围、全文检索、去重规则 |
| [总结与报告](docs/guide/reports.md) | 定时/立即/试运行、报告形态、推送、成本控制 |
| [AI 智能查询](docs/guide/chat.md) | 提问、来源编号、追问、额度 |
| [在 QQ 里直接问](docs/guide/qq-chat.md) | 触发规则、白名单、回答形态 |
| [配置详解](docs/guide/config.md) | 每个配置键的作用与默认值 |
| [命令行手册](docs/guide/cli.md) | 全部命令与退出码 |
| [保活(Android)](docs/guide/keepalive.md) | 进程不被杀的三层防护 |
| [备份与升级](docs/guide/backup.md) | 数据在哪、怎么备份/迁移/卸载 |
| [安全与隐私](docs/guide/security.md) | 暴露面、令牌、仓库隐私门禁 |
| [排错](docs/guide/troubleshooting.md) | 按现象查 |
| [插件开发](docs/guide/plugins.md) | 写一个自己的插件 |

### 设计与运维文档

| 文档 | 内容 |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | 部署:Termux/Android、Linux、Windows;NapCat 接线;保活;升级与卸载;排错 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层设计、事件与管线、插件体系、容错模型、配置体系、迁移路线 |
| [docs/CONSOLE.md](docs/CONSOLE.md) | 控制台信息架构、页面规格、交互规范 |
| [docs/INGEST.md](docs/INGEST.md) | OneBot v11 消息接收处理规范(逐条审计) |
| [docs/POWER.md](docs/POWER.md) | 进程合并与功耗档位(含真机实测内存对比) |
| [docs/CHAT.md](docs/CHAT.md) | QQ 内问答;卡片消息实测结论(为什么"卡片版本过低") |
| [docs/FAQ.md](docs/FAQ.md) | 常见问题:成本、隐私、风控、性能、卡片 |
| [CHANGELOG.md](CHANGELOG.md) | 每个版本做了什么、修了什么 |

## 安全

- **默认只监听 127.0.0.1**;控制台令牌鉴权 + HttpOnly 会话 Cookie;配置接口只回传掩码密钥。
- **数据不出本机**:消息、报告、会话都在 `data/`;只有你配置的模型接口会看到你允许的那部分记录。
- **推送默认只发私聊**;群内问答默认关闭,且只对白名单 QQ 生效。
- **仓库本身不含隐私**:见 [SECURITY.md](SECURITY.md) —— 提交前与 CI 都会跑
  `python3 tools/check_secrets.py`(通用密钥规则 + 本机禁词表),`data/`、`dist/`、本机脚本一律不入仓。

发现安全问题请按 [SECURITY.md](SECURITY.md) 的方式私下报告,不要开公开 issue。

## 平台支持

| 平台 | 状态 | 说明 |
|---|---|---|
| **Android / Termux** | ✅ 主要目标 | 日常使用与 CI(Linux)覆盖;保活、功耗档、单进程模式都为此优化 |
| **Linux** | ✅ 完整支持 | CI 覆盖 Python 3.10 / 3.12 / 3.13 |
| **Windows** | ✅ 支持 | CI 覆盖 3.12 / 3.13;服务直接用 `python -m qqops.svc` 系列命令,不需要 bash |
| **macOS** | ⚠️ 尽力支持 | 代码里已补 `ps`/`lsof` 兜底(没有 `/proc`),但 CI 上"独立启动 collector/webapp"的用例仍失败(子进程无任何输出,原因未查明);该 job 标为**实验性**,失败不阻塞。欢迎用 macOS 的人一起查 |

## 开发

```bash
python3 tests/smoke.py            # 冒烟测试(纯标准库, 不需要网络)
node tools/console_probe.js       # 用桩 DOM 渲染全部页面 + 点一遍关键按钮
python3 tools/check_secrets.py    # 隐私/密钥扫描(提交前必跑)
python3 pack.py                   # 打包 dist/qqops-<版本>.tar.gz
python3 tools/verify_package.py   # 校验"包 == 工作区 == 已提交代码"
npm ci && npm run docs:build      # 构建文档站(VitePress, 需要 Node 18+)
bash tools/install-hooks.sh       # 装 pre-commit 钩子(自动跑隐私扫描)
```

贡献流程、代码风格与隐私红线见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[AGPL-3.0](LICENSE) © QQOps contributors

> 使用者需自行遵守 QQ / NapCat / 模型服务商的条款。本项目与腾讯无关,不提供任何形式的账号安全保证;
> 用于群聊时请遵守群规与当地法律,不要采集他人隐私数据。
