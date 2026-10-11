# 保活(Android)

"进程老是被杀"是 Android 上最常见的问题, 三个主因:

1. **幽灵进程查杀**(`settings_enable_monitor_phantom_procs`):后台进程数超限时, 系统会杀掉
   Termux 的子进程;
2. **Doze / 电池优化**:息屏后冻结 Termux 的后台活动;
3. **重启后不自启**:重启手机, 服务不会自动回来。

QQOps 有三层防护, 缺哪层补哪层。

## 第一层:看门狗(随服务启动, 默认开)

每 60 秒体检采集器/调度器/控制台:

- 进程不在 → 立刻拉起;
- 进程在但**端口不响应 / 心跳停了**(假活) → 先清掉再拉起;
- 反复崩 → 指数退避(10s→30s→90s), 到阈值**熔断**并写崩溃账本(不再雪崩)。

```bash
qqops watchdog          # 手动跑一轮体检(只报告, 不重启)
qqops restart watchdog  # 重启看门狗本身
```

心跳写在 `data/run/beat_<服务>`(每个服务一个文件, 多进程也不会互相覆盖),
控制台「运维 → 服务」能看到新鲜度。

## 第二层:开机自启

安装器会写 `~/.termux/boot/qqops.sh`(同时获取唤醒锁)。需要装两个 Termux 插件才会生效:

| 插件 | 作用 |
|---|---|
| **Termux:Boot** | 开机执行 `~/.termux/boot/` 下的脚本 |
| **Termux:API**(可选) | 提供 `termux-wake-lock`, 让系统别在息屏时冻结 |

## 第三层:设备级设置(最有效, 需要电脑 + adb)

```bash
bash tools/android-keepalive.sh        # 一键执行下面的设置
# 等价手工命令:
adb shell settings put global settings_enable_monitor_phantom_procs false
adb shell dumpsys deviceidle whitelist +com.termux
adb shell cmd appops set com.termux RUN_ANY_IN_BACKGROUND allow
adb shell am set-standby-bucket com.termux active
```

手机端再手动一次:**设置 → 应用 → Termux → 电池 → 不受限制**。

::: warning 注意
`settings_max_phantom_processes` 这个键在不少机型上**不存在**, 写了也没用;
`settings_enable_monitor_phantom_procs=false` 才是有效的那个。
部分 ROM 还会在"省电模式"下额外限制后台, 建议把 Termux 加入"受保护应用/自启动白名单"。
:::

## 体检

```bash
qqops doctor
```

「手机保活」一节会逐项检查并打印**该执行的命令**:

```
[OK ] 幽灵进程查杀    已关闭
[!! ] 电池优化白名单  com.termux 未加入 → PC 上执行: adb shell dumpsys deviceidle whitelist +com.termux
[?? ] 唤醒锁          未安装 termux-wake-lock(可选: 装 Termux:API)
[OK ] 开机自启        已写 ~/.termux/boot 下的脚本(需安装 Termux:Boot 才会生效)
```

## 省电与保活的平衡

"更省电"和"更容易被杀"是一对矛盾。省电档的做法是**降低唤醒频率**, 而不是关掉保活:

```bash
qqops power saver      # 调度 60s / 主管 180s / 控制台刷新 60s / 富化推夜间
```

消息入库仍然是事件驱动的(秒级), 所以省电档不会让消息丢, 只是巡检变疏。

## 还是被杀?

1. 看 `qqops status` 的"已运行"时长:如果每次只有几分钟, 说明还在被杀;
2. 看 `data/crashes.jsonl` 与 `data/logs/watchdog.log`:是**被杀**还是**自己崩**;
3. 单进程模式更不容易被杀(一个进程而不是四个, 内存占用低得多):

   ```bash
   qqops mode single
   ```

4. 最后手段:让 Termux 保持一个前台通知(`termux-wake-lock`), 系统对前台服务的容忍度高得多。
