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

Android / Termux(推荐, 也是本项目的主要目标平台)。**把下面四行整段复制粘贴进去就行** ——
网址是固定的, 不用管版本号:

```bash
pkg update && pkg install -y python curl
curl -fL --retry 3 --retry-delay 2 -o ~/qqops.tar.gz https://github.com/mayuxiao2021/qqops/releases/latest/download/qqops-latest.tar.gz
rm -rf ~/qqops-src && mkdir -p ~/qqops-src && tar xzf ~/qqops.tar.gz -C ~/qqops-src --strip-components=1
bash ~/qqops-src/install.sh
```

逐行在做什么:

| 行 | 作用 |
|---|---|
| `pkg install` | 装 Python(程序只用标准库, 不需要 pip)与 curl |
| `curl -fL --retry 3` | 下载**最新**发布包到 `~/qqops.tar.gz`;网址里没有版本号, 以后升级也是同一行。`-f` 让 404 之类直接报错(而不是存下一个错误页), `--retry` 在断流/SSL 中断时自动重试 —— 国内直连 GitHub 偶尔会断 |
| `rm -rf … tar xzf` | 清掉上次解压的目录, 再解到 `~/qqops-src/`(`--strip-components=1` 去掉包里的版本目录) |
| `bash …/install.sh` | 安装到 `~/qqops`、生成配置与随机令牌、启动服务、自检, 并**打印控制台地址** |

看到最后打印出 `安装完成 🎉` 与控制台地址就算成功。想再看一次地址或体检:

```bash
qqops url        # 打印带令牌的控制台地址
qqops status     # 服务状态 + 近 24 小时消息数
qqops doctor     # 逐项体检(采集口 / NapCat API / 存储 / 模型 / 保活)
```

Linux 一样(通常已自带 `python3`, 只需后三行);Windows 不需要 bash, 见[部署指南](/DEPLOY#_3-windows)。

## 2. 把 NapCat 的消息接过来

编辑 NapCat 的 `onebot11_` 配置文件(在容器内 `…/napcat/config/` 下, 文件名里带你的 QQ 号;
找不到就先 `ls ~/*/opt/QQ/resources/app/app_launcher/napcat/config/`), 加上两段:

```jsonc
"network": {
  // ① 消息上报给 QQOps
  "httpClients": [
    {"enable": true, "name": "qqops", "url": "http://127.0.0.1:8080/",
     "messagePostFormat": "array", "reportSelfMessage": false, "secret": ""}
  ],
  // ② 打开 HTTP API(QQOps 用它发消息、取群列表);token 自己定一串, 例如 change-me-3000
  "httpServers": [
    {"enable": true, "name": "local-api", "host": "127.0.0.1", "port": 3000, "token": "change-me-3000"}
  ]
}
```

重启 NapCat, 再在控制台「设置 → 推送」把 `napcat_api_port`(3000)与 `token` 填成**同一个值**。
然后**在群里发一条消息**, 验证:

```bash
qqops doctor          # 采集口 / NapCat API / 存储 / 模型 / 保活 逐项体检
qqops status          # 看"近 24 小时"消息数是否在涨
```

## 3. 打开控制台

控制台只监听 `127.0.0.1`(手机本机):

- **手机浏览器**:直接用安装结束打印的那条地址(里面已经带令牌), 或随时 `qqops url` 打印一次;
- **电脑浏览器**:先转发端口, 再打开 `qqops url` 打印的地址

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
