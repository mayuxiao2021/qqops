"""链接抓取 —— 只取正文, 并且**默认拒绝一切内网/本机地址**(SSRF 防护)。

防护要点:
  * 仅允许 http/https;
  * 主机名命中黑名单(含 localhost / 内网网段 / file: / data:)直接拒绝;
  * **解析 DNS 后校验真实 IP**, 私有/回环/链路本地/保留地址一律拒绝(防 DNS 重绑定式绕过);
  * 每次重定向都重新校验, 限制跳转次数;
  * 限制响应体大小与超时, 只读 text/html 类内容。
"""
from __future__ import annotations

import gzip
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

from . import core

UA = "Mozilla/5.0 (Linux; Android 14) QQOps/2.0 (+local summarizer)"
SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "section", "article", "blockquote"}


def _host_ok(url: str) -> tuple:
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False, "URL 解析失败"
    if p.scheme not in ("http", "https"):
        return False, "只允许 http/https"
    host = (p.hostname or "").lower()
    if not host:
        return False, "缺少主机名"
    low = url.lower()
    for bad in core.LINK_BLOCK:
        if bad in low or host.startswith(bad.rstrip(".")):
            return False, "命中黑名单(%s)" % bad
    try:
        for info in socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80)):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return False, "解析到内网地址 %s" % ip
    except socket.gaierror:
        return False, "域名无法解析"
    except Exception as e:
        return False, "地址校验失败: %s" % e
    return True, ""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._buf = []
        self._meta_desc = ""

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            d = dict(attrs)
            if (d.get("name") or "").lower() in ("description", "og:description") and d.get("content"):
                self._meta_desc = self._meta_desc or d["content"].strip()
        elif tag in BLOCK_TAGS:
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in BLOCK_TAGS:
            self._buf.append("\n")

    def handle_data(self, data):
        if self._in_title and not self.title:      # title 在 <head> 里, 必须优先于跳过判断
            t = data.strip()
            if t:
                self.title = t[:200]
            return
        if self._skip:
            return
        t = data.strip()
        if not t:
            return
        self._buf.append(t + " ")

    def text(self) -> str:
        raw = "".join(self._buf)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n", raw)
        return raw.strip()


def html_to_text(html: str) -> tuple:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.title, (p.text() or p._meta_desc)


def fetch(url: str, timeout: int = 12, max_bytes: int = 512_000, max_redirects: int = 3) -> dict:
    """抓取并抽取正文。返回 {ok,url,final_url,status,title,text,bytes,error}。"""
    out = {"ok": False, "url": url, "final_url": url, "status": 0, "title": "", "text": "",
           "bytes": 0, "error": ""}
    cur, hops = url, 0
    while True:
        ok, why = _host_ok(cur)
        if not ok:
            out["error"] = why
            return out
        req = urllib.request.Request(cur, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8", "Accept-Encoding": "gzip, deflate"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                data = r.read(max_bytes + 1)
                out["status"] = r.status
                out["final_url"] = r.geturl()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and hops < max_redirects:
                loc = e.headers.get("Location")
                if not loc:
                    out["error"] = "重定向缺少 Location"
                    return out
                cur = urllib.parse.urljoin(cur, loc)
                hops += 1
                continue
            out["status"] = e.code
            out["error"] = "HTTP %s" % e.code
            return out
        except Exception as e:
            out["error"] = "%s" % e
            return out
        if enc == "gzip":
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        elif enc == "deflate":
            try:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            except Exception:
                pass
        if len(data) > max_bytes:
            data = data[:max_bytes]
        out["bytes"] = len(data)
        body = data.decode("utf-8", "replace")
        if "html" in ctype or "<html" in body[:400].lower():
            title, text = html_to_text(body)
            out["title"] = title
            out["text"] = (text or "")[:20000]
        elif any(k in ctype for k in ("text/", "json", "xml")):
            out["text"] = body[:20000]
        else:
            out["error"] = "不支持的内容类型: %s" % ctype
            return out
        out["ok"] = bool(out["text"])
        if not out["ok"]:
            out["error"] = "正文为空(可能需要登录或为纯前端页面)"
        return out


def extract_urls(text: str, limit: int = 50) -> list:
    """从消息文本里取候选链接。"""
    urls, seen = [], set()
    for m in re.findall(r"https?://[^\s\]\)\"'<>，。；、]+", text or ""):
        u = m.rstrip(".,;:!?，。；：！？）)")
        u = u.replace("&amp;", "&")
        if u in seen:
            continue
        seen.add(u)
        urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def interesting(url: str) -> bool:
    """过滤掉纯媒体/图床/表情资源, 只留"文章类"链接。"""
    low = url.lower()
    if any(k in low for k in ("multimedia.nt.qq.com.cn", "qpic.cn", "qlogo.cn", "gtimg.cn",
                              "photo.store.qq.com", "ugcimg.cn", ".jpg", ".jpeg", ".png",
                              ".gif", ".webp", ".mp4", ".mp3", ".apk", ".zip", ".exe")):
        return False
    return True


def domain_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).netloc or "")[:80]
    except Exception:
        return ""
