# 部署指南

覆盖三种环境:**Android/Termux**(主要目标,最麻烦)、**Linux**、**Windows**。
不管哪种,结构都是:

```
QQ 客户端 ── NapCat(OneBot v11 实现) ──HTTP 上报──▶ QQOps 采集口 :8080
                     ▲                                    │
                     └── HTTP API :3000 ◀── 发送/查询 ────┘
                                    控制台 :8091(默认只监听 127.0.0.1)
```

---

## 0. 前置条件

| 项 | 要求 |
|---|---|
| Python | 3.10+(QQOps 只用标准库,不需要 pip install 任何东西) |
| NapCat | 已安装且能登录 QQ(见 [NapCat 文档](https://napneko.github.io/)) |
| 空间 | 程序 < 1 MB;数据看消息量(1 万条消息约 3~8 MB) |
| 网络 | 全部走本机回环,不需要公网 |

---

## 1. Android / Termux(推荐路径)

Android 上不能跑 Docker,标准做法是 **Termux + proot-distro 容器**:

- **Termux 里**跑 QQOps(Python 原生,省电省内存);
- **proot 容器里**跑 NapCat + QQ(Linux 版 QQ)。

### 1.1 装 Termux 侧

```bash
pkg update && pkg install -y python
# 下载发布包(或 git clone 本仓库)
tar xzf qqops-<版本>.tar.gz -C ~ && bash ~/qqops-<版本>/install.sh
```

安装器会:检查 `python3` → 复制程序到 `~/qqops` → 生成配置与**随机令牌** →
迁移旧数据(可选)→ 注册 `qqops` 命令 → 启动服务 → 跑一次自检,最后打印控制台地址。

常用参数:

| 参数 | 说明 |
|---|---|
| `--no-start` | 只安装不启动 |
| `--import-db <旧库>` | 迁移旧的 `messages.db` |
| `--import-config <旧配置>` | 迁移旧配置(模型/群/推送) |

### 1.2 装 NapCat(proot 容器)

```bash
pkg install -y proot-distro
proot-distro install debian
proot-distro login debian
# 容器内(参考 NapCat 官方文档):
apt update && apt install -y curl sudo
curl -o napcat.sh https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh
bash napcat.sh
```

登录:扫码(WebUI 或二维码文件),确认能收发消息。

### 1.3 接线(只做一次)

编辑容器内 `…/Napcat/opt/QQ/resources/app/app_launcher/napcat/config/onebot11_<你的QQ>.json`:

```jsonc
"network": {
  "httpClients": [
    {"enable": true, "name": "qqops", "url": "http://127.0.0.1:8080/",
     "messagePostFormat": "array", "reportSelfMessage": false, "secret": ""}
  ],
  "httpServers": [
    {"enable": true, "name": "local-api", "host": "127.0.0.1", "port": 3000, "token": "<自定义令牌>"}
  ]
}
```

重启 NapCat,然后在控制台「设置 → 推送」把 `napcat_api_port`(3000)与 `token` 填成一致。
验证:

```bash
qqops doctor        # 采集口、NapCat API、存储、模型、保活逐项体检
```

### 1.4 保活(Android 特有,必做)

"进程老是被杀"的三个主因:幽灵进程查杀、Doze/电池优化、重启后没有自启。QQOps 有三层防护:

1. **看门狗**(随服务启动):每 60 秒体检采集器/调度器/控制台,进程不在就拉起,
   进程在但**端口不响应/心跳停了**(假活)也会先清掉再拉起。
2. **开机自启**:安装器会写 `~/.termux/boot/qqops.sh`,装上 **Termux:Boot** 后重启即自动启动。
3. **设备级设置**(需要电脑 + adb,一次性,最有效):

   ```bash
   bash tools/android-keepalive.sh     # 一键执行下面这些
   # 或手动:
   adb shell settings put global settings_enable_monitor_phantom_procs false
   adb shell dumpsys deviceidle whitelist +com.termux
   adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow
   adb shell am set-standby-bucket com.termux active
   ```

   手机端再手动一次:设置 → 应用 → Termux → 电池 → **不受限制**。

`qqops doctor` 会逐项检查上面这些,缺哪项就打印该执行的命令。

---

## 2. Linux

```bash
tar xzf qqops-<版本>.tar.gz -C ~ && bash ~/qqops-<版本>/install.sh
```

- 服务用 `nohup` + `data/run/*.pid` 管理,不依赖 screen/tmux,SSH 断线不受影响。
- 想让开机自启,写一个 systemd 单元(可选):

  ```ini
  # /etc/systemd/system/qqops.service
  [Unit]
  Description=QQOps
  After=network.target

  [Service]
  Type=forking
  User=<你的用户>
  WorkingDirectory=/home/<你的用户>/qqops
  ExecStart=/home/<你的用户>/qqops/bin/qqops start
  ExecStop=/home/<你的用户>/qqops/bin/qqops stop
  Restart=on-failure

  [Install]
  WantedBy=multi-user.target
  ```

- 运行形态默认按平台自动选:Android → 单进程;其它 → 多进程。可显式指定:
  `qqops mode single|multi|auto`。

---

## 3. Windows

```powershell
# 解压后用 PowerShell 执行
tar xzf qqops-<版本>.tar.gz -C $HOME
bash $HOME/qqops-<版本>/install.sh     # 需要 Git Bash 或 WSL 提供 bash
```

或者不装 Git Bash,直接手动跑:

```powershell
cd $HOME\qqops
python -m qqops.svc start      # 启动
python -m qqops.svc status     # 状态
python -m qqops.svc stop       # 停止
```

Windows 上注意:服务以子进程方式管理,`qqops` 的 shell 包装脚本需要 bash;
用 `python -m qqops.svc` 系列命令可以完全绕开它。

---

## 4. 日常操作

```bash
qqops status                     # 进程/端口/消息量/关注群/定时/全文索引
qqops summary --hours 24         # 立即总结一次(会推送)
qqops summary --dry-run          # 只组装提示词, 不花钱
qqops logs web                   # 跟踪某份日志
qqops backup                     # 备份数据库 + 配置
qqops doctor                     # 全链路自检
qqops mode single|multi|auto     # 运行形态(合并进程 / 隔离进程 / 跟随平台)
qqops power performance|balanced|saver   # 功耗档位
qqops extras                     # 旁路服务(NapCat 等)状态
qqops napcat restart             # 重启 NapCat
```

访问控制台(默认只监听本机):

- 手机浏览器直接开 `http://127.0.0.1:8091/?token=<令牌>`;
- 电脑访问:`adb forward tcp:8091 tcp:8091` 后开 `http://127.0.0.1:8091/?token=<令牌>`
  (令牌在 `data/config.json` 的 `web.token`,或安装结束打印的地址里)。

---

## 5. 升级与卸载

**升级**(保留数据):

```bash
qqops stop
# 用新版本覆盖程序目录(qqops/ bin/ tools/ docs/ …), 千万不要动 data/
qqops start
```

**卸载**:

```bash
bash ~/qqops/uninstall.sh          # 停止服务 + 移除命令软链; 数据保留
bash ~/qqops/uninstall.sh --purge  # 连 data/ 一起删(不可恢复, 会二次确认)
```

---

## 6. 排错

| 现象 | 排查 |
|---|---|
| 控制台打不开 | `qqops status`;`qqops logs web`;确认端口未被占用(`qqops start` 会自动回收自己的旧进程) |
| 消息不增长 | `qqops doctor` 看采集口;确认 NapCat `httpClients` 指向 `127.0.0.1:8080` 且已重启 NapCat |
| 启动报"端口被占用" | 是**自己的**旧进程会自动清掉;别人的进程会拒绝并告诉你是谁占用 |
| 总结失败 | 控制台「设置」填模型 `api_key`;`qqops logs scheduler` 看具体错误 |
| 推送失败 | 「设置 → 推送」里的 NapCat API 端口/令牌要与 NapCat 的 `httpServers` 一致 |
| 群里收到机器人消息 | 把 `push.group_id` 归零(默认只发私聊);检查是否有其它插件在发 |
| 服务老是被杀 | 见 1.4 保活三件套;`qqops doctor` 会指出缺哪项 |
| 时间不对 | 容器/系统时区;程序内部对时间回拨有保护,但容器时区错了报告时间也会错 |
| 卡片消息不显示 | 见 [CHAT.md](CHAT.md):新版 QQ 的自定义 JSON 卡片需要签名,markdown/小程序卡片依赖 NapCat 的 PacketBackend 与 QQ 版本 |

日志位置:`data/logs/{collector,scheduler,web,watchdog,plugins}.log`;异常账本:`data/crashes.jsonl`。
