# 命令行手册

所有命令都通过安装时注册的 `qqops`(它是 `bin/qqops` 的软链, 内部调用 `python -m qqops.*`)。
在 Termux 里请用绝对路径或确保 `~/bin`/`$PREFIX/bin` 在 PATH 里。

```bash
qqops help                       # 查看全部命令
```

## 服务生命周期

| 命令 | 说明 |
|---|---|
| `qqops start [服务…]` | 启动(不带参数=按运行形态启动全部:单进程模式起 runner, 多进程模式起四个服务) |
| `qqops stop [服务…]` | 停止实例(先 SIGTERM, 超时再 SIGKILL;会等端口真正释放) |
| `qqops restart [服务…]` | 重启;配置改动后用它生效(控制台改配置也会自动重启) |
| `qqops status` | 进程/端口/内存/消息量/关注的群/定时/全文索引/运行形态/功耗档 |
| `qqops url` | 打印带令牌的控制台地址 |
| `qqops logs <模块>` | 跟踪某份日志:`collector` / `scheduler` / `web` / `watchdog` / `plugins` |
| `qqops doctor` | 全链路自检:环境 / 存储 / 服务 / 适配器 / 模型 / **手机保活** |
| `qqops watchdog` | 手动跑一轮体检(只报告, 不重启服务) |

启动是**真实健康检查**:等到端口应答 / 心跳刷新才报成功;进程秒退会把日志尾部打出来。

## 运行形态与功耗

```bash
qqops mode                  # 看当前形态与配置(输出类似 "single(配置 single) · 已运行")
qqops mode single           # 单进程: 四服务合并(手机省内存, 实测 104.6MB → 27.5MB)
qqops mode multi            # 多进程: 各自独立, 隔离更好(桌面推荐)
qqops mode auto             # 跟随平台: Android 单进程, 其它多进程

qqops power                 # 看当前档位与生效参数
qqops power performance     # 性能: 调度 5s / 主管 30s / 控制台 10s / 富化立即
qqops power balanced        # 均衡(默认): 15s / 60s / 15s
qqops power saver           # 省电: 60s / 180s / 60s, 富化推到夜间窗口
qqops lowpower              # = qqops power saver
```

切换会**自动重启实例**并等端口释放。详见[功耗与资源](/POWER)。

## 总结与报告

```bash
qqops summary --hours 24        # 立即总结 24 小时(会按配置推送)
qqops summary --dry-run         # 只组装提示词, 不调用模型、不花钱
qqops summary --no-push         # 跑总结但这次不推送
qqops summary --groups 123,456  # 只总结指定群
```

## 数据维护

```bash
qqops backup                    # 备份数据库+配置到 data/backups/(调度器每天也会自动备份)
qqops prune --days 30           # 清理 30 天前的消息(保留策略里配 message_days 会自动清理)
qqops reindex                   # 重建全文索引(检索不准/索引落后时用)
qqops export.csv --hours 168    # 导出最近 7 天消息为 CSV(Excel 可直接打开)
```

## 旁路服务

QQOps 不依赖 NapCat 之外的东西, 但可以帮你管理它们:

```bash
qqops extras                    # 旁路服务状态(NapCat / AstrBot)
qqops napcat start|stop|restart # NapCat(QQ 接入)
qqops astrbot start|stop|restart# AstrBot(可选, 与本项目独立)
```

## 不开 bash 也能用(Windows / 调试)

`bin/qqops` 是 shell 包装, 等价入口在 Python 模块里:

```bash
python -m qqops.svc start|stop|restart|status [模式值]
python -m qqops.tools doctor|backup|prune|init
python -m qqops.summarizer --hours 24
python -m qqops.collector --port 8080     # 只起采集口
python -m qqops.webapp --port 8091        # 只起控制台
python -m qqops.runner --services collector,web,scheduler   # 单进程形态
```

::: tip 排查启动问题
`QQOPS_DEBUG_START=7 python -m qqops.webapp` —— 7 秒还没起来就把所有线程栈打到 stderr,
专门用来定位"启动卡住"(没有异常、没有日志的那种)。
:::

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 失败(看输出;`doctor` 会给出具体哪一项) |
| 2 | 参数错误 / 用法错误 |
