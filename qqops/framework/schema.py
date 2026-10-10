"""配置 schema —— schema 即默认值、校验规则、控制台表单(对齐 AstrBot 的 `_conf_schema.json`)。

格式(与 AstrBot 一致, 便于互通与迁移):

    {
      "basic": {
        "description": "基础设置", "type": "object", "hint": "整体说明",
        "items": {
          "enabled": {"type": "bool",   "description": "启用",   "default": true, "hint": "..."},
          "mode":    {"type": "string", "description": "模式",   "default": "a", "options": ["a","b"]},
          "hours":   {"type": "int",    "description": "小时数", "default": 24, "min": 1, "max": 720},
          "words":   {"type": "list",   "description": "词表",   "default": []},
          "key":     {"type": "string", "description": "密钥",   "default": "", "secret": true}
        }
      }
    }

能力:
  * `defaults()`   取默认值树
  * `validate()`   类型转换 + 范围/枚举校验, 非法值回退默认并给出**人话**错误
  * `flatten()`    展平成字段列表, 前端直接渲染表单(带分组/说明/hint/掩码)
"""
from __future__ import annotations

__all__ = ["defaults", "validate", "flatten", "schema_keys", "TYPE_ALIASES"]

TYPE_ALIASES = {
    "bool": "bool", "boolean": "bool", "switch": "bool",
    "int": "int", "integer": "int", "number": "float", "float": "float",
    "string": "string", "str": "string", "text": "string", "password": "string",
    "list": "list", "array": "list", "object": "object", "dict": "object",
}


def _norm_type(t: str) -> str:
    return TYPE_ALIASES.get(str(t or "string").lower(), "string")


def _is_group(node: dict) -> bool:
    return isinstance(node, dict) and (
        _norm_type(node.get("type")) == "object" or "items" in node)


# ------------------------------------------------------------------ 默认值
def defaults(schema: dict) -> dict:
    """从 schema 取出默认值树(只含声明过的键)。"""
    out = {}
    if not isinstance(schema, dict):
        return out
    for key, node in schema.items():
        if not isinstance(node, dict):
            continue
        if _is_group(node):
            out[key] = defaults(node.get("items") or {})
        else:
            out[key] = node.get("default")
    return out


def schema_keys(schema: dict) -> list:
    """所有叶子字段的路径(如 `basic.enabled`)。"""
    out = []

    def walk(node, prefix):
        for key, item in (node or {}).items():
            if not isinstance(item, dict):
                continue
            path = "%s.%s" % (prefix, key) if prefix else key
            if _is_group(item):
                walk(item.get("items") or {}, path)
            else:
                out.append(path)
    walk(schema or {}, "")
    return out


# ------------------------------------------------------------------ 校验
def _coerce(node: dict, value, path: str, errors: list):
    t = _norm_type(node.get("type"))
    label = node.get("description") or path
    try:
        if t == "bool":
            if isinstance(value, bool):
                v = value
            elif isinstance(value, (int, float)):
                v = bool(value)
            elif isinstance(value, str):
                low = value.strip().lower()
                if low in ("1", "true", "yes", "on", "y", "是", "开"):
                    v = True
                elif low in ("0", "false", "no", "off", "n", "否", "关", ""):
                    v = False
                else:
                    raise ValueError("无法识别的布尔值 %r" % value)
            else:
                raise ValueError("类型应为布尔")
        elif t == "int":
            v = int(float(value)) if isinstance(value, str) and value.strip() else int(value)
            if "min" in node and v < int(node["min"]):
                raise ValueError("不能小于 %s" % node["min"])
            if "max" in node and v > int(node["max"]):
                raise ValueError("不能大于 %s" % node["max"])
        elif t == "float":
            v = float(value)
            if "min" in node and v < float(node["min"]):
                raise ValueError("不能小于 %s" % node["min"])
            if "max" in node and v > float(node["max"]):
                raise ValueError("不能大于 %s" % node["max"])
        elif t == "list":
            if isinstance(value, (list, tuple)):
                v = list(value)
            elif isinstance(value, str):
                v = [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
            else:
                raise ValueError("类型应为列表")
        elif t == "object":
            v = value if isinstance(value, dict) else {}
        else:
            v = "" if value is None else str(value)
            opts = node.get("options") or node.get("enum")
            if opts and v not in [str(o) for o in opts]:
                raise ValueError("只能是 %s 之一" % "/".join(str(o) for o in opts))
        return v, None
    except Exception as e:
        return node.get("default"), "%s: %s(已回退默认值 %r)" % (label, e, node.get("default"))


def validate(schema: dict, values: dict, strict_groups: bool = False):
    """按 schema 校验/规整配置。返回 `(规整后的配置, 错误列表)`。

    规则:
      * 缺失的键 → 默认值
      * 非法值 → 回退默认值 + 记录一条人话错误(不抛异常, 启动不被配置拖死)
      * schema 未声明的键 → 保留(插件可能自己存状态), 但不做类型校验
    """
    values = values if isinstance(values, dict) else {}
    errors = []

    def walk_items(items: dict, src: dict, prefix: str) -> dict:
        out = {}
        for key, item in (items or {}).items():
            if not isinstance(item, dict):
                continue
            path = "%s.%s" % (prefix, key) if prefix else key
            if _is_group(item):
                sub = src.get(key)
                if not isinstance(sub, dict):
                    if sub is not None and strict_groups:
                        errors.append("%s: 类型应为对象(已回退默认值)" % path)
                    sub = {}
                out[key] = walk_items(item.get("items") or {}, sub, path)
                for k, v in sub.items():          # 组内未声明的键保留
                    if k not in out[key]:
                        out[key][k] = v
            else:
                raw = src[key] if key in src else item.get("default")
                v, err = _coerce(item, raw, path, errors)
                out[key] = v
                if err:
                    errors.append(err)
        return out

    clean = walk_items(schema or {}, values, "")
    for k, v in values.items():                   # 顶层未声明的键保留
        if k not in clean:
            clean[k] = v
    return clean, errors


# ------------------------------------------------------------------ 前端渲染
def flatten(schema: dict) -> list:
    """展平成表单字段: [{path, key, group, group_label, type, label, hint, default,
    value?, options, min, max, secret}]"""
    rows = []

    def walk(node: dict, prefix: str, group: str, group_label: str):
        for key, item in (node or {}).items():
            if not isinstance(item, dict):
                continue
            path = "%s.%s" % (prefix, key) if prefix else key
            if _is_group(item):
                walk(item.get("items") or {}, path,
                     group or path, item.get("description") or key)
            else:
                t = _norm_type(item.get("type"))
                rows.append({
                    "path": path, "key": key,
                    "group": group or "", "group_label": group_label or "",
                    "group_hint": "", "type": t,
                    "label": item.get("description") or key,
                    "hint": item.get("hint") or "",
                    "default": item.get("default"),
                    "options": item.get("options") or item.get("enum") or [],
                    "min": item.get("min"), "max": item.get("max"),
                    "secret": bool(item.get("secret")),
                    "placeholder": item.get("placeholder") or "",
                })
    walk(schema or {}, "", "", "")
    return rows
