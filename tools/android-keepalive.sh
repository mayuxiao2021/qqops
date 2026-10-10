#!/usr/bin/env bash
# ============================================================
#  安卓保活一键设置 —— 在【电脑】上运行(需要 adb 与 USB 调试)
#
#  作用: 关掉 Android 12+ 的「幽灵进程查杀」、把 Termux 加入电池优化白名单、
#        放开后台运行权限, 让 QQOps 的采集器/调度器/控制台不再被系统杀掉。
#
#  用法:  bash android-keepalive.sh          (默认 com.termux)
#         bash android-keepalive.sh com.termux
# ============================================================
set -uo pipefail
PKG="${1:-com.termux}"
ADB="$(command -v adb || echo adb)"

echo "🐧 安卓保活设置  (目标应用: $PKG)"
echo

if ! "$ADB" devices | grep -q "device$"; then
  echo "  ✘ 没有检测到设备。请先: 插好 USB → 手机弹出「允许 USB 调试」→ 点允许"
  echo "    然后重试; 也可以手动执行下面这些命令。"
fi

run() {  # 描述 命令...
  local desc="$1"; shift
  if "$ADB" "$@" >/dev/null 2>&1; then echo "  ✔ $desc"; else echo "  ! $desc   (失败, 可能需要更高权限或系统版本不支持)"; fi
}

echo "① 关闭幽灵进程查杀(Android 12+ 后台进程数超限会杀 Termux 子进程)"
run "settings put global settings_enable_monitor_phantom_procs false" \
    shell settings put global settings_enable_monitor_phantom_procs false
# Android 14 上还有一道上限, 能设置就设置(需要 root 或 adb 权限)
run "device_config put activity_manager max_phantom_processes 2147483647" \
    shell device_config put activity_manager max_phantom_processes 2147483647

echo
echo "② 电池优化白名单(防止 Doze 冻结后台)"
run "dumpsys deviceidle whitelist +$PKG" shell dumpsys deviceidle whitelist "+$PKG"

echo
echo "③ 后台运行权限"
run "appops set $PKG RUN_ANY_IN_BACKGROUND allow" \
    shell cmd appops set "$PKG" RUN_ANY_IN_BACKGROUND allow
run "appops set $PKG RUN_IN_BACKGROUND allow" \
    shell cmd appops set "$PKG" RUN_IN_BACKGROUND allow

echo
echo "④ 待机分桶设为 active(减少被冻结)"
run "am set-standby-bucket $PKG active" shell am set-standby-bucket "$PKG" active

echo
echo "⑤ 核对结果"
echo -n "  幽灵进程: "; "$ADB" shell settings get global settings_enable_monitor_phantom_procs 2>/dev/null | tr -d '\r'
echo -n "  白名单  : "; "$ADB" shell dumpsys deviceidle whitelist 2>/dev/null | grep -c "$PKG" | tr -d '\r'
echo -n "  待机桶  : "; "$ADB" shell am get-standby-bucket "$PKG" 2>/dev/null | tr -d '\r'
echo
echo "完成。建议再手动做两件事:"
echo "  · 手机设置 → 应用 → Termux → 电池 → 选「不受限制」"
echo "  · 安装 Termux:Boot 应用(本程序的 install.sh 已写入 ~/.termux/boot/qqops.sh, 关机重启后会自动拉起)"
