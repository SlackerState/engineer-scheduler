'use strict';

// ---------- 与后端通信：桌面窗口走 pywebview，网页模式走 /api ----------
// 网页模式的服务端会在页面里注入 <meta name="es-mode" content="web">，否则就是桌面窗口
const isDesktop = !document.querySelector('meta[name="es-mode"][content="web"]');
const ready = isDesktop
  ? new Promise(res => (window.pywebview && window.pywebview.api) ? res() : window.addEventListener('pywebviewready', res))
  : Promise.resolve();

async function api(method, ...args) {
  await ready;
  if (isDesktop) return window.pywebview.api[method](...args);
  const r = await fetch('/api/' + method, {method: 'POST', body: JSON.stringify(args)});
  const j = await r.json();
  if (!j.ok) throw new Error(j.error);
  return j.result;
}

// ---------- 小工具 ----------
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const WEEK = ['日', '一', '二', '三', '四', '五', '六'];
const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n); return fmtDate(d); };
const fmtDate = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const monday = iso => { const d = new Date(iso + 'T00:00:00'); return addDays(iso, -((d.getDay() + 6) % 7)); };

let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), err ? 6000 : 3000);
}
async function safe(fn) {
  try { return await fn(); } catch (e) { toast(e.message || String(e), true); }
}

function modal(html, onMount) {
  const m = $('#modal'), box = $('.modal-box', m);
  box.innerHTML = html; m.hidden = false;
  const close = () => { m.hidden = true; box.innerHTML = ''; };
  m.onclick = e => { if (e.target === m) close(); };
  onMount && onMount(box, close);
  return close;
}
function ask(message, okText = '确定') {
  return new Promise(res => modal(
    `<h2>请确认</h2><div style="white-space:pre-wrap">${esc(message)}</div>
     <div class="actions"><button data-a="no">取消</button><button class="primary" data-a="yes">${esc(okText)}</button></div>`,
    (box, close) => box.addEventListener('click', e => {
      const a = e.target.dataset.a; if (!a) return; close(); res(a === 'yes');
    })));
}

// ---------- 全局状态 ----------
const S = {boot: null, engineers: [], view: 'orders'};

async function refreshEngineers() {
  S.engineers = await api('list_engineers');
  const cur = $('#o-eng').value;
  $('#o-eng').innerHTML = '<option value="">全部工程师</option>' +
    S.engineers.map(e => `<option>${esc(e.name)}</option>`).join('');
  $('#o-eng').value = cur;
}

function show(view) {
  S.view = view;
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + view));
  ({orders: loadOrders, schedule: loadSchedule, today: loadToday, parts: loadParts,
    people: loadPeople, settings: loadSettings})[view]();
}

// 超期风险徽标 / 上门时间（预约上门时间只是考核截止，上门按 到达 > 出发 > 计划）
const riskBadge = r => r && r.level ? `<span class="badge risk-${r.level}">${esc(r.text)}</span>` : '';
const visitText = v => v ? `${esc(v.day.slice(5).replace('-', '/'))} ${esc(v.time || v.slot)}<span class="tag src-${esc(v.source)}">${esc(v.source)}</span>` : '';
const deadlineHtml = o => o.appointment
  ? `${esc(o.appointment.slice(5).replace('-', '/'))}${o.risk && o.risk.left ? `<div class="muted small">${esc(o.risk.left)}</div>` : ''}` : '<span class="muted">—</span>';

// ======================= 订单清单 =======================
async function loadOrders() {
  const rows = await safe(() => api('list_orders', $('#o-status').value, $('#o-kw').value.trim(), $('#o-eng').value));
  if (!rows) return;
  const t = $('#o-table');
  S.selected = new Set();
  updateBatch();
  if (!rows.length) { t.innerHTML = '<tr><td class="empty">没有订单。点左下「同步 D365」拉取 Udesk 创建的订单。</td></tr>'; return; }
  t.innerHTML = `<thead><tr><th class="ck"><input type="checkbox" id="o-all" title="全选可改派的订单"></th><th>日期</th><th>Udesk 工单号</th><th>服务订单号</th><th>客户</th><th>机器序列号</th>
    <th>问题</th><th>工程师</th><th>状态</th><th>D365 状态</th><th>上门</th><th>预约截止</th><th>超期</th><th>处理进度</th></tr></thead><tbody>` +
    rows.map(o => `<tr class="click" data-id="${o.id}">
      <td class="ck">${ASSIGNABLE.includes(o.d365_status_code) ? `<input type="checkbox" data-ck="${o.id}">` : ''}</td>
      <td class="nowrap">${esc(o.day)}</td><td class="nowrap mono">${esc(o.udesk_no)}</td>
      <td class="nowrap mono">${esc(o.service_order_no)}${o.order_type ? `<span class="tag">${esc(o.order_type)}</span>` : ''}</td>
      <td>${esc(o.customer)}</td><td class="mono">${esc(o.serial)}${o.model ? `<div class="muted small">${esc(o.model)}</div>` : ''}</td>
      <td class="wrap">${esc((o.problem || '').slice(0, 140))}</td><td class="nowrap">${esc(o.engineer)}</td>
      <td><span class="badge b-${esc(o.status)}">${esc(o.status)}</span></td>
      <td class="nowrap">${esc(o.d365_status)}</td>
      <td class="nowrap">${visitText(o.visit) || (o.d365_id && !['已完成', '已取消'].includes(o.status) ? '<span class="badge risk-pool">待排期</span>' : '')}</td>
      <td class="nowrap">${deadlineHtml(o)}</td>
      <td>${riskBadge(o.risk)}${o.to_return ? `<span class="badge st-todo">待归还配件 ${o.to_return}</span>` : ''}</td>
      <td class="wrap">${esc(o.progress)}</td></tr>`).join('') + '</tbody>';
}

const D365_INFO = [
  ['udesk_no', 'Udesk 工单号'], ['order_type', '订单类型'], ['d365_status', 'D365 状态'], ['network', '服务网点'],
  ['appointment', '预约上门（考核截止）'], ['dispatch_time', '派单时间'], ['setoff_time', '出发时间'], ['arrival_time', '到达时间'],
  ['complete_time', '现场完工'], ['address', '设备地址'], ['contact', '设备联系人'], ['model', '机型'],
  ['warranty', '保修状态'], ['synced_at', '最后同步'],
];

// D365 允许派工/转派的订单状态：已创建(10) → 派工；已派工(20)、已接收(70) → 转派
const ASSIGNABLE = [10, 20, 70];

function d365ActionsHtml(o) {
  const code = o.d365_status_code;
  const canAssign = ASSIGNABLE.includes(code), canAppt = code === 10;
  return `<div class="card act">
    <div class="row"><b>D365 操作</b><span class="muted small">直接修改 D365</span>
      <span class="badge b-${esc(o.status)}" style="margin-left:auto">${esc(o.d365_status || '—')}</span></div>
    <div class="row"><span class="lbl">工程师</span><b>${esc(o.engineer) || '<span class="muted">未派工</span>'}</b>
      ${canAssign ? `<button class="primary" id="od-assign" style="margin-left:auto">${code === 10 ? '派工' : '改派工程师'}</button>`
        : '<span class="muted small" style="margin-left:auto">当前状态 D365 不允许派工/改派</span>'}</div>
    <div class="row"><span class="lbl">预约截止</span>
      <input type="datetime-local" id="od-appt" value="${esc((o.appointment || '').replace(' ', 'T'))}" ${canAppt ? '' : 'disabled'}>
      ${canAppt ? '<button id="od-appt-save" style="margin-left:auto">保存到 D365</button>'
        : '<span class="muted small" style="margin-left:auto">D365 只允许「已创建」时改约，之后需 CC 在 D365 里改</span>'}</div>
  </div>`;
}

// 上门安排：本地计划上门（只存软件里），以及 D365 的实际出发/到达和考核截止
function visitPlanHtml(o) {
  const done = ['已完成', '已取消'].includes(o.status);
  const slots = S.boot.slots.map(s => `<option ${s === (o.planned_slot || '上午') ? 'selected' : ''}>${s}</option>`).join('');
  return `<div class="card act plan">
    <div class="row"><b>上门安排</b><span class="muted small">排班表按 实际到达 → 出发 → 计划上门 排</span>
      <span style="margin-left:auto">${riskBadge(o.risk)}</span></div>
    <div class="row"><span class="lbl">考核截止</span><b>${esc(o.appointment) || '—'}</b>
      ${o.risk && o.risk.left ? `<span class="muted small">${esc(o.risk.left)}</span>` : ''}</div>
    <div class="row"><span class="lbl">计划上门</span>
      <input type="date" id="od-plan-day" value="${esc(o.planned_day || '')}" ${done ? 'disabled' : ''}>
      <select id="od-plan-slot" ${done ? 'disabled' : ''}>${slots}</select>
      ${done ? '' : `<button id="od-plan-save" style="margin-left:auto">保存计划</button>${o.planned_day ? '<button class="link" id="od-plan-clear">清除</button>' : ''}`}</div>
    <div class="row"><span class="lbl">实际</span><span class="small">出发 ${esc(o.setoff_time) || '—'} · 到达 ${esc(o.arrival_time) || '—'}</span></div>
    <div class="row"><span class="lbl">排班位置</span>${o.visit ? visitText(o.visit) : '<span class="badge risk-pool">待排期</span>'}</div>
  </div>`;
}

function showResults(results) {
  const bad = results.filter(r => !r.ok);
  if (!bad.length) { toast(results.length > 1 ? `${results.length} 单全部完成` : results[0].message); return; }
  modal(`<h2>部分订单没有成功</h2><table class="grid compact"><thead><tr><th>订单</th><th>结果</th></tr></thead><tbody>` +
    results.map(r => `<tr><td class="mono">${esc(r.no)}</td><td class="${r.ok ? '' : 'conflict'}">${esc(r.message)}</td></tr>`).join('') +
    `</tbody></table><div class="actions"><button class="primary" data-close>知道了</button></div>`,
    (box, close) => ($('[data-close]', box).onclick = close));
}

function assignDialog(oids, after) {
  modal(`<h2>${oids.length > 1 ? `批量改派 ${oids.length} 单` : '派工 / 改派工程师'}</h2>
    <p class="muted small" style="margin:0">工程师名单来自 D365 的派工对话框，人员表里的人排在最前面。确认后调用 D365 官方的派工/转派接口，和在 D365 里点按钮的效果相同，工程师会收到派工通知。</p>
    <div class="row"><input id="ae-kw" placeholder="搜索姓名" style="flex:1"><button id="ae-go">搜索</button></div>
    <div id="ae-list" class="table-wrap" style="max-height:52vh;min-height:80px"></div>
    <div class="actions"><button data-close>取消</button></div>`, (box, close) => {
    $('[data-close]', box).onclick = close;
    const load = async () => {
      $('#ae-list', box).innerHTML = '<div class="empty">正在从 D365 读取工程师名单…</div>';
      const rows = await safe(() => api('d365_engineers', oids[0], $('#ae-kw', box).value.trim()));
      if (!rows) { $('#ae-list', box).innerHTML = ''; return; }
      $('#ae-list', box).innerHTML = rows.length ? `<table class="grid compact"><thead><tr><th>工程师</th><th>工号</th><th>网点</th><th></th></tr></thead><tbody>` +
        rows.map(r => `<tr><td><b>${esc(r.name)}</b>${r.local ? `<span class="tag">人员表：${esc(r.local)}</span>` : ''}</td>
          <td class="mono">${esc(r.jobcard)}</td><td>${esc(r.network)}</td>
          <td><button class="link" data-pick="${esc(r.id)}" data-name="${esc(r.name)}">选择</button></td></tr>`).join('') + '</tbody></table>'
        : '<div class="empty">没有找到工程师</div>';
    };
    $('#ae-go', box).onclick = load;
    $('#ae-kw', box).onkeydown = e => { if (e.key === 'Enter') load(); };
    $('#ae-list', box).onclick = async e => {
      const b = e.target.closest('[data-pick]'); if (!b) return;
      close();
      const what = oids.length > 1 ? `这 ${oids.length} 单` : '这单';
      if (!await ask(`在 D365 中把${what}派给 / 转派给 ${b.dataset.name}？\n\n这会直接修改 D365，工程师会收到派工通知。`, '确认派工')) return;
      toast('正在写入 D365…');
      const res = await safe(() => api('d365_assign', oids, b.dataset.pick, b.dataset.name));
      if (res) { showResults(res); after && after(); }
    };
    load();
  });
}

function updateBatch() {
  const n = S.selected ? S.selected.size : 0;
  $('#o-batch').hidden = !n;
  $('#o-batch').textContent = `批量改派（${n}）`;
}

async function openOrder(id) {
  const o = id ? await safe(() => api('get_order', id)) : {status: '待处理', day: S.boot.today};
  if (!o) return;
  const d = $('#drawer');
  const fromD365 = !!o.d365_id;
  const lockStatus = fromD365 && S.boot.settings.d365_status_overrides_local;
  const st = S.boot.order_statuses.map(s => `<option ${s === o.status ? 'selected' : ''}>${s}</option>`).join('');
  d.innerHTML = `
    <h2>${o.id ? '订单 ' + esc(o.service_order_no || o.udesk_no || '#' + o.id) : '手工新增订单'}
      ${o.d365_link ? '<button class="link" id="od-open">在 D365 打开 ↗</button>' : ''}
      <button class="x" id="od-close">关闭</button></h2>
    ${fromD365 ? d365ActionsHtml(o) + visitPlanHtml(o) + '<div class="card act parts"><b>配件</b><div id="od-parts"></div></div>' : ''}
    <div class="sec">${fromD365 ? '本地记录 <span class="muted small">（只保存在软件里，不写回 D365）</span>' : '订单信息'}</div>
    <form class="form" id="od-form" style="display:grid;gap:10px">
      <div class="two"><label>日期<input name="day" type="date" value="${esc(o.day)}"></label>
        <label>状态<select name="status" ${lockStatus ? 'disabled title="由 D365 同步"' : ''}>${st}</select></label></div>
      <div class="two"><label>服务订单号<input name="service_order_no" value="${esc(o.service_order_no)}"></label>
        <label>借料单号<input name="loan_no" value="${esc(o.loan_no)}"></label></div>
      <div class="two"><label>客户<input name="customer" value="${esc(o.customer)}"></label>
        <label>机器序列号<input name="serial" value="${esc(o.serial)}"></label></div>
      <label>工程师<input name="engineer" value="${esc(o.engineer)}" ${fromD365 ? 'disabled title="由 D365 同步，改派请用上方「改派工程师」"' : ''}></label>
      <label>问题<textarea name="problem" rows="3">${esc(o.problem)}</textarea></label>
      <label>处理进度<textarea name="progress" rows="3">${esc(o.progress)}</textarea></label>
      <div class="row"><button class="primary" type="submit">保存</button>
        ${o.id ? '<button type="button" class="danger" id="od-del">删除</button>' : ''}</div>
    </form>
    ${o.d365_id ? `<div class="sec">D365 信息</div><dl class="kv">${D365_INFO.map(([k, l]) =>
      `<dt>${l}</dt><dd>${esc(o[k]) || '<span class="muted">—</span>'}</dd>`).join('')}</dl>` : ''}`;
  d.hidden = false;
  $('#od-close').onclick = () => (d.hidden = true);
  if ($('#od-open')) $('#od-open').onclick = () => api('open_url', o.d365_link);
  S.curOrder = o;
  if (fromD365) loadOrderParts(o.id);
  const reopen = () => { refreshView(); openOrder(o.id); };
  if ($('#od-plan-save')) $('#od-plan-save').onclick = () => safe(async () => {
    const day = $('#od-plan-day').value;
    if (!day) { toast('请先选择计划上门日期', true); return; }
    await api('set_plan', o.id, day, $('#od-plan-slot').value);
    toast('计划上门已保存，排班表已更新'); reopen();
  });
  if ($('#od-plan-clear')) $('#od-plan-clear').onclick = () => safe(async () => {
    await api('set_plan', o.id, '', ''); toast('已清除计划，工单回到待排期'); reopen();
  });
  if ($('#od-assign')) $('#od-assign').onclick = () => assignDialog([o.id], reopen);
  if ($('#od-appt-save')) $('#od-appt-save').onclick = async () => {
    const v = $('#od-appt').value;
    if (!v) { toast('请先选择预约上门时间', true); return; }
    if (!await ask(`把 D365 订单 ${o.service_order_no} 的预约上门时间改为 ${v.replace('T', ' ')}？`, '保存到 D365')) return;
    const r = await safe(() => api('d365_set_appointment', o.id, v));
    if (r) { toast('预约时间已写入 D365，派单和排班已同步'); reopen(); }
  };
  $('#od-form').onsubmit = e => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.target));
    if (o.id) data.id = o.id;
    safe(async () => { await api('save_order', data); d.hidden = true; toast('已保存'); loadOrders(); });
  };
  if ($('#od-del')) $('#od-del').onclick = async () => {
    if (!await ask('删除这条订单？（D365 里的订单不受影响，下次同步若仍在范围内会重新出现）', '删除')) return;
    safe(async () => { await api('delete_order', o.id); d.hidden = true; loadOrders(); });
  };
}

function findD365() {
  modal(`<h2>按订单号查询 D365</h2>
    <div class="row"><input id="fd-no" placeholder="服务订单号（如 2610028417）或 Udesk 工单号（如 SVC-59137）" style="flex:1">
      <button class="primary" id="fd-go">查询</button></div>
    <div id="fd-out"></div><div class="actions"><button data-close>关闭</button></div>`, (box, close) => {
    $('[data-close]', box).onclick = close;
    const go = async () => {
      const no = $('#fd-no', box).value.trim(); if (!no) return;
      $('#fd-out', box).innerHTML = '<div class="muted">查询中…</div>';
      const r = await safe(() => api('find_d365', no));
      if (!r) { $('#fd-out', box).innerHTML = ''; return; }
      if (!r.ok) { $('#fd-out', box).innerHTML = `<div class="check"><span class="bad">${esc(r.message)}</span>，请先到「设置」登录 D365。</div>`; return; }
      if (!r.rows.length) { $('#fd-out', box).innerHTML = '<div class="check">D365 里没有找到这个订单号。</div>'; return; }
      $('#fd-out', box).innerHTML = r.rows.map(x => `<div class="card" style="margin-top:8px">
        <div class="row"><b class="mono">${esc(x.service_order_no)}</b><span class="mono muted">${esc(x.udesk_no)}</span>
          <span class="badge b-${esc(x.status)}">${esc(x.d365_status)}</span><span class="tag">${esc(x.order_type)}</span>
          <span style="margin-left:auto"></span><button class="link" data-open="${esc(x.d365_link)}">D365 ↗</button>
          <button class="primary" data-add="${esc(x.service_order_no)}">${x.exists ? '更新到订单清单' : '加入订单清单'}</button></div>
        <dl class="kv"><dt>客户</dt><dd>${esc(x.customer)}</dd><dt>工程师</dt><dd>${esc(x.engineer) || '—'}</dd>
          <dt>设备</dt><dd>${esc(x.serial)} ${esc(x.model)}</dd><dt>问题</dt><dd>${esc(x.problem)}</dd>
          <dt>预约上门</dt><dd>${esc(x.appointment) || '—'}</dd><dt>出发/到达</dt><dd>${esc(x.setoff_time) || '—'} / ${esc(x.arrival_time) || '—'}</dd>
          <dt>现场完工</dt><dd>${esc(x.complete_time) || '—'}</dd><dt>地址</dt><dd>${esc(x.address)}</dd>
          <dt>服务网点</dt><dd>${esc(x.network)}</dd></dl></div>`).join('');
      $$('[data-open]', box).forEach(b => (b.onclick = () => api('open_url', b.dataset.open)));
      $$('[data-add]', box).forEach(b => (b.onclick = () => safe(async () => {
        await api('add_from_d365', b.dataset.add); toast('已加入订单清单'); close(); loadOrders();
      })));
    };
    $('#fd-go', box).onclick = go;
    $('#fd-no', box).onkeydown = e => { if (e.key === 'Enter') go(); };
    $('#fd-no', box).focus();
  });
}

// ======================= 排班表 =======================
async function loadSchedule() {
  const start = $('#s-start').value, days = $('#s-days').value;
  const g = await safe(() => api('schedule', start, days));
  if (!g) return;
  S.grid = g;
  api('save_settings', {schedule_start: start, schedule_days: Number(days)});
  const head1 = g.dates.map(d => {
    const w = new Date(d + 'T00:00:00').getDay();
    return `<th colspan="2" class="day ${d === g.today ? 'today' : ''} ${w % 6 === 0 ? 'wkend' : ''}">${d.slice(5).replace('-', '/')}<small>周${WEEK[w]}</small></th>`;
  }).join('');
  const head2 = g.dates.map(() => '<th>上午</th><th>下午</th>').join('');
  const body = g.rows.map(r => `<tr><th class="name">${esc(r.name)}</th><td class="type">${esc(r.type)}</td>` +
    g.dates.map(d => S.boot.slots.map((s, i) => {
      const c = r.cells[`${d}|${s}`];
      const shown = c.raw || '';
      const cls = c.state !== 'normal' ? c.state : (c.raw ? 'st-' + c.raw : 'st-blank');
      const tip = c.n ? `该时段已派 ${c.n} 单` + (c.state === 'conflict' ? `，但排班为「${c.raw}」` : '') + '\n' +
        c.jobs.map(j => `${j.visit ? (j.visit.time ? j.visit.time + ' ' : '') + j.visit.source + ' ' : ''}${j.no || ''} ${j.customer || ''}` +
          `${j.d365_status ? '（' + j.d365_status + '）' : ''}${j.risk && j.risk.level ? ' ⚠' + j.risk.text : ''}`).join('\n') : '';
      return `<td class="cell ${cls} ${i ? 'pm' : ''}" data-e="${r.id}" data-d="${d}" data-s="${s}" title="${esc(tip)}">
        ${esc(shown)}${c.n ? `<span class="n">${c.n}</span>` : ''}</td>`;
    }).join('')).join('') + '</tr>').join('');
  const foot = [['外勤可派人数', '外勤'], ['驻点在岗人数', '驻点']].map(([label, typ]) =>
    `<tr><th class="name">${label}</th><td class="type"></td>` + g.dates.map(d => S.boot.slots.map((s, i) => {
      const n = (g.totals[typ] || {})[`${d}|${s}`] ?? 0;
      return `<td class="${n === 0 ? 'z' : n === 1 ? 'o' : ''} ${i ? 'pm' : ''}">${n}</td>`;
    }).join('')).join('') + '</tr>').join('');
  $('#s-table').innerHTML = `<thead><tr><th class="name" rowspan="2">姓名</th><th class="type" rowspan="2">类型</th>${head1}</tr>
    <tr>${head2}</tr></thead><tbody>${body}</tbody><tfoot>${foot}</tfoot>`;
  await loadPool();
}

// 待排期：定计划上门日期后进入排班表
async function loadPool() {
  const rows = await safe(() => api('unscheduled'));
  if (!rows) return;
  const slots = S.boot.slots.map(s => `<option>${s}</option>`).join('');
  $('#s-pool').innerHTML = rows.length ? `<thead><tr><th>预约截止</th><th>超期</th><th style="width:290px">计划上门</th><th>服务订单号</th>
    <th>Udesk 工单号</th><th>客户</th><th>工程师</th><th>D365 状态</th><th>问题</th></tr></thead><tbody>` + rows.map(o => `
    <tr data-pool="${o.id}"><td class="nowrap">${deadlineHtml(o)}</td><td>${riskBadge(o.risk)}</td>
      <td class="nowrap"><div class="row" style="flex-wrap:nowrap"><input type="date" data-pd value="${esc(S.boot.today)}" style="width:140px">
        <select data-ps style="width:70px">${slots}</select><button class="primary" data-plan>排期</button></div></td>
      <td class="mono nowrap click" data-order="${o.id}"><u>${esc(o.service_order_no)}</u></td><td class="mono nowrap">${esc(o.udesk_no)}</td>
      <td>${esc(o.customer)}</td><td class="nowrap">${esc(o.engineer) || '<span class="muted">未派工</span>'}</td>
      <td class="nowrap">${esc(o.d365_status)}</td><td class="wrap">${esc((o.problem || '').slice(0, 80))}</td></tr>`).join('') + '</tbody>'
    : '<tr><td class="empty">没有待排期的工单 👍</td></tr>';
}

const refreshView = () => show(S.view);

function cellMenu(td, x, y) {
  const m = $('#menu');
  const colors = {空闲: '--s-free', 外出: '--s-out', 驻点: '--s-site', 休假: '--s-leave', 培训: '--s-train', 休息: '--s-rest'};
  const row = (S.grid?.rows || []).find(r => r.id === +td.dataset.e);
  const jobs = row ? row.cells[`${td.dataset.d}|${td.dataset.s}`].jobs : [];
  m.innerHTML = (jobs.length ? `<div class="menu-head">${esc(row.name)} ${esc(td.dataset.d.slice(5))} ${esc(td.dataset.s)} 的工单</div>` +
    jobs.map(j => `<button class="job-btn" ${j.order_id ? `data-order="${j.order_id}"` : 'disabled'}>
      <b class="mono">${esc(j.no || '无工单号')}</b> ${esc(j.customer || '')}
      <span class="muted small">${j.visit ? esc((j.visit.time ? j.visit.time + ' ' : '') + j.visit.source) : ''} ${esc(j.d365_status || '')}
        ${j.risk && j.risk.level ? '· ⚠ ' + esc(j.risk.text) : ''}</span></button>`).join('') +
    '<div class="menu-head">改排班状态</div>' : '') + S.boot.statuses.map(s => `<button data-v="${s}"><span class="sw" style="background:var(${colors[s]})"></span>${s}</button>`).join('') +
    '<button data-v=""><span class="sw"></span>清空</button>';
  m.hidden = false;
  m.style.left = Math.min(x, innerWidth - 140) + 'px';
  m.style.top = Math.min(y, innerHeight - 260) + 'px';
  m.style.top = Math.max(8, Math.min(y, innerHeight - m.offsetHeight - 8)) + 'px';
  m.onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    m.hidden = true;
    if (b.dataset.order) { openOrder(+b.dataset.order); return; }
    safe(async () => { await api('set_cell', +td.dataset.e, td.dataset.d, td.dataset.s, b.dataset.v); loadSchedule(); });
  };
}

// ======================= 今日查询 =======================
async function loadToday() {
  const r = await safe(() => api('day_status', $('#t-day').value, $('#t-slot').value));
  if (!r) return;
  const k = (label, n, unit) => `<div class="kpi ${n === 0 ? 'z' : n === 1 ? 'o' : ''}"><span class="muted">${label}</span><b>${n}</b>${unit}</div>`;
  const rs = await api('risk_summary').catch(() => ({overdue: 0, soon: 0, plan_late: 0, unscheduled: 0}));
  const nRisk = rs.overdue + rs.soon + rs.plan_late;
  $('#t-kpis').innerHTML = k('外勤可派', r.field_available, '人') + k('驻点在岗', r.site_on_duty, '人') +
    `<div class="kpi"><span class="muted">当天上门</span><b>${r.orders.length}</b>单</div>
     <div class="kpi click ${nRisk ? 'z' : ''}" data-goto="超期风险" title="点击查看"><span class="muted">⚠ 超期风险</span><b>${nRisk}</b>
       单 <span class="muted small">已超期 ${rs.overdue} · 即将 ${rs.soon} · 计划晚 ${rs.plan_late}</span></div>
     <div class="kpi click ${rs.unscheduled ? 'o' : ''}" data-goto="待排期" title="点击查看"><span class="muted">待排期</span><b>${rs.unscheduled}</b>单</div>
     <div class="kpi click ${rs.to_return ? 'o' : ''}" data-goto-parts="1" title="点击查看"><span class="muted">待归还借料</span><b>${rs.to_return}</b>张</div>`;
  $('#t-cards').innerHTML = r.rows.map(x => {
    const cls = x.status.startsWith('⚠') ? 'conflict' : x.slot_count ? 'dispatched' : 'st-' + x.schedule;
    return `<div class="eng-card">
      <div class="row"><b>${esc(x.name)}</b><span class="muted small">${esc(x.type)}</span>
        <span class="badge ${cls}" style="margin-left:auto">${esc(x.status)}</span></div>
      <div class="muted small">排班：${esc(x.schedule)} · 当天 ${x.day_count} 单 · ${esc(r.slot)} ${x.slot_count} 单</div>
      ${x.jobs.length ? x.jobs.map(jobHtml).join('') : '<div class="muted small job-empty">当天没有工单</div>'}
    </div>`;
  }).join('');
  $('#t-orders').innerHTML = r.orders.length ? `<thead><tr><th>上门</th><th>工程师</th><th>服务订单号</th><th>Udesk 工单号</th>
    <th>客户</th><th>问题</th><th>D365 状态</th><th>预约截止</th><th>设备地址</th></tr></thead><tbody>` + r.orders.map(o => `
    <tr class="click" data-order="${o.id}"><td class="nowrap">${esc(o.visit.time || o.visit.slot)}<span class="tag src-${esc(o.visit.source)}">${esc(o.visit.source)}</span></td>
      <td class="nowrap">${esc(o.engineer) || '<span class="muted">未派工</span>'}${o.local ? `<span class="tag">${esc(o.local)}</span>` : ''}</td>
      <td class="mono nowrap">${esc(o.service_order_no)}${o.order_type ? `<span class="tag">${esc(o.order_type)}</span>` : ''}</td>
      <td class="mono nowrap">${esc(o.udesk_no)}</td><td>${esc(o.customer)}</td>
      <td class="wrap">${esc((o.problem || '').slice(0, 100))}</td>
      <td><span class="badge b-${esc(o.status)}">${esc(o.d365_status || o.status)}</span></td>
      <td class="nowrap">${deadlineHtml(o)}${riskBadge(o.risk)}</td>
      <td class="wrap">${esc(o.address)}</td></tr>`).join('') + '</tbody>'
    : '<tr><td class="empty">这一天没有上门的工单（没定计划的单在「排班表」下方的待排期列表里）</td></tr>';
  await loadCreated();
}

// 按创建日期查工单：选中日期及之后创建的工单
async function loadCreated() {
  const from = $('#c-from').value;
  if (!from) return;
  const r = await safe(() => api('orders_since', from, $('#c-status').value));
  if (!r) return;
  $('#c-count').textContent = `共 ${r.rows.length} 单`;
  const hint = $('#c-hint');
  hint.hidden = !r.beyond_sync;
  if (r.beyond_sync) hint.innerHTML = `平时只同步最近的工单（${esc(r.synced_from)} 起），更早创建的可能不全。
    <button class="primary" id="c-pull">从 D365 拉取 ${esc(from)} 起的工单</button>`;
  $('#c-table').innerHTML = r.rows.length ? `<thead><tr><th>创建日期</th><th>服务订单号</th><th>Udesk 工单号</th><th>客户</th>
    <th>机器序列号</th><th>问题</th><th>工程师</th><th>状态</th><th>D365 状态</th><th>预约上门</th></tr></thead><tbody>` +
    r.rows.map(o => `<tr class="click" data-order="${o.id}">
      <td class="nowrap">${esc(o.day)}</td>
      <td class="mono nowrap">${esc(o.service_order_no)}${o.order_type ? `<span class="tag">${esc(o.order_type)}</span>` : ''}</td>
      <td class="mono nowrap">${esc(o.udesk_no)}</td><td>${esc(o.customer)}</td><td class="mono">${esc(o.serial)}</td>
      <td class="wrap">${esc((o.problem || '').slice(0, 100))}</td><td class="nowrap">${esc(o.engineer) || '<span class="muted">未派工</span>'}</td>
      <td><span class="badge b-${esc(o.status)}">${esc(o.status)}</span></td>
      <td class="nowrap">${esc(o.d365_status)}</td><td class="nowrap">${esc(o.appointment)}</td></tr>`).join('') + '</tbody>'
    : '<tr><td class="empty">这个日期之后没有创建的工单</td></tr>';
}

// 一条派单（关联的 D365 工单）的小卡片，点击打开工单详情
function jobHtml(j) {
  const no = j.service_order_no || j.order_no;
  return `<div class="job ${j.order_id ? 'click' : ''}" ${j.order_id ? `data-order="${j.order_id}"` : ''}>
    <div class="row"><span class="tag slot">${esc(j.slot)}</span>
      ${j.visit ? `<span class="small">${esc(j.visit.time)}</span><span class="tag src-${esc(j.visit.source)}">${esc(j.visit.source)}</span>` : ''}
      <b class="mono small">${esc(no) || '无工单号'}</b>${j.udesk_no ? `<span class="mono muted small">${esc(j.udesk_no)}</span>` : ''}
      ${j.d365_status ? `<span class="badge b-${esc(j.order_status)}" style="margin-left:auto">${esc(j.d365_status)}</span>`
        : `<span class="tag" style="margin-left:auto">${j.source === 'd365' ? 'D365' : '手工'}</span>`}</div>
    <div>${esc(j.customer)}</div>
    ${j.problem ? `<div class="muted small clip">${esc(j.problem)}</div>` : ''}
    ${j.address ? `<div class="muted small clip">📍 ${esc(j.address)}</div>` : ''}
    ${j.appointment ? `<div class="small muted">考核截止 ${esc(j.appointment.slice(5))} ${riskBadge(j.risk)}</div>` : ''}
  </div>`;
}

// ======================= 配件（只读，来自 D365） =======================
// 这些工单状态下 D365 不允许再改配件消耗：已取消、提交审批、服务完工、远程完工、异常完工
const CONSUMPTION_LOCKED = [15, 105, 110, 130, 140];
const BORROW_STATES = ['需处理', '待归还', '归还被拒', '审批通过待退', '归还审批中', '借用中', '已用完', '已归还', ''];
const stateBadge = s => `<span class="badge bs-${esc(s)}">${esc(s)}</span>`;
const money = (v, cur) => `${esc(cur || '')}${Number(v || 0).toFixed(2)}`;
const qty = v => Number(v || 0) % 1 ? Number(v).toFixed(2) : String(Number(v || 0));

async function loadParts() {
  S.partsTab = S.partsTab || 'borrow';
  $$('#p-tabs [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === S.partsTab));
  $('#p-borrow').hidden = S.partsTab !== 'borrow';
  $('#p-charge').hidden = S.partsTab !== 'charge';
  if (S.partsTab === 'borrow') return loadBorrows();
  const cur = $('#c2-eng').value;
  $('#c2-eng').innerHTML = '<option value="">全部工程师</option>' + S.engineers.map(e => `<option>${esc(e.name)}</option>`).join('');
  $('#c2-eng').value = cur;
  return loadCharges();
}

async function loadBorrows() {
  S.borrowState = S.borrowState ?? '需处理';
  const r = await safe(() => api('parts_borrows', S.borrowState));
  if (!r) return;
  const todo = (r.counts['待归还'] || 0) + (r.counts['归还被拒'] || 0);
  $('#b-chips').innerHTML = BORROW_STATES.map(s => {
    const n = s === '需处理' ? todo : s ? (r.counts[s] || 0) : Object.values(r.counts).reduce((a, b) => a + b, 0);
    return `<button class="chip ${s === S.borrowState ? 'on' : ''}" data-state="${esc(s)}">${esc(s || '全部')} <b>${n}</b></button>`;
  }).join('');
  $('#b-table').innerHTML = r.rows.length ? `<thead><tr><th>状态</th><th>借料单</th><th>服务订单号</th><th>客户</th><th>工程师</th>
    <th>借料时间</th><th>配件（剩余 / 借出）</th><th>归还单</th><th>工单状态</th></tr></thead><tbody>` + r.rows.map(b => `
    <tr class="click" ${b.order_id ? `data-order="${b.order_id}"` : ''}>
      <td>${stateBadge(b.state)}</td>
      <td class="mono nowrap"><a class="lnk" data-link="${esc(b.d365_link)}">${esc(b.code)} ↗</a><div class="muted small">${esc(b.status)}</div></td>
      <td class="mono nowrap">${esc(b.service_order_no || '')}<div class="muted small">${esc(b.udesk_no || '')}</div></td>
      <td>${esc(b.customer || '')}</td><td class="nowrap">${esc(b.engineer || '')}</td>
      <td class="nowrap">${esc((b.created || '').slice(0, 10))}${b.days != null ? `<div class="muted small">${b.days} 天前</div>` : ''}</td>
      <td class="wrap">${b.parts.map(p => `<div><span class="mono small">${esc(p.part_no)}</span> ${esc(p.name)}
        <b class="${p.remaining_qty > 0 ? 'warn' : 'muted'}">${qty(p.remaining_qty)}</b> / ${qty(p.quantity)}</div>`).join('') || '<span class="muted">—</span>'}</td>
      <td class="nowrap">${b.returns.map(x => `<div class="mono small">${esc(x.code)} <span class="muted">${esc(x.status)}</span></div>`).join('') || '<span class="muted">—</span>'}</td>
      <td class="nowrap">${esc(b.d365_status || b.order_status || '')}</td></tr>`).join('') + '</tbody>'
    : `<tr><td class="empty">${S.borrowState === '需处理' ? '没有需要处理的借料单 👍' : '没有记录'}（点左下「同步 D365」可刷新）</td></tr>`;
}

async function loadCharges() {
  const r = await safe(() => api('parts_charges', $('#c2-from').value, $('#c2-to').value, $('#c2-eng').value, $('#c2-only').checked));
  if (!r) return;
  $('#c2-kpis').innerHTML = `<div class="kpi"><span class="muted">收费金额合计</span><b>${money(r.total, r.currency)}</b></div>
    <div class="kpi"><span class="muted">涉及工单</span><b>${r.orders}</b>单</div>
    <div class="kpi"><span class="muted">配件行数</span><b>${r.rows.length}</b>行</div>`;
  $('#c2-table').innerHTML = r.rows.length ? `<thead><tr><th>工单日期</th><th>服务订单号</th><th>客户</th><th>工程师</th><th>配件号</th><th>名称</th>
    <th>数量</th><th>剩余</th><th>单价</th><th>折扣</th><th>金额</th><th>收费</th><th>借料单</th></tr></thead><tbody>` + r.rows.map(p => `
    <tr class="click" ${p.order_id ? `data-order="${p.order_id}"` : ''}>
      <td class="nowrap">${esc(p.day || '')}</td><td class="mono nowrap">${esc(p.service_order_no || '')}</td><td>${esc(p.customer || '')}</td>
      <td class="nowrap">${esc(p.engineer || '')}</td><td class="mono nowrap">${esc(p.part_no)}</td><td>${esc(p.name)}</td>
      <td>${qty(p.quantity)}</td><td>${qty(p.remaining_qty)}</td><td class="nowrap">${money(p.price, r.currency)}</td>
      <td>${qty(p.discount)}%</td><td class="nowrap"><b>${esc(p.total_text || money(p.total, r.currency))}</b></td>
      <td>${p.is_charge ? '<span class="badge b-处理中">收费</span>' : '<span class="muted">否</span>'}</td>
      <td class="mono nowrap">${esc(p.material_code || '')}</td></tr>`).join('') + '</tbody>'
    : '<tr><td class="empty">这段时间没有配件记录</td></tr>';
}

// 工单详情里的配件部分
async function loadOrderParts(oid, refresh) {
  const box = $('#od-parts'); if (!box) return;
  box.innerHTML = `<div class="muted small">${refresh ? '正在从 D365 读取…' : '读取中…'}</div>`;
  const r = await safe(() => api(refresh ? 'refresh_order_parts' : 'order_parts', oid));
  if (!r) { box.innerHTML = ''; return; }
  const charged = r.parts.filter(p => p.is_charge);
  const orderLock = CONSUMPTION_LOCKED.includes((S.curOrder || {}).d365_status_code)
    ? `工单状态为「${(S.curOrder || {}).d365_status}」，不能改消耗` : '';
  const lockOf = p => p.writeoff_doc ? `已核销（${p.writeoff_doc}）` : p.return_order ? `已在归还单 ${p.return_order} 上` : orderLock;
  box.innerHTML = `
    <div class="row"><span class="muted small">共 ${r.parts.length} 个配件 · 收费 ${charged.length} 个，合计 <b>${money(r.charge_total, r.currency)}</b></span>
      <button class="link" id="od-parts-refresh" style="margin-left:auto">从 D365 刷新</button></div>
    ${r.parts.length ? `<table class="grid compact parts-tb"><thead><tr><th>配件</th><th title="确认数量">确认</th><th>消耗</th><th>剩余</th><th>收费</th><th>金额</th><th>借料单</th></tr></thead><tbody>` +
      r.parts.map(p => { const lock = lockOf(p); return `<tr data-part="${esc(p.id)}">
        <td><span class="mono small">${esc(p.part_no)}</span><div>${esc(p.name)}</div></td><td>${qty(p.confirm_qty)}</td>
        <td class="nowrap">${lock ? `${qty(p.consumption_qty)} <span class="muted small" title="${esc(lock)}">🔒</span>`
          : `<input type="number" class="cq" min="0" max="${p.confirm_qty}" step="1" value="${qty(p.consumption_qty)}" data-old="${p.consumption_qty}">
             <button class="link" data-save-cq hidden>保存</button>`}</td>
        <td class="${p.remaining_qty > 0 ? 'warn' : ''}">${qty(p.remaining_qty)}</td>
        <td>${p.is_charge ? '<span class="badge b-处理中">收费</span>' : '<span class="muted">否</span>'}</td>
        <td class="nowrap">${esc(p.total_text)}</td><td class="mono small">${esc(p.material_code)}</td></tr>`; }).join('') + '</tbody></table>' +
      '<div class="muted small">改「消耗」后点保存，直接写入 D365；剩余数量 = 确认 − 消耗，由 D365 重新计算。</div>'
      : '<div class="muted small">这单没有配件（或还没同步）</div>'}
    ${r.borrows.length ? '<div class="sec small">借料归还</div>' + r.borrows.map(b => `<div class="row">${stateBadge(b.state)}
      <a class="lnk mono" data-open="${esc((r.materials.find(m => m.id === b.id) || {}).d365_link || '')}">${esc(b.code)} ↗</a>
      <span class="muted small">剩余 ${qty(b.remaining)} 件 · ${esc(b.return_status || '')}${b.returns.length ? ' · 归还单 ' + b.returns.map(x => esc(x.code) + '（' + esc(x.status) + '）').join('、') : ''}</span></div>`).join('') : ''}`;
  $('#od-parts-refresh').onclick = () => loadOrderParts(oid, true);
  // 改消耗：数值变了才显示「保存」；确认后写回 D365
  box.oninput = e => {
    if (!e.target.matches('input.cq')) return;
    const btn = e.target.parentElement.querySelector('[data-save-cq]');
    btn.hidden = Number(e.target.value) === Number(e.target.dataset.old);
  };
  box.onclick = async e => {
    if (!e.target.matches('[data-save-cq]')) return;
    const tr = e.target.closest('tr[data-part]'), inp = $('input.cq', tr);
    const v = Number(inp.value), max = Number(inp.max), old = Number(inp.dataset.old);
    if (!(v >= 0 && v <= max)) { toast(`消耗只能在 0 到确认数量 ${max} 之间`, true); return; }
    const name = $('td div', tr).textContent;
    if (!await ask(`把「${name}」的消耗数量从 ${old} 改为 ${v}？\n剩余数量会变为 ${max - v}，直接写入 D365。`, '写入 D365')) return;
    const res = await safe(() => api('set_part_consumption', oid, tr.dataset.part, v));
    if (!res) return;
    toast(`已写入 D365：${res.changed.name} 消耗 ${res.changed.old} → ${res.changed.new}，剩余 ${res.changed.remaining ?? '—'}`);
    loadOrderParts(oid);
    if (S.view === 'parts') loadParts();
  };
  $$('[data-open]', box).forEach(a => (a.onclick = () => a.dataset.open && api('open_url', a.dataset.open)));
}

// ======================= 人员 =======================
async function loadPeople() {
  await refreshEngineers();
  const rows = S.engineers;
  $('#p-table').innerHTML = `<thead><tr><th style="width:50px">序号</th><th>姓名</th><th style="width:100px">类型</th><th>联系电话</th>
    <th>备注</th><th>D365 别名</th><th style="width:150px"></th></tr></thead><tbody>` + rows.map((e, i) => `<tr data-id="${e.id}">
    <td>${i + 1}</td><td><input data-k="name" value="${esc(e.name)}"></td>
    <td><select data-k="type"><option ${e.type === '外勤' ? 'selected' : ''}>外勤</option><option ${e.type === '驻点' ? 'selected' : ''}>驻点</option></select></td>
    <td><input data-k="phone" value="${esc(e.phone)}"></td><td><input data-k="remark" value="${esc(e.remark)}"></td>
    <td><input data-k="aliases" value="${esc(e.aliases)}" placeholder="如 Lee Ryan"></td>
    <td class="nowrap"><button class="link" data-mv="-1">上移</button><button class="link" data-mv="1">下移</button>
      <button class="link danger" data-rm>删除</button></td></tr>`).join('') + '</tbody>';
}

async function savePersonRow(tr) {
  const data = {id: +tr.dataset.id};
  $$('[data-k]', tr).forEach(i => (data[i.dataset.k] = i.value));
  await safe(async () => { await api('save_engineer', data); await refreshEngineers(); toast('已保存'); });
}

// ======================= 设置 =======================
async function loadSettings() {
  const b = await api('bootstrap');
  S.boot = b;
  const f = $('#set-form');
  for (const el of f.elements) {
    if (!el.name || !(el.name in b.settings)) continue;
    if (el.type === 'checkbox') el.checked = !!b.settings[el.name]; else el.value = b.settings[el.name];
  }
  updateSyncState();
}

async function saveSettings(e) {
  e.preventDefault();
  const data = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    data[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value.trim();
  }
  await safe(async () => { S.boot.settings = await api('save_settings', data); toast('设置已保存'); });
}

function setLogin(ok, msg) {
  const p = $('#login-state');
  p.className = 'pill ' + (ok === true ? 'ok' : ok === false ? 'bad' : '');
  p.textContent = ok === true ? 'D365：已登录' : ok === false ? 'D365：未登录' : 'D365：未检查';
  if (msg !== undefined) $('#login-msg').textContent = msg;
}

async function checkLogin() {
  $('#login-msg').textContent = '检查中…';
  const r = await safe(() => api('check_login'));
  if (r) setLogin(r.ok, r.ok ? '已登录，可以同步。' : r.message);
}

// ======================= 同步 =======================
let pollTimer;
async function updateSyncState() {
  const st = await api('sync_state').catch(() => null);
  if (!st) return;
  $('#btn-sync').disabled = st.running;
  $('#btn-sync').textContent = st.running ? '同步中…' : '同步 D365';
  $('#sync-msg').textContent = (st.last_run ? st.last_run.slice(5, 16) + ' ' : '') + (st.message || '');
  $('#sync-log').textContent = (st.log || []).slice().reverse().join('\n') || '（暂无）';
  if (st.need_login) setLogin(false);
  else if (st.ok) setLogin(true);
  clearTimeout(pollTimer);
  if (st.running) pollTimer = setTimeout(updateSyncState, 1500);
  else if (S.wasRunning) { S.wasRunning = false; show(S.view); }
  if (st.running) S.wasRunning = true;
}

async function startSync() {
  await safe(() => api('start_sync'));
  S.wasRunning = true;
  updateSyncState();
}

// ======================= 在线更新 =======================
function showUpdatePill(st) {
  const b = $('#btn-update');
  b.hidden = !st.available;
  b.textContent = `⬆ 新版本 ${st.latest}`;
}

async function checkUpdate(manual) {
  if (manual) $('#u-msg').textContent = '检查中…';
  const st = await api('update_check').catch(() => null);
  if (!st) return;
  $('#u-msg').textContent = st.message || '';
  showUpdatePill(st);
  if (manual && st.available) updateDialog();
}

function updateDialog() {
  let timer;
  const close = modal(`<h2>软件更新</h2><div id="ud-body"></div><div class="actions" id="ud-actions"></div>`, (box, close) => {
    const render = st => {
      const pct = st.progress || 0;
      $('#ud-body', box).innerHTML = `<div>当前版本 <b>${esc(st.current)}</b> → 新版本 <b>${esc(st.latest)}</b>
          ${st.size ? `<span class="muted small">（${(st.size / 1048576).toFixed(0)} MB）</span>` : ''}</div>
        ${st.notes ? `<div class="sec">更新内容</div><pre class="log notes">${esc(st.notes)}</pre>` : ''}
        ${['downloading', 'extracting', 'ready'].includes(st.phase)
          ? `<div class="bar-outer"><div class="bar-inner" style="width:${pct}%"></div></div>` : ''}
        <div class="muted small">${esc(st.message || '')}</div>
        ${!st.can_apply ? '<div class="check"><span class="bad">当前是开发模式运行，只能检查，不能自动替换程序。</span></div>' : ''}`;
      const a = $('#ud-actions', box);
      if (st.phase === 'ready') a.innerHTML = '<button data-close>稍后</button><button class="primary" id="ud-apply">立即重启更新</button>';
      else if (st.phase === 'downloading' || st.phase === 'extracting') a.innerHTML = '<button data-close>后台下载</button>';
      else if (st.phase === 'restarting') a.innerHTML = '';
      else a.innerHTML = `<button data-close>以后再说</button>${st.available && st.can_apply ? '<button class="primary" id="ud-dl">下载并更新</button>' : ''}`;
    };
    const poll = async () => {
      const st = await api('update_state').catch(() => null);
      if (!st) return;
      render(st);
      if (['downloading', 'extracting'].includes(st.phase)) timer = setTimeout(poll, 500);
    };
    box.addEventListener('click', async e => {
      if ('close' in e.target.dataset) { clearTimeout(timer); close(); }
      if (e.target.id === 'ud-dl') { await safe(() => api('update_download')); poll(); }
      if (e.target.id === 'ud-apply') {
        if (!await safe(() => api('update_apply'))) return;
        $('#ud-body', box).innerHTML = '<div>正在关闭并更新，几秒后新版本会自动打开…</div>';
        $('#ud-actions', box).innerHTML = '';
      }
    });
    poll();
  });
  return close;
}

// ======================= 初始化 =======================
async function init() {
  S.boot = await api('bootstrap');
  await refreshEngineers();
  $('#s-start').value = S.boot.settings.schedule_start;
  $('#s-days').value = String(S.boot.settings.schedule_days || 14);
  $('#t-day').value = S.boot.today;
  $('#t-slot').value = S.boot.slot_now;

  $('#nav').onclick = e => { const a = e.target.closest('a'); if (a) show(a.dataset.view); };
  $('#btn-sync').onclick = startSync;

  // 订单
  $('#o-status').onchange = loadOrders;
  $('#o-eng').onchange = loadOrders;
  let kwTimer; $('#o-kw').oninput = () => { clearTimeout(kwTimer); kwTimer = setTimeout(loadOrders, 250); };
  $('#o-table').onclick = e => {
    if (e.target.id === 'o-all') {
      $$('[data-ck]').forEach(c => { c.checked = e.target.checked; S.selected[c.checked ? 'add' : 'delete'](+c.dataset.ck); });
      updateBatch(); return;
    }
    if (e.target.dataset.ck) { S.selected[e.target.checked ? 'add' : 'delete'](+e.target.dataset.ck); updateBatch(); return; }
    if (e.target.closest('td.ck')) return;
    const tr = e.target.closest('tr[data-id]'); if (tr) openOrder(+tr.dataset.id);
  };
  $('#o-batch').onclick = () => assignDialog([...S.selected], loadOrders);
  $('#o-udesk').onclick = () => api('open_udesk');
  $('#o-find').onclick = findD365;
  $('#o-new').onclick = () => openOrder(null);

  // 排班
  $('#s-start').onchange = loadSchedule;
  $('#s-days').onchange = loadSchedule;
  $('#s-prev').onclick = () => { $('#s-start').value = addDays($('#s-start').value, -7); loadSchedule(); };
  $('#s-next').onclick = () => { $('#s-start').value = addDays($('#s-start').value, 7); loadSchedule(); };
  $('#s-today').onclick = () => { $('#s-start').value = monday(S.boot.today); loadSchedule(); };
  $('#s-table').onclick = e => { const td = e.target.closest('td.cell'); if (td) { e.stopPropagation(); cellMenu(td, e.clientX, e.clientY); } };
  document.addEventListener('click', e => { if (!e.target.closest('#menu')) $('#menu').hidden = true; });
  // 待排期：排期按钮
  $('#s-pool').addEventListener('click', e => {
    if (!e.target.matches('[data-plan]')) return;
    const tr = e.target.closest('tr[data-pool]');
    const day = $('[data-pd]', tr).value;
    if (!day) { toast('请先选择日期', true); return; }
    safe(async () => {
      const o = await api('set_plan', +tr.dataset.pool, day, $('[data-ps]', tr).value);
      toast(`${o.service_order_no} 已排到 ${day} ${o.planned_slot}${o.risk.level === 'plan_late' ? '（⚠ 晚于预约截止）' : ''}`);
      loadSchedule();
    });
  });
  // 今日查询的风险 / 待排期指标：点击跳到订单清单并筛选
  $('#t-kpis').addEventListener('click', e => {
    if (e.target.closest('[data-goto-parts]')) { S.borrowState = '需处理'; S.partsTab = 'borrow'; show('parts'); return; }
    const k = e.target.closest('[data-goto]'); if (!k) return;
    $('#o-status').value = k.dataset.goto; show('orders');
  });
  // 配件页
  $('#p-tabs').onclick = e => { const b = e.target.closest('[data-tab]'); if (b) { S.partsTab = b.dataset.tab; loadParts(); } };
  $('#b-chips').onclick = e => { const c = e.target.closest('[data-state]'); if (c) { S.borrowState = c.dataset.state; loadBorrows(); } };
  ['#c2-from', '#c2-to', '#c2-eng', '#c2-only'].forEach(s => ($(s).onchange = loadCharges));
  $('#c2-from').value = addDays(S.boot.today, -30);
  $('#v-parts').addEventListener('click', e => {
    const l = e.target.closest('[data-link]'); if (l) { e.stopPropagation(); api('open_url', l.dataset.link); }
  }, true);
  // 今日查询 / 派单等处带 data-order 的工单，点击打开工单详情
  $('#main').addEventListener('click', e => {
    const el = e.target.closest('[data-order]');
    if (el && !e.target.closest('#v-orders')) openOrder(+el.dataset.order);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { $('#menu').hidden = true; $('#drawer').hidden = true; } });

  // 今日
  $('#t-day').onchange = loadToday;
  $('#t-slot').onchange = loadToday;
  $('#t-now').onclick = () => { $('#t-day').value = S.boot.today; $('#t-slot').value = S.boot.slot_now; loadToday(); };
  $('#c-from').value = addDays(S.boot.today, -7);
  $('#c-from').onchange = loadCreated;
  $('#c-status').onchange = loadCreated;
  $('#c-hint').onclick = async e => {
    if (e.target.id !== 'c-pull') return;
    if (!await safe(() => api('sync_since', $('#c-from').value))) { toast('正在同步中，请稍候'); return; }
    S.wasRunning = true;
    updateSyncState();
  };

  // 人员
  $('#p-add').onclick = async () => {
    let n = 1; while (S.engineers.some(e => e.name === '新工程师' + n)) n++;
    await safe(() => api('save_engineer', {name: '新工程师' + n, type: '外勤'}));
    loadPeople();
  };
  $('#p-table').onchange = e => { const tr = e.target.closest('tr[data-id]'); if (tr) savePersonRow(tr); };
  $('#p-table').onclick = async e => {
    const tr = e.target.closest('tr[data-id]'); if (!tr) return;
    if (e.target.dataset.mv) { await safe(() => api('move_engineer', +tr.dataset.id, +e.target.dataset.mv)); loadPeople(); }
    if ('rm' in e.target.dataset) {
      if (!await ask('删除这位工程师？他的排班和派单记录也会一起删除。', '删除')) return;
      await safe(() => api('delete_engineer', +tr.dataset.id)); loadPeople();
    }
  };

  // 设置
  $('#set-form').onsubmit = saveSettings;
  $('#b-login').onclick = () => safe(async () => {
    if (!await api('login_d365')) { toast('正在同步或登录中，请稍候'); return; }
    $('#login-msg').textContent = '已弹出登录窗口，登录成功后会自动关闭并开始同步。';
    S.wasRunning = true;
    updateSyncState();
  });
  $('#b-login-check').onclick = checkLogin;
  $('#x-file').onchange = e => {
    const file = e.target.files[0]; if (!file) return;
    const rd = new FileReader();
    rd.onload = () => safe(async () => {
      $('#x-msg').textContent = '导入中…';
      const r = await api('import_excel', rd.result.split(',')[1]);
      $('#x-msg').textContent = `导入完成：人员 ${r.engineers}，排班格子 ${r.schedule}，订单 ${r.orders}`;
      await refreshEngineers();
      e.target.value = '';
    });
    rd.readAsDataURL(file);
  };
  $('#x-export').onclick = () => safe(async () => {
    const r = await api('export_excel', $('#s-start').value, Number($('#s-days').value) * 2);
    if (r.saved) { toast('已导出到 ' + r.path); return; }
    if (!r.b64) return;
    const a = document.createElement('a');
    a.href = 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + r.b64;
    a.download = r.name; a.click();
  });
  $('#x-dir').onclick = () => api('open_data_dir');

  // 更新
  $('#app-ver').textContent = 'v' + S.boot.version;
  $('#u-cur').textContent = S.boot.version;
  $('#btn-update').onclick = updateDialog;
  $('#u-check').onclick = () => checkUpdate(true);
  if (S.boot.settings.auto_check_update) setTimeout(() => checkUpdate(false), 3000);
  if (S.boot.update_result) setTimeout(() => toast(S.boot.update_result.message, !S.boot.update_result.ok), 800);

  updateSyncState();
  setInterval(updateSyncState, 20000);
  show('orders');
}

init().catch(e => toast('启动失败：' + (e.message || e), true));
