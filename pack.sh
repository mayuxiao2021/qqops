#!/data/data/com.termux/files/usr/bin/bash
# 打包成可快速部署的 tar.gz(不含数据目录)
#
# 真正的打包逻辑在 pack.py 里 —— 它会把权限位显式写进 tar,
# 所以在 Windows 上打包也不会丢掉 bin/qqops 的执行位(踩过这个坑)。
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python3}" "$SRC/pack.py"
