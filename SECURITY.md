# 安全政策

## 报告漏洞

**请不要开公开 issue。** 用 GitHub 的
[私密漏洞报告](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(仓库 → Security → Report a vulnerability)提交,那里只有维护者能看到。

请附上:影响版本、复现步骤、影响面(能否读取本机数据/越权调用 NapCat/远程执行)、你的处置建议。
我们会在 7 天内回应;修好并发布后,如果你愿意会在 CHANGELOG 里致谢。

## 支持范围

只维护 `main` 分支上的最新版本。旧版本的问题请先升级再复现。

## 这个项目会碰哪些敏感面

| 面 | 现状 | 说明 |
|---|---|---|
| 控制台/采集口监听 | 默认 `127.0.0.1` | 想从电脑访问请用 `adb forward`;不要直接暴露到公网 |
| 鉴权 | 令牌(`data/config.json` 里的 `web.token`)+ HttpOnly 会话 Cookie | 令牌只在本机配置文件里,接口回传一律掩码 |
| 采集口签名 | 可选 `collector.secret`(HMAC-SHA1) | 需要更严的场景可开 `require_signature=true` |
| 模型接口 | 只发你允许的记录 | 报告/问答会把**检索命中的消息文本**发给模型服务商,请自行评估 |
| 链接抓取 | 内置 SSRF 防护 | 默认拒绝内网地址;`allow_all` 会绕过,谨慎开启 |
| 文件解析 | 只读你库里已有的文件 | docx/xlsx/pptx/txt;PDF 需可选依赖 |
| QQ 账号 | 由 NapCat 管理 | 本项目不接触账号密码;风控与账号安全请遵守 NapCat/QQ 的规则 |

## 仓库的隐私红线(硬性)

公开仓库里**不得**出现任何只属于某个部署者的信息。这不是靠自觉,而是靠工具:

```bash
python3 tools/check_secrets.py            # 扫工作区(CI 必跑)
python3 tools/check_secrets.py --history  # 扫全历史
python3 tools/check_secrets.py --staged   # 提交前(pre-commit 钩子自动跑)
```

- **通用规则**:私钥块、`sk-`/`ghp_`/`AKIA`/`xox*`/JWT/Telegram Bot 令牌、硬编码的
  `password|token|api_key`、个人绝对路径(`C:\Users\<你的名字>`)、非 noreply 邮箱、中国大陆手机号。
- **本机禁词表**:`tools/secret_denylist.txt`(**已被 .gitignore 忽略**)里写自己的 QQ 号、群号、
  令牌、设备序列号、昵称等;任何文件出现这些词都会被拦下。格式见
  [`tools/secret_denylist.example.txt`](tools/secret_denylist.example.txt)。
- **豁免**:同一行加注释 `secret-scan: allow`,或在 `tools/.secretscanignore` 里按 glob 排除整个文件
  (两者都要在 PR 里说明理由)。

`data/`(配置、数据库、日志、报告、备份)、`dist/`(发布包)、本机部署脚本(`_probe/`)一律不入仓;
`pack.py` 在打包前也会先跑一遍扫描,防止把隐私打进发布包。

## 使用者的安全建议

1. 控制台令牌当密码看待:别贴进聊天记录、别提交进任何仓库;泄露了就改 `data/config.json` 重启。
2. 不要把控制台或采集口暴露到公网。远程访问请走 SSH 隧道 / `adb forward` / 带认证的反向代理。
3. 群内问答默认关闭 —— 群里回答是公开的,打开前先确认群规与隐私影响。
4. 模型密钥只填在 `data/config.json`(控制台表单里以掩码显示),不要写进代码或文档。
5. 定期 `qqops backup`;备份文件同样含令牌与聊天记录,别随手传到网盘。
