#!/data/data/com.termux/files/usr/bin/bash
# 打包成可快速部署的 tar.gz(不含数据目录)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VER="$(cat "$SRC/VERSION")"
OUT="$SRC/dist"
NAME="qqops-$VER"
mkdir -p "$OUT"
TMP="$(mktemp -d)"
mkdir -p "$TMP/$NAME"
for item in qqops bin tests VERSION README.md CHANGELOG.md install.sh uninstall.sh pack.sh config.example.json; do
  [ -e "$SRC/$item" ] && cp -rf "$SRC/$item" "$TMP/$NAME/"
done
find "$TMP/$NAME" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$TMP/$NAME" -name '*.pyc' -delete 2>/dev/null || true
tar czf "$OUT/$NAME.tar.gz" -C "$TMP" "$NAME"
rm -rf "$TMP"
SIZE=$(du -h "$OUT/$NAME.tar.gz" | cut -f1)
echo "已生成: $OUT/$NAME.tar.gz ($SIZE)"
echo "部署:  scp $OUT/$NAME.tar.gz <设备>:/sdcard/ && tar xzf ... && bash $NAME/install.sh"
