#!/usr/bin/env bash
# 安装 git 钩子(把仓库自带的 .githooks/ 挂上) —— 提交前自动跑隐私/密钥扫描。
#
# 用法: bash tools/install-hooks.sh
set -eu
root="$(git rev-parse --show-toplevel)"
cd "$root"

git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "✔ 已启用仓库钩子: core.hooksPath = .githooks"
echo "  提交前会自动执行: python3 tools/check_secrets.py --staged"
echo "  想临时跳过(不建议): git commit --no-verify"
echo ""
if [ ! -f tools/secret_denylist.txt ]; then
  echo "提示: 还没配置本机禁词表。复制一份并填入你自己的敏感值(QQ号/群号/令牌/设备号):"
  echo "      cp tools/secret_denylist.example.txt tools/secret_denylist.txt"
  echo "      该文件已被 .gitignore 忽略, 不会入仓。"
fi
