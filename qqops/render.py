"""报告渲染 —— Markdown / 自包含 HTML 报告页 / XLSX / DOCX。

约束: 零第三方依赖。OOXML(docx/xlsx)本质是 zip + XML, 用标准库 zipfile 手写即可,
Excel 与 Word 都能正常打开; 用 inlineStr 写字符串可省掉 sharedStrings 表。
"""
from __future__ import annotations

import io
import time
import zipfile

KIND_CN = {"deadline": "截止", "decision": "决定", "resource": "资源",
           "reminder": "提醒", "event": "事件", "todo": "待办"}
STATUS_CN = {"open": "进行中", "done": "已完成", "dropped": "已忽略"}


def fmt_ts(ts, f: str = "%m-%d %H:%M") -> str:
    try:
        return time.strftime(f, time.localtime(int(ts)))
    except Exception:
        return "-"


def fmt_due(ts) -> str:
    if not ts:
        return ""
    left = int(ts) - int(time.time())
    tag = ""
    if left < 0:
        tag = "已过期"
    elif left < 86400:
        tag = "今天"
    elif left < 2 * 86400:
        tag = "明天"
    elif left < 7 * 86400:
        tag = "%d 天后" % (left // 86400)
    return "%s%s" % (fmt_ts(ts, "%m-%d %H:%M"), ("（%s）" % tag) if tag else "")


def xml_escape(s) -> str:
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


# ---------------------------------------------------------------- Markdown
def report_md(meta: dict, sections: list) -> str:
    out = ["# 群聊整理 · %s" % meta.get("stamp", ""), ""]
    out.append("- 窗口: 最近 %s 小时 | 覆盖 %d 个群 | 生成: %s"
               % (meta.get("hours"), len(sections), fmt_ts(meta.get("generated"))))
    out.append("- 新事件: %d | 新待办: %d | 留存链接: %d"
               % (meta.get("n_events", 0), meta.get("n_todos", 0), meta.get("n_links", 0)))
    out.append("")
    for s in sections:
        out.append("---")
        out.append("")
        out.append("## %s (`%s`)" % (s.get("name") or s.get("group_id"), s.get("group_id")))
        out.append("")
        if s.get("error"):
            out.append("> ⚠️ %s" % s["error"])
            out.append("")
        if s.get("overview"):
            out.append("**概览**: %s" % s["overview"])
            out.append("")
        out.append("### 统计")
        out.append(s.get("stats") or "-")
        out.append("")
        if s.get("topics"):
            out.append("### 主要话题")
            for t in s["topics"]:
                who = ("（%s）" % "、".join(t.get("participants") or [])) if t.get("participants") else ""
                out.append("- **%s**%s: %s" % (t.get("title"), who, t.get("summary", "")))
            out.append("")
        if s.get("events"):
            out.append("### 重要事件")
            for e in s["events"]:
                due = (" · 截止 %s" % fmt_due(e.get("due_at"))) if e.get("due_at") else ""
                out.append("- `[%s]` **%s**%s%s" % (KIND_CN.get(e.get("kind"), e.get("kind")),
                                                   e.get("title"), due,
                                                   ("\n  - %s" % e["detail"]) if e.get("detail") else ""))
            out.append("")
        if s.get("todos"):
            out.append("### 待办与跟进")
            for t in s["todos"]:
                due = (" · 截止 %s" % fmt_due(t.get("due_at"))) if t.get("due_at") else ""
                who = ("@%s " % t["who"]) if t.get("who") else ""
                out.append("- %s%s%s" % (who, t.get("what"), due))
            out.append("")
        if s.get("links"):
            out.append("### 链接")
            for l in s["links"]:
                out.append("- [%s](%s)%s" % (l.get("title") or l.get("url"), l.get("url"),
                                             (" — " + l["why"]) if l.get("why") else ""))
            out.append("")
        if s.get("facts"):
            out.append("### 值得记住的事实")
            for f in s["facts"]:
                out.append("- %s" % f)
            out.append("")
        if s.get("memory"):
            out.append("### 该群记忆(下次沿用)")
            out.append("> %s" % s["memory"].replace("\n", "\n> "))
            out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- HTML 报告页
HTML_CSS = """
:root{--bg:#0b0d12;--card:#141922;--line:#232a36;--fg:#e8ecf3;--dim:#8b95a7;--acc:#4c8dff;--ok:#34d399;--warn:#fbbf24;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans CJK SC","PingFang SC",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:19px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:15px;margin:18px 0 8px;color:var(--acc)}
.meta{color:var(--dim);font-size:13px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:10px 0}
.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;min-width:120px}
.kpi b{display:block;font-size:20px}.kpi span{color:var(--dim);font-size:12px}
.tag{display:inline-block;padding:1px 8px;border-radius:99px;background:#1b2230;color:var(--dim);font-size:12px;margin-right:6px}
.tag.due{background:rgba(251,191,36,.16);color:var(--warn)}
.tag.over{background:rgba(248,113,113,.16);color:var(--bad)}
.tag.ok{background:rgba(52,211,153,.14);color:var(--ok)}
ul{margin:6px 0 6px 20px;padding:0}li{margin:3px 0}
a{color:var(--acc);text-decoration:none;word-break:break-all}
pre{background:#0a0d12;border:1px solid var(--line);border-radius:10px;padding:10px;overflow:auto;font-size:12.5px;white-space:pre-wrap}
blockquote{margin:6px 0;padding:8px 12px;border-left:3px solid var(--acc);background:#101620;border-radius:0 10px 10px 0;color:#cfd7e5}
.toc{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0 0}
.toc a{background:#141a24;border:1px solid var(--line);border-radius:99px;padding:4px 12px;font-size:13px}
footer{margin-top:40px;color:var(--dim);font-size:12px;text-align:center}
"""


def report_html(meta: dict, sections: list, profile: str = "") -> str:
    """生成自包含 HTML 报告页(样式内联, 单文件可直接发/存档)。"""
    e = xml_escape
    parts = ["<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
             "<title>群聊整理 %s</title><style>%s</style></head><body><div class=\"wrap\">"
             % (e(meta.get("stamp")), HTML_CSS)]
    parts.append("<h1>群聊整理 · %s</h1>" % e(meta.get("stamp")))
    parts.append("<div class=\"meta\">窗口 %s 小时 · 覆盖 %d 个群 · 生成 %s</div>"
                 % (e(meta.get("hours")), len(sections), e(fmt_ts(meta.get("generated")))))
    parts.append("<div class=\"kpis\">"
                 "<div class=\"kpi\"><b>%d</b><span>新事件</span></div>"
                 "<div class=\"kpi\"><b>%d</b><span>新待办</span></div>"
                 "<div class=\"kpi\"><b>%d</b><span>链接留存</span></div>"
                 "<div class=\"kpi\"><b>%d</b><span>覆盖群数</span></div></div>"
                 % (meta.get("n_events", 0), meta.get("n_todos", 0),
                    meta.get("n_links", 0), len(sections)))
    if meta.get("events_open") is not None:
        parts.append("<div class=\"card\"><b>待办总览</b>: 进行中 %d · 7 天内到期 %d · 已过期 %d"
                     % (meta.get("events_open", 0), meta.get("due_7d", 0), meta.get("overdue", 0)))
        if meta.get("due_list"):
            parts.append("<ul>" + "".join(
                "<li>%s <b>%s</b>%s</li>" % ("" if not d.get("due_at") else
                                             "<span class=\"tag %s\">%s</span>" % (
                                                 "over" if d["due_at"] < time.time() else "due",
                                                 e(fmt_due(d["due_at"]))),
                                             e(d.get("title")), "")
                for d in meta["due_list"][:12]) + "</ul>")
        parts.append("</div>")
    if profile:
        parts.append("<div class=\"card\"><b>个人档案</b><pre>%s</pre></div>" % e(profile[:1500]))
    if sections:
        parts.append("<div class=\"toc\">" + "".join(
            "<a href=\"#g%s\">%s</a>" % (e(s.get("group_id")), e(s.get("name") or s.get("group_id")))
            for s in sections) + "</div>")
    for s in sections:
        parts.append("<h2 id=\"g%s\">%s <span class=\"tag\">%s</span></h2>"
                     % (e(s.get("group_id")), e(s.get("name") or s.get("group_id")), e(s.get("group_id"))))
        if s.get("error"):
            parts.append("<div class=\"card\">⚠️ %s</div>" % e(s["error"]))
        if s.get("overview"):
            parts.append("<div class=\"card\"><b>概览</b><br>%s</div>" % e(s["overview"]))
        if s.get("stats"):
            parts.append("<pre>%s</pre>" % e(s["stats"]))
        if s.get("events"):
            parts.append("<h3>重要事件</h3><ul>")
            for ev in s["events"]:
                due = ("<span class=\"tag %s\">%s</span>" % ("over" if ev.get("due_at", 0) and ev["due_at"] < time.time() else "due",
                                                             e(fmt_due(ev["due_at"])))) if ev.get("due_at") else ""
                parts.append("<li><span class=\"tag\">%s</span>%s <b>%s</b>%s</li>"
                             % (e(KIND_CN.get(ev.get("kind"), ev.get("kind"))), due,
                                e(ev.get("title")),
                                ("<br><span class=\"meta\">%s</span>" % e(ev["detail"])) if ev.get("detail") else ""))
            parts.append("</ul>")
        if s.get("todos"):
            parts.append("<h3>待办与跟进</h3><ul>" + "".join(
                "<li>%s%s%s</li>" % (("@%s " % e(t["who"])) if t.get("who") else "", e(t.get("what")),
                                     (" <span class=\"tag due\">%s</span>" % e(fmt_due(t["due_at"]))) if t.get("due_at") else "")
                for t in s["todos"]) + "</ul>")
        if s.get("topics"):
            parts.append("<h3>主要话题</h3><ul>" + "".join(
                "<li><b>%s</b>: %s</li>" % (e(t.get("title")), e(t.get("summary"))) for t in s["topics"]) + "</ul>")
        if s.get("links"):
            parts.append("<h3>链接</h3><ul>" + "".join(
                "<li><a href=\"%s\" target=\"_blank\" rel=\"noreferrer\">%s</a>%s</li>"
                % (e(l.get("url")), e(l.get("title") or l.get("url")),
                   (" — " + e(l["why"])) if l.get("why") else "") for l in s["links"]) + "</ul>")
        if s.get("facts"):
            parts.append("<h3>值得记住的事实</h3><ul>" + "".join("<li>%s</li>" % e(f) for f in s["facts"]) + "</ul>")
        if s.get("memory"):
            parts.append("<h3>该群记忆(下次沿用)</h3><blockquote>%s</blockquote>" % e(s["memory"]))
    parts.append("<footer>QQOps 生成 · 数据仅存于本机</footer></div></body></html>")
    return "".join(parts)


# ---------------------------------------------------------------- XLSX(手写 OOXML)
def _sheet_xml(rows: list) -> str:
    def cell(r, c, v):
        ref = "%s%d" % (chr(65 + c) if c < 26 else "A" + chr(65 + c - 26), r)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return '<c r="%s"><v>%s</v></c>' % (ref, v)
        return '<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, xml_escape(v))
    body = "".join("<row r=\"%d\">%s</row>" % (i + 1, "".join(cell(i + 1, j, v) for j, v in enumerate(row)))
                   for i, row in enumerate(rows))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>%s</sheetData></worksheet>' % body)


def xlsx_bytes(rows: list, sheet_name: str = "Sheet1") -> bytes:
    """把二维表写成最小可用的 .xlsx(Excel / WPS / LibreOffice 均可打开)。"""
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>' % xml_escape(sheet_name)[:31])
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
               '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(rows))
    return buf.getvalue()


def events_rows(events: list) -> list:
    head = ["状态", "类型", "标题", "详情", "相关人", "群", "截止", "优先级", "置信度", "首次出现", "最近出现"]
    rows = [head]
    for e in events:
        rows.append([STATUS_CN.get(e.get("status"), e.get("status") or ""),
                     KIND_CN.get(e.get("kind"), e.get("kind") or ""),
                     e.get("title") or "", (e.get("detail") or "")[:300], e.get("who") or "",
                     e.get("group_name") or e.get("group_id") or "",
                     fmt_due(e.get("due_at")) if e.get("due_at") else "",
                     e.get("priority") or 2, round(float(e.get("confidence") or 0), 2),
                     fmt_ts(e.get("first_seen")), fmt_ts(e.get("last_seen"))])
    return rows


def links_rows(links: list) -> list:
    rows = [["标题", "链接", "域名", "群", "状态", "首次出现", "摘要"]]
    for l in links:
        rows.append([l.get("title") or "", l.get("url") or "", l.get("domain") or "",
                     l.get("group_id") or "", l.get("status") or "",
                     fmt_ts(l.get("first_seen")), (l.get("excerpt") or l.get("error") or "")[:300]])
    return rows


def xlsx_multi(sheets: dict, name: str = "QQOps") -> bytes:
    """多工作表 xlsx:{表名: 二维数组}。仍为零依赖手写 OOXML。"""
    names = list(sheets.keys())[:8]
    ct_parts = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
                '<Default Extension="xml" ContentType="application/xml"/>',
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    for i in range(len(names)):
        ct_parts.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">%s</Types>'
          % "".join(ct_parts))
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>')
    sh = "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
                 % (xml_escape(n)[:31], i + 1, i + 1) for i, n in enumerate(names))
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          '<sheets>%s</sheets></workbook>' % sh)
    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
                         for i in range(len(names)))
               + '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        for i, n in enumerate(names):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), _sheet_xml(sheets[n]))
    return buf.getvalue()


def md_to_docx_blocks(md: str) -> list:
    """把 Markdown 报告转成 DOCX 段落块(够用即可: 标题/列表/引用/正文)。"""
    blocks = []
    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            blocks.append(("h1", line[2:].strip()))
        elif line.startswith("## "):
            blocks.append(("h2", line[3:].strip()))
        elif line.startswith("### "):
            blocks.append(("h2", line[4:].strip()))
        elif line.strip() == "---":
            blocks.append(("p", "——————"))
        elif line.startswith("> "):
            blocks.append(("quote", line[2:].strip()))
        elif line.lstrip().startswith("- "):
            indent = "  " * ((len(line) - len(line.lstrip())) // 2)
            blocks.append(("bullet", indent + line.lstrip()[2:].replace("**", "")))
        else:
            blocks.append(("p", line.replace("**", "")))
    return blocks


# ---------------------------------------------------------------- DOCX(手写 OOXML)
def docx_bytes(blocks: list, title: str = "") -> bytes:
    """把 [(kind, text)] 写成最小可用 .docx。kind: h1/h2/p/bullet/quote。"""
    def run(text, bold=False, size=None, color=None):
        rpr = "<w:rPr>%s%s%s</w:rPr>" % ("<w:b/>" if bold else "",
                                         ("<w:sz w:val=\"%d\"/>" % size) if size else "",
                                         ("<w:color w:val=\"%s\"/>" % color) if color else "")
        return ('<w:r>%s<w:t xml:space="preserve">%s</w:t></w:r>' % (rpr, xml_escape(text)))
    paras = []
    for kind, text in blocks:
        if kind == "h1":
            paras.append('<w:p><w:pPr><w:spacing w:before="240" w:after="120"/></w:pPr>%s</w:p>'
                         % run(text, True, 36))
        elif kind == "h2":
            paras.append('<w:p><w:pPr><w:spacing w:before="180" w:after="80"/></w:pPr>%s</w:p>'
                         % run(text, True, 28))
        elif kind == "bullet":
            paras.append('<w:p><w:pPr><w:ind w:left="360"/></w:pPr>%s</w:p>' % run("• " + text))
        elif kind == "quote":
            paras.append('<w:p><w:pPr><w:ind w:left="360"/></w:pPr>%s</w:p>' % run(text, False, None, "555555"))
        else:
            paras.append("<w:p>%s</w:p>" % run(text))
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
           '</w:body></w:document>' % "".join(paras))
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def digest_text(meta: dict, sections: list, max_chars: int = 1100) -> str:
    """推送给手机的摘要(只讲与我有关的), 不是全文报告。"""
    lines = ["【今日整理 %s】" % meta.get("stamp", "")]
    due = []
    for s in sections:
        for e in (s.get("events") or []):
            if e.get("relevant", True) and e.get("due_at"):
                due.append(e)
    due.sort(key=lambda x: x.get("due_at") or 0)
    if due:
        lines.append("⏰ 有截止的 %d 项:" % len(due))
        for e in due[:6]:
            lines.append("· %s — %s" % (e.get("title"), fmt_due(e.get("due_at"))))
    hot = [e for s in sections for e in (s.get("events") or [])
           if e.get("relevant", True) and not e.get("due_at") and (e.get("priority") or 2) <= 2]
    if hot:
        lines.append("📌 重要事件 %d 条:" % len(hot))
        for e in hot[:6]:
            lines.append("· [%s] %s" % (KIND_CN.get(e.get("kind"), e.get("kind")), e.get("title")))
    todos = [t for s in sections for t in (s.get("todos") or [])]
    if todos:
        lines.append("✅ 待办 %d 条:" % len(todos))
        for t in todos[:6]:
            lines.append("· %s%s" % (t.get("what"), (" (%s)" % fmt_due(t["due_at"])) if t.get("due_at") else ""))
    for s in sections:
        if s.get("overview"):
            lines.append("💬 %s: %s" % (s.get("name") or s.get("group_id"), s["overview"]))
    links = [l for s in sections for l in (s.get("links") or [])]
    if links:
        lines.append("🔗 链接 %d 个" % len(links))
        for l in links[:3]:
            lines.append("· %s %s" % (l.get("title") or "", l.get("url")))
    txt = "\n".join(lines)
    return txt[:max_chars]
