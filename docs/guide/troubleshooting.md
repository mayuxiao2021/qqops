# 排错

先跑这两条, 八成问题能定位:

```bash
qqops doctor        # 环境 / 存储 / 服务 / 适配器 / 模型 / 保活 逐项体检
qqops status        # 进程、端口、消息量、定时、运行形态、功耗档
```

日志位置:`data/logs/{collector,scheduler,web,watchdog,plugins}.log`;
异常账本:`data/crashes.jsonl`(控制台「运维 → 账本」有聚合视图与堆栈)。

## 消息不进来

| 检查 | 做法 |
|---|---|
| 采集口活着吗 | `qqops doctor` 里 collector 应该是 `[OK]`;`curl 127.0.0.1:8080/` 应返回 JSON 计数 |
| NapCat 有没有上报 | NapCat 的 `httpClients` 里 `url` 必须是 `http://127.0.0.1:8080/`, 改完**要重启 NapCat** |
| 是不是被过滤了 | 「配置 → 采集」的 `groups`/`kinds`;采集指标里的 `skipped` 在涨说明被过滤 |
| 群没勾选 | 「群组」页勾选**只影响总结**;但要确认你搜的群确实有消息入库(「消息」页搜一下) |
| 私聊 | 默认不存(隐私);想存就打开「也保存私聊消息」 |

## 启动失败 / 启动了但没用

| 现象 | 原因与处理 |
|---|---|
| `Errno 98 端口被占用` | 自己的旧进程会自动回收;别人的进程会拒绝并告诉你占用者是谁 |
| "进程秒退" | 输出里有日志尾部;`QQOPS_DEBUG_START=7 python -m qqops.webapp` 可打出线程栈 |
| 起来了但端口不应答 | `qqops status` 里是 `◐`;看 `data/logs/<模块>.log`;多半是模型/配置阻塞了启动 |
| 单进程模式某个服务异常 | 「运维 → 服务」会显示每个服务的健康(端口/心跳)与重启次数 |

## 总结 / 问答不工作

| 现象 | 处理 |
|---|---|
| 提示"模型未配置" | 设置页填 `api_key`;`llm` 段留空时会回落到 `summarizer` |
| 401/403 | 换 key 或换 `base_url`;看日志里的原始错误 |
| 超时 | 调大 `summarizer.timeout`;换更快的模型 |
| 返回不是 JSON | 会自动降级为统计报告;可在提示词里强调"只输出 JSON" |
| 额度用完 | 插件配置里调 `daily_quota`;控制台与 QQ 问答**共用**额度 |
| 答案不准 | 点开来源编号核对;记录里没有它就会直说 |

## QQ 里问没反应

| 条件 | 默认 |
|---|---|
| 是私聊吗 | 私聊默认开(仅白名单) |
| 在不在白名单 | 默认 = 「推送」里的主人 QQ;`assistant` 插件的「允许提问的 QQ 号」可另配 |
| 群聊? | 需同时:允许群问答=开、群号在白名单、消息带前缀(`问`) |
| 被冷却挡住 | 同一人默认 5 秒内只答一次 |
| 私聊事件进得来吗 | 「私聊不入库也交给插件」必须是开(默认开) |

统计在 `/api/assistant/stats` 的 `reply` 字段(也可在控制台看到 `answered/skipped/failed`)。

## 推送失败

1. 「设置 → 推送」的 `napcat_api_port`(默认 3000)与 `token` 要和 NapCat 的 `httpServers` **一致**;
2. `qqops logs scheduler` 看具体错误(常见:token 不对、NapCat 没起、目标 QQ 不是好友);
3. 群里收不到但私聊正常 → `push.group_id` 是 0(默认只发私聊)。

## 手机保活

见[保活](/guide/keepalive)。一句话:`qqops doctor` 的「手机保活」一节会告诉你缺哪项、该执行什么命令。

## 卡片显示不出来

新版 QQ 的自定义 JSON 卡片需要签名;`forward`(合并转发)最稳。
「对话」页有「测试卡片渲染」, 会一次发几种并列出成败原因。原理见[卡片与聊天问答](/CHAT)。

## 时间不对

- 容器/系统时区错了, 报告时间也会错;程序内部对**时间回拨**有保护(未来 >1 天的事件改用本地时间);
- 检查 `TZ`(安装器会设 `Asia/Shanghai`)。

## 数据损坏

启动时会做 `PRAGMA integrity_check(quick)`;失败会提示并从最近备份恢复(需你确认)。
平时多做 `qqops backup`, 恢复步骤见[备份与升级](/guide/backup)。

## 还是要问人?

提 issue 时请带上(**先脱敏**:把令牌换成 `<token>`、QQ 号换成 `<qq>`、群名换成 `<群名>`):

- `qqops --version` 的输出(或 `VERSION` 文件);
- `qqops doctor` 的输出;
- 相关日志片段与 `data/crashes.jsonl` 里对应的条目;
- 复现步骤与期望结果。

模板见仓库的 [issue 模板](https://github.com/mayuxiao2021/qqops/issues/new/choose)。
