#!/usr/bin/env python3
"""检查文档站里"站内链接 + #锚点"是否真的存在。

为什么需要单独做: VitePress 的 `ignoreDeadLinks` 只管**页面在不在**, 不管锚点。
`/DEPLOY#3-windows` 这种锚点写错时构建照样成功, 用户点进去却停在页面顶部 —— 静默失败。
(本项目真踩过: 标题以数字开头时 VitePress 会生成 `_3-windows`, 而 GitHub 上是 `3-windows`。)

做法: 以**构建产物** `docs/.vitepress/dist/*.html` 为准, 不去猜 VitePress 的 slug 规则。
注意 README.md 之类不在站点里的文件不检查(它们由 GitHub 渲染, slug 规则不同)。

用法:
    npm run docs:build && python3 tools/check_docs_anchors.py
"""
from __future__ import annotations

import os
import re
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
DIST = os.path.join(DOCS, ".vitepress", "dist")

LINK = re.compile(r"\]\(([^)\s]+)\)")
SKIP_PREFIX = ("http://", "https://", "mailto:", "#", "//")


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def md_files() -> list:
    out = []
    for cur, dirs, files in os.walk(DOCS):
        dirs[:] = [d for d in dirs if d not in (".vitepress", "node_modules")]
        for f in files:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(cur, f), ROOT).replace(os.sep, "/"))
    return sorted(out)


def html_for(rel_md: str, page: str) -> str:
    """把 markdown 里的链接目标映射到 dist 里的 html 路径(返回相对 DIST 的路径)。"""
    if page.startswith("/"):
        rel = page.strip("/")
    else:
        rel = os.path.normpath(os.path.join(os.path.dirname(rel_md), page)).replace(os.sep, "/")
    if rel.endswith(".md"):
        rel = rel[:-3]
    rel = rel.strip("/")
    if not rel or rel == "index":
        return "index.html"
    return rel + ".html"


def anchors_in(html_rel: str) -> set:
    path = os.path.join(DIST, html_rel)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return set()
    return set(re.findall(r'id="([^"]+)"', text))


def check() -> list:
    problems = []
    cache = {}
    for rel_md in md_files():
        with open(os.path.join(ROOT, rel_md), encoding="utf-8") as f:
            text = f.read()
        for m in LINK.finditer(text):
            target = m.group(1)
            if target.startswith(SKIP_PREFIX) or "#" not in target:
                continue
            page, _, anchor = target.partition("#")
            if not anchor:
                continue
            html_rel = html_for(rel_md, page or "")
            if html_rel not in cache:
                cache[html_rel] = anchors_in(html_rel)
            if not cache[html_rel]:
                problems.append((rel_md, target, "目标页面不在构建产物里: %s" % html_rel))
                continue
            cands = {anchor, urllib.parse.unquote(anchor), urllib.parse.quote(anchor)}
            if not (cands & cache[html_rel]):
                problems.append((rel_md, target, "锚点在 %s 里不存在" % html_rel))
    return problems


def main() -> int:
    _utf8_console()
    if not os.path.isdir(DIST):
        print("没有构建产物: 先跑 npm run docs:build")
        return 2
    problems = check()
    if problems:
        print("✘ 站内锚点有问题(构建不会报, 用户点进去不跳转):")
        for rel, target, why in problems:
            print("  %s → %s\n      %s" % (rel, target, why))
        print("\n提示: 标题以数字开头时 VitePress 生成 `_3-windows`, GitHub 上是 `3-windows`;"
              "\n      在文档站里用前者, 在 README 里用后者。")
        return 1
    print("✔ 站内锚点全部有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
