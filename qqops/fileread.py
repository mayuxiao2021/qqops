"""文件解析 —— 群里发来的文档直接读正文进总结。

零第三方依赖可解析: txt/md/csv/json/log/代码、docx、xlsx、pptx(zip + XML)。
PDF 需要 pypdf(可选安装); 装了就用, 没装就明确报告"未安装"而不是静默失败。
图片交给 media.py 走多模态识别。
"""
from __future__ import annotations

import io
import json
import re
import zipfile

MAX_UNZIP = 40 * 1024 * 1024        # 解压总量上限(防 zip bomb)
TEXT_EXT = {"txt", "md", "markdown", "csv", "tsv", "json", "log", "ini", "conf", "yaml", "yml",
            "py", "js", "ts", "java", "c", "cpp", "h", "go", "rs", "sh", "sql", "html", "xml"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic"}
PDF_EXT = {"pdf"}


def ext_of(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def kind_of(name: str) -> str:
    e = ext_of(name)
    if e in IMAGE_EXT:
        return "image"
    if e == "docx":
        return "docx"
    if e in ("xlsx", "xlsm"):
        return "xlsx"
    if e == "pptx":
        return "pptx"
    if e in PDF_EXT:
        return "pdf"
    if e in TEXT_EXT:
        return "text"
    return "other"


def _safe_unzip(data: bytes) -> zipfile.ZipFile:
    z = zipfile.ZipFile(io.BytesIO(data))
    total = sum(i.file_size for i in z.infolist())
    if total > MAX_UNZIP:
        raise ValueError("压缩包解压后过大(%.1f MB)" % (total / 1048576))
    return z


def _strip_xml(xml: str) -> str:
    xml = re.sub(r"<w:p[ >]", "\n<w:p ", xml)          # 段落换行
    xml = re.sub(r"<a:p[ >]", "\n<a:p ", xml)
    xml = re.sub(r"</a:t>", " ", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    xml = xml.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", xml)).strip()


def _docx(data: bytes, max_chars: int) -> str:
    with _safe_unzip(data) as z:
        parts = [n for n in z.namelist() if n.startswith("word/") and n.endswith(".xml")]
        main = "word/document.xml"
        text = _strip_xml(z.read(main).decode("utf-8", "replace")) if main in parts else ""
        if not text:                                     # 退化: 拼所有 word/*.xml
            text = "\n".join(_strip_xml(z.read(n).decode("utf-8", "replace")) for n in parts[:20])
        return text[:max_chars]


def _pptx(data: bytes, max_chars: int) -> str:
    with _safe_unzip(data) as z:
        slides = sorted([n for n in z.namelist()
                         if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                        key=lambda n: int(re.findall(r"\d+", n)[0]))
        out = []
        for i, n in enumerate(slides[:40], 1):
            t = _strip_xml(z.read(n).decode("utf-8", "replace"))
            if t:
                out.append("【第 %d 页】\n%s" % (i, t))
        return "\n\n".join(out)[:max_chars]


def _xlsx(data: bytes, max_chars: int) -> str:
    with _safe_unzip(data) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
            for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
                shared.append(re.sub(r"<[^>]+>", "", si).strip())
        names = sorted([n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)])
        out = []
        for n in names[:6]:
            sheet = z.read(n).decode("utf-8", "replace")
            out.append("【%s】" % n.split("/")[-1].replace(".xml", ""))
            rows = re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S)
            for r in rows[:300]:
                cells = []
                for m in re.finditer(r"<c\b([^>]*)>(.*?)</c>", r, re.S):
                    attrs, body = m.group(1), m.group(2)
                    v = re.search(r"<v>(.*?)</v>", body, re.S)
                    t = re.search(r"<t[^>]*>(.*?)</t>", body, re.S)
                    if 't="s"' in attrs and v:
                        try:
                            cells.append(shared[int(v.group(1))])
                            continue
                        except Exception:
                            pass
                    cells.append(re.sub(r"<[^>]+>", "", (t or v).group(1)) if (t or v) else "")
                line = " | ".join(x.strip() for x in cells if x.strip())
                if line:
                    out.append(line)
            out.append("")
        return "\n".join(out)[:max_chars]


def _pdf(data: bytes, max_chars: int) -> str:
    try:
        from pypdf import PdfReader            # 可选依赖
    except Exception:
        try:
            from PyPDF2 import PdfReader       # 兼容旧包名
        except Exception:
            raise ValueError("未安装 pypdf(可选依赖), 无法解析 PDF; 可执行: pip install pypdf")
    r = PdfReader(io.BytesIO(data))
    out = []
    for p in r.pages[:40]:
        try:
            out.append(p.extract_text() or "")
        except Exception:
            continue
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))[:max_chars]


def read_bytes(data: bytes, name: str, max_chars: int = 4000) -> dict:
    """解析文件字节流。返回 {kind, text, error}。"""
    kind = kind_of(name)
    out = {"kind": kind, "text": "", "error": "", "name": name, "size": len(data)}
    try:
        if kind == "text":
            for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
                try:
                    out["text"] = data.decode(enc)[:max_chars]
                    break
                except Exception:
                    continue
        elif kind == "docx":
            out["text"] = _docx(data, max_chars)
        elif kind == "xlsx":
            out["text"] = _xlsx(data, max_chars)
        elif kind == "pptx":
            out["text"] = _pptx(data, max_chars)
        elif kind == "pdf":
            out["text"] = _pdf(data, max_chars)
        elif kind == "image":
            out["error"] = "图片请走多模态识别(media.caption)"
        else:
            out["error"] = "不支持的文件类型 .%s" % ext_of(name)
    except Exception as e:
        out["error"] = str(e)[:200]
    if not out["text"] and not out["error"]:
        out["error"] = "解析结果为空"
    return out
