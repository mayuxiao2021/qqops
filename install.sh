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
if command -v screen >/dev/null; then ok "screen $(screen --version 2>/dev/null | head -1 | awk '{print $NF}')(可选, 仅用于手动挂后台)"; else
  warn "未安装 screen(可选; 服务用 nohup + PID 文件管理, 不影响运行)"; fi
if command -v curl >/dev/null; then ok "curl 已安装"; else warn "未安装 curl(仅影响文档里的调试命令)"; fi
command -v termux-wake-lock >/dev/null && { termux-wake-lock 2>/dev/null && ok "已获取 wakelock(防休眠杀进程)"; }

step "2/7 复制程序"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  for item in qqops bin static tests VERSION README.md CHANGELOG.md install.sh uninstall.sh pack.sh config.example.json; do
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
for BIN in "$PREFIX/bin" "$HOME/.local/bin" "/usr/local/bin"; do
  [ -d "$BIN" ] && [ -w "$BIN" ] && ln -sf "$DEST/bin/qqops" "$BIN/qqops" 2>/dev/null && { ok "已注册: $BIN/qqops"; break; }
done

step "6/7 启动服务"
if [ "$NO_START" = "1" ]; then
  echo "  ${C_DIM}(--no-start, 稍后用 qqops start 启动)${C_OFF}"
else
  "$DEST/bin/qqops" start || warn "部分服务未启动, 请执行 qqops doctor 检查"
fi

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
    1) 打开 OneBot HTTP API —— 编辑容器内 onebot11_<QQ>.json, 在 network.httpServers 加:
       {"enable":true,"name":"local-api","host":"127.0.0.1","port":3000,"token":"<自定义令牌>"}
       然后把它填到 QQOps 配置页 push.napcat_api_port / push.token
    2) 让消息上报到采集器 —— network.httpClients 指向 http://127.0.0.1:8080/
       重启 NapCat 生效(proot 里: bash /root/_napcat_run.sh)
EOF
