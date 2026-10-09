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
const NAV = [
  ['overview', '概览', '◎'], ['plan', '计划', '📋'], ['messages', '消息', '☰'],
  ['groups', '群组', '☷'], ['tasks', '任务', '⏱'], ['reports', '报告', '📄'],
  ['logs', '日志', '⌨'], ['config', '配置', '⚙'], ['ops', '运维', '🛠'],
];
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
function buildNav() {
  q(document, '#nav').innerHTML = NAV.map(([k, t, i]) =>
    `<a href="#/${k}" data-k="${k}"><span class="ico">${i}</span>${t}</a>`).join('');
}
function go(k) {
  currentView = k;
  q(document, '#page-title').textContent = (NAV.find(n => n[0] === k) || [, k])[1];
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.k === k));
  q(document, '#sidebar').classList.remove('open');
  render();
}
async function render() {
  const v = q(document, '#view');
  if (busy) return;
  busy = true;
  v.innerHTML = '<div class="card"><div class="skel" style="width:40%"></div><div class="skel" style="margin-top:10px"></div><div class="skel" style="margin-top:10px;width:70%"></div></div>';
  try { await (VIEWS[currentView] || VIEWS.overview)(v); }
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

/* ------------------------------------------------------------------ 概览 */
const VIEWS = {};
VIEWS.overview = async (v) => {
  const d = await api('/api/overview'); if (!d || !d.stats) return;
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
            return `<div class="it"><b style="width:78px">${n}</b>${svcPill(x)}
            <span class="muted">${x.pid ? 'PID ' + x.pid : ''}${x.rss ? ' · ' + humanBytes(x.rss) : ''}${x.etime ? ' · ' + x.etime : ''}${x.port ? ' · :' + x.port : ''}</span>
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
          <span class="pill ${r.status === 'ok' ? 'ok' : (r.status === 'failed' ? 'bad' : 'warn')}">${esc(r.status)}</span>
          <b>${esc(r.job)}</b><span class="muted">${fmtTs(r.started_at)}</span>
          <span class="right muted">${dur(r.started_at, r.finished_at)} · ${esc((r.detail || '').slice(0, 40))}</span></div>`).join('') || '<div class="empty">还没有任务记录</div>'}
        </div>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="run-now">立即总结</button>
          <button id="run-dry">dry-run</button>
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
    if (r && r.ok) { toast(dry ? 'dry-run 已启动' : '总结任务已启动', 'ok'); pollTask(); }
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
  if (!d) return;
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
      <a class="btn" href="/api/export.xlsx">导出 XLSX</a>
      <a class="btn" href="/api/export.docx">导出 DOCX</a>
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
          <div class="muted" style="font-size:12px">${esc(e.group_name || e.group_id || '')}${e.who ? ' · ' + esc(e.who) : ''} · 出现 ${esc(fmtTs(e.last_seen))}${e.status !== 'open' ? ' · ' + esc(e.status) : ''}</div>
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
  v.innerHTML = `
    <div class="card"><div class="row">
      <select id="f-hours">${[1, 6, 24, 72, 168, 720].map(h => `<option value="${h}" ${h == msgState.hours ? 'selected' : ''}>近 ${h} 小时</option>`).join('')}</select>
      <select id="f-group" style="max-width:260px"><option value="">全部群</option>${gs.map(x => `<option value="${x.id}" ${String(x.id) === String(msgState.group) ? 'selected' : ''}>${esc(x.name || x.id)}</option>`).join('')}</select>
      <select id="f-kind"><option value="">全部类型</option>${['text', 'image', 'reply', 'at', 'face', 'json', 'file'].map(k => `<option ${k === msgState.kind ? 'selected' : ''}>${k}</option>`).join('')}</select>
      <input id="f-q" placeholder="全文检索: 关键词(中文按字匹配)" value="${esc(msgState.q)}" style="flex:1;min-width:140px">
      <button class="primary" id="f-go">筛选</button>
      <a class="btn" href="/api/export.csv?hours=${msgState.hours}&group=${msgState.group}">导出 CSV</a>
      <span class="pill right">共 ${res.total} 条 · 第 ${res.page}/${res.pages} 页</span>
      <span class="pill ${res.mode === 'fts' ? 'ok' : ''}" title="检索方式">${res.mode === 'fts' ? '全文索引 FTS5' : (res.mode === 'like' ? '模糊匹配 LIKE' : '按时间')}</span>
    </div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th style="width:96px">时间</th><th>群</th><th>昵称</th><th style="width:110px">类型</th><th>内容</th></tr></thead>
      <tbody>${(res.rows || []).map(r => `<tr data-id="${r.id}"><td class="muted">${fmtTs(r.ts, 'time')}</td>
        <td>${esc(r.group_name || r.group_id)}</td><td>${hl(r.nickname, msgState.q)}</td>
        <td><span class="pill">${esc(r.kinds)}</span></td><td>${hl(r.raw_message, msgState.q)}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">没有匹配的消息</td></tr>'}</tbody>
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
  const d = await api('/api/groups?refresh=1'); if (!d) return;
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
    const q = e.target.value.toLowerCase();
    v.querySelectorAll('.chip').forEach(c => c.style.display = c.dataset.name.includes(q) ? '' : 'none');
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
  const d = await api('/api/runs?limit=80'); if (!d) return;
  v.innerHTML = `<div class="card"><div class="row">
      <button class="primary" id="t-run">立即总结</button><button id="t-dry">dry-run</button>
      <span class="muted">定时: 见配置页 schedule.times</span></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>#</th><th>任务</th><th>开始</th><th>耗时</th><th>状态</th><th>详情</th><th>token</th><th>费用</th></tr></thead><tbody>
    ${(d.runs || []).map(r => `<tr><td class="muted">${r.id}</td><td>${esc(r.job)}</td><td>${fmtTs(r.started_at)}</td>
      <td>${dur(r.started_at, r.finished_at)}</td>
      <td><span class="pill ${r.status === 'ok' ? 'ok' : (r.status === 'failed' ? 'bad' : 'warn')}">${esc(r.status)}</span></td>
      <td class="muted">${esc(r.detail || '')}</td><td class="muted">${r.tokens_in || 0}/${r.tokens_out || 0}</td>
      <td class="muted">${r.cost ? r.cost.toFixed(4) : '-'}</td></tr>`).join('') || '<tr><td colspan="8" class="empty">还没有任务记录</td></tr>'}
    </tbody></table></div></div>`;
  const run = async (dry) => { const r = await api('/api/summarize', {method: 'POST', body: JSON.stringify({dry_run: dry})});
    if (r && r.ok) { toast('任务已启动', 'ok'); setTimeout(render, 4000); } };
  q(v, '#t-run').onclick = () => run(false);
  q(v, '#t-dry').onclick = () => run(true);
};

/* ------------------------------------------------------------------ 报告 */
VIEWS.reports = async (v) => {
  const d = await api('/api/reports'); if (!d) return;
  v.innerHTML = `<div class="card"><div class="row">
      <b>报告格式</b>
      <a class="btn" href="/api/export.xlsx">事件与链接 XLSX</a>
      <a class="btn" href="/api/export.docx">最新报告 DOCX</a>
      <span class="muted">报告页可直接在浏览器打开(自包含 HTML, 含事件卡片与记忆)</span></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>报告</th><th>大小</th><th>生成时间</th><th></th></tr></thead><tbody>
    ${(d.reports || []).map(r => `<tr><td>${esc(r.name)}</td><td class="muted">${humanBytes(r.size)}</td>
      <td class="muted">${fmtTs(r.mtime)}</td><td class="row">
      <button data-open="${esc(r.name)}">阅读</button>
      ${r.name.endsWith('.html') ? `<a class="btn" href="/r/${esc(r.name)}" target="_blank">报告页</a>` : ''}
      ${r.name.endsWith('.md') ? `<a class="btn" href="/api/report.docx?name=${encodeURIComponent(r.name)}">DOCX</a>` : ''}
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
let logName = 'collector';
VIEWS.logs = async (v) => {
  const d = await api('/api/logs?name=' + encodeURIComponent(logName) + '&lines=300'); if (!d) return;
  v.innerHTML = `<div class="card"><div class="row">
      ${(d.available || ['collector']).map(n => `<button class="${n === d.name ? 'primary' : ''}" data-log="${n}">${n}</button>`).join('')}
      <label class="toggle right"><input type="checkbox" id="l-follow" checked><span>自动刷新</span></label>
    </div></div>
    <div class="card"><pre id="log-box">${esc(d.text || '(空)')}</pre></div>`;
  v.querySelectorAll('[data-log]').forEach(b => b.onclick = () => { logName = b.dataset.log; render(); });
  const box = q(v, '#log-box'); box.scrollTop = box.scrollHeight;
};

/* ------------------------------------------------------------------ 配置 */
VIEWS.config = async (v) => {
  const d = await api('/api/config'); if (!d) return;
  const cfg = d.config;
  const secName = {web: '控制台', collector: '采集器', summarizer: '总结器', push: '推送', schedule: '定时', retention: '数据保留'};
  const isSecret = (s, k) => ['token', 'secret', 'api_key'].includes(k);
  const field = (s, k, val) => {
    const id = `c_${s}_${k}`;
    if (typeof val === 'boolean') return `<label class="toggle"><input type="checkbox" id="${id}" ${val ? 'checked' : ''}><span>${k}</span></label>`;
    if (Array.isArray(val)) return `<label style="flex:1"><span class="muted">${k}</span><input id="${id}" style="width:100%" value="${esc(val.join(', '))}" placeholder="逗号分隔"></label>`;
    if (typeof val === 'number') return `<label><span class="muted">${k}</span><input id="${id}" type="number" value="${val}" style="width:110px"></label>`;
    return `<label style="flex:1;min-width:220px"><span class="muted">${k}${isSecret(s, k) ? '(留空=不改)' : ''}</span>
      <input id="${id}" style="width:100%" ${isSecret(s, k) ? 'type="password" placeholder="' + esc(val) + '"' : 'value="' + esc(val) + '"'}></label>`;
  };
  v.innerHTML = `${(d.errors || []).length ? `<div class="card"><b class="err">配置校验有问题</b><ul>${d.errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul></div>` : ''}
    ${Object.entries(cfg).filter(([s]) => secName[s]).map(([s, sec]) => `<div class="card"><h3>${secName[s]} <span class="muted mono">${s}</span></h3>
      <div class="row">${Object.entries(sec).map(([k, val]) => field(s, k, val)).join('')}</div></div>`).join('')}
    <div class="card"><h3>其它</h3><pre>${esc(JSON.stringify({version: cfg.version, paths: d.paths}, null, 2))}</pre>
      <div class="row" style="margin-top:10px"><button class="primary" id="cfg-save">保存配置</button>
      <span class="muted">改配置后采集器/调度器会在 15 秒内自动生效</span></div></div>`;
  q(v, '#cfg-save').onclick = async () => {
    const patch = {};
    for (const [s, sec] of Object.entries(cfg)) {
      if (!secName[s]) continue;
      patch[s] = {};
      for (const [k, val] of Object.entries(sec)) {
        const el = q(v, `#c_${s}_${k}`); if (!el) continue;
        if (el.type === 'checkbox') patch[s][k] = el.checked;
        else if (typeof val === 'number') patch[s][k] = Number(el.value);
        else if (Array.isArray(val)) patch[s][k] = el.value.split(',').map(x => x.trim()).filter(Boolean)
          .map(x => /^\d+$/.test(x) ? Number(x) : x);
        else if (!isSecret(s, k) || el.value) patch[s][k] = el.value;
      }
    }
    const r = await api('/api/config', {method: 'POST', body: JSON.stringify({config: patch})});
    if (r && r.ok) { toast('已保存', 'ok'); render(); }
  };
};

/* ------------------------------------------------------------------ 运维 */
VIEWS.ops = async (v) => {
  const d = await api('/api/overview?hours=1'); if (!d) return;
  v.innerHTML = `
    <div class="grid k2">
      <div class="card"><h3>进程与端口 <span class="muted">PID / 内存 / 运行时长</span></h3>
        <div class="list">${Object.entries(d.services).map(([n, s]) => `<div class="it"><b style="width:78px">${n}</b>${svcPill(s)}
          <span class="muted">${s.pid ? 'PID ' + s.pid : ''}${s.rss ? ' · ' + humanBytes(s.rss) : ''}${s.etime ? ' · ' + s.etime : ''}${s.port ? ' · :' + s.port : ''}</span>
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
          <div class="it"><b>全文索引</b><span class="muted right">${d.fts && d.fts.available ? 'FTS5 · ' + d.fts.rows + ' 条' : '不可用(退化为 LIKE)'}</span></div>
          <div class="it"><b>数据目录</b><span class="muted right">${humanBytes(d.data_size)}</span></div>
          <div class="it"><b>磁盘</b><span class="muted right">剩余 ${humanBytes(d.disk.free)} / 共 ${humanBytes(d.disk.total)}</span></div>
        </div>
        <div class="row" style="margin-top:10px">
          <button class="primary" id="bk">立即备份</button>
          <button id="reidx">重建全文索引</button>
          <a class="btn" href="/api/export.csv?hours=168">导出近 7 天 CSV</a>
          <span class="muted">备份目录 data/backups</span></div></div>
      <div class="card"><h3>旁路服务 <span class="muted">可选组件, QQOps 不依赖</span></h3>
        <div class="list">${Object.entries(d.extras || {}).map(([n, x]) => `<div class="it"><b style="width:74px">${n}</b>${svcPill(x)}
          <span class="muted">${x.pid ? 'PID ' + x.pid : ''}${x.rss ? ' · ' + humanBytes(x.rss) : ''}</span>
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
      <div class="card"><h3>采集指标</h3><pre>${esc(JSON.stringify(d.counters || {}, null, 2))}</pre>
        <div class="muted">收下=入库成功; 跳过=被群/类型过滤; 拒绝=签名不符; 错误=解析或落库失败</div></div>
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
        + `<span class="mini">近24h ${st.messages.window}</span>`
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
  const hash = location.hash.replace(/^#\/?/, '') || 'overview';
  const k = hash.split('?')[0] || 'overview';
  currentView = NAV.some(n => n[0] === k) ? k : 'overview';
  q(document, '#page-title').textContent = (NAV.find(n => n[0] === currentView) || [, '概览'])[1];
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('on', a.dataset.k === currentView));
  document.querySelectorAll('#nav a').forEach(a => a.onclick = e => { e.preventDefault(); location.hash = '#/' + a.dataset.k; });
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
