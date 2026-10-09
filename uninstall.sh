#!/data/data/com.termux/files/usr/bin/bash
# QQOps 卸载器 —— 默认保留数据, --purge 连同数据一起删除
set -uo pipefail
DEST="${QQOPS_HOME:-$HOME/qqops}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

echo "🐧 QQOps 卸载"
if [ -x "$DEST/bin/qqops" ]; then
  "$DEST/bin/qqops" stop || true
fi
for name in qqops-collector qqops-scheduler qqops-web; do
  screen -S "$name" -X quit >/dev/null 2>&1 && echo "  已停止 $name"
done
for BIN in "$PREFIX/bin" "$HOME/.local/bin" "/usr/local/bin"; do
  [ -L "$BIN/qqops" ] && rm -f "$BIN/qqops" && echo "  已移除 $BIN/qqops"
done

if [ "$PURGE" = "1" ]; then
  if [ -d "$DEST/data" ]; then
    BK="$HOME/qqops-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar czf "$BK" -C "$DEST" data 2>/dev/null && echo "  数据已备份到 $BK"
  fi
  rm -rf "$DEST" && echo "  已删除 $DEST"
else
  echo "  程序目录保留: $DEST (数据未动)"
  echo "  彻底删除请执行: bash $DEST/uninstall.sh --purge"
fi
echo "完成。"
