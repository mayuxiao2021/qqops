# 在 QQ 里问记录 + 卡片能不能用(QQOps 3.3.0)

两件事:
1. **在聊天窗口里直接问** —— 不打开控制台, 私聊机器人发一句话就能查记录, 答案回到聊天里;
2. **卡片消息的真相** —— QQ 的 JSON 卡片现在要签名, 哪些能发、哪些发了不显示, 都有实测结论。

---

## 1. 在 QQ 里直接问

```
你(私聊) : 昨天群里谁提到报告?
机器人    : 昨天 12:03 小明说"这次报告要加一页预算表" [M1] ……
            ── 引用你的提问, 并附来源编号
```

### 怎么触发

| 场景 | 触发方式 | 默认 |
|---|---|---|
| **私聊** | 直接发问题(不用前缀) | ✅ 开启, 但**仅限白名单** |
| **群聊** | 必须带前缀:`问 谁要交报告` / `ai …` / `/ai …` / `? …` | ❌ 默认关闭(怕打扰 + 隐私) |

白名单默认 = 「推送」里配置的主人 QQ;`reply.allow_users` 可以另配。
**既没配白名单也没配主人 QQ 时, 一律不响应** —— 免得陌生人来烧额度。

### 为什么这样设计

| 决定 | 原因 |
|---|---|
| 默认只开私聊 | 群里回答是公开的, 记录可能含隐私; 想开要显式选群 |
| 群里强制前缀 | 否则机器人会对每句话插嘴 |
| 默认只有自己能用 | 问答要花模型额度, 不能谁来都答 |
| 每人一个会话 | 在 QQ 里可以接着追问("那昨天呢"), 与控制台「对话」页共用同一套存储 |
| 冷却 + 每日额度 | 连发不刷屏; 额度与控制台**共用**, 到点会明确回一句原因 |
| 机器人自己的消息不回应 | 否则 `message_sent` 会造成自己跟自己聊 |

### 数据流(与多进程/单进程无关)

```
NapCat ──上报──▶ collector(:8080)
                  │ 私聊默认不入库, 但打上 _not_stored 标记后发到总线
                  ▼
            管线阶段 assistant_reply(order 45)
                  │ 权限 → 前缀 → 冷却 → 检索 → 模型作答(带来源编号)
                  ▼
            NapCat API(:3000) ──▶ 你的 QQ
```

- 私聊**不入库**: 采集器只把事件交给插件, 消息内容不落库(要连私聊一起存, 打开
  「采集器 → 也保存私聊消息」);不想让私聊触发问答, 关掉「私聊不入库也交给插件」。
- 问答本身会**落库成对话**(会话/轮次), 这样控制台「对话」页能看到 QQ 里问过什么。
- 阶段级容错: 一条消息处理失败只记账, 不影响采集与其它插件。

### 配置(控制台 → 插件 → 智能查询 → 「在 QQ 里直接问」)

| 项 | 默认 | 说明 |
|---|---|---|
| 启用聊天内问答 | 开 | 关掉后只有控制台能问 |
| 允许提问的 QQ 号 | 空 | 留空 = 只有主人 QQ |
| 群里的触发前缀 | `问, ai, /ai, ?` | 私聊不需要前缀 |
| 允许在群里问 / 允许提问的群号 | 关 / 空 | 两个都要满足才回应 |
| 回答形态 | `text` | `text` 纯文本 / `forward` 合并转发卡片 / `card` 签名卡片+文本 |
| 引用提问再回答 | 开 | 用 `reply` 消息段引用你的问题 |
| 附来源编号 | 开 | 回答末尾附 `[M1] 10-10 12:03 小明` 这样的来源 |
| 同一人最短间隔 | 5 秒 | 0 = 不冷却 |
| 失败时回一句原因 | 开 | 例如额度用完 |

---

## 2. 卡片消息: 现在到底能不能用

**结论: 能发, 但"自己拼的 JSON 卡片"现在的 QQ 客户端不渲染 —— 要签名。**

### 实测(本机: NapCat 4.18.33 + QQ 3.2.32-52194, Android/proot)

| 形态 | 接口 | 实测结果 | 能否渲染 |
|---|---|---|---|
| 裸 JSON 卡片 `com.tencent.miniapp_01` / `com.tencent.structmsg` | `send_private_msg` + `json` 段 | retcode 0, 消息发出 | ❌ **客户端提示"卡片版本过低"** |
| 小程序卡片(**带签名**) | `get_mini_app_ark`(action 名就是这个) | ❌ `packetBackend 不可用 … sendPacket 超时 cmd=OidbSvcTrpcTcp.0x9067_202` | 取决于 PacketBackend |
| 官方分享卡片(联系人/群, 自带签名) | `ArkSharePeer` | ✅ retcode 0, 返回 `com.tencent.contact.lua` 的 ark | ✅ |
| **合并转发**(聊天记录卡片) | `send_private_forward_msg` | ✅ retcode 0 | ✅ 内容任意 |
| 图片 | 需要 Pillow | Termux 的 Python 没有 Pillow | 未采用(纯标准库项目) |

### 为什么会这样

- 新版 QQ 校验卡片数据的**签名**, 未签名的自定义卡片被判为"版本过低"——
  NapCat 上游 issue 里对"私发 JSON 失效"的答复就是一句 **"需要 json 签名"**。
- NapCat 提供官方签名接口 `get_mini_app_ark`(内部走 NTQQ 的 `GetMiniAppAdaptShareInfo`),
  但它依赖 **PacketBackend**, 而 PacketBackend 官方支持矩阵里
  **Linux arm64 只覆盖 QQ 构建号 28498–36580**;本机 QQ 是 **52194**,超范围 → `sendPacket 超时`。
- 所以"要么降 QQ 到支持区间, 要么等上游适配新 QQ 的 packet 协议"——这不是 QQOps 能绕过的。

### QQOps 的处理

1. `reply_mode` / 推送都支持 `forward`(合并转发卡片): **各版本 QQ 都能渲染**, 内容任意,
   长报告/长回答最合适(客户端显示成聊天记录, 点开可看全部)。
2. `ArkSharePeer` 提供"官方分享卡片"(分享联系人或群): 内容由 QQ 模板决定, 但渲染稳定。
3. `get_mini_app_ark` 走**特性探测**: 拿不到签名就自动退回文本, 并把**失败原因原样记日志**
   (升级 NapCat / 换 QQ 版本后, 同一个开关就能用上真正的签名卡片)。
4. 卡片发送**永远带兜底**: 卡片失败 → 纯文本, 消息不会丢。
5. 控制台「对话」页有 **「测试卡片渲染」** 按钮: 一次把合并转发 / 官方分享 / 签名卡片 /
   裸 JSON 四种都发到你的 QQ, 并列出每种的成功失败与原因 —— 用眼睛确认哪种能渲染。

### 相关接口

```
POST /api/assistant/reply/test   {kinds:["forward","share","miniapp","json"], user_id?}
GET  /api/assistant/stats        → reply:{answered,skipped,failed,last_ts,last_error}
```

`NapCat` 客户端能力(`qqops/core.py`):

```python
napcat.send_message(segments, user_id=…, group_id=…)   # 任意消息段
napcat.send_text(text, …, reply_to=<message_id>)       # 分段发, 可引用
napcat.send_forward(nodes, …)                          # 合并转发卡片
napcat.send_card(card, …, fallback_text=…)             # 裸 JSON 卡片(失败退文本)
napcat.miniapp_ark(...) / send_miniapp_card(...)       # 签名小程序卡片(自动退化)
napcat.ark_share_peer(user_id=…) / send_share_card(…)  # 官方分享卡片
core.forward_node(name, uin, text, title=…)            # 构造转发节点
core.qq_card(title, desc, …)                           # 构造裸卡片载荷
core.cq_plain(raw)                                     # CQ 码 → 人话(提问前清洗)
```
