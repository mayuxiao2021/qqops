"""媒体 —— NapCat 取直链 → 下载缓存 → 多模态识别(让"图片"能被读懂)。

实测: 该 DeepSeek 端点接受 `image_url` + base64 data URL, 能正确描述图片内容,
所以图片不再只是 `[图片]` 占位符, 而是带一句客观描述进入总结。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from . import core

MEDIA_DIR = os.path.join(core.DATA, "media")
ALLOW_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp")


def key_of(url: str) -> str:
    return hashlib.sha1(str(url).encode("utf-8", "replace")).hexdigest()[:24]


def ensure_dir() -> None:
    os.makedirs(MEDIA_DIR, exist_ok=True)


def safe_name(url: str, name: str = "") -> str:
    """保留中文等 Unicode 字符(只清掉路径分隔符与空白), 便于辨认缓存文件。"""
    base = os.path.basename(urllib.parse.urlparse(url).path) or "media"
    if name:
        base = name
    base = base.replace("/", "_").replace("\\", "_").strip() or "media"
    base = re.sub(r"[\s\x00-\x1f]+", "_", base)
    return base[:80]


def guess_mime(name: str, data: bytes = b"") -> str:
    e = (name or "").rsplit(".", 1)[-1].lower()
    m = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
         "webp": "image/webp", "bmp": "image/bmp"}
    if e in m:
        return m[e]
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


UA_QQ = "Mozilla/5.0 (Linux; Android 14; Pixel) AppleWebKit/537.36 QQ/9.0.0"
IMG_HEADERS = {"User-Agent": UA_QQ, "Referer": "https://qun.qq.com/",
               "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"}


def download(url: str, max_bytes: int = 3_000_000, timeout: int = 20,
             name: str = "", retry_https: bool = True) -> dict:
    """下载到本地缓存(限大小)。QQ CDN 需要 Referer/UA, 且部分旧链接走 https 才行。"""
    out = {"ok": False, "path": "", "size": 0, "mime": "", "error": "", "via": "direct"}
    for attempt in (0, 1) if retry_https else (0,):
        u = url.replace("http://", "https://", 1) if attempt else url
        try:
            req = urllib.request.Request(u, headers=IMG_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(max_bytes + 1)
                ctype = (r.headers.get("Content-Type") or "").split(";")[0]
            if len(data) > max_bytes:
                out["error"] = "超过大小上限 %.1f MB" % (max_bytes / 1048576)
                return out
            if not data:
                out["error"] = "空响应"
                return out
            mime = ctype if ctype.startswith("image/") else guess_mime(name or u, data)
            ensure_dir()
            p = os.path.join(MEDIA_DIR, "%s_%s" % (key_of(url), safe_name(u, name)))
            with open(p, "wb") as f:
                f.write(data)
            out.update({"ok": True, "path": p, "size": len(data), "mime": mime, "error": ""})
            return out
        except Exception as e:
            out["error"] = str(e)[:200]
    return out


def container_path(container_file: str) -> str:
    """把 NapCat(容器内)给的绝对路径映射成 Termux 可见路径。"""
    if not container_file or not container_file.startswith("/"):
        return ""
    return os.path.join(core._proot_rootfs(), container_file.lstrip("/"))


def find_in_napcat_cache(file_name: str, ts: int = 0, months: int = 3) -> str:
    """在 NapCat(容器内)的图片缓存里按文件名查找。

    NapCat 把图片存成小写 `md5.ext`, 而消息段里的 file 常是**大写**;
    且 Pic 目录按 `YYYY-MM` 分片 —— 所以优先查消息所在月份, 同名优先、前缀次之。
    """
    name = (file_name or "").strip().lower()
    if not name or "/" in name or len(name) < 5:
        return ""
    base = os.path.join(core._proot_rootfs(), "root", ".config", "QQ")
    if not os.path.isdir(base):
        return ""
    stem = os.path.splitext(name)[0]
    try:
        accounts = [a for a in os.listdir(base) if a.startswith("nt_qq_")]
    except Exception:
        return ""
    for acct in accounts:
        pic = os.path.join(base, acct, "nt_data", "Pic")
        if not os.path.isdir(pic):
            continue
        try:
            mons = sorted(os.listdir(pic), reverse=True)
        except Exception:
            continue
        if ts:
            want = time.strftime("%Y-%m", time.localtime(ts))
            if want in mons:
                mons.remove(want)
                mons.insert(0, want)
        for mon in mons[:months]:
            for root, _dirs, files in os.walk(os.path.join(pic, mon)):
                for f in files:
                    if f.lower() == name:
                        return os.path.join(root, f)
            for root, _dirs, files in os.walk(os.path.join(pic, mon)):
                for f in files:
                    if stem and f.lower().startswith(stem[:12]) and len(stem) >= 6:
                        return os.path.join(root, f)
    return ""


def fetch_media(nc, info: dict, group_id: int = 0, max_bytes: int = 3_000_000,
                kind: str = "image") -> dict:
    """四级取图/取文件: ① 直链(含 https 重试) ② NapCat 接口给出的地址或容器路径
       ③ 直接扫 NapCat 缓存目录(同名/前缀匹配) ④ 放弃并如实记录原因。"""
    out = {"ok": False, "path": "", "size": 0, "mime": "", "error": "", "via": ""}
    url = info.get("url") or ""
    if url:
        d = download(url, max_bytes, name=info.get("name") or "")
        if d["ok"]:
            d["via"] = "direct"
            return d
        out["error"] = d["error"]
    cont, new_url = "", ""
    if nc is not None:
        file_id = info.get("file") or info.get("file_id") or ""
        act = "get_image" if kind == "image" else "get_group_file_url"
        payload = {"file": file_id} if kind == "image" else {"group_id": int(group_id), "file_id": file_id}
        try:
            r = nc.call(act, payload)
            data = (r or {}).get("data")
            if isinstance(data, dict):
                cont = data.get("file") or data.get("path") or ""
                new_url = data.get("url") or ""
            elif isinstance(data, str):
                new_url = data
        except Exception as e:
            out["error"] = ("%s | napcat: %s" % (out["error"], str(e)[:80])).strip(" |")
        p = container_path(cont)
        if p and os.path.isfile(p):
            sz = os.path.getsize(p)
            if 0 < sz <= max_bytes:
                return {"ok": True, "path": p, "size": sz, "mime": guess_mime(p),
                        "error": "", "via": "napcat-cache"}
            out["error"] = "NapCat 缓存文件大小异常(%d 字节)" % sz
        if new_url and new_url != url:
            d = download(new_url, max_bytes, name=info.get("name") or "")
            if d["ok"]:
                d["via"] = "napcat-url"
                return d
            out["error"] = d["error"]
    if kind == "image":                       # ③ 直接扫缓存
        fname = info.get("file") or info.get("file_id") or ""
        found = find_in_napcat_cache(fname, int(info.get("ts") or 0))
        if found and os.path.isfile(found):
            sz = os.path.getsize(found)
            if 0 < sz <= max_bytes:
                return {"ok": True, "path": found, "size": sz, "mime": guess_mime(found),
                        "error": "", "via": "cache-scan"}
            out["error"] = "缓存扫描到文件但大小异常"
        elif not out["error"]:
            out["error"] = "NapCat 接口与缓存都没有这张图"
    return out


def caption(sc: dict, data: bytes, mime: str, hint: str = "", timeout: int = 90) -> tuple:
    """把图片交给多模态模型, 返回 (描述, usage)。"""
    if mime not in ALLOW_MIME:
        return ("", {})
    b64 = base64.b64encode(data).decode()
    ask = ("用一句中文客观描述这张图片: 有什么、是否是截图/文档/表格/通知; "
           "如果是截图且含文字, 摘出关键文字(不超过 80 字)。不要评价, 不要猜测用途。"
           + (("\n上下文提示: " + hint) if hint else ""))
    body = json.dumps({
        "model": sc.get("vision_model") or sc.get("model"),
        "max_tokens": 220,
        "temperature": 0.2,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": ask},
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
        ]}],
    }).encode("utf-8")
    req = urllib.request.Request(str(sc["base_url"]).rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + str(sc.get("api_key", ""))})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    txt = ((d.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
    return (re.sub(r"\s+", " ", txt).strip()[:300], (d.get("usage") or {}))


def from_segment(seg: dict) -> dict:
    """从 OneBot 消息段里取出媒体的 url / file / 名称 / 大小。"""
    if not isinstance(seg, dict):
        return {}
    d = seg.get("data") or {}
    if not isinstance(d, dict):
        return {}
    url = d.get("url") or ""
    if not url and str(d.get("file") or "").startswith("http"):
        url = d["file"]
    return {"type": seg.get("type") or "", "url": url, "file": d.get("file") or "",
            "file_id": d.get("file_id") or d.get("file") or "",
            "name": d.get("name") or d.get("file") or "",
            "size": int(d.get("file_size") or 0) if str(d.get("file_size") or "0").isdigit() else 0}


def napcat_media_url(nc: core.NapCat, seg_info: dict, group_id: int = 0) -> str:
    """消息段没带 URL 时, 向 NapCat 要直链(群文件 / 图片)。"""
    kind = seg_info.get("type")
    try:
        if kind == "file" and group_id:
            r = nc.call("get_group_file_url", {"group_id": int(group_id),
                                              "file_id": seg_info.get("file_id")})
            return ((r or {}).get("data") or {}).get("url") or ""
        if kind == "image" and seg_info.get("file"):
            r = nc.call("get_image", {"file": seg_info["file"]})
            if isinstance((r or {}).get("data"), dict):
                return (r["data"].get("url") or r["data"].get("file") or "")
            return ((r or {}).get("data") or "") if isinstance((r or {}).get("data"), str) else ""
    except Exception:
        return ""
    return ""
