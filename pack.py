#!/usr/bin/env python3
"""把 QQOps 打包成可直接部署的 tar.gz(pack.sh 的跨平台版)。

为什么要用 Python 而不是 tar 命令:
  * Windows 没有"执行位"这个概念, 在 Windows 上打包出来的 tar.gz 里
    `bin/qqops` 会丢掉 0755, 用户解包后执行会 Permission denied;
  * 这里显式写入每个文件的权限位(见 `mode_for`), 因此在任何系统上打包结果一致。

用法:
    python3 pack.py            # → dist/qqops-<版本>.tar.gz
"""
from __future__ import annotations

import os
import sys
import tarfile


def _utf8_console() -> None:
    """非 UTF-8 控制台(西文 Windows)下打印中文不能崩 —— 见 qqops/core.py 的同名函数。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


ROOT = os.path.dirname(os.path.abspath(__file__))

# 与 pack.sh 保持一致的清单(不含 data/ .testdata/ dist/ .git/ .github/)
ITEMS = ["qqops", "bin", "tests", "tools", "docs", "VERSION", "README.md", "README_EN.md",
         "CHANGELOG.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
         "install.sh", "uninstall.sh", "pack.sh", "pack.py", "config.example.json"]
SKIP_DIRS = {"__pycache__", ".testdata", "data", "run", ".git", "dist"}


def mode_for(rel: str) -> int:
    """该给包内文件什么权限位: 可执行脚本 0755, 其余 0644。"""
    rel = rel.replace(os.sep, "/")
    name = os.path.basename(rel)
    if rel.startswith("bin/") or rel.startswith("tools/") or name.endswith(".sh"):
        return 0o755
    return 0o644


def _filter(rel: str):
    def f(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.mode = mode_for(rel)
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti
    return f


def _dir_filter():
    def f(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.mode = 0o755
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = "root"
        return ti
    return f


def build(out_dir: str = None, verbose: bool = True) -> str:
    # 打包前的隐私门禁: 包是要发出去的, 里面绝不能有本机隐私/密钥
    scanner = os.path.join(ROOT, "tools", "check_secrets.py")
    if os.path.exists(scanner):
        import subprocess
        p = subprocess.run([sys.executable, scanner, "--quiet"], cwd=ROOT)
        if p.returncode != 0:
            raise SystemExit("✘ 隐私扫描未通过, 已中止打包: python3 tools/check_secrets.py")
    ver = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
    name = "qqops-" + ver
    out_dir = out_dir or os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name + ".tar.gz")
    count, missing = 0, []
    with tarfile.open(out, "w:gz") as tf:
        for item in ITEMS:
            p = os.path.join(ROOT, item)
            if not os.path.exists(p):
                missing.append(item)
                continue
            if os.path.isfile(p):
                tf.add(p, arcname="%s/%s" % (name, item), filter=_filter(item))
                count += 1
                continue
            for cur, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for d in dirs:                      # 显式写入目录项, 解包行为更一致
                    dfull = os.path.join(cur, d)
                    drel = os.path.relpath(dfull, ROOT).replace(os.sep, "/")
                    tf.add(dfull, arcname="%s/%s" % (name, drel), recursive=False,
                           filter=_dir_filter())
                for f in sorted(files):
                    if f.endswith(".pyc"):
                        continue
                    full = os.path.join(cur, f)
                    rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                    tf.add(full, arcname="%s/%s" % (name, rel), filter=_filter(rel))
                    count += 1
    if verbose:
        print("已生成: %s" % out)
        print("  版本 %s, %d 个文件, %.1f KB" % (ver, count, os.path.getsize(out) / 1024.0))
        if missing:
            print("  跳过(不存在): %s" % ", ".join(missing))
        print("  部署: 把 tar.gz 传到手机 → tar xzf → bash %s/install.sh" % name)
    return out


if __name__ == "__main__":
    _utf8_console()
    sys.exit(0 if build() else 1)
