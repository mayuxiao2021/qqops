/* 控制台前端"无头"渲染探针: 用最小 DOM 桩跑一遍 app.js 的所有视图。
 * 目的: 在真机/浏览器之前, 先把"某个视图一打开就抛异常"这类问题挡在本地。
 * 用法: node tools/console_probe.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP = path.join(__dirname, '..', 'qqops', 'static', 'app.js');
const src = fs.readFileSync(APP, 'utf8');

// ---------------------------------------------------------------- 假 DOM
function makeEl(id) {
  const el = {
    id, innerHTML: '', textContent: '', className: '', value: '', checked: false,
    dataset: {}, style: {}, children: [], scrollTop: 0, scrollHeight: 0,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else if (on) { this._s.add(c); } else { this._s.delete(c); } },
      contains(c) { return this._s.has(c); },
    },
    _handlers: {},
    set onclick(fn) { this._handlers.click = fn; },
    get onclick() { return this._handlers.click; },
    onchange: null,
    appendChild(c) { this.children.push(c); return c; },
    remove() { },
    // 同一个选择器要返回同一个对象, 否则测不到视图往子节点里写的内容
    _q: {},
    querySelector(sel) {
      if (!this._q[sel]) this._q[sel] = makeEl('anon:' + sel);
      return this._q[sel];
    },
    /* 从 innerHTML 里按 标签/类/属性 捞出元素桩 —— 只做够用的解析,
       目的是让"点了按钮到底有没有发请求"这类交互能在本地被验证(渲染通过 != 用得了)。 */
    querySelectorAll(sel) {
      const key = 'all:' + sel;
      if (this._q[key]) return this._q[key];
      const html = String(this.innerHTML || '');
      const m = /^([a-zA-Z]*)((?:\.[\w-]+)|(?:\[[\w-]+\]))*$/.exec(sel.trim());
      let tag = '', cls = [], attr = null;
      if (m) {
        tag = (m[1] || '').toLowerCase();
        cls = [...sel.matchAll(/\.([\w-]+)/g)].map(x => x[1]);
        const a = /\[([\w-]+)\]/.exec(sel);
        attr = a ? a[1] : null;
      }
      const out = [];
      const re = /<([a-zA-Z][\w-]*)([^>]*)>/g;
      let t;
      while ((t = re.exec(html))) {
        const tname = t[1].toLowerCase(), attrs = t[2] || '';
        const get = (n) => {
          const r = new RegExp(n + '\\s*=\\s*"([^"]*)"').exec(attrs);
          return r ? r[1] : null;
        };
        const klass = get('class') || '';
        if (tag && tname !== tag) continue;
        if (cls.some(c => !klass.split(/\s+/).includes(c))) continue;
        if (attr !== null && get(attr) === null) continue;
        const el = makeEl('hit:' + tname + ':' + out.length);
        el.tagName = tname.toUpperCase();
        el.className = klass;
        el.innerHTML = '';
        el.dataset = {};
        [...attrs.matchAll(/data-([\w-]+)\s*=\s*"([^"]*)"/g)].forEach(
          x => el.dataset[x[1].replace(/-(\w)/g, (_, c) => c.toUpperCase())] = x[2]);
        el.id = get('id') || '';
        out.push(el);
      }
      this._q[key] = out;
      return out;
    },
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
    getAttribute() { return null; },
    getBoundingClientRect() { return {width: 800, height: 600, top: 0, left: 0}; },
    focus() { }, blur() { },
  };
  return el;
}
const els = {};
const document = {
  hidden: false,
  getElementById(id) { return (els[id] = els[id] || makeEl(id)); },
  querySelector(sel) {
    const id = String(sel).replace(/^#/, '');
    if (/^#[A-Za-z0-9_-]+$/.test(String(sel))) return document.getElementById(id);
    return makeEl('sel:' + sel);
  },
  querySelectorAll() { return []; },
  createElement(tag) { const e = makeEl('new:' + tag); e.tagName = tag; return e; },
  addEventListener(t, fn) { (document._h = document._h || {})[t] = fn; },
  body: makeEl('body'),
};

// ---------------------------------------------------------------- 假接口数据
const FIX = {
  '/api/ops/dashboard': {
    version: '3.0.0', partial: [], enabled_groups: 23,
    services: {collector: {running: true, pid: 1, rss: 1000, etime: '00:10', port: 8080, health: {ok: true, why: '端口 8080 已应答'}},
               scheduler: {running: true, pid: 2, port: 0, health: {ok: true, why: '心跳正常'}},
               web: {running: true, pid: 3, port: 8091, health: {ok: true, why: 'ok'}},
               watchdog: {running: false, port: 0, health: {ok: false, why: '进程不存在'}}},
    extras: {napcat: {running: true, pid: 9, rss: 1e6}},
    stats: {window: 123, last_hour: 7, groups: 12, total: 4567, series: Array.from({length: 24}, (_, i) => i), kinds: {text: 100, image: 20}},
    runs: [{job: 'schedule:每天 22:00', status: 'ok', started: 1791000000}], events: {open: 3, done: 5},
    crashes: {total: 2, by_component: {collector: 2}, last: '10-10 16:00:00'},
    plugins: {total: 1, loaded: 1, failed: 0},
    schedule: {ts: 1791100000, in: 3600, label: '每天 22:00'},
    reports: ['summary_2026-10-10_0005.md'], media_pending: 0,
  },
  '/api/plugins': {total: 1, loaded: 1, failed: 0, disabled: 0, roles: ['console'],
    dirs: ['/x/plugins'],
    plugins: [{id: 'reminders', name: '截止提醒', version: '1.0.0', author: 'qqops', desc: '识别待办',
               status: 'loaded', enabled: true, builtin: true, handlers: 6, config_fields: 9,
               pages: [{id: 'reminders', title: '提醒', icon: '⏰'}]}],
    commands: [{command: 'reminders.due', help: '列出到期'}],
    tasks: [{task: 'reminders_tick', every: 60}], pages: [{id: 'reminders', title: '提醒', icon: '⏰'}]},
  '/api/plugins/config': {ok: true, path: '/x/config.json', config: {basic: {enabled: true}},
    fields: [{path: 'basic.enabled', key: 'enabled', group_label: '识别设置', type: 'bool', label: '启用识别', hint: '', default: true, options: [], secret: false, value: true},
             {path: 'basic.keywords', group_label: '识别设置', type: 'list', label: '关键词', hint: '', default: [], options: [], secret: false, value: ['提醒']},
             {path: 'notify.push', group_label: '提醒方式', type: 'bool', label: '推送', hint: '', default: false, options: [], secret: false, value: false},
             {path: 'notify.target_session', group_label: '提醒方式', type: 'string', label: '目标', hint: '', default: '', options: [], secret: true, value: ''}]},
  '/api/groups': {groups: [{id: 123, name: '测试群', enabled: true, count: 5, in_napcat: true}], enabled_count: 1, napcat_count: 1},
  '/api/events': {events: [{id: 1, title: '交报告', kind: 'deadline', status: 'open', due_ts: 1791200000, group_id: 123, group_name: '测试群'}],
    stats: {open: 1, done: 2, by_status: {open: 1, done: 2}, by_kind: {deadline: 1}, due_7d: 1, overdue: 0}},
  '/api/memory': {memories: [{scope: 'group:123', content: '约定', updated: 1791000000}], profile: '# 我'},
  '/api/links': {links: [{url: 'https://x.test', title: 'X', group_id: 123, ts: 1791000000}]},
  '/api/media': {media: [{key: 'k1', kind: 'image', caption: '图', status: 'ok'}]},
  '/api/app/crashes': {stats: {total: 2, records: 1, by_component: {collector: 2}, by_exception: {ValueError: 2}, last: '10-10 16:00:00', file: '/x/crashes.jsonl'},
    items: [{ts: 1791000000, time: '10-10 16:00:00', component: 'collector', kind: 'stage_error', exc: 'ValueError', message: 'boom', count: 2, traceback: 'tb', thread: 'MainThread', pid: 1}]},
  '/api/app/log': {entries: [{seq: 1, time: '16:00:00', module: 'collector', level: 'INFO', msg: 'hi'}], next: 1, dropped: 0, modules: [{module: 'collector', count: 1}], counts: {INFO: 1}},
  '/api/app/selftest': {ok: true, ts: 1791000000, version: '3.0.0', items: [{name: '服务 collector', ok: true, detail: 'ok'}]},
  '/api/app/status': {version: '3.0.0', pid: 1, roles: ['console'], handlers: {stage: 1, route: 5, total: 6}, routes: 20,
    bus: {published: 3, dispatched: 3, queue: {size: 0, max: 2000}, running: true, stages: []},
    plugins: {plugins: [{name: '截止提醒', status: 'loaded', handlers: 6, version: '1.0.0'}]}, crashes: {total: 0}},
  '/api/app/pipeline': {stages: [{stage: 'reminders_scan', label: '提醒扫描', order: 35, plugin: 'reminders', handler: 'scan', critical: false}], bus: {}},
  '/api/assistant/sessions': {sessions: [{id: 's1', title: '昨天群里聊了什么', turns: 2, updated: 1791000000,
      updated_text: '10-10 12:00', tokens_in: 120, tokens_out: 40, cost: 0.001}],
    today_used: 3, quota: 100, llm: {configured: true, model: 'deepseek-chat'}},
  '/api/assistant/session': {id: 's1', title: '昨天群里聊了什么', turns: [
      {id: 1, role: 'user', content: '昨天群里聊了什么', sources: [], meta: {}, created: 1791000000, time_text: '10-10 12:00'},
      {id: 2, role: 'assistant', content: '小明提到报告要加一页预算表 [#M1], 群里还有一条长期记忆 [#Y1]', ms: 1500,
       sources: [{ref: 'M1', kind: 'M', label: '10-10 12:03 小明', text: '这次报告要加一页预算表',
                  sid: 'm:101', id: 101},
                 {ref: 'Y1', kind: 'Y', label: '记忆 group:6666', text: '群里的共识: 报告要加预算表',
                  sid: 'y:group:6666|digest', scope: 'group:6666', key: 'digest'}],
       meta: {model: 'deepseek-chat', rounds: 1}, tokens_in: 120, tokens_out: 40, created: 1791000001}]},
  '/api/assistant/suggest': {suggestions: ['「交季度总结」这件事有结论了吗?', '最近群里分享的链接都讲了什么?']},
  '/api/assistant/stats': {sessions: 1, turns: 2, tokens_in: 120, tokens_out: 40, cost: 0.001,
    today_used: 3, quota: 100, llm: {configured: true, model: 'deepseek-chat'}},
  '/api/assistant/ask': {ok: true, session_id: 's1', answer: '回答', sources: [], mode: 'messages',
    count: 0, degraded: false, ms: 900},
  '/api/assistant/source/M/1': {ok: true, kind: 'message', title: '消息原文',
    record: {id: 101, ts_text: '2026-10-10 12:03:00', raw_message: '这次报告要加一页预算表',
             nickname: '小明', group_name: '测试群'}},
  '/api/assistant/source/Y/1': {ok: true, kind: 'memory', title: '长期记忆原文',
    record: {scope: 'group:6666', key: 'digest', content: '群里的共识: 报告要加预算表',
             updated_text: '10-10 20:00', hits: 3,
             origin_messages: [{id: 101, nickname: '小明', ts_text: '10-10 12:03', text: '这次报告要加一页预算表'}]}},
  '/api/ops/resources': {runtime: {mode: 'single', running_mode: 'single', pid: 1234, alive: true, rss: 28 * 1048576},
    power: {power_mode: 'saver', label: '省电', desc: '唤醒最少', scheduler_tick: 60, watchdog_interval: 180,
            console_refresh: 60, sqlite_cache_kb: 1000, defer_enrich: true, enrich_window: '02:00-06:00',
            log_quiet: true, all_profiles: {saver: {label: '省电', desc: '省'}, balanced: {label: '均衡', desc: '中'},
                                            performance: {label: '性能', desc: '快'}}},
    services: [{name: 'collector', running: true, pid: 1234, rss: 28 * 1048576, port: 8080, mode: 'single'},
               {name: 'web', running: true, pid: 1234, rss: 0, port: 8091, mode: 'single'}],
    processes: [{pid: 1234, cmd: 'python3 -m qqops.runner', rss: 28 * 1048576, etime: '01:00:00', self: true}],
    totals: {rss: 28 * 1048576, count: 1, unique_pids: 1}, self_rss: 28 * 1048576, threads: 1,
    estimate: {note: '当前已是单进程', current: 28 * 1048576, multi_equivalent: 100 * 1048576}},
  '/api/overview': {services: {collector: {running: true, pid: 1, rss: 1000, etime: '00:10', port: 8080},
      scheduler: {running: true, pid: 2}, web: {running: true, pid: 3, port: 8091}, napcat: {running: true, pid: 9}},
    extras: {napcat: {running: true, note: '必须'}, astrbot: {running: false, note: '可选'}},
    ecosystem: [{name: 'NapCat API', port: 3000, ok: true}],
    scheduler: {next: {ts: 1791100000}, alive: true}, enabled_groups: 1,
    storage: {reports: 1, backups: 2, db: 1e6, data: 2e6, disk: {free: 1e9, total: 1e10}}, db_size: 1e6, data_size: 2e6,
    task: {running: false, kind: '', result: null},
    fts: {available: true, rows: 10}, disk: {free: 1e9, total: 1e10},
    counters: {ingested: 5, skipped: 1, rejected: 0, errors: 0}, napcat: {ok: true, ms: 12, nickname: '黯'},
    ports: {collector: true, web: true, napcat: true},
    stats: {total: 10, window: 5, window_hours: 24, last_hour: 2, groups: 3, kinds: {text: 5}, series: [1, 2, 3]}, db: 1e6},
  '/api/config': {config: {web: {port: 8091, token: '******'}, collector: {port: 8080, groups: [1]}},
    errors: [], paths: {data: '/x/data', db: '/x/db', logs: '/x/logs', reports: '/x/r', backups: '/x/b', config: '/x/c'}},
  '/api/schedule': {schedule: {mode: 'daily', times: ['22:00'], enabled: true, skip_if_no_new: true,
      min_new_messages: 5, every_minutes: 360, weekdays: [1], weekly_time: '22:00'},
    next: {ts: 1791100000, label: '每天 22:00', in: 3600, mode: 'daily'},
    stats: {open: 1, done: 2}, runs: [], last_run: 1791000000},
  '/api/messages': {rows: [{id: 1, ts: 1791000000, group_id: 123, group_name: '测试群', nickname: '小明',
      raw_message: '你好', kinds: 'text', self: 0, recalled: 0, message_id: 1}],
    total: 1, page: 1, size: 50, pages: 1, mode: 'recent', fts_available: true},
  '/api/runs': {runs: [{id: 1, job: 'manual', status: 'ok', started: 1791000000, ended: 1791000060}]},
  '/api/reports': {reports: [{name: 'summary_2026-10-10_0005.md', size: 100, mtime: 1791000000}]},
  '/api/logs': {name: 'collector', available: ['collector', 'web'], text: 'line1\nline2'},
  '/api/report': {name: 'x.md', content: '# 报告'},
};

const calls = [];
/* --live: 不打桩, 直接请求真机控制台(经 adb forward), 用真实数据渲染所有页面 */
const LIVE = process.argv.includes('--live');
const LIVE_BASE = process.env.QQOPS_BASE || 'http://127.0.0.1:8091';
const LIVE_TOKEN = process.env.QQOPS_TOKEN || '';
async function fetchStub(url) {
  const p = String(url).split('?')[0];
  calls.push(String(url));
  if (LIVE) {
    const r = await fetch(LIVE_BASE + String(url), {headers: {'X-Token': LIVE_TOKEN}});
    const txt = await r.text();
    return {ok: r.ok, status: r.status, redirected: /\/login/.test(r.url || ''),
            headers: {get: () => r.headers.get('content-type') || 'application/json'},
            text: async () => txt};
  }
  const body = FIX[p];
  const ok = body !== undefined;
  return {
    ok, status: ok ? 200 : 404, redirected: false,
    headers: {get: () => 'application/json; charset=utf-8'},
    text: async () => JSON.stringify(body === undefined ? {error: 'no fixture ' + p} : body),
  };
}

// ---------------------------------------------------------------- 运行
const sandbox = {
  console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => { },
  fetch: fetchStub, document,
  location: {hash: '', search: '', href: ''},
  localStorage: {_d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }},
  sessionStorage: {_d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }},
  navigator: {userAgent: 'node-probe'},
  URLSearchParams, Date, Math, JSON, Object, Array, String, Number, Boolean, Error, RegExp, isNaN, parseInt, parseFloat,
};
sandbox.window = sandbox;
sandbox.window.QQOPS = {version: '3.0.0', refresh: 15};
sandbox.window.addEventListener = (t, fn) => { (sandbox._wh = sandbox._wh || {})[t] = fn; };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

let failures = [];
try {
  vm.runInContext(src, sandbox, {filename: 'app.js'});
} catch (e) {
  console.log('✘ app.js 执行失败:', e.message);
  process.exit(1);
}

(async () => {
  // 触发 boot(它注册在 DOMContentLoaded 上)
  const h = (document._h || {})['DOMContentLoaded'];
  if (!h) { console.log('✘ app.js 没有注册 DOMContentLoaded'); process.exit(1); }
  try { await h(); } catch (e) { failures.push('boot: ' + e.message); }

  // 注意: `const VIEWS` 是词法绑定, 不会挂到沙箱全局对象上, 得用 runInContext 取
  const evalIn = (expr) => vm.runInContext(expr, sandbox);
  const views = evalIn('Object.keys(VIEWS)');
  console.log('  发现的视图: ' + views.join(', '));
  for (const name of views) {
    const el = makeEl('view');
    try {
      const fn = evalIn('VIEWS[' + JSON.stringify(name) + ']');
      if (typeof fn !== 'function') { failures.push(name + ': 不是函数'); continue; }
      await fn(el);
      const html = String(el.innerHTML || '');
      const len = html.length;
      console.log('  ✔ ' + name.padEnd(10) + ' 渲染 ' + len + ' 字符');
      if (len < 40) failures.push(name + ': 渲染内容过少(' + len + ')');
      // 渲染质量: 真数据下不该漏出 undefined/NaN/未替换的占位
      const leaks = ['undefined', 'NaN', '[object Object]'].filter(k => html.includes(k));
      if (leaks.length) {
        failures.push(name + ': 渲染结果里漏出 ' + leaks.join('/')
          + ' → ' + (html.match(/.{0,60}(undefined|NaN|\[object Object\]).{0,40}/) || [''])[0].replace(/\s+/g, ' '));
      }
    } catch (e) {
      failures.push(name + ': ' + e.message);
      console.log('  ✘ ' + name.padEnd(10) + ' ' + e.message);
    }
  }
  // 数据页的每个页签
  for (const t of ['groups', 'events', 'memory', 'links', 'media']) {
    evalIn('dataTab = ' + JSON.stringify(t));
    const el = makeEl('view');
    try {
      await evalIn('VIEWS.data')(el);
      console.log('  · 数据/' + t.padEnd(7) + ' ' + String(el.innerHTML || '').length + ' 字符');
    } catch (e) { failures.push('data/' + t + ': ' + e.message); }
  }
  // 运维页的每个页签
  for (const t of ['svc', 'res', 'crash', 'log', 'self', 'backup']) {
    evalIn('opsTab = ' + JSON.stringify(t));
    const el = makeEl('view');
    try {
      await evalIn('VIEWS.ops')(el);
      const body = el.querySelector('#ops-body');
      console.log('  · 运维/' + t.padEnd(7) + ' ' + String(body.innerHTML || '').length + ' 字符');
    } catch (e) { failures.push('ops/' + t + ': ' + e.message); }
  }
  /* ---------------- 交互: 渲染通过 != 用得了 ----------------
     这里点一遍关键按钮, 断言它真的发出了请求 —— 发送按钮曾被一个遮蔽的
     `const q` 弄成"点了没反应", 只看渲染是发现不了的。 */
  console.log('\n交互:');
  // 实时模式: 挑一个真实会话; 发送按钮只验"接线"不真问模型(省钱不占额度)
  if (LIVE) {
    try {
      const sess = await evalIn('api')('/api/assistant/sessions');
      const first = (sess && sess.sessions && sess.sessions[0]) || null;
      if (first) evalIn('chatSession = ' + JSON.stringify(first.id));
    } catch (e) { console.log('  · 实时模式读取会话失败(不影响渲染检查): ' + e.message); }
  } else {
    evalIn('chatSession = "s1"');
  }
  const chatEl = makeEl('chat');
  try {
    await evalIn('VIEWS.chat')(chatEl);
    const sendBtn = chatEl.querySelector('#chat-send');
    if (typeof sendBtn.onclick !== 'function') throw new Error('发送按钮没有绑定点击处理');
    if (LIVE) {
      evalIn('chatBusy = true');            // 让 send() 走到守卫就返回: 只验那行会抛的取值代码
      await sendBtn.onclick();
      evalIn('chatBusy = false');
      console.log('  ✔ 发送按钮已接线(实时模式不真发起提问, 避免花额度)');
    } else {
      chatEl.querySelector('#chat-q').value = '昨天群里讨论了什么';
      calls.length = 0;
      await sendBtn.onclick();
      const asked = calls.some(u => u.includes('/api/assistant/ask'));
      console.log('  ' + (asked ? '✔' : '✘') + ' 发送按钮 -> 发起提问请求');
      if (!asked) failures.push('chat: 点发送按钮没有发起 /api/assistant/ask(按钮没接线)');
    }
  } catch (e) {
    failures.push('chat 发送: ' + e.message);
    console.log('  ✘ 发送按钮 ' + e.message);
  }
  // 引用/来源芯片: 必须带稳定 sid, 且点开能取到原文(记忆来源原先直接报未知类型)
  try {
    const cites = chatEl.querySelectorAll('[data-src]');
    const names = cites.map(c => c.dataset.src + (c.dataset.sid ? '(sid)' : '(无sid)'));
    console.log('  来源引用: ' + (names.join(', ') || '(无)'));
    const noSid = cites.filter(c => !c.dataset.sid);
    if (noSid.length) {
      // 老会话(turns 存库时还没有 sid)只提示不判失败; 新回答一定带 sid
      const msg = 'chat: 有来源引用没带 sid → ' + names.join(', ');
      if (LIVE) console.log('  · ' + msg + '(历史会话, 重新提问即可拿到 sid)');
      else failures.push(msg);
    }
    const mem = cites.find(c => c.dataset.src === 'Y');
    if (!mem && !LIVE) failures.push('chat: 回答里的长期记忆引用 [Y1] 没有渲染成可点链接');
    if (mem) {
      calls.length = 0;
      await mem.onclick({preventDefault() {}});
      const hit = calls.find(u => u.includes('/api/assistant/source/Y/'));
      const body = String((els['modal-body'] || {}).innerHTML || '');
      const okSid = !!hit && /sid=y%3A/.test(hit);
      const okBody = body.length > 20 && !body.includes('取不到');
      const liveOld = LIVE && !okSid;          // 历史会话(修复前存的)没有 sid: 只提示
      console.log('  ' + (okSid && okBody ? '✔' : (liveOld ? '·' : '✘'))
        + ' 记忆来源 -> 取到原文: ' + (hit || '(没请求)'));
      if (liveOld) console.log('  · 该会话的来源是修复前存的(没有 sid), 跳过严格判定');
      else {
        if (!okSid) failures.push('chat: 记忆来源请求没带稳定 sid → ' + (hit || '(没请求)'));
        if (!okBody) failures.push('chat: 记忆原文弹窗内容不对 → ' + body.slice(0, 120));
      }
    }
    // 实时模式: 直接按 sid 打开一条真实记忆, 验证后端 Y 分支(只读)
    if (LIVE) {
      const m = await evalIn('api')('/api/memory');
      const one = (m && (m.memories || [])[0]) || null;
      if (!one) console.log('  · 本机还没有长期记忆, 跳过记忆原文接口检查');
      else {
        const sid = 'y:' + (one.scope || '') + '|' + (one.key || 'digest');
        const d = await evalIn('api')('/api/assistant/source/Y/1?sid=' + encodeURIComponent(sid));
        const ok = !!(d && d.ok && (d.record || {}).content);
        console.log('  ' + (ok ? '✔' : '✘') + ' 真机记忆原文接口: ' + sid);
        if (!ok) failures.push('记忆原文接口失败(' + sid + '): ' + JSON.stringify(d).slice(0, 120));
      }
    }
  } catch (e) {
    failures.push('chat 来源: ' + e.message);
    console.log('  ✘ 来源点击 ' + e.message);
  }
  console.log('\n调用的接口: ' + [...new Set(calls)].sort().join(', '));
  if (failures.length) {
    console.log('\n✘ 失败 ' + failures.length + ' 项:');
    failures.forEach(f => console.log('   - ' + f));
    process.exit(1);
  }
  console.log('\n✔ 全部视图渲染通过');
})();
