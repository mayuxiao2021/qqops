#!/usr/bin/env python3
"""校验发布包: 包内文件 == 工作区 == 已提交代码, 且关键文件齐全、权限位正确。

发布前的最后一道自检(CI 的 package 任务与本地打包都会跑):
    python3 pack.py && python3 tools/verify_package.py

它会检查:
  1. 包内每个文件的 sha256 与工作区一致(防止"改了代码忘了重新打包");
  2. 工作区没有未提交改动(防止"包里的代码不在 git 里");
  3. 当前 VERSION 对应的 tag 指向 HEAD(防止"发版没打标签");
  4. 关键文件都在包里(漏打包是发布事故的高发区);
  5. 可执行脚本在包内是 0755(Windows 打包最容易丢, 用户解包后 Permission denied)。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile


def _utf8_console() -> None:
    """非 UTF-8 控制台(西文 Windows)下打印中文不能崩 —— 见 qqops/core.py 的同名函数。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEED = [
    "bin/qqops",
    "install.sh",
    "uninstall.sh",
    "VERSION",
    "README.md",
    "LICENSE",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "docs/ARCHITECTURE.md",
    "docs/CONSOLE.md",
    "docs/DEPLOY.md",
    "docs/INGEST.md",
    "docs/POWER.md",
    "docs/CHAT.md",
    "docs/FAQ.md",
    "tests/smoke.py",
    "tools/check_secrets.py",
    "qqops/core.py",
    "qqops/runner.py",
    "qqops/framework/errors.py",
    "qqops/framework/bus.py",
    "qqops/framework/manager.py",
    "qqops/console/pages.py",
    "qqops/plugins/assistant/__init__.py",
    "qqops/plugins/reminders/__init__.py",
    "qqops/static/app.js",
    "qqops/static/app.css",
]
EXEC = ("bin/", "tools/")
FAILED = []


def git(*args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, text=True)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def main() -> int:
    _utf8_console()
    ver = open(os.path.join(ROOT, "VERSION"), encoding="utf-8").read().strip()
    tar_path = os.path.join(ROOT, "dist", "qqops-%s.tar.gz" % ver)
    print("版本       : %s" % ver)
    if not os.path.isfile(tar_path):
        print("包         : ✘ 不存在(先跑 python3 pack.py)")
        return 1
    print("包         : %s (%.1f KB)" % (os.path.basename(tar_path),
                                        os.path.getsize(tar_path) / 1024.0))

    bad, count, modes = [], 0, []
    with tarfile.open(tar_path) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        for ti in members:
            rel = ti.name.split("/", 1)[1]
            disk = os.path.join(ROOT, rel.replace("/", os.sep))
            want_exec = rel.startswith(EXEC) or rel.endswith(".sh")
            if bool(ti.mode & 0o111) != want_exec:
                modes.append("%s(mode %o)" % (rel, ti.mode))
            if not os.path.exists(disk):
                bad.append((rel, "包内有", "工作区缺失"))
                continue
            a = hashlib.sha256(tf.extractfile(ti).read()).hexdigest()
            b = hashlib.sha256(open(disk, "rb").read()).hexdigest()
            count += 1
            if a != b:
                bad.append((rel, a[:10], b[:10]))
        names = [n.split("/", 1)[1] for n in tf.getnames() if "/" in n]

    print("包内文件数 : %d" % count)
    print("内容不一致 : %s" % (bad or "无(包 == 工作区)"))
    if bad:
        FAILED.append("包与工作区不一致")
    dirty = git("status", "--porcelain")
    print("git 未提交 : %s" % (dirty or "无(工作区 == 已提交代码)"))
    if dirty:
        FAILED.append("有未提交改动")
    head = git("rev-parse", "--short", "HEAD")
    tag = git("rev-list", "-n", "1", "v" + ver)
    require_tag = "--require-tag" in sys.argv
    print("HEAD       : %s | v%s 指向同一次提交: %s" % (head, ver, tag.startswith(head) if tag else "(本机无此标签)"))
    if not tag and require_tag:
        FAILED.append("缺少 v%s 标签" % ver)
    elif not tag:
        print("             (没取到标签, 跳过该检查; 发版时用 --require-tag)" )
    print("权限位     : %s" % (modes or "全部正确(可执行 0755 / 其余 0644)"))
    if modes:
        FAILED.append("包内权限位不对")
    print("关键文件   :")
    missing = []
    for need in NEED:
        ok = need in names
        if not ok:
            missing.append(need)
        print("  %s %s" % ("✔" if ok else "✘", need))
    if missing:
        FAILED.append("关键文件缺失: %s" % ", ".join(missing))

    if FAILED:
        print("\n✘ 发布校验未通过: %s" % "; ".join(FAILED))
        return 1
    print("\n✔ 发布校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
