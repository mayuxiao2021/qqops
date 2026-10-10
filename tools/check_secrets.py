#!/usr/bin/env python3
"""隐私与密钥扫描器 —— 保证"构建仓库里不出现任何隐私信息"。

为什么要它: 这个项目天然贴着私域数据(QQ 号、群号、机器人令牌、手机设备号、模型密钥)。
人肉 review 迟早漏, 所以把规则写成可执行的门禁:
  * 提交前:  `python3 tools/check_secrets.py --staged`(见 tools/install-hooks.sh)
  * CI:      `python3 tools/check_secrets.py`
  * 打包前:  `python3 pack.py` 会自动先跑一遍
  * 查历史:  `python3 tools/check_secrets.py --history`(全历史 blob 扫描)

两类规则:
  1. **通用规则**(随仓库分发, 见 RULES): 私钥块、常见平台令牌、硬编码的 password/api_key、
     个人绝对路径、非 noreply 邮箱、中国大陆手机号等。
  2. **本机禁词表**(`tools/secret_denylist.txt`, **已被 .gitignore 忽略, 永不入仓**):
     把自己的 QQ 号、群号、令牌、设备号、昵称等逐行写进去, 以后任何文件里出现都会被拦下。
     仓库里只保留 `tools/secret_denylist.example.txt` 说明格式。

豁免方式(尽量少用, 且要写清原因):
  * 同一行加注释 `secret-scan: allow`
  * 在 `tools/.secretscanignore` 里按 glob 排除整个文件

退出码: 0 = 干净; 1 = 发现硬性问题; 2 = 只有提示级问题(--strict 时也算失败)。
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys


def _utf8_console() -> None:
    """非 UTF-8 控制台(西文 Windows)下打印中文不能崩 —— 见 qqops/core.py 的同名函数。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE_FILE = os.path.join(ROOT, "tools", ".secretscanignore")
DENYLIST_FILE = os.path.join(ROOT, "tools", "secret_denylist.txt")
INLINE_ALLOW = "secret-scan: allow"
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".gz", ".zip", ".tar", ".woff",
            ".woff2", ".ttf", ".pdf", ".db", ".pyc", ".so", ".exe", ".bundle"}
MAX_BYTES = 2 * 1024 * 1024

# 占位符(文档/示例里合法出现的"假密钥")
PLACEHOLDER = re.compile(
    r"(?i)(<\s*[^>]+>|\bx{3,}\b|\*{3,}|\.{3,}|your[_-]?|placeholder|example|sample|change[_-]?me|"
    r"test|fake|dummy|todo|安装时自动生成|自定义|留空|未配置|\$\{|os\.environ|getenv)")

# (规则名, 正则, 说明)  —— 通用规则: 可以随仓库公开, 不包含任何私人信息
RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
     "私钥内容, 绝不能入库"),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "疑似 OpenAI/DeepSeek 密钥"),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
     "疑似 GitHub 令牌"),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "疑似 AWS Access Key"),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "疑似 Slack 令牌"),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
     "疑似 JWT"),
    ("telegram-token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"), "疑似 Telegram Bot 令牌"),
    ("hardcoded-secret",
     re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\b\s*[:=]\s*"
                r"[\"']([^\"'\s]{8,})[\"']"),
     "疑似硬编码口令/密钥(应放 data/config.json 或环境变量)"),
    ("personal-path", re.compile(r"[A-Za-z]:\\Users\\([A-Za-z0-9_.\-]+)\\|/home/([a-z0-9_\-]+)/"),
     "个人绝对路径(用户名会暴露本机身份), 请换成 ~ 或 <user>"),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
     "邮箱地址(允许 noreply/示例域名)"),
    ("cn-mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "疑似中国大陆手机号"),
    ("long-hex", re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])"),
     "超长十六进制串(可能是密钥/哈希), 确认无用再豁免"),
]
WARN_ONLY = {"long-hex"}
ALLOW_EMAIL = re.compile(
    r"(?i)@(users\.noreply\.github\.com|noreply\.github\.com|example\.(com|org|net)|localhost|"
    r"qq\.com|test\.local|invalid)$")
# 通用示例路径/用户名, 不算隐私
ALLOW_PATH_USER = {"public", "default", "user", "username", "yourname", "me", "<user>", "runner",
                   "admin", "test", "example"}


def load_ignore() -> list:
    if not os.path.exists(IGNORE_FILE):
        return []
    out = []
    with open(IGNORE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def load_denylist() -> list:
    if not os.path.exists(DENYLIST_FILE):
        return []
    out = []
    with open(DENYLIST_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and len(line) >= 4:
                out.append(line)
    return out


def tracked_files() -> list:
    try:
        p = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
        if p.returncode == 0:
            return [x for x in p.stdout.splitlines() if x.strip()]
    except Exception:
        pass
    out = []
    for cur, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "data", "dist", "__pycache__", ".testdata")]
        for f in files:
            out.append(os.path.relpath(os.path.join(cur, f), ROOT).replace(os.sep, "/"))
    return out


def staged_files() -> list:
    p = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                       cwd=ROOT, capture_output=True, text=True)
    return [x for x in p.stdout.splitlines() if x.strip()]


def scan_text(rel: str, text: str, deny: list) -> list:
    """返回 [(级别, 规则, 行号, 片段, 说明)]。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if INLINE_ALLOW in line:
            continue
        for name, rx, hint in RULES:
            for m in rx.finditer(line):
                val = m.group(0)
                if name == "email" and ALLOW_EMAIL.search(val):
                    continue
                if name == "personal-path":
                    who = (m.group(1) or m.group(2) or "").lower()
                    if who in ALLOW_PATH_USER:
                        continue
                if name == "hardcoded-secret" and PLACEHOLDER.search(m.group(2) or ""):
                    continue
                if name == "cn-mobile" and PLACEHOLDER.search(line):
                    continue
                level = "warn" if name in WARN_ONLY else "hard"
                out.append((level, name, i, redact(val), hint))
        low = line.lower()
        for word in deny:
            if word.lower() in low:
                out.append(("hard", "denylist", i, redact(word),
                            "命中本机禁词表(tools/secret_denylist.txt)"))
    return out


def redact(s: str, keep: int = 4) -> str:
    s = str(s)
    if len(s) <= keep * 2:
        return s[:2] + "…"
    return s[:keep] + "…" + s[-2:]


def iter_files(only: list, ignore: list) -> list:
    files = []
    for rel in (only or tracked_files()):
        if rel.endswith(tuple(SKIP_EXT)):
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in ignore):
            continue
        full = os.path.join(ROOT, rel)
        try:
            if os.path.getsize(full) > MAX_BYTES:
                continue
        except OSError:
            continue
        files.append(rel)
    return sorted(set(files))


def scan_files(files: list, deny: list) -> list:
    findings = []
    for rel in files:
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        for level, name, ln, frag, hint in scan_text(rel, text, deny):
            findings.append((level, name, rel, ln, frag, hint))
    return findings


def scan_history(deny: list) -> list:
    """扫全部历史 blob(`git grep <规则> <每个提交>`), 用于发布前自查。"""
    findings = []
    try:
        revs = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT,
                              capture_output=True, text=True).stdout.split()
    except Exception:
        return findings
    if not revs:
        return findings
    for name, rx, hint in RULES:
        try:
            p = subprocess.run(["git", "grep", "-I", "-n", "-E", "-e", rx.pattern] + revs,
                               cwd=ROOT, capture_output=True, text=True)
        except Exception:
            continue
        seen = set()
        for line in (p.stdout or "").splitlines():
            key = line.split(":", 3)[-1][:80]
            if key in seen:
                continue
            seen.add(key)
            findings.append(("hard", name, "(history) " + line.split(":", 2)[1] if ":" in line else "(history)",
                             0, redact(key), hint))
            if len(seen) >= 20:
                break
    for word in deny:
        try:
            p = subprocess.run(["git", "log", "--all", "--oneline", "-S", word],
                               cwd=ROOT, capture_output=True, text=True)
            if (p.stdout or "").strip():
                findings.append(("hard", "denylist-history", "(history)", 0, redact(word),
                                 "禁词出现在历史提交里, 需要改写历史"))
        except Exception:
            pass
    return findings


def main() -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="隐私/密钥扫描(仓库门禁)")
    ap.add_argument("--path", nargs="*", default=None, help="只扫这些路径(默认: git 跟踪的全部文件)")
    ap.add_argument("--staged", action="store_true", help="只扫暂存区(给 pre-commit 用)")
    ap.add_argument("--history", action="store_true", help="额外扫全历史 blob")
    ap.add_argument("--strict", action="store_true", help="提示级问题也返回失败")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args()

    ignore, deny = load_ignore(), load_denylist()
    only = args.path or (staged_files() if args.staged else None)
    if args.staged and not only:
        print("✔ 暂存区没有文件需要扫描")
        return 0
    files = iter_files(only, ignore)
    findings = scan_files(files, deny)
    if args.history:
        findings += scan_history(deny)

    hard = [f for f in findings if f[0] == "hard"]
    warn = [f for f in findings if f[0] != "hard"]
    if not args.quiet:
        for level, name, rel, ln, frag, hint in findings[:60]:
            mark = "✘" if level == "hard" else "!"
            print("%s [%s] %s:%s  %s\n      → %s" % (mark, name, rel, ln, frag, hint))
    print("扫描 %d 个文件%s: 硬性问题 %d, 提示 %d%s"
          % (len(files), "(+全历史)" if args.history else "", len(hard), len(warn),
             "" if deny else " · 未配置本机禁词表(tools/secret_denylist.txt)"))
    if hard:
        print("✘ 不允许提交/发布: 请按上面的建议改掉(或加 `secret-scan: allow` 注释并说明原因)")
        return 1
    if warn and args.strict:
        print("✘ --strict: 提示级问题也算失败")
        return 2
    print("✔ 未发现隐私/密钥问题")
    return 0


if __name__ == "__main__":
    sys.exit(main())
