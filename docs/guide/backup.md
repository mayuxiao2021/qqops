# 备份与升级

## 数据都在一个地方

```
~/qqops/
├── qqops/ bin/ tools/ docs/ tests/   ← 程序(可以整体覆盖升级)
└── data/                             ← 全部运行时数据, 备份只需拷它
    ├── config.json                   配置(含令牌与 API Key)
    ├── messages.db                   消息库(SQLite, WAL)
    ├── reports/                      报告(md + html)
    ├── logs/                         日志
    ├── backups/                      备份产物
    └── run/                          运行时状态(pid/心跳/运行清单)
```

**升级只覆盖程序目录, 永远不要动 `data/`。** 这条规则能避免 99% 的"升级后数据没了"。

## 备份

```bash
qqops backup                    # 数据库 + 配置 → data/backups/
```

- 调度器**每天自动备份**(按 `retention.backup_keep` 滚动, 默认留 7 份);
- 控制台「运维 → 备份」页有「立即备份」按钮, 并显示当前存储与磁盘剩余;
- 备份文件里含令牌与聊天记录, **别随手传网盘**。

## 恢复

```bash
qqops stop
cp data/backups/messages-<时间>.db data/messages.db
qqops start
qqops doctor
```

配置恢复同理:`cp data/backups/config-<时间>.json data/config.json`。

## 迁移到新手机

```bash
# 旧手机
qqops stop
tar czf qqops-data.tar.gz -C ~/qqops data

# 新手机(先装好 QQOps 与 NapCat)
tar xzf qqops-data.tar.gz -C ~/qqops
qqops start
```

注意:新手机上 NapCat 的端口/令牌可能不同, 检查「设置 → 推送」里的 `napcat_api_port` 与 `token`。

## 升级

程序和 `data/` 是分开的, 升级只覆盖程序, **`data/` 一个字节都不动**。整段复制粘贴:

```bash
qqops stop
curl -fL --retry 3 --retry-delay 2 -o ~/qqops.tar.gz https://github.com/mayuxiao2021/qqops/releases/latest/download/qqops-latest.tar.gz
rm -rf ~/qqops-src && mkdir -p ~/qqops-src && tar xzf ~/qqops.tar.gz -C ~/qqops-src --strip-components=1
bash ~/qqops-src/install.sh          # 安装器是幂等的: 覆盖程序文件, 保留 data/
qqops doctor
```

第二条命令的网址永远指向最新版, 不用去查版本号; 想升到某个**指定**版本时, 到
[Releases](https://github.com/mayuxiao2021/qqops/releases) 页面右键复制那个 `.tar.gz` 的链接,
把 `curl -fL --retry 3` 后面的网址换掉即可(`-f` 让 404 之类直接报错而不是存下一个错误页,
`--retry` 会在网络抖动/SSL 中断时自动重试)。

版本变化与迁移说明看 [CHANGELOG](https://github.com/mayuxiao2021/qqops/blob/master/CHANGELOG.md);
配置键的迁移是自动的(旧键会被搬到新位置并记日志)。升级前建议先 `qqops backup`。

## 清理与瘦身

```bash
qqops prune --days 30        # 删 30 天前的消息(先备份!)
qqops reindex                # 重建全文索引(索引文件可能比数据还大)
```

长期保留策略放进配置:设置页「数据保留 → 消息保留天数」>0 时, 调度器每天自动清理。

## 卸载

```bash
bash ~/qqops/uninstall.sh          # 停服务 + 移除命令软链, data/ 保留
bash ~/qqops/uninstall.sh --purge  # 连 data/ 一起删(不可恢复, 会二次确认)
```

## 磁盘水位

控制台会在磁盘剩余 < 500MB 时告警, 并暂停媒体补抓(避免把手机塞满)。
存储用量在「运维 → 备份」页能看到:数据库大小、数据目录大小、磁盘剩余、报告与备份数量。
