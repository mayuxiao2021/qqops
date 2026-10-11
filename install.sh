#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
#  QQOps 安装器(幂等) —— Termux / 任意 Linux
#  用法:
#    bash install.sh                     安装并启动
#    bash install.sh --no-start          只安装不启动
#    bash install.sh --import-db <路径>   迁移旧采集库
#    bash install.sh --import-config <路径> 迁移旧 summarize.json
# ============================================================
set -uo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${QQOPS_HOME:-$HOME/qqops}"
PY="$(command -v python3 || true)"
C_OK=$'\033[32m'; C_BAD=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
ok()   { echo "  ${C_OK}✔${C_OFF} $*"; }
bad()  { echo "  ${C_BAD}✘${C_OFF} $*"; }
warn() { echo "  ${C_WARN}!${C_OFF} $*"; }
step() { echo; echo "${C_DIM}── $* ──${C_OFF}"; }

NO_START=0; IMPORT_DB=""; IMPORT_CFG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --no-start) NO_START=1 ;;
    --import-db) IMPORT_DB="${2:-}"; shift ;;
    --import-config) IMPORT_CFG="${2:-}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) warn "忽略未知参数: $1" ;;
  esac
  shift
done

echo "🐧 QQOps 安装器"
echo "  源目录: $SRC"
echo "  安装到: $DEST"

step "1/7 依赖检查"
[ -n "$PY" ] || { bad "找不到 python3(在 Termux 里执行 pkg install python)"; exit 1; }
PYV="$($PY -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
if $PY -c 'import sys;sys.exit(0 if sys.version_info>=(3,9) else 1)'; then ok "python3 $PYV"; else bad "python3 版本过低($PYV), 需要 >= 3.9"; exit 1; fi
if command -v screen >/dev/null; then ok "screen 已安装(可选, 仅用于手动挂后台)"; else
  warn "未安装 screen(可选; 服务用 nohup + PID 文件管理, 不影响运行)"; fi
if command -v curl >/dev/null; then ok "curl 已安装"; else warn "未安装 curl(仅影响文档里的调试命令)"; fi
command -v termux-wake-lock >/dev/null && { termux-wake-lock 2>/dev/null && ok "已获取 wakelock(防休眠杀进程)"; }

step "2/7 复制程序"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  for item in qqops bin tests tools VERSION README.md CHANGELOG.md LICENSE .gitignore install.sh uninstall.sh pack.sh config.example.json; do
    [ -e "$SRC/$item" ] && cp -rf "$SRC/$item" "$DEST/" 2>/dev/null
  done
  ok "已复制到 $DEST"
else
  ok "已在安装目录内, 跳过复制"
fi
mkdir -p "$DEST/data/logs" "$DEST/data/reports" "$DEST/data/backups"
chmod +x "$DEST/bin/qqops" "$DEST/install.sh" "$DEST/uninstall.sh" 2>/dev/null
ok "数据目录就绪: $DEST/data"

step "3/7 初始化配置"
cd "$DEST"
if $PY -m qqops.tools init; then ok "配置已生成"; else bad "初始化失败"; exit 1; fi
TOKEN="$($PY - <<'PY'
import sys,os; sys.path.insert(0,os.getcwd())
from qqops import core
print(core.Config().data['web']['token'])
PY
)"

if [ -n "$IMPORT_DB$IMPORT_CFG" ]; then
  step "4/7 迁移旧数据"
  $PY -m qqops.tools import ${IMPORT_DB:+--db "$IMPORT_DB"} ${IMPORT_CFG:+--config "$IMPORT_CFG"} || warn "迁移部分失败, 可稍后重试"
else
  step "4/7 迁移旧数据"
  echo "  ${C_DIM}(跳过; 需要时: $PY -m qqops.tools import --db <旧库> --config <旧配置>)${C_OFF}"
fi

step "5/7 注册命令"
# $PREFIX 是 Termux 特有的, Linux 上根本没有 —— `set -u` 下直接引用会当场退出,
# 结果是"复制、配置都做完了, 服务却没启动"。必须用 ${PREFIX:-} 兜住。
BINS="$HOME/.local/bin /usr/local/bin"
[ -n "${PREFIX:-}" ] && BINS="$PREFIX/bin $BINS"
REGISTERED=0
for BIN in $BINS; do
  if [ -d "$BIN" ] && [ -w "$BIN" ]; then
    ln -sf "$DEST/bin/qqops" "$BIN/qqops" 2>/dev/null && { ok "已注册: $BIN/qqops"; REGISTERED=1; break; }
  fi
done
[ "$REGISTERED" = "1" ] || warn "没能注册 qqops 命令(可直接用 $DEST/bin/qqops)"

step "6/7 启动服务"
if [ "$NO_START" = "1" ]; then
  echo "  ${C_DIM}(--no-start, 稍后用 qqops start 启动)${C_OFF}"
else
  "$DEST/bin/qqops" start || warn "部分服务未启动, 请执行 qqops doctor 检查"
fi

step "6.5/7 开机自启与保活"
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null && ok "已获取唤醒锁" \
  || warn "未获取唤醒锁(建议 pkg install termux-api)"
BOOT_DIR="$HOME/.termux/boot"
mkdir -p "$BOOT_DIR" 2>/dev/null && cat > "$BOOT_DIR/qqops.sh" <<BOOTEOF
#!/data/data/com.termux/files/usr/bin/sh
# QQOps 开机自启(需要安装 Termux:Boot 应用才会被系统调用)
termux-wake-lock 2>/dev/null
sleep 8
bash "$DEST/bin/qqops" start >>"$DEST/data/logs/boot.log" 2>&1
BOOTEOF
chmod +x "$BOOT_DIR/qqops.sh" 2>/dev/null \
  && ok "已写开机自启脚本: $BOOT_DIR/qqops.sh(安装 Termux:Boot 后生效)" \
  || warn "写开机自启脚本失败, 可手动运行 qqops start"

step "7/7 完成"
$PY -m qqops.tools doctor 2>/dev/null | sed 's/^/  /' || true
cat <<EOF

${C_OK}安装完成 🎉${C_OFF}

  控制台: ${C_OK}http://127.0.0.1:$( $PY -c "import sys,os;sys.path.insert(0,os.getcwd());from qqops import core;print(core.Config().data['web']['port'])" )/?token=$TOKEN${C_OFF}

  常用命令:
    qqops status        查看状态
    qqops url           打印控制台地址
    qqops logs web      跟踪控制台日志
    qqops summary       立即总结一次
    qqops doctor        自检
    qqops backup        备份数据

  还需在 NapCat 侧做两件事(只做一次):
    1) 打开 OneBot HTTP API —— 编辑 NapCat 的 onebot11_ 配置(在 napcat/config/ 下, 文件名里带你的 QQ 号),
       在 network.httpServers 里加:
       {"enable":true,"name":"local-api","host":"127.0.0.1","port":3000,"token":"change-me-3000"}
       那串 token 是自己定的(这里示例用 change-me-3000), 再到控制台「设置 → 推送」填同一个值
    2) 让消息上报到采集器 —— network.httpClients 指向 http://127.0.0.1:8080/
       重启 NapCat 生效(proot 里: bash /root/_napcat_run.sh)

  控制台地址就是上面那条(带令牌);忘了随时执行: qqops url
EOF
