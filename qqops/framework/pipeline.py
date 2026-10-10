"""阶段化管线 —— 事件按固定阶段流动(对齐 AstrBot 的 `core/pipeline`)。

为什么要有管线: 现在"收消息 → 落库 → 富化 → 总结 → 推送"是一条写死的直路,
加一步就得改代码。拆成阶段后:

  * 每个阶段独立开关、独立优先级、独立失败语义;
  * 阶段失败**不会中断**事件(除非标了 `critical`), 只进账本;
  * 每阶段耗时记录在 `Event.meta["stages"]`, 控制台能看见"这条消息卡在哪一步"。

内置阶段顺序(见 docs/ARCHITECTURE.md §3.2):
    10 ingest_filter → 20 persist → 30 archive → 40 enrich → 50 summarize → 60 notify
插件可用 `@on_stage("enrich", order=45)` 插到任意位置。
"""
from __future__ import annotations

import time

from . import errors
from .registry import REGISTRY

__all__ = ["Pipeline", "STAGE_ORDER", "STAGE_LABELS"]

# 阶段名 → 默认顺序(仅用于文档与展示; 真正的顺序由装饰器 order 决定)
STAGE_ORDER = {
    "ingest_filter": 10,
    "persist": 20,
    "archive": 30,
    "enrich": 40,
    "summarize": 50,
    "notify": 60,
}
STAGE_LABELS = {
    "ingest_filter": "采集过滤",
    "persist": "落库",
    "archive": "归档",
    "enrich": "富化(媒体/链接/文件)",
    "summarize": "总结",
    "notify": "推送",
}


class Pipeline:
    """把事件依次送过各阶段。"""

    def __init__(self, registry=None, cfg_getter=None, on_stage_error=None):
        self.registry = registry or REGISTRY
        self.cfg_getter = cfg_getter          # 可调用: 返回配置 dict(用于开关)
        self.on_stage_error = on_stage_error  # 回调(阶段名, 异常, 事件)

    # ---------------------------------------------------------------- 元信息
    def stages(self, role: str = None, kind: str = None) -> list:
        rows = self.registry.handlers("stage")
        if role:
            rows = [r for r in rows if not r.meta.get("role") or r.meta.get("role") == role]
        if kind:
            rows = [r for r in rows if not r.meta.get("on") or kind in _as_list(r.meta["on"])]
        return sorted(rows, key=lambda r: (r.priority, r.name))

    def describe(self) -> list:
        out = []
        for r in self.stages():
            out.append({"stage": r.name, "order": r.priority, "plugin": r.plugin_id,
                        "handler": getattr(r.fn, "__name__", "?"),
                        "critical": bool(r.meta.get("critical")),
                        "label": STAGE_LABELS.get(r.name, r.name)})
        return out

    # ---------------------------------------------------------------- 开关
    def enabled(self, ref) -> bool:
        """阶段开关: 插件可在配置里用 `pipeline.<stage>.enabled=false` 关掉。"""
        if not self.cfg_getter:
            return True
        try:
            cfg = self.cfg_getter() or {}
            pipe = cfg.get("pipeline") or {}
            node = pipe.get(ref.name) or {}
            if isinstance(node, dict) and node.get("enabled") is False:
                return False
            plug = (cfg.get("plugins") or {}).get(ref.plugin_id) or {}
            if isinstance(plug, dict) and plug.get(ref.name) is False:
                return False
        except Exception:
            return True
        return True

    # ---------------------------------------------------------------- 执行
    def run(self, evt, ctx=None) -> object:
        """跑一遍管线。永不抛异常 —— 异常一律进账本, 事件继续(除非 critical 失败)。"""
        for ref in self.stages(kind=evt.kind):
            if evt.dropped:
                break
            if not self.enabled(ref):
                evt.note(ref.name, skipped=True)
                continue
            t0 = time.time()
            try:
                out = ref.fn(evt, ctx)
                if out is not None and out is not evt:
                    evt.add_result(out)
                evt.note(ref.name, ms=round((time.time() - t0) * 1000, 1),
                         plugin=ref.plugin_id)
            except Exception as e:                     # noqa: BLE001 —— 阶段必须隔离
                critical = bool(ref.meta.get("critical"))
                evt.note(ref.name, ms=round((time.time() - t0) * 1000, 1),
                         error="%s: %s" % (type(e).__name__, e), critical=critical)
                errors.record("stage.%s" % ref.name, exc=e, kind="stage_error",
                              extra={"event": evt.kind, "plugin": ref.plugin_id,
                                     "session": evt.session})
                if critical:
                    evt.drop("关键阶段 %s 失败: %s" % (ref.name, e))
                if self.on_stage_error:
                    try:
                        self.on_stage_error(ref.name, e, evt)
                    except Exception:
                        pass
        return evt


def _as_list(v):
    return v if isinstance(v, (list, tuple, set)) else [v]
