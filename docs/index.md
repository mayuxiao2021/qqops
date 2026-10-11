---
layout: home

hero:
  name: QQOps
  text: 在手机上把 QQ 群消息管起来
  tagline: 采集 · 总结 · AI 智能查询 · 运维控制台 —— 纯标准库 Python, 零依赖, 数据全在本机
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/quickstart
    - theme: alt
      text: 部署到 Termux
      link: /DEPLOY
    - theme: alt
      text: GitHub
      link: https://github.com/mayuxiao2021/qqops

features:
  - icon: 📥
    title: 采集
    details: 接 NapCat(OneBot v11)上报, 消息落进本地 SQLite:去重、撤回标记、时钟回拨保护、可选 HMAC 签名。
  - icon: 📝
    title: 总结
    details: 定时让模型总结出事件/待办/链接/共识/每群记忆, 产出 Markdown、自包含 HTML、Excel、Word, 可选推送到你自己的 QQ。
  - icon: 💬
    title: AI 智能查询
    details: 一句话问记录("昨天谁提到报告?"), 答案必须带 [M1] 来源编号, 点开可核对原文; 也能直接在 QQ 私聊里问。
  - icon: 🛠
    title: 运维控制台
    details: 15 个页面的手机优先界面:服务健康、崩溃账本、实时日志、资源与功耗档位、一键自检。
  - icon: 🧩
    title: 插件体系
    details: 与 AstrBot 对齐的分层与钩子(@register/@route/@command/@task/@on_event/@on_stage), 插件可热重载、可加控制台页面。
  - icon: 🔒
    title: 数据不出本机
    details: 只监听 127.0.0.1, 令牌鉴权; 只有你配置的模型接口会看到你允许的那部分记录; 推送默认只发私聊。
---

## 它解决什么问题

群里聊过的事, 过两天就找不到了; 想回顾"上周谁说要交报告", 只能一路翻聊天记录。
QQOps 把这件事变成:

- **自动存下来** → 本地全文检索(中文按字匹配), 按群/类型/时间筛;
- **自动总结** → 每天定时把有价值的结论、待办、链接沉淀成报告和长期记忆;
- **随时能问** → 控制台或 QQ 里一句话问, 答案带来源编号可核对;
- **出事看得到** → 服务状态、崩溃账本、日志、自检全在控制台。

## 从哪里开始

| 你是 | 看这里 |
|---|---|
| 第一次装 | [快速开始](/guide/quickstart) → [部署到 Termux/Android](/DEPLOY) |
| 已经跑起来了 | [控制台使用](/guide/console) · [配置详解](/guide/config) · [命令行手册](/guide/cli) |
| 想用 AI 查记录 | [AI 智能查询](/guide/chat) · [在 QQ 里直接问](/guide/qq-chat) |
| 出问题了 | [排错](/guide/troubleshooting) · [常见问题](/FAQ) |
| 想改代码/写插件 | [插件开发](/guide/plugins) · [架构总览](/ARCHITECTURE) · [贡献指南](https://github.com/mayuxiao2021/qqops/blob/master/CONTRIBUTING.md) |

## 几个事实

- **零第三方依赖**:核心只用 Python 标准库(SQLite+FTS5、http.server、urllib、threading),
  手机上的 `pip install` 经常失败, 所以干脆不需要。
- **单进程或四进程**:Android 上默认合并成一个进程(实测 4 进程 104.6 MB → 1 进程 27.5 MB),
  桌面可拆开换取隔离。
- **三档功耗**:性能/均衡/省电, 省电档把巡检拉长、把抓图抓链接推到夜间窗口。
- **隐私可验证**:仓库里有 [隐私门禁](https://github.com/mayuxiao2021/qqops/blob/master/SECURITY.md),
  提交前与 CI 都会扫描密钥/隐私;`data/` 永不进仓库。
