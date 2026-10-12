# 配置详解

配置只有一个来源:`data/config.json`(控制台「设置」页就是它的表单, 每项都有中文说明)。
保存前会做**类型转换 + 范围/枚举校验**, 非法值回退默认并给出人话提示;保存前自动快照到
`data/config.bak-<时间>`, 控制台可回滚最近 5 份。

::: tip 密钥
`secret: true` 的字段(令牌/API Key)在接口返回时**掩码**, 前端只显示"已配置/未配置";
表单里留空 = 不修改。密钥只写在 `data/config.json` 里, 不要写进代码或文档。
:::

## 采集 collector

| 键 | 默认 | 说明 |
|---|---|---|
| `host` / `port` | `127.0.0.1` / `8080` | 上报入口。**别改成 0.0.0.0**, 除非外面有反代与认证 |
| `secret` | 空 | 可选 HMAC-SHA1 签名密钥, 与 NapCat 的 `secret` 保持一致 |
| `require_signature` | `false` | 开启后没有正确签名的上报一律拒绝 |
| `groups` | `[]` | 采集过滤:只收这些群;留空=全都收 |
| `kinds` | `[]` | 类型过滤, 如 `text,image`;留空=全都收 |
| `store_private` | `false` | 是否保存私聊消息(内容层面) |
| `command_private` | `true` | 私聊不入库, 但仍交给插件(「在 QQ 里问」的前提) |
| `debug` | `false` | 每条消息记日志(排错用, 平时关掉) |
| `backfill` | `true` | 启动后自动补采一次(补回停机期间 NapCat 上报失败漏掉的消息) |
| `backfill_minutes` | `720` | 自动补采往回补多久(分钟) |
| `backfill_count` / `backfill_max_pages` | `100` / `4` | 每页多少条 / 每个会话最多往回翻几页 |
| `backfill_min_gap` | `45` | 空档小于这个秒数就不扫(重启几秒不值得扫全群) |
| `backfill_new_groups` | `true` | 库里完全没有的群也捞最近一页(刚被拉进去的新群) |
| `backfill_include_self` | `false` | 是否把机器人自己发的消息也补进来(默认不补, 与实时采集口径一致) |

## 总结 summarizer

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` / `api_key` / `model` | DeepSeek / 空 / `deepseek-chat` | OpenAI 兼容接口 |
| `temperature` | `0.3` | 越低越稳 |
| `timeout` | `180` | 单次调用超时(秒) |
| `window_hours` | `24` | 每次总结回看多久 |
| `min_messages` / `max_messages` | `3` / `600` | 太少不总结;太多只取最近的 |
| `max_chars_per_group` | `12000` | 单群记录上限(超出截断较早内容) |
| `prompt_extra` | 空 | 追加到提示词的要求(输出偏好) |
| `input_price_per_mtok` / `output_price_per_mtok` | `0` | 填了才会算钱 |

## 补料 enrich

| 分组 | 键 | 说明 |
|---|---|---|
| `vision` | `enable` / `model` / `max_images` / `max_bytes` / `detail` | 图片多模态识别(按 URL 缓存, 同一张只识别一次) |
| `files` | `enable` / `max_mb` / `max_files` / `max_chars` | 文档解析(docx/xlsx/pptx/txt;PDF 需可选 `pypdf`) |
| `links` | `enable` / `max_links` / `max_bytes` / `timeout` / `max_redirects` / `allow_all` | 链接正文抓取, 内置 SSRF 防护(`allow_all` 会绕过, 慎用) |

## 推送 push

见[总结与报告 → 推送](/guide/reports#推送)。

## 定时 schedule

| 键 | 说明 |
|---|---|
| `enabled` | 总开关 |
| `mode` | `daily` 每天定点 / `interval` 每隔 N 分钟 / `weekly` 每周几 |
| `times` | `daily` 的时间点, 可写多个, 如 `["09:00","22:00"]` |
| `every_minutes` | `interval` 的间隔(360 = 6 小时) |
| `weekdays` / `weekly_time` | `weekly` 用;1=周一 … 7=周日 |
| `skip_if_no_new` / `min_new_messages` | 自上次以来新消息太少就跳过(避免空报告与浪费) |

::: tip 改定时的入口
「任务」页顶部就是定时设置(模式/时间/星期/跳过规则), 比在设置页改更直观;
设置页只保留总开关, 重复项已隐藏。
:::

## 保留 retention

| 键 | 默认 | 说明 |
|---|---|---|
| `message_days` | `0` | `0` = 永久保留;>0 时每天清理超期消息 |
| `log_max_mb` | `4` | 单个日志文件上限, 超出轮转(保留 3 份) |
| `backup_keep` | `7` | 备份保留份数 |

## 看门狗 watchdog

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否体检与自愈 |
| `interval` | `60` | 体检间隔(秒) |
| `manage_extras` | `false` | 是否顺手守护旁路服务(NapCat 掉了会尝试拉起 —— 它会重新登录, 较慢) |

## 运行形态与功耗 runtime

| 键 | 默认 | 说明 |
|---|---|---|
| `mode` | `auto` | `auto`(Android 单进程/其它多进程) / `single` / `multi` |
| `power_mode` | `balanced` | `performance` / `balanced` / `saver` |
| `scheduler_tick` 等 | `0`/`null` | **留空即跟随档位**;填了就覆盖该档位的值 |
| `enrich_window` | `02:00-06:00` | 省电档的富化窗口(支持跨零点, 如 `22:00-06:00`) |

细节与实测数字见[功耗与资源](/POWER)。

## LLM(问答用) llm

| 键 | 说明 |
|---|---|
| `base_url` / `api_key` / `model` | 留空则**回落到 `summarizer`**, 只维护一份即可 |
| `temperature` / `max_tokens` / `timeout` / `retries` | 问答参数 |
| `input_price_per_mtok` / `output_price_per_mtok` | 成本统计用 |

## 抽取 extract

| 键 | 说明 |
|---|---|
| `structured` | 关掉则退回散文式总结(不产生事件列表) |
| `max_events` / `max_todos` / `max_links` | 每次最多留几条 |
| `memory_chars` | 每群记忆长度上限 |
| `low_confidence_drop` | 低于该置信度的事件不入库 |

## 插件配置

每个插件有独立配置 `data/config/<插件id>_config.json`, 在「插件」页用同样的表单 UI 编辑
(字段说明来自插件的 `_conf_schema.json`)。内置插件:

| 插件 | 关键配置 |
|---|---|
| 智能查询 `assistant` | 查询设置(时间范围/来源数/轮次/额度)、提示词、展示、**在 QQ 里直接问** |
| 截止提醒 `reminders` | 识别开关/关键词/群范围、提前提醒、推送 |

## 配置文件长什么样

完整默认值见仓库根目录的 [`config.example.json`](https://github.com/mayuxiao2021/qqops/blob/master/config.example.json)(密钥留空)。
实际生效的是 `data/config.json`, 首次启动由安装器生成(含随机令牌)。
