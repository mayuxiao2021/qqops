"""插件管理器 —— 发现/加载/启停/热重载/配置/任务(对齐 AstrBot 的 `PluginManager`)。

核心承诺:
  1. **坏插件不影响好插件**: 导入失败、初始化失败都只影响它自己, 记账 + 发事件 + 控制台可见;
  2. **按角色注册**: 采集进程只注册 ingest 角色的事件/阶段, 调度进程只注册任务,
     控制台进程只注册路由/页面 —— 一套插件代码在三处各司其职;
  3. **配置可校验**: 由 `_conf_schema.json` 驱动, 非法值回退默认并记账;
  4. **可热重载**: 注销 handler/路由 → 重新导入 → 重新注册, 不用重启服务。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types

from . import errors, schema as schema_mod
from .plugin import Plugin, PluginMeta
from .registry import Ref, REGISTRY
from .web import ROUTER

__all__ = ["PluginManager"]

STATE_FILE = "plugins.json"
_MARK = "_qqops_handlers"


class PluginManager:
    def __init__(self, ctx=None, dirs=None, roles=("ingest", "tasks", "console"),
                 registry=None, router=None):
        self.ctx = ctx
        self.roles = tuple(roles) if roles else ("ingest", "tasks", "console")
        self.registry = registry or REGISTRY
        self.router = router or ROUTER
        self.plugins = {}                    # id -> {"meta","inst","module"}
        self.errors = {}                     # id -> 错误说明
        self._cfg_cache = {}
        self._dirs = list(dirs) if dirs else self.default_dirs()
        self._state = self._load_state()

    # ---------------------------------------------------------------- 目录/状态
    def default_dirs(self) -> list:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = [os.path.join(here, "plugins")]                 # 内置插件
        try:
            out.append(os.path.join(errors.data_dir(), "plugins"))   # 用户插件
        except Exception:
            pass
        return [d for d in out if os.path.isdir(d)]

    @property
    def state_path(self) -> str:
        return os.path.join(errors.data_dir(), STATE_FILE)

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                data = json.load(f) or {}
            return data.get("plugins") or {}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            payload = {"schema_version": 1, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "plugins": self._state}
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except Exception as e:
            errors.record("plugins.state", exc=e, kind="state_save")

    def is_enabled(self, pid: str, default: bool = True) -> bool:
        row = self._state.get(pid) or {}
        return bool(row.get("enabled", default))

    # ---------------------------------------------------------------- 发现
    def discover(self) -> dict:
        """扫描插件目录, 返回 `{id: meta}`(不导入代码, 所以坏插件也能被发现)。"""
        found = {}
        for root in self._dirs:
            try:
                names = sorted(os.listdir(root))
            except Exception:
                continue
            for name in names:
                pdir = os.path.join(root, name)
                if not os.path.isdir(pdir) or name.startswith((".", "_")):
                    continue
                init_py = os.path.join(pdir, "__init__.py")
                meta_file = os.path.join(pdir, "metadata.json")
                if not os.path.isfile(init_py) and not os.path.isfile(meta_file):
                    continue
                raw = {}
                try:
                    with open(meta_file, encoding="utf-8-sig") as f:
                        raw = json.load(f) or {}
                    if "data" in raw and isinstance(raw["data"], dict):
                        raw = raw["data"]              # 兼容 AstrBot 市场索引格式
                except Exception as e:
                    self.errors[name] = "metadata.json 解析失败: %s" % e
                meta = PluginMeta(
                    id=str(raw.get("id") or name),
                    name=str(raw.get("name") or raw.get("display_name") or name),
                    version=str(raw.get("version") or "0.0.0"),
                    author=str(raw.get("author") or ""),
                    desc=str(raw.get("desc") or raw.get("description") or ""),
                    repo=str(raw.get("repo") or ""),
                    priority=int(raw.get("priority") or 100),
                    enabled_by_default=bool(raw.get("enabled_by_default", True)),
                    depends=list(raw.get("depends") or []),
                    dir=pdir,
                    builtin=(os.path.basename(root) == "plugins"
                             and os.path.dirname(root).endswith("qqops")),
                )
                found[meta.id] = meta
        return found

    # ---------------------------------------------------------------- 加载
    def load_all(self) -> dict:
        out = {}
        for pid, meta in self.discover().items():
            out[pid] = self.load(pid, meta=meta)
        return out

    def _import(self, pid: str, pdir: str):
        """导入插件模块。用唯一模块名, 支持相对导入(插件是包)。"""
        mod_name = "qqops_plugin_%s" % "".join(c if c.isalnum() or c == "_" else "_" for c in pid)
        init_py = os.path.join(pdir, "__init__.py")
        if not os.path.isfile(init_py):
            plugin_py = os.path.join(pdir, "plugin.py")
            if not os.path.isfile(plugin_py):
                raise FileNotFoundError("插件缺少 __init__.py / plugin.py")
            init_py = plugin_py
        spec = importlib.util.spec_from_file_location(
            mod_name, init_py, submodule_search_locations=[pdir])
        if spec is None or spec.loader is None:
            raise ImportError("无法为 %s 创建模块规格" % init_py)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _find_class(self, mod: types.ModuleType):
        best = None
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and issubclass(obj, Plugin) and obj is not Plugin:
                if getattr(obj, "_qqops_meta", None):
                    return obj
                best = best or obj
        return best

    def load(self, pid: str, meta: PluginMeta = None) -> str:
        """加载(或重载)一个插件。返回状态字符串。"""
        if meta is None:
            meta = self.discover().get(pid)
        if meta is None:
            return "not_found"
        if not self.is_enabled(pid, meta.enabled_by_default):
            meta.status = "disabled"
            self.plugins[pid] = {"meta": meta, "inst": None, "module": None}
            return "disabled"
        # 先卸载旧的(热重载)
        if pid in self.plugins and self.plugins[pid].get("inst"):
            self.unload(pid)
        try:
            mod = self._import(pid, meta.dir)
            cls = self._find_class(mod)
            if cls is None:
                raise ImportError("插件里找不到 Plugin 子类")
            deco = getattr(cls, "_qqops_meta", None) or {}
            for k, v in deco.items():                    # 装饰器优先补齐缺省字段
                if not getattr(meta, k, None) and v:
                    setattr(meta, k, v)
            inst = cls()
            inst.meta, inst.ctx = meta, self.ctx
            added = self._register_handlers(inst, meta)
            meta.handlers = added
            try:
                inst.initialize(self.ctx)
            except Exception as e:                       # 初始化失败 = 插件失败
                raise RuntimeError("initialize() 失败: %s" % e) from e
            meta.status = "loaded"
            meta.error = ""
            meta.loaded_at = time.time()
            self.plugins[pid] = {"meta": meta, "inst": inst, "module": mod}
            self._state.setdefault(pid, {})["version"] = meta.version
            self._state[pid]["loaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_state()
            self._log("INFO", "插件 %s v%s 已加载(%d 个处理器)" % (meta.id, meta.version, added))
            return "loaded"
        except Exception as e:                           # noqa: BLE001 —— 坏插件只影响自己
            meta.status = "failed"
            meta.error = "%s: %s" % (type(e).__name__, e)
            meta.handlers = 0
            self.errors[pid] = meta.error
            self.plugins[pid] = {"meta": meta, "inst": None, "module": None}
            errors.record("plugin.%s" % pid, exc=e, kind="load_failed",
                          extra={"dir": meta.dir})
            self._log("ERROR", "插件 %s 加载失败: %s(其它插件不受影响)" % (pid, meta.error))
            return "failed"

    def _register_handlers(self, inst: Plugin, meta: PluginMeta) -> int:
        """把实例方法上的装饰器标记收进注册表; 只收本进程角色需要的。"""
        added, pages = 0, []
        names = set(dir(inst))
        # 类属性里可能还有被实例属性遮蔽的, 一并扫一遍
        names |= set(dir(type(inst)))
        for name in sorted(names):
            if name.startswith("__"):
                continue
            try:
                fn = getattr(inst, name)
            except Exception:
                continue
            marks = getattr(fn, _MARK, None)
            if not marks:
                continue
            for mk in marks:
                role = mk.get("role")
                if role and role not in self.roles and "all" not in self.roles:
                    continue
                kind, mname = mk["kind"], mk["name"]
                meta_extra = {k: v for k, v in mk.items()
                              if k not in ("kind", "name", "priority")}
                if kind == "route":
                    # 路由表负责派发; 注册表的登记由 Router.add 内部完成(统一观测)
                    self.router.add(mname, fn, methods=mk.get("methods") or ("GET",),
                                    auth=bool(mk.get("auth", True)), plugin_id=meta.id,
                                    meta={k: v for k, v in meta_extra.items()
                                          if k not in ("role", "methods")})
                else:
                    self.registry.add(Ref(kind=kind, name=mname, fn=fn,
                                          plugin_id=meta.id, priority=int(mk.get("priority", 100)),
                                          meta=meta_extra))
                    if kind == "page":
                        pages.append(mk.get("page") or {"id": mname, "title": mname})
                added += 1
        meta.pages = pages
        return added

    def unload(self, pid: str) -> str:
        row = self.plugins.get(pid)
        inst = row.get("inst") if row else None
        if inst is not None:
            try:
                inst.terminate()
            except Exception as e:                       # noqa: BLE001
                errors.record("plugin.%s" % pid, exc=e, kind="terminate_error")
        self.registry.remove_plugin(pid)
        self.router.remove_plugin(pid)
        try:
            mod_name = "qqops_plugin_%s" % "".join(
                c if c.isalnum() or c == "_" else "_" for c in pid)
            sys.modules.pop(mod_name, None)
        except Exception:
            pass
        if row:
            row["inst"] = None
            if row.get("meta"):
                row["meta"].status = "disabled"
        return "unloaded"

    def reload(self, pid: str) -> str:
        st = self.load(pid)
        self._log("INFO", "插件 %s 重载: %s" % (pid, st))
        return st

    def enable(self, pid: str) -> str:
        self._state.setdefault(pid, {})["enabled"] = True
        self._save_state()
        return self.load(pid)

    def disable(self, pid: str) -> str:
        self._state.setdefault(pid, {})["enabled"] = False
        self._save_state()
        self.unload(pid)
        return "disabled"

    def toggle(self, pid: str, on: bool) -> str:
        return self.enable(pid) if on else self.disable(pid)

    # ---------------------------------------------------------------- 配置
    def schema(self, pid: str) -> dict:
        row = self.plugins.get(pid)
        pdir = (row["meta"].dir if row and row.get("meta") else None) or \
            (self.discover().get(pid).dir if self.discover().get(pid) else "")
        if not pdir:
            return {}
        path = os.path.join(pdir, "_conf_schema.json")
        try:
            with open(path, encoding="utf-8-sig") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def config_path(self, pid: str) -> str:
        return os.path.join(errors.data_dir(), "config", "%s_config.json" % pid)

    def config(self, pid: str, force: bool = False) -> dict:
        if not force and pid in self._cfg_cache:
            return self._cfg_cache[pid]
        sch = self.schema(pid)
        user = {}
        try:
            with open(self.config_path(pid), encoding="utf-8-sig") as f:
                user = json.load(f) or {}
        except Exception:
            user = {}
        clean, errs = schema_mod.validate(sch, user)
        if errs:
            errors.record("plugin.%s" % pid, kind="config_invalid",
                          message="; ".join(errs)[:500])
        self._cfg_cache[pid] = clean
        return clean

    def set_config(self, pid: str, patch: dict):
        """保存插件配置。返回 `(ok, errors, 生效后的配置)`。"""
        sch = self.schema(pid)
        cur = dict(self.config(pid, force=True))
        for k, v in (patch or {}).items():
            if isinstance(v, dict) and isinstance(cur.get(k), dict):
                cur[k].update(v)
            else:
                cur[k] = v
        clean, errs = schema_mod.validate(sch, cur)
        try:
            os.makedirs(os.path.dirname(self.config_path(pid)), exist_ok=True)
            tmp = self.config_path(pid) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.config_path(pid))
        except Exception as e:
            errors.record("plugin.%s" % pid, exc=e, kind="config_save")
            return False, ["保存失败: %s" % e], cur
        self._cfg_cache.pop(pid, None)
        if self.ctx:
            self.ctx.invalidate_plugin_cfg(pid)
        return (not errs), errs, clean

    def config_view(self, pid: str) -> dict:
        """给控制台: schema 展平 + 当前值(密钥掩码)。"""
        sch = self.schema(pid)
        cfg = self.config(pid, force=True)
        fields = schema_mod.flatten(sch)
        for f in fields:
            node = cfg
            for part in f["path"].split("."):
                node = node.get(part) if isinstance(node, dict) else None
            f["value"] = node
            if f.get("secret") and node not in (None, "", [], {}):
                f["value"] = "******"
                f["configured"] = True
        return {"fields": fields, "config": _mask_secrets(cfg, fields),
                "path": self.config_path(pid)}

    # ---------------------------------------------------------------- 任务
    def tasks(self) -> list:
        rows = []
        for ref in self.registry.handlers("task"):
            meta = ref.meta or {}
            last = self._state.get(ref.plugin_id, {}).get("task_%s" % ref.name)
            rows.append({"task": ref.name, "plugin": ref.plugin_id,
                         "every": meta.get("every"), "cron": meta.get("cron"),
                         "enabled": meta.get("enabled", True),
                         "run_on_start": meta.get("run_on_start", False),
                         "last_run": last,
                         "handler": getattr(ref.fn, "__name__", "?")})
        return rows

    def due_tasks(self, now: float = None, min_gap: float = 5.0) -> list:
        """返回到点该跑的任务(按 `every` 秒/简单 cron 判断)。"""
        now = now or time.time()
        out = []
        for ref in self.registry.handlers("task"):
            meta = ref.meta or {}
            if meta.get("enabled") is False:
                continue
            key = "task_%s" % ref.name
            last = self._state.get(ref.plugin_id, {}).get(key) or 0
            every = meta.get("every")
            cron = meta.get("cron")
            due = False
            if every:
                due = (now - float(last)) >= max(min_gap, float(every))
            elif cron:
                due = _cron_due(cron, float(last), now)
            elif meta.get("run_on_start"):
                due = not last
            if due:
                out.append(ref)
        return out

    def mark_task_run(self, ref, ts: float = None) -> None:
        row = self._state.setdefault(ref.plugin_id, {})
        row["task_%s" % ref.name] = float(ts or time.time())
        self._save_state()

    def run_task(self, ref, ctx=None) -> dict:
        """跑一个任务并记账(失败不抛出, 返回结果字典)。"""
        t0 = time.time()
        try:
            out = ref.fn(ctx or self.ctx)
            ms = round((time.time() - t0) * 1000, 1)
            self.mark_task_run(ref)
            return {"ok": True, "task": ref.name, "plugin": ref.plugin_id,
                    "ms": ms, "result": out if isinstance(out, (str, int, float, dict, list, bool, type(None))) else str(out)}
        except Exception as e:                           # noqa: BLE001
            errors.record("task.%s" % ref.name, exc=e, kind="task_error",
                          extra={"plugin": ref.plugin_id, "ms": round((time.time() - t0) * 1000, 1)})
            self.mark_task_run(ref)
            return {"ok": False, "task": ref.name, "plugin": ref.plugin_id,
                    "error": "%s: %s" % (type(e).__name__, e),
                    "ms": round((time.time() - t0) * 1000, 1)}

    # ---------------------------------------------------------------- 命令
    def commands(self) -> list:
        return [{"command": r.name, "plugin": r.plugin_id,
                 "help": (r.meta or {}).get("help", ""),
                 "handler": getattr(r.fn, "__name__", "?")}
                for r in self.registry.handlers("command")]

    def run_command(self, name: str, args: dict = None, ctx=None) -> dict:
        rows = self.registry.handlers("command", name)
        if not rows:
            return {"ok": False, "error": "没有这个命令: %s" % name}
        ref = rows[0]
        t0 = time.time()
        try:
            try:
                out = ref.fn(args or {}, ctx or self.ctx)
            except TypeError:
                out = ref.fn(args or {})
            return {"ok": True, "command": name, "plugin": ref.plugin_id,
                    "ms": round((time.time() - t0) * 1000, 1), "result": out}
        except Exception as e:                           # noqa: BLE001
            errors.record("command.%s" % name, exc=e, kind="command_error")
            return {"ok": False, "command": name,
                    "error": "%s: %s" % (type(e).__name__, e)}

    # ---------------------------------------------------------------- 页面
    def pages(self) -> list:
        out = []
        for ref in self.registry.handlers("page"):
            meta = dict((ref.meta or {}).get("page") or {})
            meta.setdefault("id", ref.name)
            meta.setdefault("title", ref.name)
            meta.setdefault("icon", "🧩")
            meta.setdefault("order", 200)
            meta["plugin"] = ref.plugin_id
            meta["api"] = meta.get("api") or ("/api/plugins/%s/page" % ref.plugin_id)
            out.append(meta)
        return sorted(out, key=lambda m: (m.get("order", 200), m.get("id", "")))

    # ---------------------------------------------------------------- 状态
    def status(self) -> list:
        known = self.discover()
        for pid, meta in known.items():                  # 未加载的也列出来
            if pid not in self.plugins:
                meta.status = "disabled" if not self.is_enabled(pid, meta.enabled_by_default) else "discovered"
                self.plugins.setdefault(pid, {"meta": meta, "inst": None, "module": None})
        rows = [row["meta"].to_dict() | {"enabled": self.is_enabled(pid, row["meta"].enabled_by_default)}
                for pid, row in self.plugins.items() if row.get("meta")]
        return sorted(rows, key=lambda r: (r.get("priority", 100), r.get("id", "")))

    def state(self) -> dict:
        st = self.status()
        return {"total": len(st),
                "loaded": len([r for r in st if r["status"] == "loaded"]),
                "failed": len([r for r in st if r["status"] == "failed"]),
                "disabled": len([r for r in st if r["status"] == "disabled"]),
                "roles": list(self.roles),
                "dirs": self._dirs,
                "errors": dict(self.errors),
                "plugins": st}

    # ---------------------------------------------------------------- 内部
    def _log(self, level: str, msg: str) -> None:
        if self.ctx:
            self.ctx.log("plugins", level, msg)


def _mask_secrets(cfg: dict, fields: list) -> dict:
    """按字段表把 secret 字段替换成掩码(控制台展示用)。"""
    import copy
    out = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    for f in fields or []:
        if not f.get("secret"):
            continue
        parts = f["path"].split(".")
        node = out
        for p in parts[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict) and node.get(parts[-1]) not in (None, "", [], {}):
            node[parts[-1]] = "******"
    return out


def _cron_due(cron: str, last: float, now: float) -> bool:
    """极简 cron: "分 时 日 月 周"; 只判断"本分钟内该跑且本分钟还没跑过"。"""
    try:
        parts = str(cron).split()
        if len(parts) != 5:
            return False
        lt = time.localtime(now)
        fields = [lt.tm_min, lt.tm_hour, lt.tm_mday, lt.tm_mon, (lt.tm_wday + 1) % 7]
        for spec, val in zip(parts, fields):
            if spec == "*":
                continue
            ok = False
            for chunk in spec.split(","):
                if "/" in chunk:
                    base, step = chunk.split("/", 1)
                    base = 0 if base in ("*", "") else int(base)
                    if val >= base and (val - base) % int(step) == 0:
                        ok = True
                elif "-" in chunk:
                    a, b = chunk.split("-", 1)
                    if int(a) <= val <= int(b):
                        ok = True
                elif int(chunk) == val:
                    ok = True
            if not ok:
                return False
        return (now - last) > 55          # 同一分钟不重复跑
    except Exception:
        return False
