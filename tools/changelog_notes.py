#!/usr/bin/env python3
"""从 CHANGELOG.md 抽出某个版本的段落, 当作 GitHub Release 的发布说明。

为什么需要它: `gh release create --generate-notes` 只会给一句 "Full Changelog: <compare 链接>",
对"能不能用起来"毫无帮助; 而 CHANGELOG 里已经写好了人话(改了什么、踩了什么坑)。
发布说明直接取自同一份 CHANGELOG, 就不会出现"改了 CHANGELOG 但 Release 页是空的"。

约定: 版本标题形如 `## [3.4.1] — 2026-10-11`, 段落范围到下一个 `## [` 之前。

用法:
    python3 tools/changelog_notes.py 3.4.1        # 打印该版本段落(v 前缀可有可无)
    python3 tools/changelog_notes.py v3.4.1

找不到该版本时: 退出码 2, 并且**不打印半截内容** —— 调用方据此回退到 GitHub 自动生成的 notes。
"""
from __future__ import annotations

import re
import sys

# 版本标题: 允许 `## [1.2.3]` / `## 1.2.3` / `## [1.2.3] — 日期`
HEAD = re.compile(r"^##\s+\[?v?(\d+\.\d+\.\d+[^\s\]]*)\]?\s*(.*)$")


def _utf8_console() -> None:
    """非 UTF-8 控制台(西文 Windows)下打印中文不能崩 —— 见 qqops/core.py 的同名函数。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def extract(text: str, version: str) -> str:
    """返回 version 对应的段落(不含标题行); 没有该版本时返回空串。"""
    want = version.strip().lstrip("vV")
    lines = text.lstrip("\ufeff").splitlines()
    start = None
    for i, line in enumerate(lines):
        m = HEAD.match(line)
        if not m:
            continue
        if m.group(1) == want:
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start, len(lines)):
        if HEAD.match(lines[j]) or lines[j].startswith("## "):
            end = j
            break
    body = "\n".join(lines[start:end]).strip("\n")
    return body.strip()


def main(argv: list) -> int:
    if len(argv) < 2:
        print("用法: changelog_notes.py <版本号, 如 3.4.1>")
        return 2
    try:
        with open("CHANGELOG.md", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print("读不到 CHANGELOG.md: %s" % e, file=sys.stderr)
        return 2
    body = extract(text, argv[1])
    if not body:
        print("CHANGELOG.md 里没有 %s 的段落" % argv[1], file=sys.stderr)
        return 2
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    _utf8_console()
    sys.exit(main(sys.argv))
