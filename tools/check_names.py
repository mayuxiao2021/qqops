#!/usr/bin/env python3
"""静态检查: 有没有"用了但没定义"的全局名字 —— 也就是 NameError 的静态版。

为什么需要它: 控制台按钮点了没反应/报错, 最常见的原因就是某个函数里少 import 了一个模块。
本项目真踩过: `console/pages.py` 的 `_spawn_restart()` 用了 `os` 却没 `import os`
→ 运维页「切换功耗档位 / 进程模式」按钮直接 500。那条分支要走 POST + 真实重启才触发,
测试很难覆盖到, 于是拖到用户先发现。这里在提交/CI 阶段就拦住。

做法: 用 ast 收集每个函数的局部名(参数、赋值、推导式目标、嵌套定义…)、模块级全局名与内置名,
再检查所有 Load 上下文的 Name 能否解析。比 pyflakes 简单, 但足够抓住"少 import"。
刻意忽略**注解**(`from __future__ import annotations` 下是字符串)与 def 行的默认值/装饰器,
避免误报。

用法:
    python3 tools/check_names.py            # 扫 qqops/ tools/ pack.py
    python3 tools/check_names.py --all      # 连 tests/ 一起扫
"""
from __future__ import annotations

import ast
import builtins
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__", "__builtins__",
                                 "__spec__", "__loader__", "WindowsError"}
FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _walk(node, prune=()):
    """像 ast.walk, 但遇到 prune 里的类型就不再往下走; node 可以是节点或节点列表。"""
    stack = list(node) if isinstance(node, list) else [node]
    while stack:
        n = stack.pop()
        yield n
        if prune and isinstance(n, prune):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _targets(node) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
    return out


def bindings(nodes: list, into_functions: bool = False) -> set:
    """这批语句会绑定哪些名字。

    into_functions=False: 不进函数/类体(用于"模块级全局名"), 但函数名/类名本身算绑定。
    into_functions=True : 全都算(用于函数局部名 —— 宽松些, 少误报)。
    """
    prune = () if into_functions else (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                       ast.Lambda)
    out = set()
    for n in _walk(nodes, prune):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                out |= _targets(t)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            out |= _targets(n.target)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            out |= _targets(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars:
                    out |= _targets(it.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.NamedExpr):
            out |= _targets(n.target)
        elif isinstance(n, ast.comprehension):
            out |= _targets(n.target)
        elif isinstance(n, ast.Lambda):
            a = n.args
            out |= {x.arg for x in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs)}
            if a.vararg:
                out.add(a.vararg.arg)
            if a.kwarg:
                out.add(a.kwarg.arg)
    return out


def _params(fn) -> set:
    a = fn.args
    out = {x.arg for x in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs)}
    if a.vararg:
        out.add(a.vararg.arg)
    if a.kwarg:
        out.add(a.kwarg.arg)
    return out


def annotation_ids(tree) -> set:
    """注解里的名字不算"使用"(future annotations 下根本不会求值)。"""
    skip = set()
    for n in ast.walk(tree):
        ann = None
        if isinstance(n, ast.arg):
            ann = n.annotation
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ann = n.returns
        elif isinstance(n, ast.AnnAssign):
            ann = n.annotation
        if ann is not None:
            skip |= {id(x) for x in ast.walk(ann)}
    return skip


def check_module(path: str) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        return [(path, getattr(e, "lineno", 0), "语法错误: %s" % e)]
    except Exception:
        return []

    skip = annotation_ids(tree)
    globals_ = bindings(tree.body) | BUILTINS
    problems = []

    def scan(node, scopes: list):
        """检查 node 子树里的 Name 载入。

        遇到嵌套函数/类就停: 它们有各自的作用域, 由 visit() 带着正确的 scopes 再扫一遍。
        (不停的话, 嵌套函数的参数会被当成"外层未定义" —— 这是本检查器最早的误报来源。)
        """
        for n in _walk(node, prune=(ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                                    ast.ClassDef)):
            if id(n) in skip:
                continue
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if not any(n.id in s for s in scopes) and n.id not in globals_:
                    problems.append((path, n.lineno, "未定义的名字: %s" % n.id))

    def visit(node, scopes: list):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNC_TYPES):
                inner = scopes + [_params(child) | bindings(child.body, into_functions=True)]
                scan(child.body, inner)
                visit(child, inner)
            elif isinstance(child, ast.ClassDef):
                inner = scopes + [bindings(child.body, into_functions=True)]
                visit(child, inner)
            else:
                visit(child, scopes)

    visit(tree, [])
    return problems


def py_files(include_tests: bool) -> list:
    out = []
    for base in ("qqops", "tools"):
        for cur, dirs, files in os.walk(os.path.join(ROOT, base)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
            out += [os.path.join(cur, f) for f in files if f.endswith(".py")]
    out.append(os.path.join(ROOT, "pack.py"))
    if include_tests:
        out += [os.path.join(ROOT, "tests", f) for f in os.listdir(os.path.join(ROOT, "tests"))
                if f.endswith(".py")]
    return sorted(out)


def main(argv: list) -> int:
    _utf8_console()
    problems = []
    for p in py_files("--all" in argv):
        problems += check_module(p)
    if problems:
        print("✘ 发现可能触发 NameError 的名字(多半是少 import):")
        seen = set()
        for path, line, msg in problems:
            key = (path, line, msg)
            if key in seen:
                continue
            seen.add(key)
            print("  %s:%s  %s" % (os.path.relpath(path, ROOT).replace(os.sep, "/"), line, msg))
        return 1
    print("✔ 没有未定义的名字")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
