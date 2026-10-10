#!/usr/bin/env python3
"""把冒烟测试日志里的失败整理成 GitHub Actions annotation 与 job summary。

为什么单独做成脚本: 之前把 python heredoc 内嵌在 workflow 的 `run:` 里, YAML 缩进一错
heredoc 结束符就带上了缩进, bash 直接语法错误(exit 2)—— 全平台一起挂, 还看不到原因。
做成独立脚本后既能被 CI 调用, 也能在本地用测试覆盖。

用法:
    python3 tools/ci_annotate.py smoke.log            # 打印 ::error:: 行(GitHub 会收成 annotation)
    python3 tools/ci_annotate.py smoke.log --summary  # 额外把摘要写进 $GITHUB_STEP_SUMMARY
"""
from __future__ import annotations

import os
import re
import sys

FAIL_LINE = re.compile(r"^(FAIL|ERROR): ")
DETAIL = re.compile(r'File ".*", line \d+|^[A-Za-z_]*(Error|Exception)\b|^AssertionError\b')


def esc(s: str) -> str:
    """GitHub workflow 命令的转义规则(%, CR, LF)。"""
    return s.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def parse(log_text: str, limit: int = 8) -> list:
    """返回 [(失败标题, 原因摘要)]。"""
    lines = log_text.lstrip("\ufeff").splitlines()      # 容忍 BOM(有些工具写日志会带)
    out = []
    for i, line in enumerate(lines):
        if not FAIL_LINE.match(line):
            continue
        stop = min(i + 60, len(lines))
        ctx = [x.strip() for x in lines[i + 1:stop] if DETAIL.search(x)]
        detail = " / ".join(ctx[-3:]) or "(无堆栈)"
        # 多行失败信息(例如 "未在超时内监听\n<子进程输出>\n<日志尾巴>")在异常行**之后** ——
        # 这些上下文才是定位关键(CI 上日志本身下载不了)。注意 unittest 的 ---- 分隔线在
        # FAIL 头之后、异常行之前, 不能拿它当结束标志。
        last = max([j for j in range(i + 1, stop) if DETAIL.search(lines[j])] or [i])
        extra = [x.strip() for x in lines[last + 1:last + 8] if x.strip()]
        if extra:
            detail += " || " + " / ".join(extra)[:800]
        out.append((line.strip(), detail))
        if len(out) >= limit:
            break
    return out


def main(argv: list) -> int:
    if len(argv) < 2:
        print("用法: ci_annotate.py <日志文件> [--summary]")
        return 2
    path = argv[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print("读不到日志 %s: %s" % (path, e))
        return 0                      # 不要让"整理日志"这件事把 CI 再搞挂一次
    items = parse(text)
    if not items:
        # 没有 FAIL/ERROR 行, 说明是启动/导入期就挂了: 把最后几行原样发出去(别重复输出)
        tail = " / ".join(x.strip() for x in text.splitlines()[-6:] if x.strip())
        items = [("冒烟测试未产生失败详情", tail[:900] or "(日志为空)")]
    for name, detail in items:
        print("::error title=%s::%s" % (esc(name[:120]), esc(detail[:900])))
    if "--summary" in argv:
        out = os.environ.get("GITHUB_STEP_SUMMARY")
        if out:
            try:
                with open(out, "a", encoding="utf-8") as f:
                    f.write("### 冒烟测试失败\n\n")
                    for name, detail in items:
                        f.write("- **%s**\n  - `%s`\n" % (name, detail))
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
