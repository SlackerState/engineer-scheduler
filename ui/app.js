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
  const opts = S.engineers.map(e => `<option value="${e.id}">${esc(e.name)}（${esc(e.type)}）</option>`).join('');
  $('#d-eng').innerHTML = '<option value="">请选择</option>' + opts;
  const cur = $('#o-eng').value;
  $('#o-eng').innerHTML = '<option value="">全部工程师</option>' +
    S.engineers.map(e => `<option>${esc(e.name)}</option>`).join('');
  $('#o-eng').value = cur;
}

function show(view) {
  S.view = view;
  $$('#nav a').forEach(a => a.classList.toggle('on', a.dataset.view === view));
  $$('.view').forEach(v => v.classList.toggle('on', v.id === 'v-' + view));
  ({orders: loadOrders, schedule: loadSchedule, today: loadToday, dispatch: loadDispatch,
    people: loadPeople, settings: loadSettings})[view]();
}

// ======================= 订单清单 =======================
async function loadOrders() {
  const rows = await safe(() => api('list_orders', $('#o-status').value, $('#o-kw').value.trim(), $('#o-eng').value));
  if (!rows) return;
  const t = $('#o-table');
  if (!rows.length) { t.innerHTML = '<tr><td class="empty">没有订单。点左下「同步 D365」拉取 Udesk 创建的订单。</td></tr>'; return; }
  t.innerHTML = `<thead><tr><th>日期</th><th>Udesk 工单号</th><th>服务订单号</th><th>客户</th><th>机器序列号</th>
    <th>问题</th><th>工程师</th><th>状态</th><th>D365 状态</th><th>预约上门</th><th>处理进度</th></tr></thead><tbody>` +
    rows.map(o => `<tr class="click" data-id="${o.id}">
      <td class="nowrap">${esc(o.day)}</td><td class="nowrap mono">${esc(o.udesk_no)}</td>
      <td class="nowrap mono">${esc(o.service_order_no)}${o.order_type ? `<span class="tag">${esc(o.order_type)}</span>` : ''}</td>
      <td>${esc(o.customer)}</td><td class="mono">${esc(o.serial)}${o.model ? `<div class="muted small">${esc(o.model)}</div>` : ''}</td>
      <td class="wrap">${esc((o.problem || '').slice(0, 140))}</td><td class="nowrap">${esc(o.engineer)}</td>
      <td><span class="badge b-${esc(o.status)}">${esc(o.status)}</span></td>
      <td class="nowrap">${esc(o.d365_status)}</td><td class="nowrap">${esc(o.appointment)}</td>
      <td class="wrap">${esc(o.progress)}</td></tr>`).join('') + '</tbody>';
}

const D365_INFO = [
  ['udesk_no', 'Udesk 工单号'], ['order_type', '订单类型'], ['d365_status', 'D365 状态'], ['network', '服务网点'],
  ['appointment', '预约上门'], ['dispatch_time', '派单时间'], ['setoff_time', '出发时间'], ['arrival_time', '到达时间'],
  ['complete_time', '现场完工'], ['address', '设备地址'], ['contact', '设备联系人'], ['model', '机型'],
  ['warranty', '保修状态'], ['synced_at', '最后同步'],
];

async function openOrder(id) {
  const o = id ? await safe(() => api('get_order', id)) : {status: '待处理', day: S.boot.today};
  if (!o) return;
  const d = $('#drawer');
  const st = S.boot.order_statuses.map(s => `<option ${s === o.status ? 'selected' : ''}>${s}</option>`).join('');
  d.innerHTML = `
    <h2>${o.id ? '订单 ' + esc(o.service_order_no || o.udesk_no || '#' + o.id) : '手工新增订单'}
      ${o.d365_link ? '<button class="link" id="od-open">在 D365 打开 ↗</button>' : ''}
      <button class="x" id="od-close">关闭</button></h2>
    <form class="form" id="od-form" style="display:grid;gap:10px">
      <div class="two"><label>日期<input name="day" type="date" value="${esc(o.day)}"></label>
        <label>状态<select name="status">${st}</select></label></div>
      <div class="two"><label>服务订单号<input name="service_order_no" value="${esc(o.service_order_no)}"></label>
        <label>借料单号<input name="loan_no" value="${esc(o.loan_no)}"></label></div>
      <div class="two"><label>客户<input name="customer" value="${esc(o.customer)}"></label>
        <label>机器序列号<input name="serial" value="${esc(o.serial)}"></label></div>
      <label>工程师<input name="engineer" value="${esc(o.engineer)}"></label>
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
      const tip = c.n ? `该时段已派 ${c.n} 单` + (c.state === 'conflict' ? `，但排班为「${c.raw}」` : '') : '';
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
}

function cellMenu(td, x, y) {
  const m = $('#menu');
  const colors = {空闲: '--s-free', 外出: '--s-out', 驻点: '--s-site', 休假: '--s-leave', 培训: '--s-train', 休息: '--s-rest'};
  m.innerHTML = S.boot.statuses.map(s => `<button data-v="${s}"><span class="sw" style="background:var(${colors[s]})"></span>${s}</button>`).join('') +
    '<button data-v=""><span class="sw"></span>清空</button>';
  m.hidden = false;
  m.style.left = Math.min(x, innerWidth - 140) + 'px';
  m.style.top = Math.min(y, innerHeight - 260) + 'px';
  m.onclick = e => {
    const b = e.target.closest('button'); if (!b) return;
    m.hidden = true;
    safe(async () => { await api('set_cell', +td.dataset.e, td.dataset.d, td.dataset.s, b.dataset.v); loadSchedule(); });
  };
}

// ======================= 今日查询 =======================
async function loadToday() {
  const r = await safe(() => api('day_status', $('#t-day').value, $('#t-slot').value));
  if (!r) return;
  const k = (label, n, unit) => `<div class="kpi ${n === 0 ? 'z' : n === 1 ? 'o' : ''}"><span class="muted">${label}</span><b>${n}</b>${unit}</div>`;
  $('#t-kpis').innerHTML = k('外勤可派', r.field_available, '人') + k('驻点在岗', r.site_on_duty, '人');
  $('#t-table').innerHTML = `<thead><tr><th>姓名</th><th>类型</th><th>该时段状态</th><th>当天派单数</th><th>该时段派单数</th>
    <th>排班表标注</th><th>当天派单</th></tr></thead><tbody>` + r.rows.map(x => {
    const cls = x.status.startsWith('⚠') ? 'conflict' : x.slot_count ? 'dispatched' : 'st-' + x.schedule;
    return `<tr><td><b>${esc(x.name)}</b></td><td>${esc(x.type)}</td><td><span class="badge ${cls}">${esc(x.status)}</span></td>
      <td>${x.day_count}</td><td>${x.slot_count}</td><td>${esc(x.schedule)}</td>
      <td class="wrap">${x.jobs.map(j => `${esc(j.slot)}：${esc(j.customer)} ${j.order_no ? '<span class="mono muted">' + esc(j.order_no) + '</span>' : ''}`).join('<br>') || '<span class="muted">—</span>'}</td></tr>`;
  }).join('') + '</tbody>';
}

// ======================= 派单 =======================
function formData() { return Object.fromEntries(new FormData($('#d-form'))); }

async function refreshDispatchCheck() {
  const f = formData();
  const [chk, people] = await Promise.all([api('check_dispatch', f), f.day ? api('day_status', f.day, f.slot) : null]);
  const missing = chk.missing.length ? `<span class="bad">⚠ 还需填写：${chk.missing.join('、')}</span>` : '<span class="ok">✓ 必填项已填齐</span>';
  $('#d-check').innerHTML = `${missing}<br>排班表该时段状态：${esc(chk.schedule || '—')}<br>该时段已派单数：${f.engineer_id ? chk.slot_count : '—'}<br>
    结果：<span class="${chk.ok ? 'ok' : 'bad'}">${esc(chk.message)}</span>`;
  if (people) $('#d-people').innerHTML = '<thead><tr><th>姓名</th><th>类型</th><th>状态</th><th>该时段派单数</th></tr></thead><tbody>' +
    people.rows.map(x => `<tr><td>${esc(x.name)}</td><td>${esc(x.type)}</td>
      <td><span class="badge ${x.status.startsWith('⚠') ? 'conflict' : x.slot_count ? 'dispatched' : 'st-' + x.schedule}">${esc(x.status)}</span></td>
      <td>${x.slot_count}</td></tr>`).join('') + '</tbody>';
}

async function loadDispatch() {
  const f = $('#d-form');
  if (!f.day.value) f.day.value = S.boot.today;
  if (!f.dataset.init) { f.slot.value = S.boot.slot_now; f.dataset.init = 1; }
  await refreshDispatchCheck();
  await loadRecords();
}

async function loadRecords() {
  const rows = await safe(() => api('list_dispatches', $('#r-from').value, $('#r-to').value));
  if (!rows) return;
  $('#r-table').innerHTML = rows.length ? `<thead><tr><th>日期</th><th>工程师</th><th>时段</th><th>客户</th><th>地址</th><th>工单号</th>
    <th>预计时长</th><th>来源</th><th>排班表该时段状态</th><th>检查</th><th></th></tr></thead><tbody>` + rows.map(d => `<tr>
    <td class="nowrap">${esc(d.day)}</td><td>${esc(d.engineer)}</td><td>${esc(d.slot)}</td><td>${esc(d.customer)}</td>
    <td class="wrap">${esc(d.address)}</td><td class="mono">${esc(d.order_no)}</td><td>${d.hours ?? ''}</td>
    <td>${d.source === 'd365' ? '<span class="tag">D365 预约</span>' : '<span class="tag">手工</span>'}</td>
    <td>${esc(d.schedule)}</td><td class="${d.check === '✓' ? '' : 'conflict'}">${esc(d.check)}</td>
    <td><button class="link danger" data-del="${d.id}">删除</button></td></tr>`).join('') + '</tbody>'
    : '<tr><td class="empty">没有派单记录</td></tr>';
}

async function submitDispatch(e) {
  e.preventDefault();
  const f = formData();
  let r = await safe(() => api('submit_dispatch', f, false));
  if (!r) return;
  if (r.need_confirm) {
    if (!await ask(r.message, '仍然提交')) return;
    r = await safe(() => api('submit_dispatch', f, true));
    if (!r) return;
  }
  if (!r.ok) { toast(r.message, true); return; }
  toast(r.message);
  ['customer', 'address', 'order_no', 'hours'].forEach(k => ($('#d-form')[k].value = ''));
  $('#d-form').engineer_id.value = '';
  loadDispatch();
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
  $('#o-table').onclick = e => { const tr = e.target.closest('tr[data-id]'); if (tr) openOrder(+tr.dataset.id); };
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
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { $('#menu').hidden = true; $('#drawer').hidden = true; } });

  // 今日
  $('#t-day').onchange = loadToday;
  $('#t-slot').onchange = loadToday;
  $('#t-now').onclick = () => { $('#t-day').value = S.boot.today; $('#t-slot').value = S.boot.slot_now; loadToday(); };

  // 派单
  $('#d-form').onchange = refreshDispatchCheck;
  $('#d-form').oninput = e => { if (e.target.name !== 'day') refreshDispatchCheck(); };
  $('#d-form').onsubmit = submitDispatch;
  $('#r-from').onchange = loadRecords;
  $('#r-to').onchange = loadRecords;
  $('#r-clear').onclick = () => { $('#r-from').value = $('#r-to').value = ''; loadRecords(); };
  $('#r-table').onclick = async e => {
    const b = e.target.closest('[data-del]'); if (!b) return;
    if (!await ask('删除这条派单记录？（排班表上的「外出」不会自动撤销）', '删除')) return;
    safe(async () => { await api('delete_dispatch', +b.dataset.del); loadDispatch(); });
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
      $('#x-msg').textContent = `导入完成：人员 ${r.engineers}，排班格子 ${r.schedule}，派单记录 ${r.dispatches}，订单 ${r.orders}`;
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

  updateSyncState();
  setInterval(updateSyncState, 20000);
  show('orders');
}

init().catch(e => toast('启动失败：' + (e.message || e), true));
