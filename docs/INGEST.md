# QQOps 消息接收处理规范

> 本文是 NapCat → QQOps 采集器的**接口契约与处理规范**,同时是 2026-10-10 那次
> 「检查消息接收处理规范」的审计记录:每一行的"现状/处置"都对应代码与测试。
> 实现见 `qqops/collector.py`、`qqops/core.py`(Store),测试见 `tests/smoke.py::TestIngestSpec`。

---

## 1. 链路总览

```
NapCat(OneBot v11)  --HTTP POST-->  collector(127.0.0.1:8080)
                                      │ 1) 校验(体积/签名)
                                      │ 2) 解析 JSON
                                      │ 3) 按 post_type 分派
                                      ├─ message / message_sent → 过滤 → 落库(去重) → 发事件
                                      ├─ notice                → 撤回标记 → 发事件
                                      └─ meta_event            → 心跳写 state
                                      ↓
                              store(messages/wal) → 总线(ingest 角色插件: 提醒扫描等)
```

**为什么先落库再发事件**:NapCat 对上报有超时,事件交给总线异步处理,采集路径只做"收下+入库",
200 才能尽快返回。总线/插件的异常都被隔离,不会影响接收。

---

## 2. 逐条规范与审计结论

| # | 规范要求 | 现状 | 处置 |
|---|---|---|---|
| 1 | 正确处理 `Transfer-Encoding: chunked`(NapCat 默认这么发) | ✅ 已实现 | 补了裸 socket 端到端测试(`test_07`) |
| 2 | `Content-Length` 缺失/为 0 时不要读错 | ✅ 返回空体 → 400 | 保持 |
| 3 | 请求体要有上限,防止畸形上报吃光内存 | ❌ 原先无上限 | **新增 8MB 上限,超限直接 413(不读体)** |
| 4 | 仅监听 loopback | ✅ 默认 `127.0.0.1` | 保持(配置可改,但不建议) |
| 5 | 签名可选:配了 `secret` 就校验 `X-Signature`(HMAC-SHA1) | ✅ 已实现 | 保持;`require_signature` 可强制 |
| 6 | 应答必须是 OneBot 认可的 `{"status":"ok","retcode":0}` | ✅ HTTP/1.0 + `Connection: close` | 保持(客户端解析错位的坑踩过) |
| 7 | `post_type=message` → 落库 | ✅ | 保持 |
| 8 | `post_type=message_sent`(机器人自己发的) | ❌ 原先忽略 | **改为落库并标记 `self=1`**(对话上下文需要) |
| 9 | `post_type=notice`(撤回/入群等) | ❌ 原先忽略 | **`group_recall` → 标记 `recalled=1` 并发 `message.recalled` 事件**;其余记 DEBUG |
| 10 | `post_type=meta_event`(心跳/生命周期) | ❌ 原先忽略 | **心跳写 `state.napcat_beat` + `napcat_status`**,连接状态变化记日志(这是"NapCat 真的连着吗"唯一可靠信号) |
| 11 | `post_type=request`(加好友/入群请求) | 忽略 | 计数 + INFO 日志(不需要处理,但不能静默) |
| 12 | 未知 `post_type` 不能崩,要计数 | ❌ 原先静默吞掉 | 计数 + WARN 日志 |
| 13 | 同一消息重复上报(断线重连补发)必须去重 | ❌ 原先会重复入库 | **`(group_id, message_id)` 唯一索引 + `INSERT OR IGNORE`**,重复计数;建索引前自动清理历史重复 |
| 14 | 空消息(无 `raw_message` 也无 `message`)不入库 | ❌ 原先会插空行 | 返回 `empty`,计入 `skipped` |
| 15 | 事件时间可能不可靠(手机时钟回拨) | ❌ 原先直接采信 | **落在未来 >1 天时改用本地时间**(否则时间线错乱、总结范围错) |
| 16 | 消息段类型要归一化后再统计/过滤 | ⚠️ 只映射了 `record` | 扩展 `mface/marketface→face`、`node→forward`、`flash→image`(`core.kinds_of`) |
| 17 | 私聊默认不存(机器人不参与私聊) | ✅ `store_private=false` | 保持;`message_sent` 不受类型过滤 |
| 18 | 群白名单/类型过滤 | ✅ `collector.groups` / `collector.kinds` | 保持;被过滤的计入 `skipped` |
| 19 | 全文索引与消息同步写入 | ✅ 插入时同步 FTS | 保持;群名变更时同步更新索引副本 |
| 20 | 并发写入要能扛住(SQLite 锁) | ✅ WAL + 15s 超时 | **加 `busy_timeout=5000` + 落库忙锁退避重试 3 次** |
| 21 | 落库失败不能丢现场 | ⚠️ 只记日志 | **写入崩溃账本**(`data/crashes.jsonl`),控制台可见 |
| 22 | 端口被占必须明确失败,不能"看起来在跑" | ✅ 3.0.0 起 | 保持(记账 + 非零退出) |
| 23 | 采集指标要能自证 | ⚠️ 只有 4 项 | **新增 `duplicate`/`notice`/`meta`/`sent` 计数**,控制台「运维→备份」可见 |

---

## 3. 采集器应答语义

| 情况 | HTTP | 响应体 | 计数器 |
|---|---|---|---|
| 正常上报 | 200 | `{"status":"ok","retcode":0}` | 按类型 |
| 签名不符 | 403 | `{"status":"forbidden"}` | `rejected` |
| 缺签名但要求签名 | 403 | `{"status":"signature_required"}` | `rejected` |
| JSON 非法 / 不是对象 | 400 | `{"status":"invalid_json"}` | `errors` |
| 请求体超限 | 413 | `{"status":"payload_too_large"}` | `errors` |

> 约定:**只要 200 就代表"我收下了"**。落库失败会记 200 + `errors` 计数 + 账本 ——
> 对 NapCat 而言重发同一条也是安全的(有去重),不会造成脏数据。

---

## 4. 数据模型约束(messages)

| 字段 | 含义 | 约束 |
|---|---|---|
| `ts` | 事件时间(秒) | 未来 >1 天会被纠正为本地时间 |
| `message_id` | OneBot 消息号 | 与 `group_id` 组成唯一键(>0 时生效) |
| `self` | 是否机器人自己发 | `message_sent` 或 `user_id == self_id` |
| `recalled` | 是否已撤回 | `notice/group_recall` 置 1;查询可 `exclude_recalled` |
| `kinds` | 归一化消息类型串 | 见规范 16 |
| `platform` | 来源平台 | 默认 `aiocqhttp` |

---

## 5. 自查方法

```bash
# 接收计数与版本
curl -s http://127.0.0.1:8080/ | python3 -m json.tool

# 规范全部用例(8 项: 去重/空消息/self/撤回/心跳/畸形/分块/类型归一)
python3 -m unittest tests.smoke.TestIngestSpec -v

# 真机上跑一遍
cd ~/qqops && python tests/smoke.py
```

控制台对应入口:**运维 → 备份(采集指标)**、**运维 → 实时日志(模块 collector)**、
**数据 → 事件/媒体**、**消息页**里撤回消息会带「已撤回」标记。
