/* QQQps 控制台前端 —— 零依赖, 原生 ES6 */
'use strict';
const VER = window.QQOPS.version, REFRESH = Math.max(5, window.QQOPS.refresh || 15);
let timer = null, currentView = 'overview', busy = false;

/* ------------------------------------------------------------------ 基础 */
/* 客户端错误上报: 任何异常都写进服务端日志, 便于远程定位(前端问题最难隔着屏幕查) */
function report(kind, msg, stack) {
  try {
    fetch('/api/clientlog', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        kind, msg: String(msg == null ? '' : msg).slice(0, 400),
        stack: String(stack == null ? '' : stack).slice(0, 3000),
        url: location.href.split('?')[0] + (location.hash || ''),
        ua: navigator.userAgent,
      }),
    }).catch(() => {});
  } catch (e) { /* 上报本身失败就算了 */ }
}
window.addEventListener('error', e => report('error', e.message, e.error && e.error.stack));
window.addEventListener('unhandledrejection', e => report('promise',
  (e.reason && e.reason.message) || e.reason, e.reason && e.reason.stack));

/* 元素缺失时返回哑元素, 避免绑定事件时抛错导致整页渲染失败 */
const DUMMY = {
  value: '', checked: false, textContent: '', innerHTML: '', className: '', scrollTop: 0, scrollHeight: 0,
  dataset: {}, style: {}, classList: {add() {}, remove() {}, toggle() {}, contains: () => false},
  closest: () => DUMMY, appendChild() {}, remove() {}, focus() {}, click() {},
  addEventListener() {}, querySelector: () => DUMMY, querySelectorAll: () => [],
};
function q(root, sel) { try { return (root && root.querySelector(sel)) || DUMMY; } catch (e) { report('selector', sel + ': ' + e.message); return DUMMY; } }

/* 令牌: 优先用 URL 里的 ?token=(存 sessionStorage), 再退回 Cookie。
   这样即使用户浏览器禁用/丢失 Cookie, 控制台依然可用 —— 否则 /api/* 会被 302 到登录页,
   前端拿到 HTML 再 JSON.parse 失败, 表现为"渲染失败"。 */
const URL_TOKEN = (function () {
  try {
    const t = new URLSearchParams(location.search).get('token');
    if (t) { sessionStorage.setItem('qqops_token', t); return t; }
    return sessionStorage.getItem('qqops_token') || '';
  } catch (e) { return ''; }
})();

async function api(path, opts) {
  let r;
  const o = Object.assign({headers: {'Content-Type': 'application/json'}}, opts || {});
  if (URL_TOKEN) o.headers['X-Token'] = URL_TOKEN;
  try {
    r = await fetch(path, o);
  } catch (e) {
    report('fetch', path + ' -> ' + e.message);
    toast('请求失败: ' + path, 'bad');
    return null;
  }
  if (r.status === 401 || (r.redirected && /\/login/.test(r.url))) {
    report('auth', path + ' 未通过鉴权 (status=' + r.status + ', redirected=' + r.redirected + ')');
    location.href = '/login';
    return null;
  }
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('json')) {
    report('nonjson', path + ' content-type=' + ct + ' status=' + r.status);
    location.href = '/login';
    return null;
  }
  const txt = await r.text();
  let data = {};
  try { data = txt ? JSON.parse(txt) : {}; } catch (e) {
    report('badjson', path + ' <- ' + txt.slice(0, 200));
    return null;
  }
  if (!r.ok && data.error) toast(data.error, 'bad');
  return data;
}
function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || '');
  el.textContent = msg;
  q(document, '#toasts').appendChild(el);
  setTimeout(() => el.remove(), 4200);
}
/* 数据没拿到时不留白(设计规范: 失败只降级, 不白屏)。
   会话过期/服务刚重启/接口报错都会走到这里, 复用 errState 的错误卡片 + 一键重试。 */
function failCard(v, msg) {
  v.innerHTML = errState(msg || '接口没有返回数据',
    '常见原因: 令牌/会话过期(用带 ?token= 的地址重新打开) · 服务刚重启(等几秒再重试) · 接口报错(运维 → 日志里有对应记录)。');
  wireRetry(v, () => { try { render(); } catch (e) { report('retry', e.message); } });
  return null;
}
function modal(title, html) {
  q(document, '#modal-title').textContent = title;
  q(document, '#modal-body').innerHTML = html;
  q(document, '#modal').classList.remove('hidden');
}
function closeModal() { q(document, '#modal').classList.add('hidden'); }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
/* 命中词高亮(先转义再按词切分, 不引入 HTML 注入) */
function hl(text, q) {
  let out = esc(text);
  if (!q) return out;
  for (const tok of (String(q).match(/[\u3400-\u4dbf\u4e00-\u9fff]+|[A-Za-z0-9_]+/g) || [])) {
    const t = esc(tok);
    if (t) out = out.split(t).join('<mark>' + t + '</mark>');
  }
  return out;
}
function lsGet(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式下可能被拒 */ } }
function fmtTs(t, f) { const d = new Date((t || 0) * 1000); if (!t) return '-'; const p = n => String(n).padStart(2, '0');
  return f === 'time' ? `${p(d.getHours())}:${p(d.getMinutes())}` : `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`; }
function humanBytes(n) { const u = ['B','KB','MB','GB','TB']; let i = 0; n = n || 0; while (n >= 1024 && i < u.length-1) { n /= 1024; i++; } return n.toFixed(n < 10 && i > 0 ? 1 : 0) + ' ' + u[i]; }
function dur(a, b) { if (!a || !b) return '-'; const s = Math.max(0, b - a); return s < 60 ? s + 's' : Math.floor(s/60) + 'm' + (s%60) + 's'; }
function md(text) {
  const lines = esc(text || '').split('\n'); let out = [], inUl = false;
  for (const ln of lines) {
    const b = ln.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`(.+?)`/g, '<code>$1</code>');
    if (/^#{1,6} /.test(ln)) { const n = ln.match(/^#+/)[0].length; if (inUl) { out.push('</ul>'); inUl = false; } out.push(`<h${n}>${esc(ln.slice(n+1))}</h${n}>`); }
    else if (/^\s*[-*] /.test(ln)) { if (!inUl) { out.push('<ul>'); inUl = true; } out.push('<li>' + b.replace(/^\s*[-*] /, '') + '</li>'); }
    else if (/^---+$/.test(ln.trim())) { if (inUl) { out.push('</ul>'); inUl = false; } out.push('<hr>'); }
    else if (!ln.trim()) { if (inUl) { out.push('</ul>'); inUl = false; } }
    else { if (inUl) { out.push('</ul>'); inUl = false; } out.push('<p>' + b + '</p>'); }
  }
  if (inUl) out.push('</ul>');
  return out.join('');
}
function sparkline(series, w = 320, h = 46) {
  if (!series || !series.length) return '';
  const max = Math.max(1, ...series), step = w / Math.max(1, series.length - 1);
  const pts = series.map((v, i) => `${(i*step).toFixed(1)},${(h - 4 - (v/max)*(h-10)).toFixed(1)}`).join(' ');
  const area = `0,${h} ${pts} ${w},${h}`;
  return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <polyline points="${pts}" fill="none" stroke="#4c8dff" stroke-width="2"/>
    <polygon points="${area}" fill="url(#g)" opacity=".22"/>
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#4c8dff"/><stop offset="100%" stop-color="#4c8dff" stop-opacity="0"/></linearGradient></defs></svg>`;
}
function svcPill(s) { return `<span class="pill ${s.running ? 'ok' : 'bad'}"><span class="dot ${s.running ? 'ok' : 'bad'}"></span>${s.running ? '运行中' : '已停止'}</span>`; }

/* ------------------------------------------------------------------ 路由 */
/* 10 个一级页面: 仪表盘 / 对话 / 消息 / 总结 / 任务 / 插件 / 数据 / 运维 / 设置 / 帮助 */
const NAV = [
  ['dashboard', '仪表盘', '📊'], ['chat', '对话', '💬'], ['messages', '消息', '🗂'],
  ['reports', '总结', '📝'], ['tasks', '任务', '⏱'], ['plugins', '插件', '🧩'],
  ['data', '数据', '🗃'], ['ops', '运维', '🛠'], ['settings', '设置', '⚙'], ['help', '帮助', '❓'],
];
/* 手机底部标签栏: 5 个高频页 + "更多" */
const BOTTOM = ['dashboard', 'chat', 'messages', 'reports', 'ops'];
function dueTag(ts) {
  if (!ts) return '';
  const left = ts - Date.now() / 1000;
  let cls = 'pill', tag = fmtTs(ts, 'time');
  if (left < 0) { cls = 'pill bad'; tag += ' 已过期'; }
  else if (left < 86400) { cls = 'pill warn'; tag += ' 今天'; }
  else if (left < 2 * 86400) { cls = 'pill warn'; tag += ' 明天'; }
  else if (left < 7 * 86400) { cls = 'pill warn'; tag += ' ' + Math.floor(left / 86400) + '天后'; }
  return `<span class="${cls}">${tag}</span>`;
}
const KIND_CN = {deadline: '截止', decision: '决定', resource: '资源', reminder: '提醒', event: '事件', todo: '待办'};
/* ---------------- 中文映射层: 界面尽量全中文, 专有名词(QQ/NapCat/AstrBot)保留 ---------------- */
const SVC_CN = {collector: '采集器', scheduler: '调度器', web: '控制台', napcat: 'NapCat', astrbot: 'AstrBot'};
const MSGKIND_CN = {text: '文本', image: '图片', reply: '回复', at: '@某人', face: '表情',
  json: '卡片', file: '文件', record: '语音', voice: '语音', video: '视频', forward: '转发',
  mface: '表情', marketface: '表情', music: '音乐', node: '合并转发'};
const RUN_CN = {ok: '成功', partial: '部分成功', failed: '失败', empty: '无数据', running: '进行中'};
const STATUS_CN = {open: '进行中', done: '已完成', dropped: '已忽略'};
const svcName = (n) => SVC_CN[n] || n;
const kindName = (k) => MSGKIND_CN[k] || k;
const kindsText = (s) => String(s || '').split(',').filter(Boolean).map(kindName).join('·') || '—';
const runName = (s) => RUN_CN[s] || s || '';
const jobName = (j) => ({manual: '手动'}[j]) || String(j || '').replace(/^schedule:/, '定时 ');
const statusName = (s) => STATUS_CN[s] || s || '';
function buildNav() {
  q(document, '#nav').innerHTML = NAV.map(([k, t, i]) =>
    `<a href="#/${k}" data-k="${k}"><span class="ico">${i}</span>${t}</a>`).join('');
  const bn = q(document, '#bottomnav');
  if (bn) {
    bn.innerHTML = BOTTOM.map(k => {
      const n = NAV.find(x => x[0] === k) || [k, k, ''];
      return `<a href="#/${k}" data-k="${k}"><span class="ico">${n[2]}</span>${n[1]}</a>`;
    }).join('') + `<a href="#" id="bn-more" data-more="1"><span class="ico">⋯</span>更多</a>`;
  }
}
function markNav(k) {
  document.querySelectorAll('#nav a,#bottomnav a').forEach(a =>
    a.classList.toggle('on', a.dataset.k === k));
}
function go(k) {
  currentView = k;
  q(document, '#page-title').textContent = (NAV.find(n => n[0] === k) || [, k])[1];
  markNav(k);
  q(document, '#sidebar').classList.remove('open');
  render();
}
async function render() {
  const v = q(document, '#view');
  if (busy) return;
  busy = true;
  if (logTimer) { clearInterval(logTimer); logTimer = null; }   /* 离开日志页就停掉轮询 */
  v.innerHTML = '<div class="card"><div class="skel" style="width:40%"></div><div class="skel" style="margin-top:10px"></div><div class="skel" style="margin-top:10px;width:70%"></div></div>';
  try { await (VIEWS[currentView] || VIEWS.dashboard)(v); }
  catch (e) {
    report('render', currentView + ': ' + (e && e.message), e && e.stack);
    v.innerHTML = `<div class="card"><div class="row"><b class="err">渲染失败: ${esc(currentView)}</b>
      <span class="muted">已自动上报到服务端 data/logs/client.log</span>
      <button class="right" onclick="location.reload()">重新加载</button>
      <button onclick="location.hash='#/overview'">回到概览</button></div>
      <pre>${esc(e && e.stack || e)}</pre></div>`;
  }
  finally { busy = false; }
}

/* ------------------------------------------------------------------ 视图注册表 */
const VIEWS = {};

/* ------------------------------------------------------- 通用组件(三态 + 卡片) */
function card(title, body, sub, right) {
  return `<div class="card">${title ? `<h3>${esc(title)}${sub ? ` <span class="muted">${esc(sub)}</span>` : ''}${right ? `<span class="right">${right}</span>` : ''}</h3>` : ''}${body}</div>`;
}
function kpi(label, val, sub) {
  return `<div class="card kpi"><div class="l">${esc(label)}</div><div class="v">${val}</div><div class="sub">${sub || ''}</div></div>`;
}
function emptyState(text, hint) {
  return `<div class="empty"><div class="e-ico">🗒</div><div>${esc(text)}</div>${hint ? `<div class="muted">${hint}</div>` : ''}</div>`;
}
function errState(msg, hint) {
  return `<div class="card errcard"><b class="err">⚠ ${esc(msg)}</b>
    <div class="muted">${hint || '这一块暂时取不到数据, 其它功能不受影响。'}</div>
    <div class="row" style="margin-top:8px"><button data-retry="1">重试</button></div></div>`;
}
function tabsHtml(name, items, active) {
  return `<div class="tabs" data-tabs="${name}">` + items.map(([k, t, badge]) =>
    `<a href="#" data-tab="${k}" class="${k === active ? 'on' : ''}">${t}${badge ? ` <span class="badge">${badge}</span>` : ''}</a>`).join('') + `</div>`;
}
function wireTabs(root, name, cb) {
  root.querySelectorAll(`[data-tabs="${name}"] a`).forEach(a => a.onclick = e => {
    e.preventDefault(); cb(a.dataset.tab);
  });
}
function wireRetry(root, fn) {
  const b = root.querySelector('[data-retry]');
  if (b) b.onclick = fn;
}
function setBanner(html, kind) {
  const b = q(document, '#banner');
  if (!b) return;
  if (!html) { b.className = 'hidden'; b.innerHTML = ''; return; }
  b.className = 'banner ' + (kind || 'warn');
  b.innerHTML = html + '<button class="icon-btn right" onclick="this.parentNode.className=\'hidden\'">✕</button>';
}
function svcRow(name, s) {
  const h = (s && s.health) || {};
  const ok = s && s.running && h.ok !== false;
  const cls = !s || !s.running ? 'bad' : (h.ok === false ? 'warn' : 'ok');
  const txt = !s || !s.running ? '未运行' : (h.ok === false ? '假活' : '正常');
  return `<div class="it"><b style="width:76px">${esc(svcName(name))}</b>
    <span class="pill ${cls}"><span class="dot ${cls}"></span>${txt}</span>
    <span class="muted">${s && s.pid ? '进程号 ' + s.pid : ''}${s && s.rss ? ' · ' + humanBytes(s.rss) : ''}${s && s.etime ? ' · ' + s.etime : ''}${s && s.port ? ' · 端口 ' + s.port : ''}</span>
    <span class="muted right">${esc((h.why || '').slice(0, 40))}</span></div>`;
}

/* ------------------------------------------------------- 仪表盘 */
const CRASH_KIND_CN = {stage_error: '阶段异常', handler_error: '插件处理异常', request_error: '请求异常',
  load_failed: '插件加载失败', task_error: '任务异常', stage_failed: '阶段失败', uncaught: '未捕获异常',
  retry_exhausted: '重试耗尽', crash_loop: '反复崩溃', summary_failed: '总结失败', bind_failed: '端口占用',
  store_failed: '落库失败', config_invalid: '配置非法', crash_loop_stop: '已熔断', queue_full: '队列满'};
const kindCn = (k) => CRASH_KIND_CN[k] || k || '';
VIEWS.dashboard = async (v) => {
  const d = await api('/api/ops/dashboard');
  if (!d) { v.innerHTML = errState('仪表盘数据加载失败'); wireRetry(v, render); return; }
  const svc = d.services || {}, st = d.stats || {}, cr = d.crashes || {}, pl = d.plugins || {};
  const runs = d.runs || [], ev = d.events || {}, sch = d.schedule || {};
  const up = Object.values(svc).filter(s => s && s.running).length;
  const total = Object.keys(svc).length;
  const bad = cr.total || 0;
  const rt = d.runtime || {}, pw = d.power || {};
  const modeBadge = rt.alive
    ? (rt.mode === 'single' ? '单进程' : '多进程') + ' · ' + (POWER_CN[pw.power_mode] || pw.power_mode || '')
    : '';
  if (bad > 0) {
    setBanner(`最近有 <b>${bad}</b> 次异常记录 — <a href="#/ops">到运维页看异常账本</a>`, bad > 20 ? 'bad' : 'warn');
  } else { setBanner(''); }
  const head = `<div class="card hero">
    <div class="row"><span class="hdot ${up === total && total ? 'ok' : (up ? 'warn' : 'bad')}"></span>
      <b style="font-size:16px">${up}/${total} 个服务在运行</b>
      <span class="muted">版本 ${esc(d.version)} · ${esc((pl.loaded || 0) + ' 个插件已加载')}${pl.failed ? ` · <span class="err">${pl.failed} 个失败</span>` : ''}</span>
      ${modeBadge ? `<span class="pill">${esc(modeBadge)}</span>` : ''}
      <span class="right row">
        <button class="primary" id="qa-sum">立即总结</button>
        <button id="qa-reload">重载插件</button>
        <button id="qa-restart">重启服务</button>
      </span></div>
    ${d.partial && d.partial.length ? `<div class="muted" style="margin-top:6px">部分数据降级: ${d.partial.map(p => esc(p.key)).join('、')}</div>` : ''}
  </div>`;
  const kpis = `<div class="grid k4">
    ${kpi('近 24 小时消息', st.window || 0, `最近 1 小时 ${st.last_hour || 0} 条`)}
    ${kpi('活跃群', st.groups || 0, `共 ${d.enabled_groups || 0} 个群开启总结`)}
    ${kpi('待办 / 事件', ev.open || 0, `已完成 ${ev.done || 0}`)}
    ${kpi('异常记录', bad, bad ? '点击运维页查看' : '一切正常 ✅')}
  </div>`;
  const trend = card('消息趋势(近 24 小时)', `<div class="spark">${sparkline(st.series || [], 520, 60)}</div>
    <div class="row" style="margin-top:4px">${Object.entries(st.kinds || {}).slice(0, 6).map(([k, c]) =>
      `<span class="pill">${esc(kindsText(k))} ${c}</span>`).join('') || '<span class="muted">暂无数据</span>'}</div>`);
  const svcCard = card('服务', `<div class="list">${Object.entries(svc).map(([n, s]) => svcRow(n, s)).join('') || '<div class="muted">无</div>'}</div>`,
    '进程号 / 内存 / 运行时长');
  const sched = card('下次总结', `<div class="list">
      <div class="it"><b>计划</b><span class="right">${esc(sch.label || '未启用')}</span></div>
      <div class="it"><b>下次运行</b><span class="right">${sch.ts ? fmtTs(sch.ts, 'full') + ' (还有 ' + Math.max(0, Math.round((sch.in || 0) / 60)) + ' 分钟)' : '-'}</span></div>
    </div><div class="row" style="margin-top:8px"><a class="btn" href="#/tasks">去调整计划</a></div>`);
  const rep = card('最近报告', `<div class="list">${(d.reports || []).slice(0, 5).map(f =>
    `<div class="it"><b class="ellip">${esc(String(f).replace(/^summary_/, '').replace(/\.md$/, ''))}</b>
      <span class="right row"><a class="btn" href="#" data-report="${esc(f)}">查看</a></span></div>`).join('') || emptyState('还没有报告', '到「任务」页点“立即总结”即可生成第一份')}</div>`);
  const crash = card('异常与崩溃', `<div class="list">${(cr.by_component ? Object.entries(cr.by_component).slice(0, 5) : []).map(([c, n]) =>
    `<div class="it"><b>${esc(c)}</b><span class="right muted">${n} 次</span></div>`).join('') || '<div class="muted">无异常记录 ✅</div>'}</div>
    <div class="row" style="margin-top:8px"><a class="btn" href="#/ops">打开异常账本</a></div>`);
  const runsCard = card('最近运行', `<div class="list">${runs.slice(0, 5).map(r =>
    `<div class="it"><b class="ellip">${esc(jobName(r.job))}</b><span class="pill ${r.status === 'ok' ? 'ok' : (r.status === 'skipped' ? '' : 'bad')}">${esc(runName(r.status))}</span>
      <span class="right muted">${fmtTs(r.started, 'time')}</span></div>`).join('') || '<div class="muted">暂无运行记录</div>'}</div>`);
  v.innerHTML = head + kpis + `<div class="grid k2">${trend}${svcCard}</div>
    <div class="grid k3">${sched}${rep}${crash}</div>${runsCard}`;
  q(v, '#qa-sum').onclick = async () => {
    toast('已开始总结, 稍后到「总结」页查看…', 'warn');
    const r = await api('/api/summarize', {method: 'POST', body: JSON.stringify({hours: 24})});
    if (r && r.ok) toast('总结完成: ' + (r.result && (r.result.status || '')) , 'ok');
  };
  q(v, '#qa-reload').onclick = async () => {
    const r = await api('/api/plugins/reload', {method: 'POST', body: '{}'});
    if (r && r.ok) { toast('插件已全部重载', 'ok'); render(); }
  };
  q(v, '#qa-restart').onclick = () => { location.hash = '#/ops'; toast('到运维页可以逐个重启服务', 'warn'); };
  v.querySelectorAll('[data-report]').forEach(a => a.onclick = async e => {
    e.preventDefault();
    const r = await api('/api/report?name=' + encodeURIComponent(a.dataset.report));
    if (r && r.content) modal(a.dataset.report, `<pre class="md">${esc(r.content)}</pre>`);
  });
};

/* ------------------------------------------------------- 插件 */
const PLUGIN_STATUS_CN = {loaded: '已加载', failed: '加载失败', disabled: '已停用', discovered: '待加载'};
VIEWS.plugins = async (v) => {
  const d = await api('/api/plugins');
  if (!d) { v.innerHTML = errState('插件列表加载失败'); wireRetry(v, render); return; }
  const rows = d.plugins || [];
  const cards = rows.map(p => {
    const st = p.status || 'discovered';
    const cls = st === 'loaded' ? 'ok' : (st === 'failed' ? 'bad' : '');
    return `<div class="card plug">
      <div class="row"><b>${esc(p.name)}</b><span class="pill">${esc(p.version)}</span>
        <span class="pill ${cls}">${PLUGIN_STATUS_CN[st] || st}</span>
        ${p.builtin ? '<span class="pill">内置</span>' : ''}
        <span class="right row">
          <button data-toggle="${esc(p.id)}" data-on="${p.enabled ? 0 : 1}">${p.enabled ? '停用' : '启用'}</button>
          <button data-cfg="${esc(p.id)}">配置</button>
          <button data-reload="${esc(p.id)}">重载</button>
        </span></div>
      <div class="muted" style="margin-top:4px">${esc(p.desc || '')}</div>
      <div class="row" style="margin-top:6px">
        <span class="muted">${esc(p.author || '')}</span>
        <span class="muted">处理器 ${p.handlers || 0}</span>
        <span class="muted">可配置项 ${p.config_fields || 0}</span>
        ${(p.pages || []).length ? `<span class="muted">页面 ${(p.pages || []).map(x => esc(x.title)).join('/')}</span>` : ''}
      </div>
      ${st === 'failed' ? `<div class="err" style="margin-top:6px">失败原因: ${esc(p.error || '')}</div>` : ''}
    </div>`;
  }).join('');
  const cmds = (d.commands || []).map(c => `<div class="it"><b>${esc(c.command)}</b>
    <span class="muted">${esc(c.help || '')}</span>
    <span class="right row"><button data-cmd="${esc(c.command)}">执行</button></span></div>`).join('');
  const tasks = (d.tasks || []).map(t => `<div class="it"><b>${esc(t.task)}</b>
    <span class="muted">${t.every ? '每 ' + t.every + ' 秒' : (t.cron ? 'cron ' + esc(t.cron) : '-')}</span>
    <span class="right row"><button data-runtask="${esc(t.task)}">立即执行</button></span></div>`).join('');
  v.innerHTML = `<div class="card"><div class="row"><b>插件系统</b>
      <span class="muted">共 ${d.total || 0} 个 · 已加载 ${d.loaded || 0} · 停用 ${d.disabled || 0}
      ${d.failed ? ` · <span class="err">失败 ${d.failed}</span>` : ''} · 角色 ${(d.roles || []).join('/')}</span>
      <span class="right row"><button id="pl-reload-all">重载全部</button></span></div>
    <div class="muted" style="margin-top:6px">插件目录: ${(d.dirs || []).map(esc).join(' · ')}</div></div>
    ${cards || emptyState('还没有插件')}
    <div class="grid k2">
      ${card('插件命令', `<div class="list">${cmds || '<div class="muted">无</div>'}</div>`)}
      ${card('插件定时任务', `<div class="list">${tasks || '<div class="muted">无</div>'}</div>`)}
    </div>
    ${(d.pages || []).length ? card('插件页面', `<div class="row">${d.pages.map(p =>
      `<a class="btn" href="#/plugins" data-page="${esc(p.id)}">${esc(p.icon || '🧩')} ${esc(p.title)}</a>`).join('')}</div>`) : ''}`;
  q(v, '#pl-reload-all').onclick = async () => {
    const r = await api('/api/plugins/reload', {method: 'POST', body: '{}'});
    toast(r && r.ok ? '已重载全部插件' : '重载失败', r && r.ok ? 'ok' : 'bad');
    render();
  };
  v.querySelectorAll('[data-toggle]').forEach(b => b.onclick = async () => {
    const r = await api('/api/plugins/toggle', {method: 'POST',
      body: JSON.stringify({id: b.dataset.toggle, enabled: b.dataset.on === '1'})});
    toast(r && r.ok ? '已' + (b.dataset.on === '1' ? '启用' : '停用') : ('失败: ' + ((r && r.error) || '')),
      r && r.ok ? 'ok' : 'bad');
    render();
  });
  v.querySelectorAll('[data-reload]').forEach(b => b.onclick = async () => {
    const r = await api('/api/plugins/reload', {method: 'POST', body: JSON.stringify({id: b.dataset.reload})});
    toast(r && r.ok ? '已重载 ' + b.dataset.reload + ' (' + r.status + ')' : '重载失败', r && r.ok ? 'ok' : 'bad');
    render();
  });
  v.querySelectorAll('[data-cm]').forEach(b => b.onclick = async () => {
    const r = await api('/api/plugins/command', {method: 'POST', body: JSON.stringify({name: b.dataset.cm})});
    modal('命令结果: ' + b.dataset.cm, `<pre>${esc(JSON.stringify(r && (r.result !== undefined ? r.result : r), null, 2))}</pre>`);
  });
  v.querySelectorAll('[data-runtask]').forEach(b => b.onclick = async () => {
    const r = await api('/api/ops/tasks/run', {method: 'POST', body: JSON.stringify({task: b.dataset.runtask})});
    toast(r && r.ok ? '任务完成' : ('任务失败: ' + ((r && r.error) || '')), r && r.ok ? 'ok' : 'bad');
    if (r) modal('任务结果: ' + b.dataset.runtask, `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`);
  });
  v.querySelectorAll('[data-cfg]').forEach(b => b.onclick = () => openPluginConfig(b.dataset.cfg));
};
async function openPluginConfig(pid) {
  const d = await api('/api/plugins/config?id=' + encodeURIComponent(pid));
  if (!d || !d.fields) return toast('读取配置失败', 'bad');
  const groups = {};
  d.fields.forEach(f => { (groups[f.group_label || '设置'] = groups[f.group_label || '设置'] || []).push(f); });
  const field = f => {
    const id = 'cf-' + f.path.replace(/\./g, '_');
    let input;
    if (f.type === 'bool') input = `<label class="switch"><input type="checkbox" id="${id}" ${f.value ? 'checked' : ''}><span></span></label>`;
    else if (f.options && f.options.length) input = `<select id="${id}">${f.options.map(o => {
      const v = (o && typeof o === 'object') ? o.value : o;
      const t = (o && typeof o === 'object') ? (o.label || o.value) : o;
      return `<option value="${esc(v)}" ${String(f.value) === String(v) ? 'selected' : ''}>${esc(t)}</option>`;
    }).join('')}</select>`;
    else if (f.type === 'int' || f.type === 'float') input = `<input id="${id}" type="number" value="${esc(f.value)}" ${f.min != null ? `min="${f.min}"` : ''} ${f.max != null ? `max="${f.max}"` : ''}>`;
    else if (f.type === 'list') input = `<input id="${id}" value="${esc((f.value || []).join(', '))}" placeholder="逗号分隔">`;
    else input = `<input id="${id}" type="${f.secret ? 'password' : 'text'}" value="${esc(f.secret ? '' : f.value)}" ${f.secret && f.configured ? 'placeholder="已配置(留空则不修改)"' : ''}>`;
    return `<div class="field"><label for="${id}">${esc(f.label)}${f.secret ? ' 🔑' : ''}</label>${input}
      <div class="muted">${esc(f.hint || '')}${f.default != null && f.default !== '' ? ` (默认: ${esc(JSON.stringify(f.default))})` : ''}</div></div>`;
  };
  const html = Object.entries(groups).map(([g, fs]) =>
    `<div class="cfg-group"><h4>${esc(g)}</h4>${fs.map(field).join('')}</div>`).join('');
  modal('插件配置 · ' + pid, `${html}<div class="row" style="margin-top:10px">
    <button class="primary" id="cf-save">保存</button>
    <span class="muted">${esc(d.path || '')}</span></div>`);
  q(document, '#cf-save').onclick = async () => {
    const patch = {};
    d.fields.forEach(f => {
      const el = document.getElementById('cf-' + f.path.replace(/\./g, '_'));
      if (!el) return;
      let val;
      if (f.type === 'bool') val = el.checked;
      else if (f.type === 'int') val = parseInt(el.value, 10);
      else if (f.type === 'float') val = parseFloat(el.value);
      else if (f.type === 'list') val = el.value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
      else val = el.value;
      if (f.secret && !String(el.value || '').trim()) return;      // 密钥留空 = 不改
      const parts = f.path.split('.');
      let node = patch;
      parts.forEach((p, i) => { if (i === parts.length - 1) node[p] = val; else node = (node[p] = node[p] || {}); });
    });
    const r = await api('/api/plugins/config/save', {method: 'POST', body: JSON.stringify({id: pid, config: patch})});
    if (r && r.ok) { toast('已保存', 'ok'); closeModal(); render(); }
    else {
      const errs = (r && r.errors) || [];
      toast('保存有问题: ' + (errs[0] || (r && r.error) || '未知'), 'warn');
      if (errs.length) modal('配置校验提示', `<div class="muted">非法项已回退为默认值:</div><ul>${errs.map(e => `<li>${esc(e)}</li>`).join('')}</ul>`);
    }
  };
}

/* ------------------------------------------------------- 对话(AI 智能查询) */
let chatSession = '', chatBusy = false;
const SRC_ICON = {M: '💬', E: '📌', Y: '🧠', L: '🔗', P: '🖼'};
VIEWS.chat = async (v) => {
  const [sessions, sug] = await Promise.all([api('/api/assistant/sessions'), api('/api/assistant/suggest')]);
  const sess = (sessions && sessions.sessions) || [];
  const llm = (sessions && sessions.llm) || {};
  const quota = (sessions && sessions.quota) || 0;
  const used = (sessions && sessions.today_used) || 0;
  let turns = [];
  if (chatSession) {
    const d = await api('/api/assistant/session?id=' + encodeURIComponent(chatSession));
    turns = (d && d.turns) || [];
  }
  const bubbles = turns.map(t => {
    if (t.role === 'user') return `<div class="bub user">${esc(t.content)}</div>`;
    const srcs = (t.sources || []).map(s =>
      `<a href="#" class="srcchip" data-src="${esc(s.kind)}" data-ref="${esc(String(s.ref).slice(1))}" data-sid="${esc(s.sid || '')}" data-url="${esc(s.url || '')}" title="${esc(s.text || '')}">${SRC_ICON[s.kind] || '•'} ${esc(s.label || s.ref)}</a>`).join(' ');
    const meta = t.meta || {};
    const info = [
      meta.model ? esc(meta.model) : '',
      t.tokens_in ? `${t.tokens_in}+${t.tokens_out} tokens` : '',
      meta.rounds ? `${meta.rounds} 轮` : '',
      t.ms ? (t.ms / 1000).toFixed(1) + 's' : '',
      meta.degraded ? '<span class="err">降级</span>' : '',
    ].filter(Boolean).join(' · ');
    return `<div class="bub bot">${renderAnswer(t.content, t.sources)}
      ${srcs ? `<div class="srcs"><span class="muted">来源</span> ${srcs}</div>` : ''}
      ${info ? `<div class="muted bub-meta">${info}</div>` : ''}</div>`;
  }).join('');
  v.innerHTML = `<div class="chatwrap">
    <aside class="chatlist">
      <div class="row"><b>会话</b><span class="right muted">今日 ${used}/${quota || '-'}</span></div>
      <button class="primary" id="chat-new" style="width:100%;margin:8px 0">＋ 新对话</button>
      <div class="list" id="chat-sessions">${sess.map(s =>
        `<div class="it ${s.id === chatSession ? 'on' : ''}" data-sid="${esc(s.id)}">
          <span class="ellip">${esc(s.title || '(无标题)')}</span>
          <button class="right icon-btn" data-del="${esc(s.id)}" title="删除">✕</button></div>`).join('')
        || '<div class="muted">还没有对话</div>'}</div>
      <div class="muted" style="margin-top:10px;font-size:12px">
        模型: ${llm.configured ? esc(llm.model || '已配置') : '<span class="err">未配置</span>'}
        ${llm.configured ? '' : '<br>到「设置」填 API Key, 或让 summarizer 有 key'}
      </div>
      <div class="card" style="margin-top:10px">
        <h3>QQ 里也能问 <span class="muted">不用打开控制台</span></h3>
        <div class="muted" style="font-size:12px;line-height:1.7">
          私聊机器人直接发问题即可(仅限白名单, 默认只有你自己);<br>
          群里要带前缀「问」, 且需在插件配置里放行该群。
        </div>
        <div class="row" style="margin-top:8px">
          <button id="chat-cardtest">测试卡片渲染</button>
        </div>
        <div class="muted" style="font-size:12px;margin-top:6px">
          会往你的 QQ 发几种卡片; 哪种能正常显示, 就在插件配置里选哪种回答形态。
        </div>
      </div>
    </aside>
    <section class="chatmain">
      <div class="chatturns" id="chat-turns">
        ${bubbles || `<div class="card">${emptyState('问一句关于你聊天记录的话',
          '例如: 昨天群里讨论了什么 / 谁提到过报告 / 有哪些快到期的待办')}
          <div class="row" style="margin-top:10px">${((sug && sug.suggestions) || []).map(s =>
            `<button class="chipbtn" data-ask="${esc(s)}">${esc(s)}</button>`).join('')}</div></div>`}
      </div>
      <div class="chatinput">
        <textarea id="chat-q" rows="2" placeholder="问点什么…  (Enter 发送, Shift+Enter 换行)"></textarea>
        <div class="row" style="margin-top:6px">
          <label class="toggle">范围
            <select id="chat-hours">
              <option value="0">默认</option><option value="24">近 24 小时</option>
              <option value="72">近 3 天</option><option value="168">近 7 天</option>
              <option value="720">近 30 天</option><option value="8760">近一年</option>
            </select></label>
          <span id="chat-state" class="muted"></span>
          <span class="right row">
            <button id="chat-clear">清屏</button>
            <button class="primary" id="chat-send">发送</button>
          </span>
        </div>
      </div>
    </section></div>`;
  const send = async (text) => {
    // 注意: 变量名不能叫 q —— 会遮蔽全局 q(root, sel) 帮助函数, 发送按钮点了没反应
    const question = String(text || q(v, '#chat-q').value || '').trim();
    if (!question || chatBusy) return;
    chatBusy = true;
    q(v, '#chat-state').textContent = '检索记录并思考中…';
    q(v, '#chat-send').disabled = true;
    q(v, '#chat-q').value = '';
    const hours = parseInt(q(v, '#chat-hours').value, 10) || 0;
    const r = await api('/api/assistant/ask', {method: 'POST',
      body: JSON.stringify({question, session_id: chatSession, hours})});
    chatBusy = false;
    q(v, '#chat-send').disabled = false;
    q(v, '#chat-state').textContent = '';
    if (!r) return toast('查询失败', 'bad');
    if (r.ok === false) { toast(r.error || '查询失败', 'bad'); return; }
    chatSession = r.session_id || chatSession;
    if (r.degraded) toast(r.error ? '模型不可用, 已降级显示记录' : '记录不足, 已如实说明', 'warn');
    render();
  };
  q(v, '#chat-send').onclick = () => send();
  q(v, '#chat-q').onkeydown = e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  };
  q(v, '#chat-new').onclick = () => { chatSession = ''; render(); };
  q(v, '#chat-clear').onclick = () => { chatSession = ''; render(); };
  q(v, '#chat-cardtest').onclick = async () => {
    const b = q(v, '#chat-cardtest');
    b.disabled = true; b.textContent = '发送中…';
    const r = await api('/api/assistant/reply/test', {method: 'POST', body: '{}'});
    b.disabled = false; b.textContent = '测试卡片渲染';
    if (!r || !r.ok) return toast((r && r.error) || '发送失败', 'bad');
    const rows = Object.entries(r.results || {}).map(([k, v]) => {
      const ok = v && (v.status === 'ok' || v.ark);
      const why = !v ? '无结果'
        : (v.status === 'ok' ? '已发送' : (v.error || v.message || JSON.stringify(v)));
      return `<div class="it"><span class="pill ${ok ? 'ok' : 'bad'}">${ok ? '成功' : '失败'}</span>
        <b style="width:88px">${esc(CARD_CN[k] || k)}</b>
        <span class="right muted ellip">${esc(String(why).slice(0, 90))}</span></div>`;
    }).join('');
    modal('卡片测试', `<div class="muted">已发到 QQ ${esc(String(r.user_id || ''))}, 去聊天窗口看哪种卡片能正常显示:</div>
      <div class="list" style="margin-top:8px">${rows}</div>
      <div class="muted" style="margin-top:10px;line-height:1.7">
        · <b>合并转发卡片</b>: 显示成"聊天记录", 各版本 QQ 都能渲染, 内容任意;<br>
        · <b>官方分享卡片</b>: QQ 原生联系人/群卡片, 带签名, 但内容由 QQ 模板决定;<br>
        · <b>小程序卡片</b>: 需要 NapCat 的 PacketBackend 支持当前 QQ 版本, 否则会提示"卡片版本过低";<br>
        · <b>裸 JSON 卡片</b>: 不带签名, 现在的 QQ 基本不渲染(仅作对照)。
      </div>`);
  };
  v.querySelectorAll('[data-ask]').forEach(b => b.onclick = () => send(b.dataset.ask));
  v.querySelectorAll('[data-sid]').forEach(el => el.onclick = e => {
    if (e.target.dataset.del) return;
    chatSession = el.dataset.sid; render();
  });
  v.querySelectorAll('[data-del]').forEach(b => b.onclick = async e => {
    e.stopPropagation();
    if (!confirm('删除这个会话?')) return;
    await api('/api/assistant/delete', {method: 'POST', body: JSON.stringify({id: b.dataset.del})});
    if (chatSession === b.dataset.del) chatSession = '';
    toast('已删除', 'ok'); render();
  });
  v.querySelectorAll('[data-src]').forEach(a => a.onclick = async e => {
    e.preventDefault();
    const kind = a.dataset.src, ref = a.dataset.ref, sid = a.dataset.sid || '';
    const url = `/api/assistant/source/${encodeURIComponent(kind)}/${encodeURIComponent(ref)}`
      + (sid ? '?sid=' + encodeURIComponent(sid) : '');
    const d = await api(url);
    if (!d || !d.ok) return toast((d && d.error) || '取不到原文', 'bad');
    const r = d.record || {};
    modal(d.title || '来源原文', srcBody(kind, r));
  });
  const box = q(v, '#chat-turns');
  if (box.scrollHeight) box.scrollTop = box.scrollHeight;
};
/* 来源原文的展示: 每种来源给出"能核对"的字段, 而不是一坨 JSON */
function srcBody(kind, r) {
  const line = (k, v) => (v === '' || v == null || v === 0) ? '' :
    `<div class="it"><b>${esc(k)}</b><span class="right muted">${esc(String(v))}</span></div>`;
  const when = (ts) => ts ? fmtTs(ts) : '';
  if (kind === 'M') {
    return `<div class="muted">${esc(r.ts_text || when(r.ts))} · ${esc(r.group_name || r.group_id || '')} · ${esc(r.nickname || '')}
      ${r.recalled ? ' · <span class="err">已撤回</span>' : ''}${r.self ? ' · 机器人自己发的' : ''}</div>
      <pre>${esc(r.raw_message || '')}</pre>${line('消息 id', r.id)}`;
  }
  if (kind === 'Y') {
    const origin = (r.origin_messages || []).map(m =>
      `<div class="it"><span class="muted">${esc(m.ts_text || '')}</span>
       <b class="ellip">${esc(m.nickname || '')}</b>
       <span class="muted ellip">${esc(m.text || '')}</span></div>`).join('');
    return `<div class="muted">范围 ${esc(r.scope || '')} · 键 ${esc(r.key || '')} ·
      更新 ${esc(r.updated_text || '')} · 命中 ${esc(String(r.hits || 0))}</div>
      <pre>${esc(r.content || '')}</pre>
      ${origin ? `<h3 style="margin-top:12px">沉淀来源(该群最近消息)</h3><div class="list">${origin}</div>` : ''}`;
  }
  if (kind === 'E') {
    return `<div class="list">
      ${line('标题', r.title)}${line('类型', KIND_CN[r.kind] || r.kind)}${line('状态', r.status)}
      ${line('截止', r.due_at ? fmtTs(r.due_at) : '')}${line('群', r.group_id)}${line('提出人', r.who)}
      ${line('来源', r.origin)}${line('置信度', r.confidence)}${line('记录时间', when(r.first_seen))}</div>
      ${r.detail ? `<pre>${esc(r.detail)}</pre>` : ''}
      ${r.notes ? `<div class="muted">备注: ${esc(r.notes)}</div>` : ''}`;
  }
  if (kind === 'L') {
    return `<div class="list">${line('标题', r.title)}${line('域名', r.domain)}
      ${line('群', r.group_id)}${line('状态', r.status)}${line('抓取时间', r.fetched_at ? fmtTs(r.fetched_at) : '')}</div>
      <div class="row" style="margin:8px 0"><a class="btn" href="${esc(r.url || '#')}" target="_blank" rel="noopener">打开链接</a>
      <span class="muted ellip">${esc(r.url || '')}</span></div>
      ${r.excerpt ? `<pre>${esc(r.excerpt)}</pre>` : ''}
      ${r.error ? `<div class="err">抓取失败: ${esc(r.error)}</div>` : ''}`;
  }
  if (kind === 'P') {
    const isImg = (r.kind === 'image' || !r.kind) && r.key;
    return `${isImg ? `<img src="/api/media/raw?key=${encodeURIComponent(r.key)}" style="max-width:100%;border-radius:10px" alt="">` : ''}
      <div class="list">${line('类型', r.kind)}${line('文件名', r.name)}${line('状态', r.status)}
      ${line('大小', r.size ? humanBytes(r.size) : '')}${line('群', r.group_id)}${line('消息 id', r.message_id)}
      ${line('识别说明', r.caption)}</div>
      ${r.url ? `<div class="muted ellip">${esc(r.url)}</div>` : ''}
      ${r.error ? `<div class="err">${esc(r.error)}</div>` : ''}`;
  }
  return `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`;
}
/* 把回答里的 [#M1] / [M1] 变成可点的来源芯片(先转义再做替换, 防注入)。
   `sources` 是该轮回答的来源表: 用它把编号(M1/Y2)换成稳定 sid —— 编号只在一份
   上下文里唯一, 直接拿序号去查会打开另一条记录或报"未知类型"。 */
function renderAnswer(text, sources) {
  const map = {};
  (sources || []).forEach(s => { if (s && s.ref) map[String(s.ref)] = s; });
  const safe = esc(text || '');
  return safe.replace(/\n/g, '<br>')
    .replace(/\[#?([MEYLP])(\d+)\]/g, (m, k, n) => {
      const s = map[k + n] || {};
      return `<a href="#" class="cite" data-src="${k}" data-ref="${n}" data-sid="${esc(s.sid || '')}">[${k}${n}]</a>`;
    });
}

/* ------------------------------------------------------- 数据 */
let dataTab = 'groups';
VIEWS.data = async (v) => {
  const T = [['groups', '群组'], ['events', '事件/待办'], ['memory', '记忆'], ['links', '链接'], ['media', '媒体']];
  v.innerHTML = tabsHtml('data', T, dataTab) + '<div id="data-body"></div>';
  wireTabs(v, 'data', t => { dataTab = t; VIEWS.data(v); });
  const box = q(v, '#data-body');
  if (dataTab === 'groups') {
    const d = await api('/api/groups');
    if (!d) { box.innerHTML = errState('群组加载失败'); wireRetry(box, () => VIEWS.data(v)); return; }
    const gs = d.groups || [];
    box.innerHTML = card(`群组(${d.enabled_count || 0}/${gs.length} 个开启)`,
      `<div class="list">${gs.slice(0, 200).map(g => `<div class="it">
        <label class="switch"><input type="checkbox" data-gid="${g.id}" ${g.enabled ? 'checked' : ''}><span></span></label>
        <b class="ellip">${esc(g.name || g.id)}</b><span class="muted">${g.id}</span>
        <span class="right muted">近 7 天 ${g.count} 条${g.in_napcat ? '' : ' · 已退群'}</span></div>`).join('') || emptyState('还没有群')}</div>`,
      '勾选 = 参与总结', `<button id="g-all">全部开启</button>`);
    box.querySelectorAll('[data-gid]').forEach(c => c.onchange = async () => {
      await api('/api/groups', {method: 'POST', body: JSON.stringify({id: +c.dataset.gid, enabled: c.checked})});
      toast('已' + (c.checked ? '开启' : '关闭') + ' ' + c.dataset.gid, 'ok');
    });
    q(box, '#g-all').onclick = async () => {
      const ids = gs.map(g => g.id);
      await api('/api/groups/bulk', {method: 'POST', body: JSON.stringify({ids, enabled: true})});
      toast('已全部开启', 'ok'); VIEWS.data(v);
    };
  } else if (dataTab === 'events') {
    const d = await api('/api/events?status=all&limit=200');
    if (!d) { box.innerHTML = errState('事件加载失败'); wireRetry(box, () => VIEWS.data(v)); return; }
    const ev = d.events || [];
    box.innerHTML = card(`事件与待办(${ev.length})`, `<div class="list">${ev.map(e => `<div class="it">
      ${dueTag(e.due_ts)}<b class="ellip">${esc(e.title || '')}</b>
      <span class="pill">${esc(KIND_CN[e.kind] || e.kind || '')}</span>
      <span class="muted">${esc(e.group_id || '')}</span>
      <span class="right row"><button data-done="${e.id}" data-st="${e.status}">${e.status === 'done' ? '恢复' : '完成'}</button></span></div>`).join('') || emptyState('还没有事件', '总结后会自动抽取; 也可以手动新建')}</div>`);
    box.querySelectorAll('[data-done]').forEach(b => b.onclick = async () => {
      await api('/api/events', {method: 'POST', body: JSON.stringify({id: +b.dataset.done,
        status: b.dataset.st === 'done' ? 'open' : 'done'})});
      toast('已更新', 'ok'); VIEWS.data(v);
    });
  } else if (dataTab === 'memory') {
    const d = await api('/api/memory');
    if (!d) { box.innerHTML = errState('记忆加载失败'); wireRetry(box, () => VIEWS.data(v)); return; }
    box.innerHTML = card('长期记忆', `<div class="list">${(d.memories || []).map(m =>
      `<div class="it"><b>${esc(m.scope || m.key || '')}</b><span class="muted ellip">${esc((m.content || '').slice(0, 120))}</span>
       <span class="right row"><span class="muted">${fmtTs(m.updated, 'time')}</span>
       <button data-msrc="${esc(m.scope || '')}|${esc(m.key || 'digest')}">看原文</button></span></div>`).join('')
      || emptyState('还没有记忆', '总结会逐步沉淀群里的共识')}</div>`,
      '点「看原文」看这条记忆本身, 以及它是从该群哪些消息沉淀出来的')
      + card('个人档案(profile.md)', `<pre class="md">${esc(d.profile || '(空)')}</pre>`);
    box.querySelectorAll('[data-msrc]').forEach(b => b.onclick = async () => {
      const d2 = await api('/api/assistant/source/Y/1?sid=' + encodeURIComponent('y:' + b.dataset.msrc));
      if (!d2 || !d2.ok) return toast((d2 && d2.error) || '取不到原文', 'bad');
      modal(d2.title || '长期记忆原文', srcBody('Y', d2.record || {}));
    });
  } else if (dataTab === 'links') {
    const d = await api('/api/links');
    if (!d) { box.innerHTML = errState('链接加载失败'); wireRetry(box, () => VIEWS.data(v)); return; }
    box.innerHTML = card(`链接(${(d.links || []).length})`, `<div class="list">${(d.links || []).map(l =>
      `<div class="it"><a href="${esc(l.url)}" target="_blank" rel="noopener" class="ellip">${esc(l.title || l.url)}</a>
       <span class="right muted">${esc(l.group_id || '')} ${fmtTs(l.ts, 'time')}</span></div>`).join('') || emptyState('还没有链接')}</div>`);
  } else {
    const d = await api('/api/media?limit=60');
    if (!d) { box.innerHTML = errState('媒体加载失败'); wireRetry(box, () => VIEWS.data(v)); return; }
    const ms = d.media || [];
    box.innerHTML = card(`媒体(${ms.length})`, `<div class="mgrid">${ms.map(m =>
      `<figure class="mcell">${m.kind === 'image' || !m.kind ? `<img loading="lazy" src="/api/media/raw?key=${encodeURIComponent(m.key)}" alt="">` : `<div class="mfile">🎵</div>`}
        <figcaption class="muted">${esc((m.caption || '').slice(0, 40) || m.status || '')}</figcaption></figure>`).join('') || emptyState('还没有媒体', '群里发的图片会在总结时被抓取')}</div>`);
  }
};

/* ------------------------------------------------------- 帮助 */
VIEWS.help = async (v) => {
  v.innerHTML = card('快速上手', `<ol class="ol">
      <li>手机浏览器打开本页(带 token 的链接), 加到桌面即可当 App 用。</li>
      <li><b>仪表盘</b> 看整体健康; 红色横幅说明有异常, 点进「运维」看账本。</li>
      <li><b>数据 → 群组</b> 勾选要参与总结的群。</li>
      <li><b>任务</b> 设置总结时间(每天/每隔/每周), 点「立即执行」可先手动跑一次。</li>
      <li><b>插件</b> 里可以启停插件、改配置; 改了配置立刻生效, 不用重启。</li>
    </ol>`)
    + card('常见问题', `<div class="list">
      <div class="it"><b>收不到消息</b><span class="muted right">看「运维→服务」里 NapCat 是否在线、采集器是否运行</span></div>
      <div class="it"><b>报告没生成</b><span class="muted right">「任务」看运行历史; 若无新消息会按配置跳过</span></div>
      <div class="it"><b>推送失败</b><span class="muted right">报告已落盘, 到「总结」页可手动推送</span></div>
      <div class="it"><b>服务老是被杀</b><span class="muted right">看「运维→自检」的保活项; 电脑上跑 tools/android-keepalive.sh</span></div>
      <div class="it"><b>界面显示异常</b><span class="muted right">浏览器控制台会把错误自动上报到 data/logs/client.log</span></div>
    </div>`)
    + card('命令行速查', `<pre class="md">bin/qqops start|stop|restart|status   启停与状态
bin/qqops doctor                    一键体检(含手机保活)
bin/qqops summary --hours 24        手动总结
bin/qqops backup|prune              备份 / 清理
bin/qqops napcat start|stop         旁路服务
bin/qqops watchdog --once           看门狗体检一轮
python tests/smoke.py               自测(改代码后跑)</pre>`)
    + card('关于', `<div class="list">
      <div class="it"><b>版本</b><span class="right">${esc(VER)}</span></div>
      <div class="it"><b>架构</b><span class="right muted">事件总线 + 阶段化管线 + 插件体系(见 docs/ARCHITECTURE.md)</span></div>
      <div class="it"><b>异常账本</b><span class="right"><a class="btn" href="#/ops">打开</a></span></div>
    </div>`);
};
VIEWS.overview = async (v) => {
  const d = await api('/api/overview'); if (!d || !d.stats) return failCard(v, '概览接口没有返回数据(/api/overview)');
  const s = d.stats, st = d.services, np = d.napcat || {};
  const kpi = (l, val, sub) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${val}</div><div class="sub">${sub || ''}</div></div>`;
  const kinds = Object.entries(s.kinds || {}).slice(0, 5).map(([k, c]) => `<span class="pill">${esc(k)} ${c}</span>`).join(' ');
  const nx = d.scheduler.next || {};
  v.innerHTML = `
    <div class="grid k4">
      ${kpi('消息总数', s.total, `近 ${s.window_hours}h: ${s.window}`)}
      ${kpi('近 1 小时', s.last_hour, `涉及 ${s.groups} 个群`)}
      ${kpi('NapCat', np.ok ? '在线' : '离线', np.ok ? `${esc(np.nickname || '')} · ${np.groups || 0} 群 · ${np.ms}ms` : esc(np.error || ''))}
      ${kpi('调度器', d.scheduler.alive ? '正常' : '心跳丢失', `${(d.scheduler.times || []).join(' / ') || '未设定时'}`)}
      ${kpi('下次总结', nx.ts ? fmtTs(nx.ts, 'time') : '未设定', nx.in ? `还有 ${Math.max(1, Math.round(nx.in / 60))} 分钟` : '')}
    </div>
    <div class="grid k2">
      <div class="card"><h3>消息趋势(近 ${s.window_hours}h)</h3>${sparkline(s.series)}
        <div class="row" style="margin-top:8px">${kinds || '<span class="muted">无数据</span>'}</div></div>
      <div class="card"><h3>服务状态 <span class="muted">进程 / 内存 / 运行时长</span></h3>
        <div class="list">
          ${['collector', 'scheduler', 'web', 'napcat'].map(n => {
            const x = st[n] || {};
            return `<div class="it"><b style="width:78px">${svcName(n)}</b>${svcPill(x)}
            <span class="muted">${x.pid ? '进程号 ' + x.pid : ''}${x.rss ? ' · 内存 ' + humanBytes(x.rss) : ''}${x.etime ? ' · 已运行 ' + x.etime : ''}${x.port ? ' · 端口 ' + x.port : ''}</span>
            <span class="right row">
              <button data-svc="${n}" data-act="restart">重启</button>
              <button data-svc="${n}" data-act="stop" class="danger">停止</button>
            </span></div>`; }).join('')}
        </div>
        <div class="row" style="margin-top:10px">
          <span class="pill ${d.ports.collector ? 'ok' : 'bad'}">采集口 ${d.ports.collector ? '通' : '不通'}</span>
          ${(d.ecosystem || []).map(e => `<span class="pill ${e.ok ? 'ok' : ''}" title="端口 ${e.port}">${esc(e.name)} ${e.ok ? '✓' : '—'}</span>`).join('')}
        </div>
        <div class="row" style="margin-top:8px">
          <span class="pill">库 ${humanBytes(d.storage.db)}</span>
          <span class="pill">数据 ${humanBytes(d.storage.data)}</span>
          <span class="pill">报告 ${d.storage.reports}</span>
          <span class="pill">备份 ${d.storage.backups}</span>
          <span class="pill">磁盘剩 ${humanBytes(d.storage.disk.free)}</span>
          <span class="pill">启用群 ${d.enabled_groups}</span>
          <span class="pill">采集 收${(d.counters || {}).ingested || 0} / 跳${(d.counters || {}).skipped || 0} / 错${(d.counters || {}).errors || 0}</span>
        </div>
      </div>
    </div>
    <div class="grid k2">
      <div class="card"><h3>最活跃的群(近 ${s.window_hours}h)</h3>
        <div class="table-wrap"><table><thead><tr><th>群</th><th>条数</th><th>最近</th></tr></thead><tbody>
        ${(s.top_groups || []).map(g => `<tr><td><a href="#/messages?group=${g.group_id}">${esc(g.name || g.group_id)}</a></td><td>${g.c}</td><td class="muted">${fmtTs(g.last, 'time')}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">暂无数据</td></tr>'}
        </tbody></table></div></div>
      <div class="card"><h3>最近任务</h3>
        <div class="list">${(d.runs || []).slice(0, 6).map(r => `<div class="it">
          <span class="pill ${r.status === 'ok' ? 'ok' : (r.status === 'failed' ? 'bad' : 'warn')}">${esc(runName(r.status))}</span>
          <b>${esc(jobName(r.job))}</b><span class="muted">${fmtTs(r.started_at)}</span>
          <span class="right muted">${dur(r.started_at, r.finished_at)} · ${esc((r.detail || '').slice(0, 40))}</span></div>`).join('') || '<div class="empty">还没有任务记录</div>'}
        </div>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="run-now">立即总结</button>
          <button id="run-dry" title="只组装提示词, 不调用模型, 不花钱">试运行(不花钱)</button>
          <span class="muted" id="task-state">${d.task.running ? '任务进行中…' : ''}</span>
        </div>
      </div>
    </div>`;
  v.querySelectorAll('[data-svc]').forEach(b => b.onclick = async () => {
    const svc = b.dataset.svc, act = b.dataset.act;
    if (svc === 'web' && act !== 'start') {
      toast('控制台正在重启, 3 秒后刷新页面…', 'warn');
      fetch('/api/service', {method: 'POST', body: JSON.stringify({name: svc, action: act}),
        headers: {'Content-Type': 'application/json'}}).catch(() => {});
      setTimeout(() => location.reload(), 3500);
      return;
    }
    const r = await api('/api/service', {method: 'POST', body: JSON.stringify({name: svc, action: act})});
    toast((r && r.message) || '已提交', 'ok'); setTimeout(render, 2000);
  });
  const start = async (dry) => {
    const r = await api('/api/summarize', {method: 'POST', body: JSON.stringify({dry_run: dry})});
    if (r && r.ok) { toast(dry ? '试运行已启动(不调用模型)' : '总结任务已启动', 'ok'); pollTask(); }
  };
  q(v, '#run-now').onclick = () => start(false);
  q(v, '#run-dry').onclick = () => start(true);
};
async function pollTask() {
  const st = q(document, '#task-state');
  for (let i = 0; i < 60; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const d = await api('/api/overview?hours=1');
    if (st) st.textContent = d.task.running ? '任务进行中…' : '任务完成';
    if (!d.task.running) { toast('任务结束', 'ok'); render(); return; }
  }
}

/* ------------------------------------------------------------------ 计划(事件/待办/记忆) */
let planState = {status: 'open', kind: '', group: '', q: '', soon: false};

VIEWS.plan = async (v) => {
  const qs = new URLSearchParams({status: planState.status, kind: planState.kind,
    group: planState.group, q: planState.q, soon: planState.soon ? '1' : '0', limit: '300'});
  const [d, mem] = await Promise.all([api('/api/events?' + qs), api('/api/memory')]);
  if (!d) return failCard(v, '待办接口没有返回数据(/api/events)');
  const st = d.stats || {by_status: {}, by_kind: {}, due_7d: 0, overdue: 0};
  const gs = [...new Map((d.events || []).map(e => [e.group_id, e.group_name || e.group_id])).entries()];
  const kpi = (l, val, sub) => `<div class="card kpi"><div class="l">${l}</div><div class="v">${val}</div><div class="sub">${sub || ''}</div></div>`;
  v.innerHTML = `
    <div class="grid k4">
      ${kpi('进行中', st.by_status.open || 0, `共 ${(d.events || []).length} 条匹配`)}
      ${kpi('7 天内到期', st.due_7d || 0, '含今天')}
      ${kpi('已过期', st.overdue || 0, st.overdue ? '需要尽快处理' : '没有积压')}
      ${kpi('已完成', st.by_status.done || 0, `已忽略 ${st.by_status.dropped || 0}`)}
    </div>
    <div class="card"><div class="row">
      <select id="p-status">${[['open', '进行中'], ['done', '已完成'], ['dropped', '已忽略'], ['all', '全部']].map(([k, t]) => `<option value="${k}" ${k === planState.status ? 'selected' : ''}>${t}</option>`).join('')}</select>
      <select id="p-kind"><option value="">全部类型</option>${Object.entries(KIND_CN).map(([k, t]) => `<option value="${k}" ${k === planState.kind ? 'selected' : ''}>${t}</option>`).join('')}</select>
      <select id="p-group"><option value="">全部群</option>${gs.map(([id, n]) => `<option value="${id}" ${String(id) === String(planState.group) ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>
      <input id="p-q" placeholder="搜索标题/详情" value="${esc(planState.q)}" style="flex:1;min-width:130px">
      <label class="toggle"><input type="checkbox" id="p-soon" ${planState.soon ? 'checked' : ''}><span>仅 7 天内</span></label>
      <button class="primary" id="p-go">筛选</button>
      <a class="btn" href="/api/export.xlsx">导出表格(Excel)</a>
      <a class="btn" href="/api/export.docx">导出文档(Word)</a>
    </div>
    <div class="row" style="margin-top:10px">
      <input id="n-title" placeholder="手工新增: 标题" style="flex:1;min-width:150px">
      <input id="n-due" placeholder="截止(如 明天 / 2026-10-20)" style="width:180px">
      <select id="n-kind">${Object.entries(KIND_CN).map(([k, t]) => `<option value="${k}">${t}</option>`).join('')}</select>
      <select id="n-group"><option value="">(不指定群)</option>${gs.map(([id, n]) => `<option value="${id}">${esc(n)}</option>`).join('')}</select>
      <button id="n-add">添加</button>
    </div></div>
    <div class="card"><h3>事件与待办 <span class="muted">勾选完成 / 忽略 / 编辑 / 删除</span></h3>
      <div class="list">${(d.events || []).map(e => `<div class="it" style="align-items:flex-start">
        <span class="pill">${esc(KIND_CN[e.kind] || e.kind)}</span>
        ${dueTag(e.due_at)}
        <div style="flex:1;min-width:160px">
          <b>${esc(e.title)}</b>
          ${e.detail ? `<div class="muted" style="font-size:12.5px">${esc(e.detail)}</div>` : ''}
          <div class="muted" style="font-size:12px">${esc(e.group_name || e.group_id || '')}${e.who ? ' · ' + esc(e.who) : ''} · 最近出现 ${esc(fmtTs(e.last_seen))}${e.status !== 'open' ? ' · ' + esc(statusName(e.status)) : ''}</div>
        </div>
        <span class="row">
          ${e.status === 'open' ? `<button data-done="${e.id}">完成</button><button data-drop="${e.id}">忽略</button>`
            : `<button data-reopen="${e.id}">恢复</button>`}
          <button data-edit="${e.id}">编辑</button>
          <button class="danger" data-del="${e.id}">删除</button>
        </span></div>`).join('') || '<div class="empty">还没有事件。跑一次总结后, 重要事件会自动出现在这里; 也可以在上面手工添加。</div>'}
      </div></div>
    <div class="grid k2">
      <div class="card"><h3>群记忆 <span class="muted">每次总结会带上, 让上下文连续</span></h3>
        <div class="list">${(mem.memories || []).filter(m => m.scope.startsWith('group:')).map(m => `<div class="it" style="align-items:flex-start">
          <b style="min-width:110px">群 ${esc(m.scope.replace('group:', ''))}</b>
          <div style="flex:1;min-width:160px" class="muted">${esc((m.content || '').slice(0, 200))}${(m.content || '').length > 200 ? '…' : ''}</div>
          <button data-mem="${esc(m.scope)}">编辑</button></div>`).join('') || '<div class="empty">暂无记忆</div>'}
        </div></div>
      <div class="card"><h3>个人档案 <span class="muted">判断"与我相关"的依据</span></h3>
        <textarea id="profile" style="width:100%;height:190px">${esc(mem.profile || '')}</textarea>
        <div class="row" style="margin-top:8px"><button class="primary" id="p-save">保存档案</button>
        <span class="muted">目标 / 关注点 / 输出偏好, 越具体越准</span></div></div>
    </div>`;
  const apply = () => { planState.status = q(v, '#p-status').value; planState.kind = q(v, '#p-kind').value;
    planState.group = q(v, '#p-group').value; planState.q = q(v, '#p-q').value;
    planState.soon = q(v, '#p-soon').checked; render(); };
  q(v, '#p-go').onclick = apply;
  q(v, '#p-q').onkeydown = e => { if (e.key === 'Enter') apply(); };
  const act = async (id, body) => { await api('/api/events', {method: 'POST', body: JSON.stringify(Object.assign({id}, body))}); render(); };
  v.querySelectorAll('[data-done]').forEach(b => b.onclick = () => act(+b.dataset.done, {status: 'done'}));
  v.querySelectorAll('[data-drop]').forEach(b => b.onclick = () => act(+b.dataset.drop, {status: 'dropped'}));
  v.querySelectorAll('[data-reopen]').forEach(b => b.onclick = () => act(+b.dataset.reopen, {status: 'open'}));
  v.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
    if (!confirm('删除这条事件?')) return;
    await api('/api/events', {method: 'POST', body: JSON.stringify({id: +b.dataset.del, action: 'delete'})});
    render();
  });
  v.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => {
    const e = (d.events || []).find(x => x.id == b.dataset.edit) || {};
    modal('编辑事件 #' + e.id, `<div class="list">
      <input id="e-title" value="${esc(e.title || '')}" placeholder="标题">
      <input id="e-detail" value="${esc(e.detail || '')}" placeholder="详情">
      <input id="e-due" value="${e.due_at ? new Date(e.due_at * 1000).toISOString().slice(0, 10) : ''}" placeholder="截止 YYYY-MM-DD">
      <select id="e-pri">${[1, 2, 3].map(p => `<option value="${p}" ${p == e.priority ? 'selected' : ''}>优先级 ${p}</option>`).join('')}</select>
      <input id="e-notes" value="${esc(e.notes || '')}" placeholder="备注">
      <button class="primary" id="e-save">保存</button></div>`);
    q(document, '#e-save').onclick = async () => {
      await api('/api/events', {method: 'POST', body: JSON.stringify({
        id: e.id, title: q(document, '#e-title').value, detail: q(document, '#e-detail').value,
        due: q(document, '#e-due').value, priority: +q(document, '#e-pri').value,
        notes: q(document, '#e-notes').value})});
      closeModal(); toast('已保存', 'ok'); render();
    };
  });
  v.querySelectorAll('[data-mem]').forEach(b => b.onclick = () => {
    const scope = b.dataset.mem;
    const cur = (mem.memories || []).find(m => m.scope === scope) || {};
    modal('编辑记忆 · ' + scope, `<textarea id="m-c" style="width:100%;height:220px">${esc(cur.content || '')}</textarea>
      <div class="row" style="margin-top:8px"><button class="primary" id="m-save">保存</button>
      <button class="danger" id="m-del">删除该记忆</button></div>`);
    q(document, '#m-save').onclick = async () => {
      await api('/api/memory', {method: 'POST', body: JSON.stringify({scope, content: q(document, '#m-c').value})});
      closeModal(); toast('已保存', 'ok'); render();
    };
    q(document, '#m-del').onclick = async () => {
      await api('/api/memory/delete', {method: 'POST', body: JSON.stringify({scope})});
      closeModal(); toast('已删除', 'ok'); render();
    };
  });
  q(v, '#p-save').onclick = async () => {
    const r = await api('/api/memory', {method: 'POST', body: JSON.stringify({profile: q(v, '#profile').value})});
    if (r && r.ok) toast('档案已保存(下次总结生效)', 'ok');
  };
  q(v, '#n-add').onclick = async () => {
    const title = q(v, '#n-title').value;
    if (!title) return toast('请填标题', 'warn');
    await api('/api/events/new', {method: 'POST', body: JSON.stringify({
      title, due: q(v, '#n-due').value, kind: q(v, '#n-kind').value,
      group_id: +q(v, '#n-group').value || 0})});
    toast('已添加', 'ok'); render();
  };
};

/* ------------------------------------------------------------------ 消息 */
let msgState = {hours: 24, group: '', kind: '', q: '', page: 1, size: 50};
VIEWS.messages = async (v) => {
  const [g, res] = await Promise.all([api('/api/groups'), api(`/api/messages?${new URLSearchParams(msgState)}`)]);
  const gs = (g && g.groups) || [];
  if (!res) return failCard(v, '消息接口没有返回数据(/api/messages)');
  v.innerHTML = `
    <div class="card"><div class="row">
      <select id="f-hours">${[1, 6, 24, 72, 168, 720].map(h => `<option value="${h}" ${h == msgState.hours ? 'selected' : ''}>近 ${h} 小时</option>`).join('')}</select>
      <select id="f-group" style="max-width:260px"><option value="">全部群</option>${gs.map(x => `<option value="${x.id}" ${String(x.id) === String(msgState.group) ? 'selected' : ''}>${esc(x.name || x.id)}</option>`).join('')}</select>
      <select id="f-kind"><option value="">全部类型</option>${['text', 'image', 'reply', 'at', 'face', 'json', 'file', 'record', 'forward'].map(k => `<option value="${k}" ${k === msgState.kind ? 'selected' : ''}>${kindName(k)}</option>`).join('')}</select>
      <input id="f-q" placeholder="全文检索: 关键词(中文按字匹配)" value="${esc(msgState.q)}" style="flex:1;min-width:140px">
      <button class="primary" id="f-go">筛选</button>
      <a class="btn" href="/api/export.csv?hours=${msgState.hours}&group=${msgState.group}">导出表格</a>
      <span class="pill right">共 ${res.total} 条 · 第 ${res.page}/${res.pages} 页</span>
      <span class="pill ${res.mode === 'fts' ? 'ok' : ''}" title="检索方式">${res.mode === 'fts' ? '全文索引' : (res.mode === 'like' ? '模糊匹配' : '按时间')}</span>
    </div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th style="width:96px">时间</th><th>群</th><th>昵称</th><th style="width:110px">类型</th><th>内容</th></tr></thead>
      <tbody>${(res.rows || []).map(r => `<tr data-id="${r.id}"><td class="muted">${fmtTs(r.ts, 'time')}</td>
        <td>${esc(r.group_name || r.group_id)}</td><td>${hl(r.nickname, msgState.q)}</td>
        <td><span class="pill">${esc(kindsText(r.kinds))}</span></td><td>${hl(r.raw_message, msgState.q)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">没有匹配的消息</td></tr>'}</tbody>
    </table></div>
    <div class="row" style="margin-top:10px">
      <button id="p-prev" ${res.page <= 1 ? 'disabled' : ''}>← 上一页</button>
      <button id="p-next" ${res.page >= res.pages ? 'disabled' : ''}>下一页 →</button>
      <span class="muted right">每页 ${res.size} 条</span>
    </div></div>`;
  const apply = () => { msgState.hours = +q(v, '#f-hours').value; msgState.group = q(v, '#f-group').value;
    msgState.kind = q(v, '#f-kind').value; msgState.q = q(v, '#f-q').value; msgState.page = 1; render(); };
  q(v, '#f-go').onclick = apply;
  q(v, '#f-q').onkeydown = e => { if (e.key === 'Enter') apply(); };
  q(v, '#p-prev').onclick = () => { msgState.page--; render(); };
  q(v, '#p-next').onclick = () => { msgState.page++; render(); };
  v.querySelectorAll('tr[data-id]').forEach(tr => tr.onclick = () => {
    const r = (res.rows || []).find(x => x.id == tr.dataset.id);
    modal('消息详情', `<div class="list"><div class="it"><b>时间</b><span class="right">${fmtTs(r.ts)}</span></div>
      <div class="it"><b>群</b><span class="right">${esc(r.group_name || r.group_id)} (${r.group_id})</span></div>
      <div class="it"><b>发送者</b><span class="right">${esc(r.nickname)} (${r.user_id})</span></div>
      <div class="it"><b>类型</b><span class="right">${esc(r.kinds)}</span></div></div>
      <pre style="margin-top:10px">${esc(r.raw_message)}</pre>`);
  });
};

/* ------------------------------------------------------------------ 群组 */
VIEWS.groups = async (v) => {
  const d = await api('/api/groups?refresh=1'); if (!d) return failCard(v, '群组接口没有返回数据(/api/groups)');
  const gs = d.groups || [];
  v.innerHTML = `
    <div class="card"><div class="row">
      <span class="pill">NapCat 实时群数 <b>${d.napcat_count}</b></span>
      <span class="pill ok">已启用总结 ${d.enabled_count}</span>
      <span class="pill">本地记录 ${gs.length}</span>
      ${d.napcat_error ? `<span class="pill bad">NapCat 接口异常: ${esc(d.napcat_error)}</span>` : ''}
      <input id="g-q" placeholder="搜索群名/群号" style="flex:1;min-width:150px">
      <button id="g-all-on">全部启用</button><button id="g-all-off">全部停用</button>
      <button class="primary" id="g-refresh">刷新</button>
      <span class="muted right">勾选=参与每日总结</span>
    </div></div>
    <div class="card"><div class="chips" id="g-list">
      ${gs.map(x => `<label class="chip ${x.enabled ? 'on' : ''}" data-name="${esc((x.name || '') + ' ' + x.id).toLowerCase()}">
        <input type="checkbox" data-id="${x.id}" ${x.enabled ? 'checked' : ''}>
        <span>${esc(x.name || '未知')}</span><span class="num">${x.count || 0}${x.in_napcat ? '' : ' ·离线'}</span></label>`).join('')}
      ${gs.length ? '' : '<div class="empty">还没有群数据(NapCat 未连接或未收到消息)</div>'}
    </div></div>`;
  q(v, '#g-q').oninput = e => {
    const kw = e.target.value.toLowerCase();
    v.querySelectorAll('.chip').forEach(c => c.style.display = c.dataset.name.includes(kw) ? '' : 'none');
  };
  const bulk = async (on) => {
    const ids = [...v.querySelectorAll('.chip input:checked')].map(i => +i.dataset.id);
    if (!ids.length) return toast('没有勾选任何群', 'warn');
    await api('/api/groups/bulk', {method: 'POST', body: JSON.stringify({ids, enabled: on})});
    toast(`已${on ? '启用' : '停用'} ${ids.length} 个群`, 'ok'); render();
  };
  q(v, '#g-all-on').onclick = () => bulk(true);
  q(v, '#g-all-off').onclick = () => {
    v.querySelectorAll('.chip input').forEach(i => i.checked = true); bulk(false);
  };
  q(v, '#g-refresh').onclick = () => render();
  v.querySelectorAll('.chip input').forEach(cb => cb.onchange = async () => {
    const r = await api('/api/groups', {method: 'POST', body: JSON.stringify({id: +cb.dataset.id, enabled: cb.checked})});
    cb.closest('.chip').classList.toggle('on', cb.checked);
    if (r && r.ok) toast('已更新', 'ok');
  });
};

/* ------------------------------------------------------------------ 任务 */
VIEWS.tasks = async (v) => {
  const [d, s] = await Promise.all([api('/api/runs?limit=80'), api('/api/schedule')]);
  if (!d) return failCard(v, '任务接口没有返回数据(/api/runs)');
  const sc = (s && s.schedule) || {};
  const nx = (s && s.next) || {};
  const mins = nx.ts ? Math.max(0, Math.round((nx.ts - Date.now() / 1000) / 60)) : null;
  const leftText = mins === null ? '' : (mins < 60 ? mins + ' 分钟后' : Math.floor(mins / 60) + ' 小时 ' + (mins % 60) + ' 分后');
  const wdName = ['一', '二', '三', '四', '五', '六', '日'];
  const wd = sc.weekdays || [1, 2, 3, 4, 5];
  const hide = (cond) => cond ? '' : 'hidden';
  v.innerHTML = `<div class="card"><h3>定时设置 <span class="muted">总结时间可以随便调, 不必按天</span></h3>
    <div class="row">
      <label class="toggle"><input type="checkbox" id="s-on" ${sc.enabled !== false ? 'checked' : ''}><span>启用定时</span></label>
      <select id="s-mode">
        <option value="daily" ${(sc.mode || 'daily') === 'daily' ? 'selected' : ''}>每天定点</option>
        <option value="interval" ${sc.mode === 'interval' ? 'selected' : ''}>每隔一段时间</option>
        <option value="weekly" ${sc.mode === 'weekly' ? 'selected' : ''}>每周几天</option>
      </select>
      <span id="s-box-daily" class="row ${hide((sc.mode || 'daily') === 'daily')}">时间点
        <input id="s-times" value="${esc((sc.times || ['22:00']).join(', '))}" placeholder="22:00, 09:30" style="width:170px" title="可填多个, 逗号分隔"></span>
      <span id="s-box-interval" class="row ${hide(sc.mode === 'interval')}">每隔
        <input id="s-every" type="number" min="1" max="10080" value="${sc.every_minutes || 360}" style="width:90px"> 分钟</span>
      <span id="s-box-weekly" class="row ${hide(sc.mode === 'weekly')}">
        ${wdName.map((n, i) => `<label class="toggle"><input type="checkbox" class="s-wd" value="${i + 1}" ${wd.includes(i + 1) ? 'checked' : ''}><span>周${n}</span></label>`).join('')}
        <input id="s-wtime" value="${esc(sc.weekly_time || '22:00')}" style="width:76px"></span>
      <label class="toggle" title="自上次总结以来消息太少就不跑, 避免空报告与浪费"><input type="checkbox" id="s-skip" ${sc.skip_if_no_new !== false ? 'checked' : ''}><span>无新消息就跳过</span></label>
      <input id="s-min" type="number" min="0" value="${sc.min_new_messages != null ? sc.min_new_messages : 5}" style="width:70px" title="至少多少条新消息才值得跑">
      <button class="primary" id="s-save">保存定时</button>
    </div>
    <div class="muted" style="margin-top:8px">当前: <b>${esc(nx.label || '未启用')}</b>${nx.ts ? ` · 下次 <b>${fmtTs(nx.ts)}</b>（${leftText}）` : ''}${s.last_run ? ` · 上次运行 ${fmtTs(s.last_run)}` : ''}${s.new_since_last != null ? ` · 距上次新增 ${s.new_since_last} 条消息` : ''}</div>
    <div class="muted">三种模式: <b>每天定点</b>(可填多个时间) / <b>每隔一段</b>(如 360 分钟 = 6 小时) / <b>每周几天</b>(周一~周日任选 + 时刻) · 时间窗(回看多久)在「配置」页“总结器”里改</div>
  </div>
    <div class="card"><div class="row">
      <button class="primary" id="t-run">立即总结</button><button id="t-dry" title="只组装提示词, 不调用模型">试运行(不花钱)</button>
      <span class="muted">想马上看结果就点左边; 定时到点后会自动跑并把摘要私聊发给你</span></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>序号</th><th>任务</th><th>开始</th><th>耗时</th><th>状态</th><th>详情</th><th>用量(输入/输出)</th><th>费用</th></tr></thead><tbody>
    ${(d.runs || []).map(r => `<tr><td class="muted">${r.id}</td><td>${esc(jobName(r.job))}</td><td>${fmtTs(r.started_at)}</td>
      <td>${dur(r.started_at, r.finished_at)}</td>
      <td><span class="pill ${r.status === 'ok' ? 'ok' : (r.status === 'failed' ? 'bad' : 'warn')}">${esc(runName(r.status))}</span></td>
      <td class="muted">${esc(r.detail || '')}</td><td class="muted">${r.tokens_in || 0}/${r.tokens_out || 0}</td>
      <td class="muted">${r.cost ? r.cost.toFixed(4) : '—'}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">还没有任务记录</td></tr>'}
    </tbody></table></div>
    <div class="muted" style="margin-top:8px">「用量」= 送入模型与模型输出的词元数; 「费用」按配置里的单价计算, 未填单价则为 —</div></div>`;
  const mode = q(v, '#s-mode');
  const syncMode = () => {
    const m = mode.value;
    q(v, '#s-box-daily').classList.toggle('hidden', m !== 'daily');
    q(v, '#s-box-interval').classList.toggle('hidden', m !== 'interval');
    q(v, '#s-box-weekly').classList.toggle('hidden', m !== 'weekly');
  };
  mode.onchange = syncMode;
  syncMode();
  q(v, '#s-save').onclick = async () => {
    const body = {enabled: q(v, '#s-on').checked, mode: mode.value,
      times: q(v, '#s-times').value, every_minutes: +q(v, '#s-every').value,
      weekdays: [...v.querySelectorAll('.s-wd:checked')].map(x => +x.value),
      weekly_time: q(v, '#s-wtime').value,
      skip_if_no_new: q(v, '#s-skip').checked, min_new_messages: +q(v, '#s-min').value};
    const r = await api('/api/schedule', {method: 'POST', body: JSON.stringify(body)});
    if (r && r.ok) { toast('定时已保存: ' + ((r.next || {}).label || ''), 'ok'); render(); }
    else if (r && r.errors && r.errors.length) toast(r.errors.join('; '), 'bad');
  };
  const run = async (dry) => { const r = await api('/api/summarize', {method: 'POST', body: JSON.stringify({dry_run: dry})});
    if (r && r.ok) { toast(dry ? '试运行已启动(不调用模型)' : '总结任务已启动', 'ok'); setTimeout(render, 4000); } };
  q(v, '#t-run').onclick = () => run(false);
  q(v, '#t-dry').onclick = () => run(true);
};

/* ------------------------------------------------------------------ 报告 */
VIEWS.reports = async (v) => {
  const d = await api('/api/reports'); if (!d) return failCard(v, '报告列表没有返回数据(/api/reports)');
  v.innerHTML = `<div class="card"><div class="row">
      <b>报告格式</b>
      <a class="btn" href="/api/export.xlsx">导出表格(Excel)</a>
      <a class="btn" href="/api/export.docx">导出文档(Word)</a>
      <span class="muted">报告页可直接在浏览器打开(自包含网页, 含事件卡片与记忆)</span></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>报告</th><th>大小</th><th>生成时间</th><th></th></tr></thead><tbody>
    ${(d.reports || []).map(r => `<tr><td>${esc(r.name)}</td><td class="muted">${humanBytes(r.size)}</td>
      <td class="muted">${fmtTs(r.mtime)}</td><td class="row">
      <button data-open="${esc(r.name)}">阅读</button>
      ${r.name.endsWith('.html') ? `<a class="btn" href="/r/${esc(r.name)}" target="_blank">网页版</a>` : ''}
      ${r.name.endsWith('.md') ? `<a class="btn" href="/api/report.docx?name=${encodeURIComponent(r.name)}">导出 Word</a>` : ''}
      </td></tr>`).join('')
      || '<tr><td colspan="4" class="empty">还没有报告, 去「任务」页点一次「立即总结」</td></tr>'}
  </tbody></table></div></div>`;
  v.querySelectorAll('[data-open]').forEach(b => b.onclick = async () => {
    const n = b.dataset.open;
    if (n.endsWith('.html')) { window.open('/r/' + encodeURIComponent(n), '_blank'); return; }
    const r = await api('/api/report?name=' + encodeURIComponent(n));
    if (r && r.content) modal(r.name, `<div class="md">${md(r.content)}</div>`);
  });
};

/* ------------------------------------------------------------------ 日志 */
const LOG_CN = {collector: '采集器', scheduler: '调度器', web: '控制台', webapp: '控制台',
  summarizer: '总结器', client: '浏览器上报', core: '核心', extras: '旁路服务', '': ''};
const logName = (n) => LOG_CN[n] || n;
let logName_ = 'collector';
VIEWS.logs = async (v) => {
  const d = await api('/api/logs?name=' + encodeURIComponent(logName_) + '&lines=300'); if (!d) return failCard(v, '日志接口没有返回数据(/api/logs)');
  v.innerHTML = `<div class="card"><div class="row">
      ${(d.available || ['collector']).map(n => `<button class="${n === d.name ? 'primary' : ''}" data-log="${n}">${logName(n)}</button>`).join('')}
      <span class="muted">当前: ${logName(d.name)}（文件 ${esc(d.name)}.log）</span>
      <label class="toggle right"><input type="checkbox" id="l-follow" checked><span>自动刷新</span></label>
    </div></div>
    <div class="card"><pre id="log-box">${esc(d.text || '(空)')}</pre></div>`;
  v.querySelectorAll('[data-log]').forEach(b => b.onclick = () => { logName_ = b.dataset.log; render(); });
  const box = q(v, '#log-box'); box.scrollTop = box.scrollHeight;
};

/* ------------------------------------------------------------------ 配置 */
const CFG_SEC = {web: '控制台', collector: '采集器', summarizer: '总结器', push: '推送',
  schedule: '定时', retention: '数据保留', extract: '整理规则', enrich: '补料(图片/文件/链接)',
  watchdog: '看门狗(保活)'};
/* [中文名, 说明] */
const CFG_CN = {
  web: {host: ['监听地址', '默认 127.0.0.1 只允许本机访问, 更安全'],
        port: ['端口', '浏览器访问用的端口'], token: ['访问令牌', '留空表示不修改'],
        session_hours: ['登录有效期(小时)', '过期后需重新用令牌进入'],
        auto_refresh: ['页面自动刷新(秒)', '0 或关闭自动刷新开关即可不刷新']},
  collector: {host: ['监听地址', '保持 127.0.0.1'], port: ['端口', 'NapCat 的消息上报地址'],
              secret: ['签名密钥', '可选; 与 NapCat 上报配置一致'],
              require_signature: ['强制校验签名', '开启后没有正确签名的上报一律拒绝'],
              groups: ['采集过滤: 只收这些群', '填群号, 逗号分隔; 留空=全部都收(总结哪些群在「群组」页勾选)'],
              kinds: ['采集过滤: 只收这些类型', '如 text,image; 留空=全部都收'],
              store_private: ['也保存私聊消息', '默认只存群消息; 想连私聊内容一起进总结就打开'],
              command_private: ['私聊不入库也交给插件', '关掉后「在 QQ 里问 AI」不再响应私聊(隐私换功能)'],
              debug: ['记录每条消息到日志', '排错用, 平时建议关闭']},
  summarizer: {base_url: ['模型接口地址', 'OpenAI 兼容接口, 例如 https://api.deepseek.com/v1'],
               api_key: ['模型密钥', '留空表示不修改'], model: ['模型名', '例如 deepseek-chat'],
               temperature: ['随机性', '0 最稳定, 1 最发散; 建议 0.2~0.4'],
               timeout: ['单次调用超时(秒)', ''], groups: ['(已合并到群组页)', '控制台「群组」页的勾选优先于这里'],
               kinds: ['总结时分析哪些类型', '如 text,image,reply,at,json'],
               window_hours: ['时间窗(小时)', '每次总结回看多久的消息(定时模式与时间在「任务」页)'],
               min_messages: ['少于此条数不总结', '避免对冷清群产生无意义报告'],
               max_messages: ['每次最多取多少条', '越大越贵, 建议 300~800'],
               max_chars_per_group: ['单群记录上限(字符)', '超出会截断较早内容'],
               prompt_extra: ['附加要求', '会追加到提示词里, 用来固定输出偏好'],
               input_price_per_mtok: ['输入单价(元/百万词元)', '填了才会算费用'],
               output_price_per_mtok: ['输出单价(元/百万词元)', '']},
  push: {enable: ['启用推送', '通过 NapCat 发到你的 QQ'],
         user_id: ['推送到我的 QQ 号', ''], group_id: ['推送到群号', '留 0 = 只发私聊, 不在群里发言'],
         napcat_api_port: ['NapCat 接口端口', '默认 3000'],
         token: ['NapCat 接口令牌', '留空表示不修改'],
         max_chars: ['单条消息上限(字符)', '超长会分段发送'],
         report_link: ['附带报告页链接', '推送末尾附上本机报告页地址']},
  schedule: {enabled: ['启用定时总结', '定时的模式与时间在「任务」页里改'],
             mode: ['调度模式', '每天定点 / 每隔一段 / 每周几天'],
             times: ['每天的时间点', '如 22:00; 可写多个'],
             every_minutes: ['间隔(分钟)', '每隔模式使用, 360 = 6 小时'],
             weekdays: ['每周哪几天', '1=周一 … 7=周日'],
             weekly_time: ['每周的时刻', '如 22:00'],
             skip_if_no_new: ['无新消息就跳过', '避免空报告与浪费'],
             min_new_messages: ['最少新消息数', '低于此数不跑']},
  retention: {message_days: ['消息保留天数', '0 = 永久保留'],
              log_max_mb: ['单个日志上限(MB)', '超出自动轮转'],
              backup_keep: ['备份保留份数', '每天自动备份, 超出删旧']},
  extract: {structured: ['结构化抽取', '关闭则退回散文式总结(不产生事件列表)'],
            max_events: ['每群最多事件数', ''], max_todos: ['每群最多待办数', ''],
            max_links: ['报告里最多保留几条链接', '与「补料」里"每群最多抓几条正文"不同: 这里限制报告条数'],
            memory_chars: ['记忆长度上限(字符)', '每群保留的"上次记忆"长度'],
            low_confidence_drop: ['丢弃低置信事件', '低于该值(0~1)的事件不入库']},
  enrich: {vision: ['图片识别', '把群里图片交给多模态模型描述'],
           files: ['文件解析', '文档内容读进总结'],
           links: ['链接抓取', '抓取文章类链接的正文']},
  watchdog: {enabled: ['启用看门狗', '每 60 秒体检一次, 服务死掉或假活就自动拉起'],
             interval: ['体检间隔(秒)', '建议 30~120'],
             manage_extras: ['同时守护旁路服务', '开启后 NapCat 不在也会尝试拉起(它会重新登录, 较慢)']},
};
const CFG_SUB = {
  vision: {enable: '启用', model: '专用模型(空=用上面的模型)', max_images: '每群最多识别几张',
           max_bytes: '单张图片最大字节', detail: '描述详略'},
  files: {enable: '启用', max_mb: '单个文件上限(MB)', max_files: '每群最多几个文件',
          max_chars: '正文截取上限(字符)'},
  links: {enable: '启用', max_links: '每群最多抓几条正文', max_bytes: '单个网页最大字节',
          timeout: '抓取超时(秒)', max_redirects: '最多跳转次数', allow_all: '允许访问内网(危险, 不建议)'},
};
/* 与其它位置重复的项: 隐藏, 只在唯一入口维护(避免两处能改、互相打架) */
const CFG_HIDE = {
  summarizer: ['groups'],
  schedule: ['mode', 'times', 'every_minutes', 'weekdays', 'weekly_time', 'skip_if_no_new', 'min_new_messages'],
};
const CFG_NOTE = {
  summarizer: '「总结哪些群」请在 **「群组」页**勾选(那里是实时群列表, 也是唯一准绳), 因此这里不再重复显示。',
  schedule: '定时的模式与时间请在 **「任务」页顶部的定时设置**里改(支持 每天定点 / 每隔一段 / 每周几天); 这里只保留总开关。',
  collector: '「只收这些群/类型」是**采集阶段**的过滤(决定哪些消息入库); 总结阶段分析哪些类型在上面的「总结器」里设置。',
};

VIEWS.config = async (v) => {
  const d = await api('/api/config'); if (!d) return failCard(v, '配置接口没有返回数据(/api/config)');
  const cfg = d.config;
  const isSecret = (k) => ['token', 'secret', 'api_key'].includes(k);
  const label = (s, k) => (CFG_CN[s] || {})[k] || null;
  const lab = (s, k) => { const v = label(s, k); return [ (v && v[0]) || k, (v && v[1]) || '' ]; };
  const input = (id, val, lp) => {
    const nm = esc(lp[0]), hint = esc(lp[1] || '');
    if (typeof val === 'boolean') {
      return `<label class="toggle" title="${hint}"><input type="checkbox" id="${id}" ${val ? 'checked' : ''}><span>${nm}</span></label>`;
    }
    if (Array.isArray(val)) {
      return `<label style="flex:1;min-width:200px" title="${hint}">
        <span class="muted">${nm}</span>
        <input id="${id}" style="width:100%" value="${esc(val.join(', '))}" placeholder="逗号分隔"></label>`;
    }
    if (typeof val === 'number') {
      return `<label title="${hint}"><span class="muted">${nm}</span>
        <input id="${id}" type="number" value="${val}" style="width:120px"></label>`;
    }
    const sec = id.endsWith('_token') || id.endsWith('_secret') || id.endsWith('_api_key');
    return `<label style="flex:1;min-width:220px" title="${hint}">
      <span class="muted">${nm}${sec ? '(留空=不改)' : ''}</span>
      <input id="${id}" style="width:100%" ${sec ? 'type="password" placeholder="' + esc(val) + '"' : 'value="' + esc(val) + '"'}></label>`;
  };
  const subBlock = (s, k, obj) => `<div style="flex:1;min-width:260px;border:1px dashed var(--line);border-radius:10px;padding:8px 10px">
      <div class="muted" style="font-size:12px;margin-bottom:6px">${esc(lab(s, k)[0])} · ${esc(lab(s, k)[1])}</div>
      <div class="row">${Object.entries(obj).map(([k2, v2]) =>
        input(`c_${s}_${k}_${k2}`, v2, [(CFG_SUB[k] || {})[k2] || k2, ''])).join('')}</div></div>`;
  const secHtml = (s, sec) => {
    const hideList = CFG_HIDE[s] || [];
    const shown = Object.entries(sec).filter(([k]) => !hideList.includes(k));
    const hiddenNames = Object.entries(sec).filter(([k]) => hideList.includes(k)).map(([k]) => lab(s, k)[0]);
    const note = CFG_NOTE[s] ? `<div class="muted" style="margin-top:10px;font-size:12.5px">说明: ${CFG_NOTE[s].replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')}${hiddenNames.length ? ' <span class="pill">已隐藏重复项: ' + hiddenNames.join('、') + '</span>' : ''}</div>` : '';
    return `<div class="card"><h3>${CFG_SEC[s]} <span class="muted mono" style="font-size:12px">${s}</span></h3>
      <div class="row">${shown.map(([k, val]) =>
        (val && typeof val === 'object' && !Array.isArray(val)) ? subBlock(s, k, val) : input(`c_${s}_${k}`, val, lab(s, k))
      ).join('')}</div>${note}</div>`;
  };
  v.innerHTML = `${(d.errors || []).length ? `<div class="card"><b class="err">配置有问题</b><ul>${d.errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul></div>` : ''}
    ${Object.entries(cfg).filter(([s]) => CFG_SEC[s]).map(([s, sec]) => secHtml(s, sec)).join('')}
    <div class="card"><h3>其它</h3>
      <pre>${esc(JSON.stringify({version: cfg.version, paths: d.paths}, null, 2))}</pre>
      <div class="row" style="margin-top:10px"><button class="primary" id="cfg-save">保存配置</button>
      <span class="muted">保存后采集器/调度器 15 秒内自动生效; 鼠标悬停在选项名上可看说明</span></div></div>`;
  q(v, '#cfg-save').onclick = async () => {
    const patch = {};
    const put = (s, obj, k, v2) => { patch[s] = patch[s] || {}; patch[s][k] = v2; };
    for (const [s, sec] of Object.entries(cfg)) {
      if (!CFG_SEC[s]) continue;
      patch[s] = patch[s] || {};
      for (const [k, val] of Object.entries(sec)) {
        if (val && typeof val === 'object' && !Array.isArray(val)) {
          const sub = {};
          for (const k2 of Object.keys(val)) {
            const el = q(v, `#c_${s}_${k}_${k2}`); if (!el || el.tagName === undefined) continue;
            if (el.type === 'checkbox') sub[k2] = el.checked;
            else if (typeof val[k2] === 'number') sub[k2] = Number(el.value);
            else if (typeof val[k2] === 'boolean') sub[k2] = !!el.checked;
            else if (el.value !== '') sub[k2] = el.value;
          }
          Object.assign(patch[s][k] = patch[s][k] || {}, sub);
          continue;
        }
        const el = q(v, `#c_${s}_${k}`); if (!el) continue;
        if (el.type === 'checkbox') put(s, null, k, el.checked);
        else if (typeof val === 'number') put(s, null, k, Number(el.value));
        else if (Array.isArray(val)) put(s, null, k, el.value.split(',').map(x => x.trim()).filter(Boolean)
          .map(x => /^\d+$/.test(x) ? Number(x) : x));
        else if (!isSecret(k) || el.value) put(s, null, k, el.value);
      }
    }
    const r = await api('/api/config', {method: 'POST', body: JSON.stringify({config: patch})});
    if (r && r.ok) { toast('配置已保存', 'ok'); render(); }
  };
};

/* ------------------------------------------------------------------ 运维 */
const OPS_TABS = [['svc', '服务'], ['res', '资源与功耗'], ['crash', '异常账本'],
                  ['log', '实时日志'], ['self', '自检'], ['backup', '备份与维护']];
const _opsLegacy = async (v) => {
  const d = await api('/api/overview?hours=1'); if (!d) return failCard(v, '服务状态没有返回数据(/api/overview)');
  v.innerHTML = `
    <div class="grid k2">
      <div class="card"><h3>进程与端口 <span class="muted">进程号 / 内存 / 运行时长</span></h3>
        <div class="list">${Object.entries(d.services).map(([n, s]) => `<div class="it"><b style="width:78px">${svcName(n)}</b>${svcPill(s)}
          <span class="muted">${s.pid ? '进程号 ' + s.pid : ''}${s.rss ? ' · 内存 ' + humanBytes(s.rss) : ''}${s.etime ? ' · 已运行 ' + s.etime : ''}${s.port ? ' · 端口 ' + s.port : ''}</span>
          <span class="right row">
          <button data-act="start" data-svc="${n}">启动</button>
          <button data-act="restart" data-svc="${n}">重启</button>
          <button class="danger" data-act="stop" data-svc="${n}">停止</button></span></div>`).join('')}</div>
        <div class="row" style="margin-top:10px">
          ${(d.ecosystem || []).map(e => `<span class="pill ${e.ok ? 'ok' : 'bad'}">${esc(e.name)} :${e.port} ${e.ok ? '通' : '断'}</span>`).join('')}
        </div>
        <div class="row" style="margin-top:8px">
          <span class="pill">下次总结 ${d.scheduler.next && d.scheduler.next.ts ? fmtTs(d.scheduler.next.ts, 'time') : '-'}</span>
          <span class="pill">启用群 ${d.enabled_groups}</span>
          <span class="pill">报告 ${d.storage.reports}</span>
          <span class="pill">备份 ${d.storage.backups}</span>
        </div>
      </div>
      <div class="card"><h3>数据与备份</h3>
        <div class="list">
          <div class="it"><b>数据库</b><span class="muted right">${humanBytes(d.db_size)} · ${core_count(d)}</span></div>
          <div class="it"><b>全文索引</b><span class="muted right">${d.fts && d.fts.available ? '已建立 · ' + d.fts.rows + ' 条' : '不可用(退化为模糊匹配)'}</span></div>
          <div class="it"><b>数据目录</b><span class="muted right">${humanBytes(d.data_size)}</span></div>
          <div class="it"><b>磁盘</b><span class="muted right">剩余 ${humanBytes(d.disk.free)} / 共 ${humanBytes(d.disk.total)}</span></div>
        </div>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="bk">立即备份</button>
          <button id="reidx">重建全文索引</button>
          <a class="btn" href="/api/export.csv?hours=168">导出近 7 天 CSV</a>
          <span class="muted">备份目录 data/backups</span></div></div>
      <div class="card"><h3>旁路服务 <span class="muted">可选组件, QQOps 不依赖</span></h3>
        <div class="list">${Object.entries(d.extras || {}).map(([n, x]) => `<div class="it"><b style="width:74px">${svcName(n)}</b>${svcPill(x)}
          <span class="muted">${x.pid ? '进程号 ' + x.pid : ''}${x.rss ? ' · 内存 ' + humanBytes(x.rss) : ''}</span>
          <span class="right row">
            <button data-xact="start" data-xsvc="${n}">启动</button>
            <button data-xact="restart" data-xsvc="${n}">重启</button>
            <button class="danger" data-xact="stop" data-xsvc="${n}">停止</button></span></div>`).join('')}</div>
        <div class="muted" style="margin-top:8px">
          ${esc(((d.extras || {}).napcat || {}).note || '')}<br>${esc(((d.extras || {}).astrbot || {}).note || '')}
        </div></div>
    </div>
    <div class="grid k2">
      <div class="card"><h3>NapCat</h3>
        <div class="list">
          <div class="it"><b>状态</b><span class="${d.napcat.ok ? 'pill ok' : 'pill bad'} right">${d.napcat.ok ? '在线' : '离线'}</span></div>
          <div class="it"><b>账号</b><span class="right">${esc(d.napcat.nickname || '-')} ${d.napcat.user_id ? '(' + d.napcat.user_id + ')' : ''}</span></div>
          <div class="it"><b>延迟</b><span class="right muted">${d.napcat.ms} ms</span></div>
        </div>
        <div class="row" style="margin-top:10px"><b>发送测试</b><input id="send-text" placeholder="要发送的内容" style="flex:1;min-width:160px">
          <input id="send-uid" type="number" placeholder="目标QQ" style="width:120px"><button class="primary" id="send-go">发送</button></div>
        <div class="muted" style="margin-top:6px">留空 QQ 则按配置的 push 目标发送</div></div>
      <div class="card"><h3>采集指标</h3>
        <div class="list">
          <div class="it"><b>收下(入库成功)</b><span class="right">${(d.counters || {}).ingested || 0} 条</span></div>
          <div class="it"><b>跳过(被群/类型过滤)</b><span class="right">${(d.counters || {}).skipped || 0} 条</span></div>
          <div class="it"><b>拒绝(签名不符)</b><span class="right">${(d.counters || {}).rejected || 0} 条</span></div>
          <div class="it"><b>错误(解析或落库失败)</b><span class="right">${(d.counters || {}).errors || 0} 条</span></div>
        </div>
        <div class="muted" style="margin-top:8px">采集器自启动以来的累计计数; 重启服务后归零重新计。</div></div>
    </div>`;
  v.querySelectorAll('[data-act]').forEach(b => b.onclick = async () => {
    const svc = b.dataset.svc, act = b.dataset.act;
    if (svc === 'web' && act !== 'start') {
      toast('控制台正在重启, 3 秒后刷新页面…', 'warn');
      fetch('/api/service', {method: 'POST', body: JSON.stringify({name: svc, action: act}),
        headers: {'Content-Type': 'application/json'}}).catch(() => {});
      setTimeout(() => location.reload(), 3500);
      return;
    }
    const r = await api('/api/service', {method: 'POST', body: JSON.stringify({name: svc, action: act})});
    toast((r && r.message) || '已提交', 'ok'); setTimeout(render, 2000);
  });
  q(v, '#bk').onclick = async () => { const r = await api('/api/backup', {method: 'POST', body: '{}'});
    if (r && r.ok) toast('备份完成: ' + r.file, 'ok'); };
  q(v, '#reidx').onclick = async () => { const r = await api('/api/reindex', {method: 'POST', body: '{}'});
    if (r && r.ok) toast('索引已重建: ' + r.rows + ' 条', 'ok'); };
  v.querySelectorAll('[data-xsvc]').forEach(b => b.onclick = async () => {
    const svc = b.dataset.xsvc, act = b.dataset.xact;
    toast(act + ' ' + svc + ' 执行中…', 'warn');
    const r = await api('/api/service', {method: 'POST', body: JSON.stringify({name: svc, action: act})});
    toast((r && r.message) || '已提交', 'ok');
    setTimeout(render, 3000);
  });
  q(v, '#send-go').onclick = async () => {
    const text = q(v, '#send-text').value; const uid = +q(v, '#send-uid').value || 0;
    if (!text) return toast('请输入内容', 'warn');
    const r = await api('/api/napcat/send', {method: 'POST', body: JSON.stringify({text, user_id: uid})});
    if (r && r.ok) toast('已发送 ' + r.chunks + ' 段', 'ok');
  };
};
function core_count(d) { return (d.stats && d.stats.total) + ' 条消息'; }

/* --------------------------------------------- 运维: 分页签(服务/账本/日志/自检/备份) */
let opsTab = 'svc', logCursor = 0, logTimer = null, logPaused = false;
const LOG_LEVEL_CN = {DEBUG: '调试', INFO: '信息', WARN: '警告', ERROR: '错误'};
VIEWS.ops = async (v) => {
  let crashBadge = '';
  try {
    const c = await api('/api/app/crashes?limit=1');
    if (c && c.stats && c.stats.total) crashBadge = c.stats.total;
  } catch (e) { /* 徽标取不到不影响页面 */ }
  v.innerHTML = tabsHtml('ops', OPS_TABS.map(([k, t]) => [k, t, k === 'crash' ? crashBadge : '']), opsTab)
    + '<div id="ops-body"></div>';
  wireTabs(v, 'ops', t => {
    opsTab = t;
    if (logTimer) { clearInterval(logTimer); logTimer = null; }
    VIEWS.ops(v);
  });
  const box = q(v, '#ops-body');
  if (opsTab === 'svc') return _opsLegacy(box);
  if (opsTab === 'res') return _opsRes(box);
  if (opsTab === 'crash') return _opsCrash(box);
  if (opsTab === 'log') return _opsLog(box);
  if (opsTab === 'self') return _opsSelf(box);
  return _opsBackup(box);
};
/* --------------------------------------------- 运维: 资源与功耗(进程合并/低功耗) */
const CARD_CN = {forward: '合并转发卡片', share: '官方分享卡片', miniapp: '小程序卡片', json: '裸 JSON 卡片'};
const MODE_CN = {single: '单进程(省内存)', multi: '多进程(隔离好)', auto: '自动'};
const POWER_CN = {performance: '性能', balanced: '均衡', saver: '省电'};
async function _opsRes(box) {
  const d = await api('/api/ops/resources');
  if (!d) { box.innerHTML = errState('资源数据读取失败'); wireRetry(box, render); return; }
  const rt = d.runtime || {}, prof = d.power || {}, tot = d.totals || {};
  const mb = n => (n / 1048576).toFixed(1) + ' MB';
  const mode = rt.mode || 'multi';
  const cur = prof.power_mode || 'balanced';
  const svc = (d.services || []).map(s => `<div class="it">
      <span class="pill ${s.running ? 'ok' : 'bad'}"><span class="dot ${s.running ? 'ok' : 'bad'}"></span>${s.running ? '运行' : '停'}</span>
      <b style="width:76px">${esc(svcName(s.name))}</b>
      <span class="muted">${s.pid ? '进程号 ' + s.pid : ''}${s.port ? ' · 端口 ' + s.port : ''}</span>
      <span class="right muted">${s.rss ? mb(s.rss) : ''}</span></div>`).join('');
  const procs = (d.processes || []).map(p => `<div class="it">
      <b>pid ${p.pid}</b>${p.self ? '<span class="pill">当前进程</span>' : ''}
      <span class="muted ellip">${esc(p.cmd || '')}</span>
      <span class="right muted">${p.rss ? mb(p.rss) : ''}${p.etime ? ' · ' + esc(p.etime) : ''}</span></div>`).join('');
  const est = d.estimate || {};
  box.innerHTML = `<div class="grid k2">
    ${card('运行形态', `<div class="row" style="margin-bottom:8px">
        <span class="pill ${mode === 'single' ? 'ok' : ''}">${esc(MODE_CN[mode] || mode)}</span>
        ${rt.pid ? `<span class="pill">pid ${rt.pid}</span>` : ''}
        <span class="pill">本机 QQOps 进程 ${tot.count || 0} 个</span>
        <span class="pill">合计内存 ${mb(tot.rss || 0)}</span>
      </div>
      <div class="row">
        <button data-mode="single" ${mode === 'single' ? 'disabled' : ''}>合并为单进程</button>
        <button data-mode="multi" ${mode === 'multi' ? 'disabled' : ''}>切回多进程</button>
        <button data-mode="auto">跟随平台</button>
      </div>
      <div class="muted" style="margin-top:8px">
        单进程 = 采集/调度/控制台/看门狗 共用一个解释器(手机上省 ~25MB×3);
        多进程 = 各自独立, 一个崩了不影响其它。切换会自动重启实例。
      </div>`, '进程合并')}
    ${card('功耗档位', `<div class="row" style="margin-bottom:8px">
        <span class="pill ${cur === 'saver' ? 'ok' : ''}">${esc(POWER_CN[cur] || cur)}</span>
        <span class="muted">${esc(prof.desc || '')}</span></div>
      <div class="row">${['saver', 'balanced', 'performance'].map(k =>
        `<button data-power="${k}" ${cur === k ? 'disabled' : ''}>${POWER_CN[k]}${k === 'saver' ? '(低功耗)' : ''}</button>`).join('')}</div>
      <div class="list" style="margin-top:10px">
        <div class="it"><b>调度间隔</b><span class="right muted">${prof.scheduler_tick} 秒</span></div>
        <div class="it"><b>主管/看门狗间隔</b><span class="right muted">${prof.watchdog_interval} 秒</span></div>
        <div class="it"><b>控制台自动刷新</b><span class="right muted">${prof.console_refresh} 秒</span></div>
        <div class="it"><b>SQLite 缓存</b><span class="right muted">${prof.sqlite_cache_kb} KB</span></div>
        <div class="it"><b>图片/链接抓取</b><span class="right muted">${prof.defer_enrich ? '夜间窗口 ' + esc(prof.enrich_window) : '收到就抓'}</span></div>
        <div class="it"><b>日志安静模式</b><span class="right muted">${prof.log_quiet ? '只记 WARN+' : 'INFO 起'}</span></div>
      </div>`, '越低越省电')}
  </div>
  ${card('服务内存', `<div class="list">${svc || '<div class="muted">无</div>'}</div>`,
    `单进程模式下它们共享同一个 PID · 本进程 ${mb(d.self_rss || 0)}`)}
  ${card('本机 QQOps 进程', `<div class="list">${procs || '<div class="muted">(读取不到 /proc, Windows 下仅显示当前进程)</div>'}</div>`)}
  ${est.note ? card('合并的效果', `<div class="muted">${esc(est.note)}</div>
    <div class="list" style="margin-top:8px">
      <div class="it"><b>当前合计</b><span class="right">${mb(est.current || 0)}</span></div>
      ${est.single_expected ? `<div class="it"><b>预计合并后</b><span class="right">约 ${mb(est.single_expected)}</span></div>` : ''}
      ${est.multi_equivalent ? `<div class="it"><b>若拆成四进程</b><span class="right muted">约 ${mb(est.multi_equivalent)}</span></div>` : ''}
    </div>`) : ''}
  ${d.profiles ? card('三档对照', `<div class="list">${Object.entries(d.profiles).map(([k, v]) =>
    `<div class="it"><b style="width:70px">${esc(v.label)}</b><span class="muted ellip">${esc(v.desc)}</span>
     <span class="right row"><button data-power="${k}" ${cur === k ? 'disabled' : ''}>切到此档</button></span></div>`).join('')}</div>`) : ''}`;
  const apply = async (patch) => {
    const r = await api('/api/ops/power', {method: 'POST', body: JSON.stringify(patch)});
    if (!r || !r.ok) return toast((r && r.error) || '切换失败', 'bad');
    toast('已应用: ' + JSON.stringify(r.applied) + ' · ' + (r.restart || ''), 'ok');
    if (r.restart && String(r.restart).indexOf('已触发') === 0) {
      toast('实例正在重启, 8 秒后自动刷新…', 'warn');
      setTimeout(() => location.reload(), 8000);
    } else { setTimeout(render, 1500); }
  };
  box.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => apply({mode: b.dataset.mode}));
  box.querySelectorAll('[data-power]').forEach(b => b.onclick = () => apply({power_mode: b.dataset.power}));
}
async function _opsCrash(box) {
  const d = await api('/api/app/crashes?limit=80');
  if (!d) { box.innerHTML = errState('异常账本读取失败'); wireRetry(box, render); return; }
  const st = d.stats || {}, items = d.items || [];
  const comps = Object.entries(st.by_component || {});
  const excs = Object.entries(st.by_exception || {});
  box.innerHTML = `<div class="grid k2">
    ${card('总览', `<div class="list">
      <div class="it"><b>异常次数(含重复)</b><span class="right">${st.total || 0}</span></div>
      <div class="it"><b>去重记录</b><span class="right">${st.records || 0}</span></div>
      <div class="it"><b>最近一条</b><span class="right muted">${esc(st.last || '无')}</span></div>
      <div class="it"><b>账本文件</b><span class="right muted ellip">${esc(st.file || '')}</span></div>
    </div><div class="row" style="margin-top:8px"><button class="danger" id="cr-clear">清空账本</button>
      <span class="muted">清空前建议先复制排查</span></div>`)}
    ${card('按组件 / 按异常', `<div class="list">
      ${comps.map(([c, n]) => `<div class="it"><b>${esc(c)}</b><span class="right muted">${n} 次</span></div>`).join('') || '<div class="muted">无</div>'}
      <div class="it" style="border-top:1px solid var(--line)"><b class="muted">异常类型</b></div>
      ${excs.map(([c, n]) => `<div class="it"><b>${esc(c)}</b><span class="right muted">${n} 次</span></div>`).join('')}
    </div>`)}
  </div>
  ${card(`最近记录(${items.length})`, `<div class="list">${items.map(r => `<div class="it crash">
      <span class="pill ${r.kind === 'crash_loop' ? 'bad' : ''}">${esc(kindCn(r.kind))}</span>
      <b class="ellip">${esc(r.component)}</b>
      <span class="muted ellip">${esc((r.message || '').slice(0, 90))}</span>
      <span class="right muted">${esc(r.time || '')}${r.count > 1 ? ' ×' + r.count : ''}</span>
      <button class="right" data-detail="${esc(r.ts)}">详情</button></div>`).join('') || emptyState('没有异常记录 ✅', '这说明最近一切正常')}</div>`)}
  ${items.length ? `<div class="card"><h3>详情(选中一条)</h3><pre class="md" id="cr-detail">点上面的「详情」查看堆栈</pre></div>` : ''}`;
  q(box, '#cr-clear').onclick = async () => {
    if (!confirm('确定清空异常账本? 清空后无法恢复。')) return;
    const r = await api('/api/app/crashes/clear', {method: 'POST', body: '{}'});
    toast(r && r.ok ? '已清空 ' + r.cleared + ' 条' : '清空失败', r && r.ok ? 'ok' : 'bad');
    render();
  };
  box.querySelectorAll('[data-detail]').forEach(b => b.onclick = () => {
    const row = items.find(x => String(x.ts) === b.dataset.detail) || {};
    q(box, '#cr-detail').textContent = `[${row.time}] ${row.component} / ${row.kind} / ${row.exc}
消息: ${row.message || ''}
线程: ${row.thread || ''}  进程: ${row.pid || ''}
额外: ${JSON.stringify(row.extra || {}, null, 1)}

${row.traceback || '(无堆栈)'}`;
  });
}
async function _opsLog(box) {
  const mods = ['', 'collector', 'scheduler', 'web', 'watchdog', 'plugins', 'app', 'client'];
  const cur = lsGet('qqops_log_mod', ''), lvl = lsGet('qqops_log_lvl', '');
  box.innerHTML = card('实时日志', `<div class="row">
      <select id="lg-mod">${mods.map(m => `<option value="${m}" ${m === cur ? 'selected' : ''}>${m ? logName(m) : '全部模块'}</option>`).join('')}</select>
      <select id="lg-lvl">${['', 'DEBUG', 'INFO', 'WARN', 'ERROR'].map(l =>
        `<option value="${l}" ${l === lvl ? 'selected' : ''}>${l ? LOG_LEVEL_CN[l] + '及以上' : '全部级别'}</option>`).join('')}</select>
      <button id="lg-pause">${logPaused ? '继续' : '暂停'}</button>
      <button id="lg-clear">清屏</button>
      <a class="btn" href="/api/logs?name=collector&lines=500" target="_blank">看采集器日志文件</a>
      <span class="muted" id="lg-state">已收到 ${logCursor} 条</span>
    </div>
    <pre class="log" id="lg-out"></pre>`, '按游标增量拉取, 不刷整页');
  q(box, '#lg-mod').onchange = e => { lsSet('qqops_log_mod', e.target.value); logCursor = 0; VIEWS.ops(document.getElementById('view')); };
  q(box, '#lg-lvl').onchange = e => { lsSet('qqops_log_lvl', e.target.value); logCursor = 0; VIEWS.ops(document.getElementById('view')); };
  q(box, '#lg-pause').onclick = () => { logPaused = !logPaused; toast(logPaused ? '已暂停' : '继续接收', 'ok'); VIEWS.ops(document.getElementById('view')); };
  q(box, '#lg-clear').onclick = () => { logCursor = 0; q(box, '#lg-out').textContent = ''; };
  const out = q(box, '#lg-out');
  const pull = async () => {
    if (logPaused) return;
    const d = await api(`/api/app/log?since=${logCursor}&module=${encodeURIComponent(cur)}&level=${encodeURIComponent(lvl)}&limit=200`);
    if (!d || !d.entries) return;
    if (d.dropped) out.textContent += `…(${d.dropped} 条因缓冲已丢弃)\n`;
    d.entries.forEach(e => {
      out.textContent += `${e.time} [${e.level}] ${logName(e.module)}: ${e.msg}\n`;
    });
    logCursor = d.next || logCursor;
    q(box, '#lg-state').textContent = `已收到 ${logCursor} 条`;
    if (out.textContent.length > 60000) out.textContent = out.textContent.slice(-40000);
    out.scrollTop = out.scrollHeight;
  };
  await pull();
  if (logTimer) clearInterval(logTimer);
  logTimer = setInterval(pull, 3000);
  return undefined;
}
async function _opsSelf(box) {
  const d = await api('/api/app/selftest');
  const fw = await api('/api/app/status');
  const pipe = await api('/api/app/pipeline');
  if (!d) { box.innerHTML = errState('自检失败'); wireRetry(box, render); return; }
  const bus = (fw && fw.bus) || {};
  const busLine = `已发布 ${bus.published || 0} · 已分发 ${bus.dispatched || 0}`
    + (bus.queue ? ` · 队列 ${bus.queue.size}/${bus.queue.max}` : '')
    + (bus.running ? ' · 运行中' : ' · 未启动');
  box.innerHTML = `<div class="grid k2">
    ${card('一键自检', `<div class="list">${(d.items || []).map(i =>
      `<div class="it"><span class="pill ${i.ok ? 'ok' : 'bad'}">${i.ok ? '通过' : '异常'}</span>
       <b>${esc(i.name)}</b><span class="right muted ellip">${esc(i.detail || '')}</span></div>`).join('')}</div>
      <div class="row" style="margin-top:8px"><b>${d.ok ? '结论: 全部通过 ✅' : '结论: 有项目异常 ⚠'}</b>
      <button class="right" id="st-again">重新自检</button></div>`, '环境 / 服务 / 数据库 / 插件 / 账本')}
    ${card('框架', `<div class="list">
      <div class="it"><b>版本</b><span class="right">${esc((fw && fw.version) || VER)}</span></div>
      <div class="it"><b>进程号</b><span class="right">${(fw && fw.pid) || '-'}</span></div>
      <div class="it"><b>角色</b><span class="right">${esc(((fw && fw.roles) || []).join('/'))}</span></div>
      <div class="it"><b>处理器</b><span class="right muted">${esc(JSON.stringify((fw && fw.handlers) || {}))}</span></div>
      <div class="it"><b>接口数</b><span class="right">${(fw && fw.routes) || 0}</span></div>
      <div class="it"><b>事件总线</b><span class="right muted">${busLine}</span></div>
    </div>`)}
  </div>
  ${card('管线阶段', `<div class="list">${((pipe && pipe.stages) || []).map(s =>
    `<div class="it"><b style="width:120px">${esc(s.label || s.stage)}</b><span class="pill">序号 ${s.order}</span>
     <span class="muted">${esc(s.plugin || '框架')} · ${esc(s.handler)}${s.critical ? ' · 关键阶段' : ''}</span></div>`).join('') || '<div class="muted">无阶段</div>'}</div>`)}
  ${card('插件', `<div class="list">${(((fw && fw.plugins && fw.plugins.plugins) || [])).map(p =>
    `<div class="it"><b>${esc(p.name)}</b><span class="pill ${p.status === 'loaded' ? 'ok' : (p.status === 'failed' ? 'bad' : '')}">${PLUGIN_STATUS_CN[p.status] || p.status}</span>
     <span class="right muted">处理器 ${p.handlers || 0} · ${esc(p.version || '')}</span></div>`).join('')}</div>`)}`;
  q(box, '#st-again').onclick = () => VIEWS.ops(document.getElementById('view'));
}
async function _opsBackup(box) {
  const d = await api('/api/overview?hours=1');
  if (!d) { box.innerHTML = errState('数据信息读取失败'); wireRetry(box, render); return; }
  box.innerHTML = `<div class="grid k2">
    ${card('存储', `<div class="list">
      <div class="it"><b>数据库</b><span class="right muted">${humanBytes(d.db_size)} · ${core_count(d)}</span></div>
      <div class="it"><b>全文索引</b><span class="right muted">${d.fts && d.fts.available ? '已建立 · ' + d.fts.rows + ' 条' : '不可用'}</span></div>
      <div class="it"><b>数据目录</b><span class="right muted">${humanBytes(d.data_size)}</span></div>
      <div class="it"><b>磁盘剩余</b><span class="right muted">${humanBytes(d.disk.free)} / ${humanBytes(d.disk.total)}</span></div>
      <div class="it"><b>报告 / 备份</b><span class="right muted">${d.storage.reports} / ${d.storage.backups}</span></div>
    </div>`)}
    ${card('操作', `<div class="row">
      <button class="primary" id="bk">立即备份</button>
      <button id="reidx">重建全文索引</button>
      <a class="btn" href="/api/export.xlsx" target="_blank">导出事件/链接 xlsx</a>
      <a class="btn" href="/api/export.docx" target="_blank">导出最近报告 docx</a>
      <a class="btn" href="/api/export.csv?hours=168" target="_blank">导出近 7 天 CSV</a>
    </div><div class="muted" style="margin-top:8px">备份落在 data/backups; 恢复请用命令行(避免误操作覆盖数据)</div>`)}
  </div>
  ${card('采集指标', `<div class="list">
    <div class="it"><b>收下</b><span class="right">${(d.counters || {}).ingested || 0} 条</span></div>
    <div class="it"><b>跳过</b><span class="right">${(d.counters || {}).skipped || 0} 条</span></div>
    <div class="it"><b>拒绝</b><span class="right">${(d.counters || {}).rejected || 0} 条</span></div>
    <div class="it"><b>错误</b><span class="right">${(d.counters || {}).errors || 0} 条</span></div>
  </div>`, '采集器自启动以来的累计')}`;
  q(box, '#bk').onclick = async () => {
    const r = await api('/api/backup', {method: 'POST', body: '{}'});
    if (r && r.ok) toast('备份完成: ' + r.file, 'ok');
  };
  q(box, '#reidx').onclick = async () => {
    const r = await api('/api/reindex', {method: 'POST', body: '{}'});
    if (r && r.ok) toast('索引已重建: ' + r.rows + ' 条', 'ok');
  };
}
VIEWS.settings = VIEWS.config;      /* 设置页 = 原有配置视图(字段说明+校验已在其中) */

/* ------------------------------------------------------------------ 顶部状态条 */
async function pollStatus() {
  const dot = q(document, '#health-dot'), tx = q(document, '#health-text');
  try {
    const st = await api('/api/status');
    if (!st || !st.services) throw new Error('bad payload');
    dot.className = 'dot ok';
    tx.textContent = '控制台正常';
    const strip = q(document, '#status-strip');
    if (strip) {
      const items = [
        ['采集', st.services.collector.running],
        ['调度', st.services.scheduler.running && st.scheduler.alive],
        ['控制台', st.services.web.running],
        ['NapCat', st.services.napcat.running],
      ];
      strip.innerHTML = items.map(([n, ok]) =>
        `<span class="mini ${ok ? '' : 'bad'}"><i class="dot ${ok ? 'ok' : 'bad'}"></i>${n}</span>`).join('')
        + `<span class="mini">近 24 小时 ${st.messages.window}</span>`
        + `<span class="mini">启用群 ${st.enabled_groups}</span>`
        + (st.scheduler.next && st.scheduler.next.ts
          ? `<span class="mini">下次 ${fmtTs(st.scheduler.next.ts, 'time')}</span>` : '');
    }
  } catch (e) {
    dot.className = 'dot bad';
    tx.textContent = '不可达';
  }
}

/* ------------------------------------------------------------------ 启动 */
function boot() {
  buildNav();
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const k = hash.split('?')[0] || 'dashboard';
  currentView = NAV.some(n => n[0] === k) ? k : 'dashboard';
  q(document, '#page-title').textContent = (NAV.find(n => n[0] === currentView) || [, '仪表盘'])[1];
  markNav(currentView);
  document.querySelectorAll('#nav a,#bottomnav a').forEach(a => {
    if (a.id === 'bn-more') {
      a.onclick = e => { e.preventDefault(); q(document, '#sidebar').classList.toggle('open'); };
      return;
    }
    a.onclick = e => { e.preventDefault(); location.hash = '#/' + a.dataset.k; };
  });
  q(document, '#menu-btn').onclick = () => q(document, '#sidebar').classList.toggle('open');
  q(document, '#refresh-btn').onclick = () => render();
  q(document, '#modal-close').onclick = closeModal;
  q(document, '#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
  const ar = q(document, '#auto-refresh');
  ar.checked = lsGet('qqops_auto', '1') !== '0';
  const plan = () => { if (timer) clearInterval(timer); if (ar.checked) timer = setInterval(() => { if (!document.hidden) render(); }, REFRESH * 1000); };
  ar.onchange = () => { lsSet('qqops_auto', ar.checked ? '1' : '0'); plan(); };
  plan();
  window.addEventListener('hashchange', () => { const kk = location.hash.replace(/^#\/?/, '').split('?')[0]; if (kk) go(kk); });
  render();
  pollStatus();
  setInterval(pollStatus, 10000);   // 顶部状态条: 与页面内数据独立刷新
}
document.addEventListener('DOMContentLoaded', boot);
