# 安全与隐私

这份文档讲**你怎么把 QQOps 用安全**, 以及**这个仓库怎么保证不带隐私**。
漏洞报告渠道见 [SECURITY.md](https://github.com/mayuxiao2021/qqops/blob/master/SECURITY.md)(走私密报告, 别开公开 issue)。

## 默认就安全的几件事

| 面 | 默认 | 说明 |
|---|---|---|
| 控制台 / 采集口监听 | `127.0.0.1` | 只有本机能访问;电脑访问用 `adb forward tcp:8091 tcp:8091` |
| 控制台鉴权 | 随机令牌 + HttpOnly 会话 Cookie | 令牌在 `data/config.json`, 接口回传一律掩码 |
| 推送 | 只发私聊 | `push.group_id = 0`;想"不在群里说话"就保持 0 |
| 群内问答 | 关闭 | 打开后也只对白名单群 + 白名单 QQ + 带前缀的消息生效 |
| 私聊问答 | 只对主人 QQ | 既没配白名单也没配主人 → 一律不响应 |
| 链接抓取 | SSRF 防护 | 默认拒绝内网地址 |

## 你要自己注意的

1. **令牌当密码看待**:别贴进聊天记录、别提交进任何仓库。泄露了就改
   `data/config.json` 的 `web.token` 然后 `qqops restart`(旧令牌立即失效)。
2. **不要把 8091/8080 暴露到公网**。远程访问请走 SSH 隧道、`adb forward`, 或自己加一层
   带认证的反向代理。
3. **模型会看到记录的片段**:总结与问答会把**检索命中的消息文本**发给你配置的模型服务商。
   不要对含敏感信息的群开总结/问答;图片识别与链接抓取同理, 都是你开了才跑。
4. **群内问答是公开的**:回答会出现在群里, 打开前先确认群规与隐私影响。
5. **备份也是敏感数据**:`data/backups/` 里含令牌与聊天记录。

## 数据留在哪

| 数据 | 位置 | 会不会外发 |
|---|---|---|
| 消息 | `data/messages.db` | 只有总结/问答时, 命中的片段发给模型 |
| 报告 | `data/reports/` | 只有你点"推送"或开了自动推送时发到你的 QQ |
| 会话与轮次 | `data/messages.db`(`chat_*` 表) | 不外发 |
| 长期记忆 / 档案 | `data/messages.db` + `data/profile.md` | 作为提示词的一部分发给模型 |
| 日志 / 账本 | `data/logs/`, `data/crashes.jsonl` | 不外发 |

## 仓库的隐私红线(硬性, 有工具)

公开仓库里不得出现任何只属于某个部署者的信息, 靠三层机制保证:

```bash
python3 tools/check_secrets.py            # 扫工作区(CI 必跑)
python3 tools/check_secrets.py --history  # 扫全历史
python3 tools/check_secrets.py --staged   # 提交前(pre-commit 钩子自动跑)
bash tools/install-hooks.sh               # 装上钩子
```

- **通用规则**:私钥块、`sk-`/`ghp_`/`AKIA`/`xox*`/JWT/Telegram Bot 令牌、硬编码的
  `password|token|api_key`、个人绝对路径(`C:\Users\<你的名字>`)、非 noreply 邮箱、手机号;
- **本机禁词表** `tools/secret_denylist.txt`(**已 gitignore, 永不入仓**):写你自己的 QQ 号、
  群号、令牌、设备序列号、昵称;任何文件出现这些词都会被拦下;
- **打包门禁**:`pack.py` 打包后会**再查一遍包内容**(文件名 + 禁词), 命中就删包报错 ——
  文件是按目录整批加的, 只靠清单迟早漏(真机测试抓到过一次:禁词表随 `tools/` 被打进包);
- `data/`、`dist/`、本机脚本一律不入仓。

## 平台差异

| 平台 | 进程归属与端口回收 | 备注 |
|---|---|---|
| Android / Termux | `pgrep` + `/proc/<pid>/environ` 比对 `QQOPS_DATA`, 外加 `data/run/*.pid` | 手机上读不到 `/proc/net/tcp`, 所以不依赖端口反查 |
| Linux | `/proc/net/tcp` 反查 inode → fd, 精确到 pid | 最强的定位能力 |
| macOS | `lsof -iTCP:<port> -sTCP:LISTEN` | 尽力支持 |
| Windows | 不反查端口;靠 pid 文件 + 命令行 | 探活用 ctypes `OpenProcess`(不能用 `os.kill(pid,0)`, 那是 TerminateProcess) |

::: tip 一条安全语义
**读不到对方环境变量时, 绝不认领那个进程。** 认错人就会去杀别人的进程;
所以归属判定以"本机 pid 文件记录"为主凭据, 环境变量比对为辅。
:::

## 报告安全问题

见 [SECURITY.md](https://github.com/mayuxiao2021/qqops/blob/master/SECURITY.md):
GitHub「Security → Report a vulnerability」私密提交, 附影响版本、复现步骤、影响面。
