# 快速开始

目标是 **10 分钟内**在手机上跑起来:消息能进来、能总结、能在控制台看到。

::: tip 前置
- 一部 Android 手机(或 Linux 机器 / Windows 电脑);
- 一个能登录的 QQ 小号(用来当机器人);
- 一个模型 API Key(可选, 不填也能采集与检索, 只是不能总结和问答)。
:::

## 0. 先理解三个进程

```
QQ ←→ NapCat(负责连 QQ)  ──HTTP 上报──▶  QQOps 采集口 :8080
                                          │  落库 SQLite + 处理
                          控制台 :8091  ◀──┘  只看 127.0.0.1
                          NapCat API :3000 ◀── 发送/查询出口
```

QQOps 自己**不碰 QQ 协议**, 只做"收消息 → 存下来 → 总结/查询 → 发回去"。所以必须先把
[NapCat](https://github.com/NapNeko/NapCatQQ) 跑起来并登录。

## 1. 装 QQOps

Android / Termux(推荐, 也是本项目的主要目标平台):

```bash
pkg update && pkg install -y python
# 把发布包传到手机后:
tar xzf qqops-<版本>.tar.gz -C ~ && bash ~/qqops-<版本>/install.sh
```

安装器会:检查 `python3` → 复制到 `~/qqops` → 生成配置与**随机令牌** → 注册 `qqops` 命令 →
写开机自启脚本 → 启动服务 → 跑一次自检, 最后打印控制台地址。

Linux / Windows 见[部署指南](/DEPLOY)。

## 2. 把 NapCat 的消息接过来

编辑 NapCat 的 `onebot11_<你的QQ>.json`, 加上两段(完整示例见[部署指南](/DEPLOY#_1-3-接线-只做一次)):

```jsonc
"network": {
  // ① 消息上报给 QQOps
  "httpClients": [
    {"enable": true, "name": "qqops", "url": "http://127.0.0.1:8080/",
     "messagePostFormat": "array", "reportSelfMessage": false, "secret": ""}
  ],
  // ② 打开 HTTP API(QQOps 用它发消息、取群列表)
  "httpServers": [
    {"enable": true, "name": "local-api", "host": "127.0.0.1", "port": 3000, "token": "<自定义令牌>"}
  ]
}
```

重启 NapCat, 然后**在群里发一条消息**, 验证:

```bash
qqops doctor          # 采集口 / NapCat API / 存储 / 模型 / 保活 逐项体检
qqops status          # 看"近 24 小时"消息数是否在涨
```

## 3. 打开控制台

控制台只监听 `127.0.0.1`(手机本机):

- **手机浏览器**:直接用安装结束打印的地址 `http://127.0.0.1:8091/?token=<令牌>`;
- **电脑浏览器**:先转发端口, 再打开同一地址

  ```bash
  adb forward tcp:8091 tcp:8091
  ```

令牌在 `data/config.json` 的 `web.token`(控制台「设置」页也能改)。

初次进来建议按这个顺序点一遍:

1. **群组** 页 → 勾选要参与总结的群(默认不采集群, 只存你勾的);
2. **设置** 页 → 填模型 `base_url` / `api_key` / `model`;
3. **任务** 页 → 设定定时总结的时间, 点「试运行(不花钱)」确认提示词正常;
4. **运维** 页 → 跑「一键自检」, 顺便看看服务健康与日志。

## 4. 跑一次总结

```bash
qqops summary --dry-run     # 只组装提示词, 不调用模型(不花钱)
qqops summary --hours 24    # 真跑一次, 会推送(如果开了推送)
```

报告会写到 `data/reports/`(`.md` + 自包含 `.html`), 控制台「总结」页可阅读、可导出 Excel/Word。

## 5. 让它省电、别被杀

```bash
qqops mode single           # 单进程: 四个服务合并(手机默认就是它)
qqops power saver           # 省电档: 巡检拉长, 抓图/抓链接推到夜间
qqops doctor                # 「手机保活」一节会告诉你还缺哪项设置
```

Android 上最关键是**关掉电池优化**并允许后台运行;`tools/android-keepalive.sh` 能一键做完
(需要电脑 + adb), 细节见[保活](/guide/keepalive)。

## 6. 然后呢

| 想要 | 看 |
|---|---|
| 用一句话问记录 | [AI 智能查询](/guide/chat) |
| 在 QQ 私聊里直接问 | [在 QQ 里直接问](/guide/qq-chat) |
| 让报告自动推送到 QQ | [配置详解 → 推送](/guide/config#推送-push) |
| 改采集范围/过滤 | [配置详解 → 采集](/guide/config#采集-collector) |
| 出问题 | [排错](/guide/troubleshooting) |

::: warning 关于隐私
QQOps 会把**检索命中的那部分消息文本**发给你配置的模型服务商(总结/问答时)。
其余数据都留在 `data/` 里。群内问答默认关闭, 私聊问答只对白名单 QQ 生效。
:::
