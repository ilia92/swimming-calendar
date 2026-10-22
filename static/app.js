/* Swimming Calendar - single page app, no build step, no dependencies.
 * All data comes from /api/v1. Text goes through t(); DOM is built with el() (never innerHTML
 * with user data). Most work happens in popups (a small stack on one <dialog>), so there are
 * only a few pages. Live sync: long-poll /api/v1/changes, then re-render page and popup. */
'use strict';

// ------------------------------------------------------------------ state

const S = {
  me: null,
  settings: {},
  lang: localGet('lang') || 'bg',
  strings: {},
  rev: 0,
  route: '',
  anchor: null,                          // selected day (Date)
  calMode: localGet('calMode2') || 'month',
  templates: [],
  balance: null,                         // swimmer's own credits
  dirty: false,                          // data changed while the user was typing
};

function localGet(k) { try { return localStorage.getItem('swim.' + k); } catch (e) { return null; } }
function localSet(k, v) { try { localStorage.setItem('swim.' + k, v); } catch (e) { /* private mode */ } }

const narrow = () => matchMedia('(max-width: 760px)').matches;

// ------------------------------------------------------------------ i18n

async function loadLang(lang) {
  const res = await fetch('/i18n/' + lang + '.json', { cache: 'no-cache' });
  S.strings = flatten(await res.json());
  S.lang = lang;
  document.documentElement.lang = lang;
  localSet('lang', lang);
}

function flatten(obj, prefix = '', out = {}) {
  for (const [k, v] of Object.entries(obj)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v, prefix + k + '.', out);
    else out[prefix + k] = v;
  }
  return out;
}

function t(key, params) {
  let s = S.strings[key];
  if (s === undefined) return key;
  if (params) s = s.replace(/\{(\w+)\}/g, (_, p) => (params[p] ?? ''));
  return s;
}

const locale = () => (S.lang === 'bg' ? 'bg-BG' : 'en-GB');

// ------------------------------------------------------------------ dates (local, YYYY-MM-DD)

const iso = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const parseISO = (s) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const monday = (d) => addDays(d, -((d.getDay() + 6) % 7));
const todayISO = () => S.settings.today || iso(new Date());
const fmt = (s, opts) => new Intl.DateTimeFormat(locale(), opts).format(typeof s === 'string' ? parseISO(s) : s);
const fmtDay = (s) => fmt(s, { weekday: 'long', day: 'numeric', month: 'long' });
const fmtShort = (s) => fmt(s, { weekday: 'short', day: 'numeric', month: 'short' });
const fmtDate = (s) => fmt(s, { day: 'numeric', month: 'short', year: 'numeric' });
const weekdayName = (i, style = 'long') => fmt(addDays(new Date(2024, 0, 1), i), { weekday: style });   // 2024-01-01 = Monday
const fmtTs = (s) => new Intl.DateTimeFormat(locale(), { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(s));
const mins = (hhmm) => { const [h, m] = hhmm.split(':').map(Number); return h * 60 + m; };
const hhmm = (m) => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
const isWeekend = (ds) => { const g = parseISO(ds).getDay(); return g === 0 || g === 6; };
const nowMinutes = () => { const n = new Date(); return n.getHours() * 60 + n.getMinutes(); };
const bookingStarted = (b) => new Date(b.date + 'T' + b.start) <= new Date();
const COACH_REASONS = ['session_cancelled', 'availability_removed', 'slot_removed', 'session_removed', 'schedule_changed'];
const byCoach = (b) => b && b.status === 'cancelled' && (b.by_coach || COACH_REASONS.includes(b.reason));
const isDayMode = () => narrow() || S.calMode === 'day';

// ------------------------------------------------------------------ DOM helpers

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (k === 'value') n.value = v;
    else if (k === 'checked') n.checked = !!v;
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return n;
}
const clean = (nodes) => nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false);
function render(...nodes) { document.getElementById('app').replaceChildren(...clean(nodes)); }

function toast(msg, kind, action) {
  const n = el('div', { class: 'toast ' + (kind || '') }, el('span', {}, msg),
    action ? el('button', { type: 'button', class: 'toast-action', onclick: () => { n.remove(); action.fn(); } }, action.label) : null);
  const box = document.getElementById('toasts');
  while (box.children.length >= 2) box.firstChild.remove();
  box.append(n);
  setTimeout(() => n.remove(), action ? 7000 : kind === 'error' ? 6000 : 3000);
}

const badge = (status, b) => (b && b.reason === 'pending_approval'
  ? el('span', { class: 'badge excused' }, '⏳ ' + t('ack.pending_badge'))
  : byCoach(b || {})
  ? el('span', { class: 'badge called-off' }, t('status.called_off'))
  : el('span', { class: 'badge ' + status }, t('status.' + status)));
const field = (label, input) => el('label', { class: 'field' }, label, input);
const btn = (label, onclick, cls = '') => el('button', { class: 'btn ' + cls, type: 'button', onclick }, label);
const h2 = (text) => el('h2', {}, text);

function formData(form) {
  const out = {};
  for (const x of form.elements) {
    if (!x.name || x.disabled) continue;
    if (x.type === 'checkbox') {
      if (x.dataset.multi) { out[x.name] = out[x.name] || []; if (x.checked) out[x.name].push(x.value); } else out[x.name] = x.checked;
    } else out[x.name] = x.value;
  }
  return out;
}

function tabs(items, current, onPick) {
  return el('div', { class: 'tabs' }, items.map(([k, label]) =>
    el('button', { class: 'btn small' + (k === current ? ' active' : ''), type: 'button', onclick: () => onPick(k) }, label)));
}

// ------------------------------------------------------------------ avatars

function avatar(u, size = 32) {
  const style = 'width:' + size + 'px;height:' + size + 'px;font-size:' + Math.round(size * 0.4) + 'px';
  if (u && u.avatar) {
    return el('img', { class: 'avatar', style, alt: '', loading: 'lazy', src: '/api/v1/users/' + u.id + '/avatar?v=' + u.avatar });
  }
  const name = (u && u.name) || '?';
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join('');
  let h = 0;
  for (const ch of String((u && u.id) || name)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return el('span', { class: 'avatar', style: style + ';background:hsl(' + h + ' 55% 46%)' }, initials || '?');
}

/** Overlapping little avatars of a slot's swimmers ("+N" when there are more). */
function avatarStack(attendees, size = 20, max = 4) {
  const list = attendees.filter((a) => a.status !== 'cancelled');
  if (!list.length) return null;
  const shown = list.slice(0, list.length > max ? max - 1 : max);
  return el('span', { class: 'av-stack', title: list.map((a) => a.member || a.name).join(', ') },
    shown.map((a) => avatar({ id: a.swimmer_id, name: a.member || a.name, avatar: a.avatar }, size)),
    list.length > shown.length ? el('span', { class: 'avatar av-more', style: 'width:' + size + 'px;height:' + size + 'px;font-size:' + Math.round(size * 0.42) + 'px' },
      '+' + (list.length - shown.length)) : null);
}

/** Shrink a picked photo to a 320px centered square JPEG (keeps uploads tiny). */
function photoToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const side = Math.min(img.naturalWidth, img.naturalHeight);
      const c = document.createElement('canvas');
      c.width = c.height = 320;
      c.getContext('2d').drawImage(img, (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, 320, 320);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL('image/jpeg', 0.85));
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('image')); };
    img.src = url;
  });
}

function avatarEditor(u) {
  const file = el('input', { type: 'file', accept: 'image/*', class: 'hidden' });
  file.addEventListener('change', async () => {
    if (!file.files[0]) return;
    let data;
    try { data = await photoToDataUrl(file.files[0]); } catch (e) { toast(t('errors.invalid_image'), 'error'); return; }
    const out = await act(() => api('POST', '/users/' + u.id + '/avatar', { image: data }), t('avatar.saved'));
    if (out && out.user && S.me && out.user.id === S.me.id) { S.me.avatar = out.user.avatar; renderShell(); }
  });
  return el('div', { class: 'avatar-edit' },
    avatar(u, 72),
    el('div', { class: 'row' },
      btn('📷 ' + t(u.avatar ? 'avatar.change' : 'avatar.add'), () => file.click(), 'small'),
      u.avatar ? btn(t('avatar.remove'), async () => {
        const out = await act(() => api('DELETE', '/users/' + u.id + '/avatar'), t('common.saved'));
        if (out && out.user && S.me && out.user.id === S.me.id) { S.me.avatar = null; renderShell(); }
      }, 'small ghost danger') : null),
    file);
}

// ------------------------------------------------------------------ pickers
// A closed select-like trigger that opens a popup with a search box and a scrollable list
// (same feel as "Move to" in the task tree planner). Keyboard: arrows, Enter, Escape.

function combo({ items, placeholder, label, row, text, onChoose, selected }) {
  let open = false;
  let current = selected || null;
  const labelEl = el('span', { class: 'combo-label' });
  const trigger = el('button', { type: 'button', class: 'combo-trigger' }, labelEl, el('span', { class: 'combo-arrow' }, '▾'));
  const filter = el('input', { type: 'search', class: 'combo-filter', placeholder: t('common.search'), autocomplete: 'off' });
  const list = el('div', { class: 'combo-options', role: 'listbox' });
  const pop = el('div', { class: 'combo-popup' }, filter, list);
  const wrap = el('div', { class: 'combo' }, trigger, pop);
  const setLabel = () => labelEl.replaceChildren(current ? row(current) : el('span', { class: 'muted' }, placeholder));
  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    const hits = items.filter((x) => !q || text(x).toLowerCase().includes(q));
    list.replaceChildren(...(hits.length ? hits.map((x, i) => el('div', {
      class: 'combo-option' + (i === 0 ? ' active' : '') + (current === x ? ' selected' : ''), role: 'option',
      onclick: () => choose(x),
    }, row(x))) : [el('div', { class: 'combo-empty' }, t('common.no_matches'))]));
    list._hits = hits;
  };
  const choose = (x) => { current = x; setLabel(); close(); if (onChoose) onChoose(x); };
  const outside = (ev) => { if (!wrap.isConnected) document.removeEventListener('pointerdown', outside, true); else if (!wrap.contains(ev.target)) close(); };
  function close() { open = false; wrap.classList.remove('open'); document.removeEventListener('pointerdown', outside, true); }
  trigger.addEventListener('click', () => {
    if (open) return close();
    open = true; wrap.classList.add('open'); filter.value = ''; draw();
    document.addEventListener('pointerdown', outside, true);
    setTimeout(() => { filter.focus(); pop.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, 0);
  });
  filter.addEventListener('input', draw);
  filter.addEventListener('keydown', (ev) => {
    const rows = [...list.querySelectorAll('.combo-option')];
    const idx = rows.findIndex((r) => r.classList.contains('active'));
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      if (!rows.length) return;
      const next = (idx + (ev.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      rows.forEach((r) => r.classList.remove('active'));
      rows[next].classList.add('active');
      rows[next].scrollIntoView({ block: 'nearest' });
    } else if (ev.key === 'Enter') {
      ev.preventDefault();
      if (idx >= 0) choose(list._hits[idx]);
    } else if (ev.key === 'Escape') {
      ev.preventDefault(); ev.stopPropagation(); close(); trigger.focus();
    }
  });
  setLabel();
  wrap.value = () => current;
  wrap.reset = () => { current = null; setLabel(); };
  if (label) wrap.setAttribute('aria-label', label);
  return wrap;
}

const pad2 = (n) => String(n).padStart(2, '0');

/** Little drawn calendar icons for the Day / Week / Month tabs. */
function calIcon(kind) {
  const NS = 'http://www.w3.org/2000/svg';
  const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const svg = mk('svg', { viewBox: '0 0 18 18', width: 18, height: 18, class: 'cal-icon', 'aria-hidden': 'true' });
  svg.append(mk('rect', { x: 1.5, y: 2.5, width: 15, height: 14, rx: 2.5, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.5 }),
    mk('path', { d: 'M1.5 6.5h15M5.5 1v3M12.5 1v3', stroke: 'currentColor', 'stroke-width': 1.5, 'stroke-linecap': 'round' }));
  const cells = { day: [[6.5, 9, 5, 5]], week: [3.5, 6.1, 8.7, 11.3].map((x) => [x, 10, 2.2, 3]),
    month: [3.5, 7.75, 12].flatMap((x) => [[x, 8.5, 2.5, 2.5], [x, 12.3, 2.5, 2.5]]) }[kind];
  for (const [x, y, w, h] of cells) svg.append(mk('rect', { x, y, width: w, height: h, rx: 0.8, fill: 'currentColor' }));
  return svg;
}
// "Иван, Мария", "Иван; Мария" and "Иван Мария" all give ["Иван", "Мария"]
const splitMembers = (v) => (v || '').split(/[\s,;]+/).filter(Boolean);

/** 24-hour time picker (two selects); the value lives in a hidden input named `name`. */
function timePicker(name, value, step = 5) {
  const [h0, m0] = (value || '08:00').split(':').map(Number);
  const hidden = el('input', { type: 'hidden', name, value: pad2(h0) + ':' + pad2(m0) });
  const minutesList = [];
  for (let m = 0; m < 60; m += step) minutesList.push(m);
  if (!minutesList.includes(m0)) minutesList.push(m0), minutesList.sort((a, b) => a - b);
  const hs = el('select', { 'aria-label': 'hh' }, Array.from({ length: 24 }, (_, i) => el('option', { value: pad2(i), selected: i === h0 }, pad2(i))));
  const ms = el('select', { 'aria-label': 'mm' }, minutesList.map((m) => el('option', { value: pad2(m), selected: m === m0 }, pad2(m))));
  const sync = (ev) => {
    if (ev) ev.stopPropagation();
    hidden.value = hs.value + ':' + ms.value;
    hidden.dispatchEvent(new Event('input', { bubbles: true }));
    hidden.dispatchEvent(new Event('change', { bubbles: true }));
  };
  hs.addEventListener('change', sync); ms.addEventListener('change', sync);
  hs.addEventListener('input', (ev) => ev.stopPropagation()); ms.addEventListener('input', (ev) => ev.stopPropagation());
  hidden._set = (v) => {
    const [h, m] = v.split(':');
    hs.value = h;
    if (![...ms.options].some((o) => o.value === m)) ms.append(el('option', { value: m }, m));
    ms.value = m;
    hidden.value = v;
  };
  return el('div', { class: 'tp' }, hs, el('span', { class: 'tp-sep' }, ':'), ms, hidden);
}

const holidayCache = {};
async function holidaysForMonth(y, m) {
  const k = y + '-' + m;
  if (!holidayCache[k]) {
    const first = new Date(y, m, 1);
    holidayCache[k] = api('GET', '/holidays?from=' + iso(monday(first)) + '&to=' + iso(addDays(monday(new Date(y, m + 1, 0)), 6)))
      .then((r) => r.holidays).catch(() => ({}));
  }
  return holidayCache[k];
}

/** Date picker with a Monday-first mini calendar; weekends and BG holidays in red. */
function datePicker(name, value, opts = {}) {
  const hidden = el('input', { type: 'hidden', name, value: value || '' });
  const label = el('span', {});
  const trigger = el('button', { type: 'button', class: 'combo-trigger' }, label, el('span', { class: 'combo-arrow' }, '📅'));
  const pop = el('div', { class: 'combo-popup dp' });
  const wrap = el('div', { class: 'combo dp-wrap' }, trigger, pop, hidden);
  let view = parseISO(value || todayISO());
  view = new Date(view.getFullYear(), view.getMonth(), 1);
  const setLabel = () => label.replaceChildren(hidden.value ? fmt(hidden.value, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })
    : el('span', { class: 'muted' }, opts.placeholder || '—'));
  const outside = (ev) => { if (!wrap.isConnected) document.removeEventListener('pointerdown', outside, true); else if (!wrap.contains(ev.target)) close(); };
  function close() { wrap.classList.remove('open'); document.removeEventListener('pointerdown', outside, true); }
  const pick = (ds) => {
    hidden.value = ds;
    setLabel(); close();
    hidden.dispatchEvent(new Event('input', { bubbles: true }));
    hidden.dispatchEvent(new Event('change', { bubbles: true }));
    if (opts.onChange) opts.onChange(ds);
  };
  async function draw() {
    const y = view.getFullYear(); const m = view.getMonth();
    const hol = await holidaysForMonth(y, m);
    const start = monday(view);
    const cells = [];
    for (let i = 0; i < 7; i++) cells.push(el('span', { class: 'dp-dow' + (i >= 5 ? ' weekend' : '') }, weekdayName(i, 'narrow')));
    for (let i = 0; i < 42; i++) {
      const d = addDays(start, i);
      const ds = iso(d);
      if (i >= 35 && d.getMonth() !== m) break;
      cells.push(el('button', {
        type: 'button', title: hol[ds] ? hol[ds][S.lang] : null,
        class: 'dp-day' + (d.getMonth() !== m ? ' other' : '') + (isWeekend(ds) || hol[ds] ? ' weekend' : '') +
          (ds === todayISO() ? ' today' : '') + (ds === hidden.value ? ' selected' : ''),
        onclick: () => pick(ds),
      }, d.getDate()));
    }
    pop.replaceChildren(
      el('div', { class: 'dp-head' },
        btn('‹', () => { view = new Date(y, m - 1, 1); draw(); }, 'small ghost icon'),
        el('strong', {}, fmt(view, { month: 'long', year: 'numeric' })),
        btn('›', () => { view = new Date(y, m + 1, 1); draw(); }, 'small ghost icon')),
      el('div', { class: 'dp-grid' }, cells),
      el('div', { class: 'dp-foot' },
        btn(t('common.today'), () => pick(todayISO()), 'small ghost'),
        opts.optional ? btn(t('common.clear'), () => pick(''), 'small ghost') : null));
  }
  trigger.addEventListener('click', () => {
    if (wrap.classList.contains('open')) return close();
    if (hidden.value) { const v = parseISO(hidden.value); view = new Date(v.getFullYear(), v.getMonth(), 1); }
    wrap.classList.add('open'); draw();
    document.addEventListener('pointerdown', outside, true);
  });
  pop.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(); } });
  setLabel();
  return wrap;
}

// ------------------------------------------------------------------ API

class ApiErr extends Error {
  constructor(status, body) { super(body.message || 'error'); this.status = status; this.code = body.error; this.body = body; }
}

let pending = 0;
let busyTimer = null;
function busy(delta) {
  pending = Math.max(0, pending + delta);
  clearTimeout(busyTimer);
  const bar = document.getElementById('busy');
  if (pending) busyTimer = setTimeout(() => bar.classList.add('on'), 250);   // quick requests: no flicker
  else bar.classList.remove('on');
}

async function api(method, path, body) {
  const opts = { method, headers: {}, credentials: 'same-origin' };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const tracked = !path.startsWith('/changes');
  if (tracked) busy(1);
  let res;
  try { res = await fetch('/api/v1' + path, opts); } finally { if (tracked) busy(-1); }
  const ctype = res.headers.get('Content-Type') || '';
  const data = ctype.includes('json') ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new ApiErr(res.status, typeof data === 'object' ? data : { error: 'server_error' });
    if (res.status === 401 && path !== '/login' && S.me) { S.me = null; closeDialog(); showLogin(); }
    if (err.code === 'password_change_required' && S.me) { S.me.must_change_password = true; closeDialog(); showForcedPasswordChange(); }
    throw err;
  }
  return data;
}

function errText(e) {
  if (e instanceof ApiErr) {
    const key = 'errors.' + e.code;
    return S.strings[key] ? t(key) : (e.message || e.code);
  }
  return t('errors.network');
}

/** Run an API action, toast the outcome, then redraw page and open popup. */
async function act(fn, okMsg) {
  try {
    const out = await fn();
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(out) : okMsg);
    await refreshAll();
    return out || true;
  } catch (e) {
    if (!(e instanceof ApiErr && (e.status === 401 || e.code === 'password_change_required'))) toast(errText(e), 'error');
    return null;
  }
}

// ------------------------------------------------------------------ popups
// A stack of builder functions on one <dialog>. Builders are async and fetch their own data,
// so a popup can simply be rebuilt after any change (or when someone else changed something).

const D = { stack: [], node: document.getElementById('dialog') };

async function popup(builder, opts = {}) {
  if (opts.replace) D.stack.pop();
  else if (!opts.keep) D.stack = [];
  D.stack.push(builder);
  await renderDialog();
}

async function renderDialog() {
  const builder = D.stack[D.stack.length - 1];
  if (!builder) return;
  let content;
  try { content = await builder(); } catch (e) {
    if (e instanceof ApiErr && e.status === 401) return;
    content = [el('p', { class: 'notice' }, errText(e))];
  }
  if (builder !== D.stack[D.stack.length - 1]) return;     // user navigated meanwhile
  const head = el('div', { class: 'dlg-head' },
    D.stack.length > 1 ? btn('‹ ' + t('common.back'), backDialog, 'ghost small') : null,
    el('span', { class: 'spacer' }),
    btn('✕', closeDialog, 'ghost small icon'));
  D.node.replaceChildren(head, ...clean([content]));
  if (!D.node.open) D.node.showModal();
}

function backDialog() { D.stack.pop(); if (D.stack.length) renderDialog(); else closeDialog(); }
function closeDialog() { D.stack = []; if (D.node.open) D.node.close(); }
D.node.addEventListener('close', () => { D.stack = []; if (S.dirty) { S.dirty = false; refresh(); } });
D.node.addEventListener('click', (ev) => { if (ev.target === D.node) closeDialog(); });   // backdrop

function confirmDialog(message, okLabel, danger) {
  const c = document.getElementById('confirm');
  return new Promise((resolve) => {
    let answer = false;
    c.onclose = () => resolve(answer);
    const done = (v) => { answer = v; c.close(); };
    c.replaceChildren(el('p', {}, message), el('div', { class: 'actions' },
      btn(t('common.cancel'), () => done(false)),
      btn(okLabel || t('common.ok'), () => done(true), danger ? 'danger solid' : 'primary')));
    c.showModal();
  });
}

async function refreshAll() {
  await refresh();
  if (D.node.open) await renderDialog();
}

// ------------------------------------------------------------------ shell

const isCoach = () => S.me && S.me.role === 'coach';

const NAV_ICONS = { today: '📍', calendar: '📅', swimmers: '👥', stats: '📊', visits: '🎟️' };

function navItems() {
  if (!S.me) return [];
  if (isCoach()) return [['today', 'nav.today'], ['calendar', 'nav.calendar'], ['swimmers', 'nav.swimmers'], ['stats', 'nav.stats']];
  return [['calendar', 'nav.calendar']];
}

function renderShell() {
  document.title = t('app.title');
  document.getElementById('offline').textContent = '⚠ ' + t('common.offline');
  document.getElementById('brand-text').textContent = t('app.title');
  for (const b of document.querySelectorAll('#lang-flags button')) b.classList.toggle('active', b.dataset.lang === S.lang);
  const top = document.getElementById('topbar');
  top.hidden = !S.me || S.me.must_change_password;
  if (!S.me) return;
  document.getElementById('account-btn').replaceChildren(avatar(S.me, 26), el('span', { class: 'acc-name' }, S.me.name.split(' ')[0]));
  const settingsBtn = document.getElementById('settings-btn');
  settingsBtn.hidden = !isCoach();
  settingsBtn.title = t('settings.title');
  const pill = document.getElementById('credits-pill');
  pill.hidden = isCoach() || !S.balance;
  if (S.balance) {
    pill.className = 'pill' + (S.balance.low ? ' low' : '');
    pill.textContent = t('credits.pill', { n: S.balance.available });
    const exp = expiryText(S.balance);
    pill.title = t('nav.visits') + (exp ? ' · ' + exp.text : '');
  }
  const nav = document.getElementById('nav');
  const current = S.route.split('/')[0];
  if (isCoach()) {
    nav.replaceChildren(...navItems().map(([r, k]) => el('a', { href: '#/' + r, class: current === r ? 'active' : '' },
      el('span', { class: 'nav-ico', 'aria-hidden': 'true' }, NAV_ICONS[r]), el('span', { class: 'nav-txt' }, t(k)))));
  } else {
    nav.replaceChildren(el('a', { href: '#/calendar', class: 'active' }, el('span', { class: 'nav-ico', 'aria-hidden': 'true' }, NAV_ICONS.calendar),
      el('span', { class: 'nav-txt' }, t('nav.calendar'))),
      el('a', { href: '#', onclick: (ev) => { ev.preventDefault(); popup(myVisitsPopup); } }, el('span', { class: 'nav-ico', 'aria-hidden': 'true' }, NAV_ICONS.visits),
        el('span', { class: 'nav-txt' }, t('nav.visits')),
        S.attention ? el('span', { class: 'nav-badge' }, S.attention) : null));
  }
}

async function setLang(lang) {
  if (lang === S.lang) return;
  await loadLang(lang);
  if (S.me) { try { await api('PATCH', '/me', { lang }); } catch (e) { /* not fatal */ } }
  renderShell();
  refreshAll();
}
for (const b of document.querySelectorAll('#lang-flags button')) b.addEventListener('click', () => setLang(b.dataset.lang));

function flagButtons() {
  return el('div', { class: 'flags' }, [['bg', 'Български'], ['en', 'English']].map(([lang, name]) => el('button', {
    type: 'button', class: lang === S.lang ? 'active' : '', title: name,
    onclick: async () => { await loadLang(lang); showLogin(); },
  }, el('img', { src: '/flags/' + (lang === 'en' ? 'gb' : 'bg') + '.svg', alt: lang.toUpperCase() }))));
}
document.getElementById('account-btn').addEventListener('click', () => popup(accountPopup));
document.getElementById('settings-btn').addEventListener('click', () => popup(settingsPopup));
document.getElementById('credits-pill').addEventListener('click', () => popup(myVisitsPopup));

function applyTheme() {
  const th = localGet('theme');
  if (th) document.documentElement.dataset.theme = th; else delete document.documentElement.dataset.theme;
}
document.getElementById('theme-toggle').addEventListener('click', () => {
  const dark = document.documentElement.dataset.theme === 'dark' ||
    (!document.documentElement.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
  localSet('theme', dark ? 'light' : 'dark');
  applyTheme();
});

async function logout() {
  try { await api('POST', '/logout'); } catch (e) { /* ignore */ }
  S.me = null; S.balance = null;
  closeDialog();
  showLogin();
}

// ------------------------------------------------------------------ login & password

function showLogin() {
  renderShell();
  let busy = false;
  const err = el('p', { class: 'notice hidden' });
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      if (busy) return;
      busy = true;
      try {
        const out = await api('POST', '/login', formData(form));
        if (out.user.lang && out.user.lang !== S.lang) await loadLang(out.user.lang);
        await boot();
      } catch (e) {
        err.textContent = errText(e);
        err.classList.remove('hidden');
      } finally { busy = false; }
    },
  },
  field(t('login.username'), el('input', { name: 'username', autocomplete: 'username', autocapitalize: 'none', required: true, autofocus: true })),
  field(t('login.password'), el('input', { name: 'password', type: 'password', autocomplete: 'current-password', required: true })),
  err,
  el('button', { class: 'btn primary', type: 'submit' }, t('login.submit')),
  el('p', { class: 'muted small', style: 'text-align:center;margin:0' }, t('login.forgot')),
  flagButtons());
  render(el('div', { class: 'login-wrap' }, el('div', { class: 'card login' },
    el('div', { class: 'logo-big' }, '🏊'), el('h1', {}, t('app.title')), el('p', { class: 'muted login-sub' }, t('login.tagline')), form)));
}

function passwordForm(onDone) {
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      if (v.new_password !== v.repeat) { toast(t('account.mismatch'), 'error'); return; }
      try {
        await api('POST', '/me/password', { current_password: v.current_password, new_password: v.new_password });
        toast(t('account.password_changed'));
        form.reset();
        if (onDone) onDone();
      } catch (e) { toast(errText(e), 'error'); }
    },
  },
  field(t('account.current_password'), el('input', { name: 'current_password', type: 'password', autocomplete: 'current-password', required: true })),
  field(t('account.new_password'), el('input', { name: 'new_password', type: 'password', autocomplete: 'new-password', minlength: 8, required: true })),
  field(t('account.repeat_password'), el('input', { name: 'repeat', type: 'password', autocomplete: 'new-password', minlength: 8, required: true })),
  el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit' }, t('account.change_password'))));
  return form;
}

function showForcedPasswordChange() {
  renderShell();
  render(el('div', { class: 'login-wrap' }, el('div', { class: 'card login' },
    el('h1', {}, t('account.first_login_title')),
    el('p', { class: 'muted' }, t('account.first_login_text', { name: S.me.name })),
    passwordForm(() => boot()),
    btn(t('nav.logout'), logout, 'ghost small'))));
}

// ------------------------------------------------------------------ router

const ROUTES = { today: viewToday, calendar: viewCalendar, swimmers: viewSwimmers, stats: viewStats };

async function refresh() {
  if (!S.me) return showLogin();
  if (S.me.must_change_password) return showForcedPasswordChange();
  const raw = location.hash.replace(/^#\/?/, '');
  const name = raw.split('/')[0];
  if (!ROUTES[name] || !navItems().some(([r]) => r === name)) {
    location.replace(isCoach() ? '#/today' : '#/calendar');
    return;
  }
  S.route = raw;
  renderShell();
  try {
    await ROUTES[name]();
  } catch (e) {
    if (!(e instanceof ApiErr && e.status === 401)) render(el('div', { class: 'notice' }, errText(e)));
  }
}
window.addEventListener('hashchange', () => { S.dirty = false; refresh(); });

function calendarActive() {
  return S.me && !S.me.must_change_password && S.route.startsWith('calendar') && !D.node.open && !document.getElementById('confirm').open;
}
function setMode(m) { S.calMode = m; localSet('calMode2', m); refresh(); }
document.addEventListener('keydown', (ev) => {
  if (!calendarActive() || ev.ctrlKey || ev.metaKey || ev.altKey || ev.target.matches('input, textarea, select, [contenteditable]')) return;
  const k = ev.key.toLowerCase();
  if (ev.key === 'ArrowLeft') shiftCal(-1);
  else if (ev.key === 'ArrowRight') shiftCal(1);
  else if (k === 't') { S.anchor = parseISO(todayISO()); refresh(); }
  else if (k === 'd') setMode('day');
  else if (k === 'w' && !narrow()) setMode('week');
  else if (k === 'm') setMode('month');
  else return;
  ev.preventDefault();
});
let touch = null;
document.addEventListener('touchstart', (ev) => {
  touch = calendarActive() && ev.touches.length === 1 && !ev.target.closest('.combo, input, textarea, select, .tg')
    ? { x: ev.touches[0].clientX, y: ev.touches[0].clientY, t: Date.now() } : null;
}, { passive: true });
document.addEventListener('touchend', (ev) => {
  if (!touch) return;
  const dx = ev.changedTouches[0].clientX - touch.x;
  const dy = ev.changedTouches[0].clientY - touch.y;
  if (Math.abs(dx) > 70 && Math.abs(dy) < 45 && Date.now() - touch.t < 600) shiftCal(dx < 0 ? 1 : -1);   // swipe = next / previous
  touch = null;
}, { passive: true });

setInterval(() => {
  if (S.route === 'today' && !D.node.open && !document.getElementById('confirm').open &&
    !(document.activeElement && document.activeElement.matches('input, textarea, select'))) refresh();
}, 30000);

let lastNarrow = narrow();
window.addEventListener('resize', () => {
  if (narrow() !== lastNarrow) { lastNarrow = narrow(); if (S.route.startsWith('calendar')) refresh(); }
});

// ------------------------------------------------------------------ live sync

async function liveSync() {
  for (;;) {
    if (!S.me) { await sleep(3000); continue; }
    try {
      const out = await api('GET', '/changes?since=' + S.rev + '&wait=25');
      if (S.offline) { S.offline = false; document.getElementById('offline').hidden = true; refreshAll(); }
      if (out.rev !== S.rev) {
        const first = S.rev === 0;
        S.rev = out.rev;
        if (!first) {
          if (document.activeElement && document.activeElement.matches('input, textarea, select')) S.dirty = true;
          else refreshAll();
        }
      }
    } catch (e) {
      if (!(e instanceof ApiErr)) { S.offline = true; document.getElementById('offline').hidden = false; }
      await sleep(5000);
    }
  }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ calendar

function calRange() {
  if (!S.anchor) S.anchor = parseISO(todayISO());
  if (S.calMode === 'month') {
    const first = new Date(S.anchor.getFullYear(), S.anchor.getMonth(), 1);
    const last = new Date(first.getFullYear(), first.getMonth() + 1, 0);
    return { first, start: monday(first), end: addDays(monday(last), 6) };
  }
  const start = monday(S.anchor);
  return { start, end: addDays(start, 6) };
}

function shiftCal(dir) {
  if (S.calMode === 'month') S.anchor = new Date(S.anchor.getFullYear(), S.anchor.getMonth() + dir, 1);
  else S.anchor = addDays(S.anchor, isDayMode() ? dir : dir * 7);
  refresh();
}

async function viewCalendar() {
  const r = calRange();
  const reqs = [api('GET', '/sessions?from=' + iso(r.start) + '&to=' + iso(r.end))];
  if (!isCoach()) reqs.push(api('GET', '/credits/' + S.me.id));
  const [data, cr] = await Promise.all(reqs);
  if (cr) { S.balance = cr.balance; renderShell(); }

  const title = S.calMode === 'month'
    ? fmt(r.first, { month: 'long', year: 'numeric' })
    : (isDayMode() ? fmtDay(iso(S.anchor)) : fmt(r.start, { day: 'numeric', month: 'short' }) + ' – ' + fmt(r.end, { day: 'numeric', month: 'short', year: 'numeric' }));
  const modeTab = (k) => [k, [calIcon(k), el('span', {}, t('calendar.' + k))]];
  // fixed slots (nav · title · modes · actions) so nothing jumps when the title length changes
  const toolbar = el('div', { class: 'cal-toolbar' },
    el('div', { class: 'cal-nav row nowrap' },
      btn('‹', () => shiftCal(-1), 'small icon'),
      btn('›', () => shiftCal(1), 'small icon'),
      btn(t('common.today'), () => { S.anchor = parseISO(todayISO()); refresh(); }, 'small')),
    el('strong', { class: 'cal-title' }, title),
    el('div', { class: 'cal-modes' }, tabs(narrow() ? [modeTab('day'), modeTab('month')] : [modeTab('day'), modeTab('week'), modeTab('month')],
      S.calMode === 'month' ? 'month' : (narrow() ? 'day' : S.calMode),
      (m) => { S.calMode = m; localSet('calMode2', m); refresh(); })),
    isCoach() ? el('div', { class: 'cal-actions row' },
      btn('🕒 ' + t('availability.button'), () => popup(availabilityPopup), 'small'),
      btn('🌴 ' + t('absence.button'), () => popup(absencePopup), 'small'),
      btn('+ ' + t('slot.create'), () => popup(() => createSlotPopup(S.calMode === 'month' || iso(S.anchor) < todayISO() ? todayISO() : iso(S.anchor), '17:00')), 'small primary')) : null);

  let body;
  if (S.calMode === 'month') body = monthGrid(r, data);
  else if (isDayMode()) body = await dayAgenda(r, data);
  else body = weekView(r, data);
  let offBanner = null;
  let nextUp = null;
  let ackBanner = null;
  if (isCoach()) {
    const att = (await api('GET', '/bookings?attention=1')).bookings;
    if (att.length) {
      const disputes = att.filter((b) => (b.ack || {}).status === 'disputed');
      ackBanner = el('div', { class: 'notice ack-banner' },
        el('strong', {}, '⚠ ' + t('ack.coach_banner', { d: disputes.length, p: att.length - disputes.length })),
        att.map((b) => el('a', { href: '#', onclick: (ev) => { ev.preventDefault(); popup(() => swimmerPopup(b.swimmer_id)); } },
          fmtShort(b.date) + ' ' + b.start + ' · ' + (b.member || b.swimmer_name) + ((b.ack || {}).status === 'disputed' ? ' ⚠' : ' ⏳'))));
    }
  } else {
    const recent = (await api('GET', '/bookings?from=' + iso(addDays(parseISO(todayISO()), -(S.settings.ack_window_days || 3) - 1)))).bookings;
    const att = (await api('GET', '/bookings?attention=1')).bookings;
    const toConfirm = recent.filter((b) => b.can_ack && !(b.ack || {}).status);
    const promoted = att.filter((b) => b.waitlist_notice);
    const unread = (await api('GET', '/messages')).messages.filter((m) => !m.read);
    S.attention = att.length + toConfirm.length + unread.length;
    if (promoted.length || unread.length) {
      offBanner = el('div', {}, unread.map((m) => el('div', { class: 'notice info msg-banner' },
        el('div', { class: 'grow' }, el('strong', {}, '📣 ' + m.from + ': '), m.text, el('div', { class: 'muted small' }, fmtTs(m.created_at))),
        btn(t('ack.seen'), () => act(() => api('POST', '/messages/' + m.id + '/read'), null), 'small'))),
      promoted.length ? el('div', { class: 'notice wait-banner' }, el('strong', { class: 'grow' }, '🎉 ' + t('wait.promoted') + ' '),
        promoted.map((b) => el('span', { class: 'nowrap' }, fmtShort(b.date) + ' ' + b.start + (b.member ? ' (' + b.member + ')' : '') + ' ')),
        btn(t('ack.seen'), () => act(async () => { for (const b of promoted) await api('POST', '/bookings/' + b.id + '/ack', { action: 'seen' }); }, null), 'small')) : null);
    }
    att.splice(0, att.length, ...att.filter((b) => !b.waitlist_notice));
    renderShell();
    if (att.length || toConfirm.length) {
      ackBanner = el('div', { class: 'notice info ack-banner' },
        att.length ? el('div', {}, el('strong', {}, '🔔 ' + t('ack.swimmer_banner', { n: att.length })), ' ',
          el('a', { href: '#', onclick: (ev) => { ev.preventDefault(); popup(myVisitsPopup); } }, t('ack.review'))) : null,
        toConfirm.length ? el('div', { class: 'row' }, el('span', { class: 'grow' }, '✓ ' + t('ack.to_confirm', { n: toConfirm.length })),
          btn(t('ack.confirm_all'), () => act(async () => {
            for (const b of toConfirm) await api('POST', '/bookings/' + b.id + '/ack', { action: 'confirm' });
          }, t('ack.confirmed')), 'small primary'),
          btn(t('ack.review'), () => popup(myVisitsPopup), 'small')) : null);
    }
    const upcoming = (await api('GET', '/bookings?from=' + todayISO())).bookings;
    nextUp = upcoming.filter((b) => b.status === 'booked' && new Date(b.date + 'T' + b.end) > new Date())
      .sort((a, b) => (a.date + a.start).localeCompare(b.date + b.start))[0] || null;
    const msgBanner = offBanner;
    offBanner = null;
    const off = upcoming.filter(byCoach);
    const noted = upcoming.filter((b) => b.status === 'booked' && b.session_note).reverse();
    if (noted.length) {
      offBanner = el('div', { class: 'notice info note-banner' }, el('strong', {}, '📢 ' + t('session.note_banner')),
        noted.map((b) => el('a', { href: '#', onclick: (ev) => { ev.preventDefault(); popup(() => sessionPopup(b.session_key, b.date)); } },
          el('strong', {}, fmtShort(b.date) + ' ' + b.start), ' — ' + b.session_note)));
    }
    if (off.length) {
      offBanner = [offBanner, el('div', { class: 'notice off-banner' }, el('strong', {}, '⚠ ' + t('session.off_banner')), ' ',
        off.reverse().map((b) => el('a', { href: '#', onclick: (ev) => { ev.preventDefault(); popup(() => sessionPopup(b.session_key, b.date)); } },
          fmtShort(b.date) + ' ' + b.start)))];
    }
    offBanner = [msgBanner, offBanner];
  }
  render(
    !isCoach() && S.balance ? creditsBar(S.balance) : null,
    !isCoach() ? nextTraining(nextUp) : null,
    ackBanner,
    offBanner,
    toolbar,
    !isCoach() ? legend() : null,
    isCoach() && !data.sessions.length && S.calMode !== 'month' ? el('div', { class: 'notice info' },
      t('calendar.no_sessions'), ' ', el('a', { href: '#', onclick: (ev) => { ev.preventDefault(); popup(availabilityPopup); } }, t('calendar.set_availability'))) : null,
    body);
}

/** Book a free slot with one tap; the toast offers undo while cancelling is still allowed. */
async function quickBook(s) {
  if ((s.free_members || []).length) return popup(() => sessionPopup(s.key, s.date));   // joint account: pick who comes
  try {
    const out = await api('POST', '/bookings', { session_key: s.key });
    const b = out.booking;
    toast(t('book.booked_at', { when: fmtShort(s.date) + ' ' + s.start }), '', b.can_cancel
      ? { label: t('book.undo'), fn: () => act(() => api('DELETE', '/bookings/' + b.id), t('book.cancelled')) } : null);
    await refreshAll();
  } catch (e) { toast(errText(e), 'error'); }
}

function nextTraining(b) {
  if (!b) return null;
  const days = Math.round((parseISO(b.date) - parseISO(todayISO())) / 864e5);
  const when = days === 0 ? t('next.today') : days === 1 ? t('next.tomorrow') : t('next.in_days', { n: days });
  return el('button', { type: 'button', class: 'next-card', onclick: () => popup(() => sessionPopup(b.session_key, b.date)) },
    el('span', { class: 'next-label' }, '⏭ ' + t('next.label')),
    el('strong', {}, fmtShort(b.date) + ' · ' + b.start + '–' + b.end),
    el('span', { class: 'muted small grow' }, [b.member, b.location].filter(Boolean).join(' · ')),
    el('span', { class: 'badge booked' }, when));
}

function legend() {
  return el('div', { class: 'legend' },
    el('span', {}, el('i', { class: 'ev-swatch free' }), t('legend.free')),
    el('span', {}, el('i', { class: 'ev-swatch mine' }), t('legend.mine')),
    el('span', {}, el('i', { class: 'ev-swatch full' }), t('legend.full')),
    el('span', { class: 'muted small' }, S.settings.swimmers_can_book === false ? t('legend.coach_books') : t('legend.hint')));
}

function expiryText(bal) {
  const ex = bal.expiry || [];
  if (!ex.length || bal.available <= 0) return null;
  const dated = ex.filter((e) => e.date);
  if (!dated.length) return { text: t('credits.no_expiry'), soon: false };
  const soon = (parseISO(dated[0].date) - parseISO(todayISO())) / 864e5 <= 14;
  if (ex.length === 1) return { text: t('credits.valid_until_date', { date: fmtDate(dated[0].date) }), soon };
  return {
    text: ex.map((e) => (e.date ? t('credits.n_until', { n: e.remaining, date: fmt(e.date, { day: 'numeric', month: 'short' }) })
      : t('credits.n_no_expiry', { n: e.remaining }))).join(' · '),
    soon,
  };
}

function creditsBar(bal) {
  const exp = expiryText(bal);
  const pct = bal.total ? Math.round(bal.available / Math.max(1, bal.available + bal.used + bal.reserved) * 100) : 0;
  return el('div', { class: 'credits-bar' + (bal.low ? ' low' : ''), onclick: isCoach() ? null : () => popup(myVisitsPopup) },
    el('span', { class: 'big ring', style: '--pct:' + pct, title: pct + '%' }, el('span', {}, String(bal.available))),
    el('div', { class: 'grow' },
      el('div', {}, el('strong', {}, t('credits.left_of', { left: bal.available, total: bal.total })),
        exp ? el('span', { class: 'expiry' + (exp.soon ? ' soon' : '') }, ' · ' + (exp.soon ? '⏰ ' : '') + exp.text) : null,
        bal.unpaid ? el('span', { class: 'badge danger', style: 'margin-left:.4rem' }, t('credits.unpaid', { n: bal.unpaid })) : null),
      el('div', { class: 'meter' }, el('span', { style: 'width:' + pct + '%' })),
      el('div', { class: 'muted small' }, t('credits.used_reserved', { used: bal.used, reserved: bal.reserved }),
        bal.low ? el('span', { class: 'warn-text' }, ' · ' + t(isCoach() ? 'credits.low_coach' : 'credits.low_hint')) : null)));
}

function dayClasses(ds, holidays) {
  return [isWeekend(ds) ? 'weekend' : '', holidays[ds] ? 'holiday' : '', ds === todayISO() ? 'today' : ''].filter(Boolean).join(' ');
}

function sessionState(s) {
  if (!isCoach() && byCoach(s.my_booking)) return 'cancelled mine-off';
  if (s.cancelled) return 'cancelled';
  if (!isCoach() && s.my_booking && s.my_booking.status !== 'cancelled') return 'mine';
  if (s.started) return 'past';
  if (s.free === 0) return 'full';
  return 'free';
}

function weekView(r, data) {
  let days = [];
  for (let d = r.start; d <= r.end; d = addDays(d, 1)) days.push(iso(d));
  const single = isDayMode();
  const strip = single ? dayStrip(days, data) : null;
  if (single) days = [iso(S.anchor)];
  return el('div', { class: single ? 'day-mode' : '' }, strip, timeGrid(days, data));
}

function dayStrip(days, data) {
  const marks = {};
  for (const s of data.sessions) {
    if (!isCoach() && s.my_booking && s.my_booking.status !== 'cancelled') marks[s.date] = 'mine';
    else if (!marks[s.date] && !s.cancelled) marks[s.date] = 'has';
  }
  return el('div', { class: 'day-strip' }, days.map((ds) => el('button', {
    type: 'button',
    class: 'strip-day ' + dayClasses(ds, data.holidays) + (ds === iso(S.anchor) ? ' selected' : ''),
    onclick: () => { S.anchor = parseISO(ds); refresh(); },
  }, el('span', { class: 'dow' }, fmt(ds, { weekday: 'short' })), el('span', { class: 'num' }, parseISO(ds).getDate()),
  el('i', { class: 'dot ' + (marks[ds] || '') }))));
}

const PX = 1.15;   // pixels per minute in the time grid

function layoutLanes(list) {
  // list sorted by start; overlapping sessions share the column width
  let cluster = [];
  let end = -1;
  const flush = () => {
    const lanes = [];
    for (const s of cluster) {
      let i = lanes.findIndex((e) => e <= s._s);
      if (i < 0) { i = lanes.length; lanes.push(0); }
      lanes[i] = s._e;
      s._lane = i;
    }
    for (const s of cluster) s._lanes = lanes.length;
    cluster = [];
  };
  for (const s of list) {
    if (cluster.length && s._s >= end) { flush(); end = -1; }
    cluster.push(s);
    end = Math.max(end, s._e);
  }
  flush();
}

function timeGrid(days, data) {
  const list = data.sessions.filter((s) => days.includes(s.date)).map((s) => ({ ...s, _s: mins(s.start), _e: mins(s.end) }));
  let lo = Math.min(...list.map((s) => s._s), 24 * 60);
  let hi = Math.max(...list.map((s) => s._e), 0);
  if (!list.length) { lo = 8 * 60; hi = 20 * 60; }
  lo = Math.floor(lo / 60) * 60;
  hi = Math.min(24 * 60, Math.max(Math.ceil(hi / 60) * 60, lo + 4 * 60));
  const height = (hi - lo) * PX;

  const head = el('div', { class: 'tg-row tg-head' }, el('div', { class: 'tg-corner' }),
    days.map((ds) => el('button', {
      type: 'button',
      class: 'tg-dayhead ' + dayClasses(ds, data.holidays),
      title: data.holidays[ds] ? data.holidays[ds][S.lang] : null,
      onclick: () => popup(() => dayPopup(ds)),
    },
    el('span', { class: 'dow' }, fmt(ds, { weekday: narrow() ? 'long' : 'short' })),
    el('span', { class: 'num' }, parseISO(ds).getDate()),
    data.holidays[ds] ? el('span', { class: 'hol' }, data.holidays[ds][S.lang]) : null)));

  const axis = el('div', { class: 'tg-axis', style: 'height:' + height + 'px' });
  for (let m = lo; m < hi; m += 60) axis.append(el('span', { style: 'top:' + ((m - lo) * PX) + 'px' }, hhmm(m)));

  const cols = days.map((ds) => {
    const col = el('div', {
      class: 'tg-col ' + dayClasses(ds, data.holidays),
      style: 'height:' + height + 'px;background-size:100% ' + (60 * PX) + 'px',
    });
    const dayList = list.filter((s) => s.date === ds);
    layoutLanes(dayList);
    for (const s of dayList) col.append(eventBlock(s, lo));
    if (ds === iso(new Date())) {
      const nm = nowMinutes();
      if (nm >= lo && nm <= hi) col.append(el('div', { class: 'now-line', style: 'top:' + ((nm - lo) * PX) + 'px' }));
    }
    if (isCoach()) {
      col.title = t('calendar.click_to_add');
      col.addEventListener('click', (ev) => {
        if (ev.target !== col) return;
        const m = Math.round((lo + ev.offsetY / PX) / 15) * 15;
        popup(() => createSlotPopup(ds, hhmm(Math.min(m, 23 * 60))));
      });
    }
    return col;
  });
  return el('div', { class: 'tg', style: '--cols:' + days.length },
    head, el('div', { class: 'tg-row tg-body' }, axis, cols));
}

function eventBlock(s, lo) {
  const state = sessionState(s);
  const h = (s._e - s._s) * PX - 3;
  const width = 100 / s._lanes;
  const fill = s.capacity ? Math.min(100, Math.round(s.booked / s.capacity * 100)) : 0;
  let line2;
  if (s.cancelled) line2 = s.cancel_reason === 'holiday' ? t('sessions.holiday_short') : t('sessions.cancelled');
  else if (isCoach()) line2 = s.attendees.filter((a) => a.status !== 'cancelled').map((a) => a.member || (isDayMode() && !narrow() ? a.name : a.name.split(' ')[0])).join(', ');
  else if (state === 'mine') line2 = '✓ ' + ((s.my_bookings || []).length > 1 ? (s.my_bookings.map((b) => b.member).join(', ')) : t('status.' + s.my_booking.status));
  if (!isCoach() && byCoach(s.my_booking)) line2 = '✕ ' + t('status.called_off');
  else if (state === 'full') line2 = t('sessions.full');
  else if (state === 'past') line2 = '';
  else line2 = t('sessions.free_short', { n: s.free });
  return el('button', {
    type: 'button',
    class: 'ev ' + state + (s.kind === 'extra' ? ' extra' : ''),
    style: 'top:' + ((s._s - lo) * PX + 1) + 'px;height:' + h + 'px;left:calc(' + (s._lane * width) + '% + 2px);width:calc(' + width + '% - 4px);' +
      (isCoach() && !s.cancelled ? '--fill:' + fill + '%' : ''),
    title: s.start + '–' + s.end + (s.location ? ' · ' + s.location : '') + (s.note ? '\n📢 ' + s.note : ''),
    onclick: () => popup(() => sessionPopup(s.key, s.date)),
  },
  el('span', { class: 'ev-time' }, el('span', {}, s.start, s.note ? el('span', { class: 'note-mark' }, ' 📢') : null),
    isCoach() && !s.cancelled ? el('span', { class: 'ev-count' }, s.booked + '/' + s.capacity) : null),
  h > 34 && isCoach() && !s.cancelled && s.booked ? el('span', { class: 'ev-line ev-avs' }, avatarStack(s.attendees, h > 60 ? 22 : 18, 8)) : null,
  h > 34 && line2 && !(isCoach() && !s.cancelled && s.booked) ? el('span', { class: 'ev-line' }, line2) : null,
  h > 70 && isCoach() && !s.cancelled && s.booked ? el('span', { class: 'ev-line' }, line2) : null,
  h > 54 && (s.title || s.location) ? el('span', { class: 'ev-line dim' }, s.title || s.location) : null);
}

/** One time in a swimmer's month cell: free -> book, mine/cancelled -> open, otherwise greyed. */
function slotChip(s) {
  const own = (s.my_bookings || []).filter((b) => b.status !== 'cancelled');
  const open = (ev) => { ev.stopPropagation(); popup(() => sessionPopup(s.key, s.date)); };
  if (own.length) {
    return el('button', { type: 'button', class: 'chip slot mine' + (s.note ? ' noted' : ''), title: s.note || null, onclick: open },
      narrow() ? (own.length > 1 ? own.length + '×' : '') + s.start : '✓' + (own.length > 1 ? own.length : '') + ' ' + s.start);   // green = mine
  }
  if (byCoach(s.my_booking)) return el('button', { type: 'button', class: 'chip slot x', title: t('status.called_off'), onclick: open }, '✕ ' + s.start);
  if (s.can_book) {
    return el('button', {
      type: 'button', class: 'chip slot free', title: t('book.tap_to_book') + (s.note ? '\n📢 ' + s.note : ''),
      onclick: (ev) => { ev.stopPropagation(); quickBook(s); },
    }, s.start, narrow() ? null : el('span', { class: 'slot-free' }, ' · ' + t('sessions.free_short', { n: s.free })));
  }
  if ((s.my_waitlist || []).length) return el('button', { type: 'button', class: 'chip slot wait', title: t('wait.on_list', { pos: s.my_waitlist[0].position }), onclick: open }, '⏳' + s.start);
  const why = s.free === 0 ? t('sessions.full') + ' · ' + t('wait.join') : s.started ? t('session.past') : t('errors.too_far');
  return el('button', { type: 'button', class: 'chip slot off', title: why, onclick: open }, s.start);
}

function monthGrid(r, data) {
  const by = {};
  for (const s of data.sessions) (by[s.date] = by[s.date] || []).push(s);
  const cells = [];
  for (let i = 0; i < 7; i++) cells.push(el('div', { class: 'dow' + (i >= 5 ? ' weekend' : '') }, weekdayName(i, 'short')));
  for (let d = new Date(r.start); d <= r.end; d = addDays(d, 1)) {
    const ds = iso(d);
    const list = (by[ds] || []).filter((s) => !s.cancelled);
    const allCancelled = (by[ds] || []).length > 0 && !list.length;
    let summary = [];
    const MAX_ROWS = 5;
    if (isCoach()) {
      const booked = list.reduce((a, s) => a + s.booked, 0);
      const cap = list.reduce((a, s) => a + s.capacity, 0);
      if (list.length && narrow()) {
        summary = [el('span', { class: 'chip' + (booked ? ' booked' : ''), title: t('month.slots', { n: list.length }) }, booked + '/' + cap)];
      } else if (list.length) {
        // desktop: one row per slot - time, fill bar, booked/places
        summary = list.slice(0, MAX_ROWS).map((s) => el('span', { class: 'mrow' + (s.booked ? ' has' : '') + (s.free === 0 ? ' full' : '') },
          el('span', { class: 'mt', title: s.note || null }, s.start, s.note ? '📢' : null),
          s.booked ? avatarStack(s.attendees, 15, 6) : el('span', { class: 'mbar' }, el('i', { style: 'width:0%' })),
          el('span', { class: 'mn' }, s.booked + '/' + s.capacity)));
        if (list.length > MAX_ROWS) summary.push(el('span', { class: 'more-rows' }, t('month.more', { n: list.length - MAX_ROWS })));
        summary.unshift(el('span', { class: 'day-total' + (booked ? ' booked' : '') }, booked + '/' + cap));
      }
    } else {
      const past = ds < todayISO();
      const slots = (by[ds] || []).filter((s) => {
        const mine = (s.my_bookings || []).some((b) => b.status !== 'cancelled') || byCoach(s.my_booking);
        return mine || (!past && !s.cancelled);
      });
      const room = narrow() ? 6 : 7;
      summary = slots.slice(0, room).map((s) => slotChip(s));
      if (slots.length > room) summary.push(el('span', { class: 'more-rows' }, t('month.more', { n: slots.length - room })));
    }
    if (allCancelled) summary.push(el('span', { class: 'chip x' }, t('sessions.cancelled')));
    cells.push(el('div', {
      role: 'button', tabindex: 0,
      onkeydown: (ev) => { if (ev.key === 'Enter' && ev.target === ev.currentTarget) popup(() => dayPopup(ds)); },
      class: 'cell ' + dayClasses(ds, data.holidays) + (d.getMonth() !== r.first.getMonth() ? ' other' : '') + (!isCoach() ? ' sw' : ''),
      title: data.holidays[ds] ? data.holidays[ds][S.lang] : null,
      onclick: () => popup(() => dayPopup(ds)),
    }, el('span', { class: 'num' }, d.getDate()),
    data.holidays[ds] ? el('span', { class: 'hol' }, data.holidays[ds][S.lang]) : null,
    summary));
  }
  return el('div', { class: 'month' }, cells);
}

// ------------------------------------------------------------------ popups: day & session

async function dayPopup(ds) {
  const data = await api('GET', '/sessions?from=' + ds + '&to=' + ds);
  const hol = data.holidays[ds];
  const active = data.sessions.filter((s) => !s.cancelled && !s.started);
  const rows = data.sessions.map((s) => {
    const state = sessionState(s);
    let right;
    if (isCoach()) right = el('span', { class: 'badge ' + (s.cancelled ? 'danger' : s.free === 0 ? 'warn' : '') }, s.cancelled ? t('sessions.cancelled') : s.booked + '/' + s.capacity);
    else if (state === 'mine') right = badge(s.my_booking.status);
    else if (s.can_book && (s.free_members || []).length) right = btn(t('book.book'), () => {}, 'small primary');   // opens the slot to pick members
    else if (s.can_book) right = btn(t('book.book'), (ev) => { ev.stopPropagation(); act(() => api('POST', '/bookings', { session_key: s.key }), t('book.booked')); }, 'small primary');
    else right = el('span', { class: 'muted small' }, s.cancelled ? t('sessions.cancelled') : state === 'full' ? t('sessions.full') : '');
    return el('li', { class: 'clickable state-' + state, onclick: () => popup(() => sessionPopup(s.key, s.date), { keep: true }) },
      el('strong', { class: 'nowrap' }, s.start + '–' + s.end),
      isCoach() ? el('span', { class: 'grow row nowrap', style: 'gap:.4rem;min-width:0' }, avatarStack(s.attendees, 24, 10),
        el('span', { class: 'muted small ellipsis' }, s.attendees.filter((a) => a.status !== 'cancelled').map((a) => a.member || a.name).join(', ')))
        : el('span', { class: 'muted small grow' }, [s.title, s.location].filter(Boolean).join(' · ')),
      right,
      s.note ? el('div', { class: 'row-notes visit-note coach' }, '📢 ' + s.note) : null);
  });
  return [
    el('h3', { class: isWeekend(ds) || hol ? 'red' : '' }, fmtDay(ds)),
    hol ? el('p', { class: 'notice info' }, '🎉 ' + hol[S.lang]) : null,
    rows.length ? el('ul', { class: 'list' }, rows) : el('p', { class: 'muted' }, t('sessions.none_day')),
    isCoach() ? el('div', { class: 'actions' },
      active.length ? btn(t('day.cancel_all'), async () => {
        if (!(await confirmDialog(t('day.confirm_cancel_all', { n: active.length }), t('day.cancel_all'), true))) return;
        act(async () => { for (const s of active) await api('PUT', '/overrides/' + encodeURIComponent(s.key), { cancelled: true }); }, t('common.saved'));
      }, 'danger') : null,
      btn('+ ' + t('slot.create'), () => popup(() => createSlotPopup(ds, '17:00'), { keep: true }), 'primary')) : null,
  ];
}

function sessionHead(s, inline) {
  return [
    inline
      ? el('div', { class: 'agenda-head' }, el('strong', { class: 'agenda-time' }, s.start + '–' + s.end),
        el('span', { class: 'muted small grow' }, [s.title, s.location, S.me.is_admin ? s.coach_name : null].filter(Boolean).join(' · ')),
        el('span', { class: 'small nowrap' }, t('session.places', { booked: s.booked, capacity: s.capacity })))
      : el('h3', {}, fmtDay(s.date), el('span', { class: 'muted' }, ' · ' + s.start + '–' + s.end)),
    inline ? null : el('div', { class: 'muted small' }, [s.title, s.location, S.me.is_admin ? s.coach_name : null].filter(Boolean).join(' · ')),
    s.holiday ? el('p', { class: 'notice info' }, '🎉 ' + s.holiday[S.lang]) : null,
    s.cancelled && !(s.called_off || []).length ? el('p', { class: 'notice' }, s.cancel_reason === 'holiday' ? t('sessions.holiday', { name: s.holiday ? s.holiday[S.lang] : '' }) : t('sessions.cancelled')) : null,
    s.note ? el('p', { class: 'notice info session-note' }, '📢 ' + s.note) : null,
    el('div', { class: 'cap' }, el('div', { class: 'meter' }, el('span', { style: 'width:' + Math.min(100, s.booked / s.capacity * 100) + '%' })),
      inline ? null : el('span', { class: 'small nowrap' }, t('session.places', { booked: s.booked, capacity: s.capacity }))),
  ];
}

async function sessionPopup(key, ds) {
  const data = await api('GET', '/sessions?from=' + ds + '&to=' + ds + (isCoach() ? '&detail=1' : ''));
  const s = data.sessions.find((x) => x.key === key);
  if (!s) return [el('p', { class: 'muted' }, t('errors.session_not_found'))];
  return sessionHead(s, false).concat(isCoach() ? await coachSessionBody(s) : swimmerSessionBody(s));
}

/** Day view: every slot of the day fully expanded, no popups needed. */
async function dayAgenda(r, data) {
  const ds = iso(S.anchor);
  const days = [];
  for (let d = r.start; d <= r.end; d = addDays(d, 1)) days.push(iso(d));
  let list = data.sessions.filter((x) => x.date === ds);
  let swimmers = null;
  if (isCoach() && list.length) {
    const [detail, sw] = await Promise.all([api('GET', '/sessions?from=' + ds + '&to=' + ds + '&detail=1'), api('GET', '/swimmers')]);
    list = detail.sessions;
    swimmers = sw.swimmers;
  }
  const hol = data.holidays[ds];
  const panels = [];
  for (const x of list) {
    const body = isCoach() ? await coachSessionBody(x, swimmers) : swimmerSessionBody(x, true);
    panels.push(el('section', { class: 'agenda-slot ' + sessionState(x) }, sessionHead(x, true), body));
  }
  return el('div', { class: 'day-mode' },
    dayStrip(days, data),
    hol ? el('p', { class: 'notice info' }, '🎉 ' + hol[S.lang]) : null,   // the date itself is already in the toolbar
    // joint accounts get a live count on every slot instead
    !isCoach() && panels.length && !(S.me.members || []).length ? el('p', { class: 'muted small' }, bookHint(1)) : null,
    panels.length ? el('div', { class: 'agenda' }, panels) : el('div', { class: 'card empty' }, t('sessions.none_day')),
    isCoach() ? el('div', { class: 'actions' }, btn('+ ' + t('slot.create'), () => popup(() => createSlotPopup(ds, '17:00')), 'primary')) : null);
}

function swimmerSessionBody(s, inline) {
  if (byCoach(s.my_booking) && !(s.my_bookings || []).length) return [el('p', { class: 'notice' }, '✕ ' + t('session.called_off_swimmer'))];
  const own = (s.my_bookings || []).filter((b) => b.status !== 'cancelled');
  const out = [];
  if (own.length) {
    out.push(el('ul', { class: 'list own-bookings' }, own.map((b) => el('li', {},
      el('div', { class: 'own-line' }, el('strong', { class: 'grow' }, b.member ? '👤 ' + b.member : t('session.you_are')), badge(b.status),
      b.can_cancel ? btn(t('book.cancel'), async () => {
        if (await confirmDialog(t('book.confirm_cancel', { when: fmtShort(s.date) + ' ' + s.start + (b.member ? ' (' + b.member + ')' : '') }), t('book.cancel'), true)) {
          act(() => api('DELETE', '/bookings/' + b.id), t('book.cancelled'));
        }
      }, 'small danger') : null),
      b.note ? el('div', { class: 'visit-note coach' }, '📝 ', b.note, el('span', { class: 'muted' }, ' — ' + t('notes.from_coach'))) : null,
      visitNoteInput(b.id, b.swimmer_note, t('notes.swimmer_ph'))))));
    if (own.some((b) => b.status === 'booked' && !b.can_cancel)) out.push(el('p', { class: 'muted small' }, t('book.cancel_closed_long', { h: S.settings.cancel_cutoff_hours })));
  }
  if (s.can_book) {
    const members = s.free_members || [];
    const picks = members.slice(0, s.free).map((m) => el('label', { class: 'member-pick' },
      el('input', { type: 'checkbox', value: m, checked: true }), el('span', {}, m)));
    const hint = el('p', { class: 'muted small book-hint' });
    const repeat = !inline && s.kind === 'slot' ? el('select', { class: 'auto repeat-sel', 'aria-label': t('repeat.label') },
      [1, 4, 8].map((w) => el('option', { value: w }, w === 1 ? t('repeat.once') : t('repeat.weeks', { n: w })))) : null;
    const bookBtn = btn(t('book.book'), async () => {
        const body = { session_key: s.key, repeat_weeks: repeat ? Number(repeat.value) : 1 };
        if (members.length) {
          body.members = picks.map((p) => p.querySelector('input')).filter((c) => c.checked).map((c) => c.value);
          if (!body.members.length) { toast(t('errors.pick_members'), 'error'); return; }
        }
        act(() => api('POST', '/bookings', body), (r) => (r && body.repeat_weeks > 1
          ? t('repeat.done', { n: new Set(r.bookings.map((b) => b.date)).size, skipped: (r.skipped || []).length }) : t('book.booked')));
      }, 'primary');
    // one visit per person: keep the hint and the button in step with the ticked members
    const update = () => {
      const n = members.length ? picks.filter((p) => p.querySelector('input').checked).length : 1;
      hint.replaceChildren(bookHint(n));
      hint.classList.toggle('warn-text', !!S.balance && n > S.balance.available);
      bookBtn.textContent = t('book.book') + (members.length && n !== 1 ? ' (' + n + ')' : '');
      bookBtn.disabled = members.length > 0 && n === 0;
    };
    picks.forEach((p) => p.querySelector('input').addEventListener('change', update));
    update();
    if (!inline || members.length) out.push(hint);
    out.push(el('div', { class: 'book-row' }, picks.length ? el('div', { class: 'member-picks' }, picks) : null, repeat, bookBtn));
  } else if ((s.my_waitlist || []).length) {
    out.push(el('div', { class: 'wait-box' }, s.my_waitlist.map((w) => el('div', { class: 'row' },
      el('span', { class: 'grow' }, '⏳ ' + t('wait.on_list', { pos: w.position }) + (w.member ? ' · ' + w.member : '')),
      btn(t('wait.leave'), () => act(() => api('DELETE', '/waitlist/' + w.id), t('wait.left')), 'small ghost danger')))));
  } else if (!s.started && !s.cancelled && s.free === 0 && S.settings.swimmers_can_book !== false) {
    out.push(el('p', { class: 'muted small' }, t('wait.hint')));
    out.push(el('div', { class: 'actions' }, btn('⏳ ' + t('wait.join'), async () => {
      const members = S.me.members || [];
      const who = members.length ? await pickMembers(S.me, members) : [null];
      if (!who || !who.length) return;
      act(async () => { for (const m of who) await api('POST', '/waitlist', { session_key: s.key, member: m }); }, t('wait.joined'));
    }, 'primary')));
  } else if (!own.length && !s.cancelled) {
    out.push(el('p', { class: 'muted small' }, s.started ? t('session.past') : s.free === 0 ? t('sessions.full') : t('errors.too_far')));
  }
  return out;
}

/** Inline note on one visit; saved on Enter / leaving the field. Coach and swimmer both see it. */
function visitNoteInput(bookingId, value, placeholder) {
  return el('input', {
    class: 'visit-note-input', value: value || '', maxlength: 500, placeholder,
    onchange: (ev) => act(() => api('POST', '/bookings/' + bookingId + '/note', { text: ev.target.value }), t('notes.saved')),
    onkeydown: (ev) => { if (ev.key === 'Enter') ev.target.blur(); },
  });
}

function visitNotes(b, opts = {}) {
  return [
    b.note ? el('div', { class: 'visit-note coach' }, '📝 ', el('span', {}, b.note), opts.who ? el('span', { class: 'muted' }, ' — ' + t('notes.from_coach')) : null) : null,
    b.swimmer_note ? el('div', { class: 'visit-note swimmer' }, '✉️ ', el('span', {}, b.swimmer_note), opts.who ? el('span', { class: 'muted' }, ' — ' + t('notes.from_swimmer')) : null) : null,
  ];
}

function bookHint(n) {
  const left = S.balance ? S.balance.available : '?';
  return t(n === 1 ? 'session.book_hint' : 'session.book_hint_many', { n, left, h: S.settings.cancel_cutoff_hours });
}

/** Which members of a joint account are coming (checkboxes, all free ones pre-ticked). */
function pickMembers(account, free) {
  const c = document.getElementById('confirm');
  return new Promise((resolve) => {
    let answer = null;
    c.onclose = () => resolve(answer);
    const boxes = free.map((m) => el('label', { class: 'member-pick' }, el('input', { type: 'checkbox', value: m, checked: true }), el('span', {}, m)));
    c.replaceChildren(
      el('p', {}, el('strong', {}, account.name), ' — ', t('session.who_comes')),
      el('div', { class: 'member-picks' }, boxes),
      el('div', { class: 'actions' },
        btn(t('common.cancel'), () => c.close()),
        btn(t('calendar.book_in'), () => {
          answer = boxes.map((b) => b.querySelector('input')).filter((x) => x.checked).map((x) => x.value);
          c.close();
        }, 'primary')));
    c.showModal();
  });
}

function creditsChip(c) {
  return el('span', { class: 'credit-chip' + (c.unpaid ? ' danger' : c.low ? ' warn' : ''), title: t('credits.chip_title') },
    el('span', { class: 'ok-text' }, '✓' + c.used), ' · ', el('span', {}, '⏳' + c.reserved), ' · ', el('strong', {}, t('credits.left_short', { n: c.available })),
    c.unpaid ? ' · ' + t('credits.unpaid', { n: c.unpaid }) : null);
}

function attendeeCard(s, a) {
  const commentBox = el('form', {
    class: 'form hidden comment-form', onsubmit: (ev) => {
      ev.preventDefault();
      const text = ev.target.elements.text.value;
      act(() => api('POST', '/users/' + a.swimmer_id + '/comments', { text, session_key: s.key }), t('common.saved'));
    },
  }, el('textarea', { name: 'text', required: true, maxlength: 4000, placeholder: t('comments.placeholder') }),
  el('div', { class: 'actions' }, el('button', { class: 'btn small primary', type: 'submit' }, t('comments.add'))));
  const setStatus = (st) => changeStatus(a.booking_id, a.status, st, a.member || a.name, a.protected);
  const stBtn = (st, label) => el('button', {
    type: 'button', class: 'seg ' + st + (a.status === st ? ' on' : ''), onclick: () => (a.status === st ? null : setStatus(st)),
  }, label);
  return el('div', { class: 'att ' + a.status },
    el('div', { class: 'att-head' },
      avatar({ id: a.swimmer_id, name: a.name, avatar: a.avatar }, 40),
      el('a', { href: '#', class: 'att-name', onclick: (ev) => { ev.preventDefault(); popup(() => swimmerPopup(a.swimmer_id), { keep: true }); } }, a.member || a.name),
      a.member ? el('span', { class: 'small members-tag' }, '👥 ' + a.name) : null,
      badge(a.status, a.pending && a.status === 'cancelled' ? { reason: 'pending_approval' } : null),
      el('span', { class: 'spacer' }),
      a.phone ? el('a', { href: 'tel:' + a.phone, class: 'small nowrap' }, '📞 ' + a.phone) : null),
    el('div', { class: 'att-info' }, creditsChip(a.credits)),
    a.notes ? el('div', { class: 'att-note small' }, '📌 ' + a.notes) : null,
    a.last_comment ? el('div', { class: 'att-note small muted', title: fmtTs(a.last_comment.created_at) }, '💬 ' + a.last_comment.text) : null,
    a.session_comments.map((c) => el('div', { class: 'att-note small session-comment', title: t('comments.private') }, '🔒 ' + c.text, el('span', { class: 'muted' }, ' — ' + c.author_name))),
    coachAckMarks(a, () => act(() => api('POST', '/bookings/' + a.booking_id + '/resolve'), t('ack.resolved'))),
    a.swimmer_note ? el('div', { class: 'visit-note swimmer small' }, '✉️ ', a.swimmer_note, el('span', { class: 'muted' }, ' — ' + t('notes.from_swimmer'))) : null,
    visitNoteInput(a.booking_id, a.note, t('notes.coach_ph')),
    el('div', { class: 'att-actions' },
      s.started
        ? el('div', { class: 'segs' }, stBtn('attended', '✓ ' + t('status.attended')), stBtn('no_show', '✗ ' + t('status.no_show')), stBtn('excused', t('status.excused')))
        : null,
      el('span', { class: 'spacer' }),
      el('button', { type: 'button', class: 'btn small ghost icon', title: t('comments.private'),
        onclick: () => { commentBox.classList.toggle('hidden'); commentBox.elements.text.focus(); } }, '🔒💬'),
      btn(t('session.remove'), async () => {
        if (await confirmDialog(t('session.confirm_remove', { name: a.name }), t('session.remove'), true)) setStatus('cancelled');
      }, 'small ghost danger')),
    commentBox);
}

async function coachSessionBody(s, swimmers) {
  const sw = swimmers || (await api('GET', '/swimmers')).swimmers;
  const listed = s.attendees.filter((a) => a.status !== 'cancelled' || a.pending);
  const freeMembers = (x) => (x.members || []).filter((m) => !listed.some((a) => a.swimmer_id === x.id && a.member === m));
  const avail = sw.filter((x) => x.coach_id === s.coach_id &&
    ((x.members || []).length ? freeMembers(x).length > 0 : !listed.some((a) => a.swimmer_id === x.id)));
  const force = el('input', { type: 'checkbox' });
  const add = !s.cancelled && avail.length ? el('div', { class: 'add-box' },
    combo({
      items: avail,
      placeholder: '+ ' + t('calendar.add_swimmer'),
      text: (x) => x.name + ' ' + x.username + ' ' + (x.phone || ''),
      row: (x) => el('span', { class: 'opt-row' }, avatar(x, 26),
        el('span', { class: 'grow ellipsis' }, el('strong', {}, x.name),
          (x.members || []).length ? el('span', { class: 'small members-tag' }, ' 👥 ' + x.members.join(', ')) : el('span', { class: 'muted small' }, ' ' + x.username)),
        el('span', { class: 'badge ' + (x.balance.available > 0 ? (x.balance.low ? 'warn' : 'ok') : 'danger') }, t('credits.left_short', { n: x.balance.available }))),
      // picking a name books immediately; "Remove" on the card undoes it
      onChoose: async (x) => {
        const body = { session_key: s.key, swimmer_id: x.id, force: force.checked };
        if ((x.members || []).length) {
          body.members = await pickMembers(x, freeMembers(x));
          if (!body.members || !body.members.length) return;
        }
        act(() => api('POST', '/bookings', body), t('calendar.added', { name: x.name }));
      },
    }),
    el('label', { class: 'check small', title: t('calendar.force_hint') }, force, t('calendar.force'))) : null;

  // PUT replaces the whole override, so always send what should stay
  const saveOverride = (extra) => {
    const body = { note: s.note, capacity: s.capacity, ...extra };
    if (!('cancelled' in extra)) {
      if (s.cancel_reason === 'coach') body.cancelled = true;
      else if (s.holiday && !s.cancelled) body.cancelled = false;
    }
    return act(() => api('PUT', '/overrides/' + encodeURIComponent(s.key), body), t('common.saved'));
  };
  const capacity = el('input', {
    type: 'number', min: 1, max: S.settings.max_capacity || 8, value: s.capacity, class: 'cap-input',
    onchange: (ev) => { if (Number(ev.target.value) !== s.capacity) saveOverride({ capacity: Number(ev.target.value) }); },
  });
  const note = el('input', {
    value: s.note, maxlength: 300, placeholder: t('session.note_ph'),
    onchange: (ev) => { if (ev.target.value !== s.note) saveOverride({ note: ev.target.value }); },
    onkeydown: (ev) => { if (ev.key === 'Enter') ev.target.blur(); },
  });

  return [
    s.called_off && s.called_off.length ? el('p', { class: 'notice' },
      t('session.called_off_coach', { names: s.called_off.join(', ') })) : null,
    h2(t('session.swimmers') + ' (' + listed.length + ')'),
    (s.waitlist || []).length ? el('p', { class: 'muted small' }, '⏳ ' + t('wait.coach_list', { names: s.waitlist.join(', ') })) : null,
    listed.length ? el('div', { class: 'atts' }, listed.map((a) => attendeeCard(s, a))) : el('p', { class: 'muted' }, t('calendar.nobody')),
    add,
    el('section', { class: 'manage' },
      h2(t('session.manage')),
      el('div', { class: 'manage-row' },
        field(t('fields.capacity'), capacity),
        field(t('session.note_label'), note)),
      el('div', { class: 'actions' },
        s.overridden ? btn(t('calendar.reset_session'), () => act(() => api('DELETE', '/overrides/' + encodeURIComponent(s.key)), t('common.saved')), 'ghost small') : null,
        // a training with people in it can only be cancelled (they get notified), never deleted
        !listed.length && !(s.called_off || []).length ? btn('🗑 ' + t('common.delete'), async () => {
          if (await confirmDialog(t('session.confirm_delete', { when: fmtShort(s.date) + ' ' + s.start }), t('common.delete'), true) &&
            await act(() => api('DELETE', '/sessions/' + encodeURIComponent(s.key)), t('common.deleted'))) backDialog();
        }, 'danger small') : null,
        s.started && !s.cancelled ? null : btn(s.cancelled ? t('calendar.restore_session') : t('calendar.cancel_session'), async () => {
          if (!s.cancelled && listed.length &&
            !(await confirmDialog(t('calendar.confirm_cancel_session', { n: listed.length }), t('calendar.cancel_session'), true))) return;
          saveOverride({ cancelled: !s.cancelled });
        }, (s.cancelled ? 'primary' : 'danger') + ' small'))),
  ];
}

const CONSUMES = ['attended', 'no_show'];
/** Mirror of the server rule: would this change count against the swimmer? */
const costsSwimmer = (from, to) => (CONSUMES.includes(to) && !CONSUMES.includes(from)) || (from === 'attended' && to === 'no_show');

/** Coach status change, aware of locked visits (those become a request to the swimmer). */
async function changeStatus(bookingId, from, to, name, isProtected) {
  if (!(await confirmStatus(to, name))) return false;
  if (isProtected && costsSwimmer(from, to) &&
    !(await confirmDialog(t('ack.request_confirm', { name: name || '' }), t('ack.send_request')))) return false;
  return act(() => api('PATCH', '/bookings/' + bookingId, { status: to }),
    (r) => (r && r.requested ? t('ack.request_sent', { name: name || '' }) : t('common.saved')));
}

/** A one-line text prompt on the small confirm dialog (used for disputes). */
function promptText(message, placeholder, okLabel) {
  const c = document.getElementById('confirm');
  return new Promise((resolve) => {
    let answer = null;
    c.onclose = () => resolve(answer);
    const box = el('textarea', { maxlength: 500, placeholder, required: true });
    c.replaceChildren(el('p', {}, message), box, el('div', { class: 'actions' },
      btn(t('common.cancel'), () => c.close()),
      btn(okLabel, () => { if (!box.value.trim()) { box.focus(); return; } answer = box.value.trim(); c.close(); }, 'danger solid')));
    c.showModal();
    box.focus();
  });
}

const ackAct = (b, action, body, msg) => act(() => api('POST', '/bookings/' + b.id + '/ack', { action, ...body }), msg);

/** Swimmer-side controls and state of one past visit: confirm/dispute, coach requests, history. */
function visitAck(b) {
  if (!b.ended || (b.status === 'cancelled' && !b.pending)) return null;
  const ack = b.ack || {};
  const parts = [];
  if (b.pending) {
    parts.push(el('div', { class: 'ack-request' },
      el('span', { class: 'grow' }, '⏳ ', t('ack.coach_asks', { from: t('status.' + b.status), to: t('status.' + b.pending.status) })),
      btn(t('ack.approve'), () => ackAct(b, 'approve', {}, t('ack.approved')), 'small primary'),
      btn(t('ack.reject'), () => ackAct(b, 'reject', {}, t('ack.rejected')), 'small danger')));
  }
  if (ack.status === 'disputed') parts.push(el('div', { class: 'ack-state disputed' }, '⚠ ' + t('ack.disputed') + (ack.comment ? ': ' + ack.comment : '')));
  else if (ack.status === 'resolved') parts.push(el('div', { class: 'ack-state' }, '✔ ' + t('ack.resolved')));
  else if (ack.status === 'confirmed') parts.push(el('div', { class: 'ack-state ok' }, '✓ ' + t('ack.confirmed')));
  else if (b.protected) parts.push(el('div', { class: 'ack-state' }, '🔒 ' + t('ack.final')));
  if (b.can_ack || ack.status === 'changed') {
    parts.push(el('div', { class: 'ack-actions' },
      el('span', { class: 'muted small grow' }, ack.status === 'changed' ? t('ack.changed_reconfirm')
        : ack.status === 'disputed' ? t('ack.withdraw') : t('ack.until', { date: fmtShort(b.ack_until.slice(0, 10)) })),
      btn('✓ ' + t('ack.confirm'), () => ackAct(b, 'confirm', {}, t('ack.confirmed')), 'small'),
      b.can_ack && ack.status !== 'disputed' ? btn(t('ack.dispute'), async () => {
        const comment = await promptText(t('ack.dispute_q'), t('ack.dispute_ph'), t('ack.dispute'));
        if (comment) ackAct(b, 'dispute', { comment }, t('ack.disputed_sent'));
      }, 'small ghost danger') : null));
  }
  if (b.unseen && !b.pending) {
    parts.push(el('div', { class: 'ack-request' }, el('span', { class: 'grow' }, '🔔 ' + t('ack.changed_by_coach')),
      btn(t('ack.seen'), () => ackAct(b, 'seen', {}, null), 'small')));
  }
  const hist = (b.history || []);
  if (hist.length) parts.push(historyList(hist));
  return parts.length ? el('div', { class: 'row-notes ack-box' + (b.unseen || b.pending ? ' attention' : '') }, parts) : null;
}

function historyList(hist) {
  return el('details', { class: 'history' }, el('summary', {}, t('ack.history', { n: hist.length })),
    el('ul', {}, hist.map((h) => el('li', {}, el('span', { class: 'muted' }, fmtTs(h.at) + ' · ' + h.by_name + ': '),
      t('ack.h_' + h.kind), h.from || h.to ? ' ' + [h.from && t('status.' + h.from), h.to && t('status.' + h.to)].filter(Boolean).join(' → ') : '',
      h.comment ? ' — „' + h.comment + '“' : ''))));
}

/** Coach-side markers on a visit: locked, confirmed/disputed, waiting for approval. */
function coachAckMarks(b, onResolve) {
  const ack = b.ack || {};
  return [
    b.pending ? el('div', { class: 'ack-request small' }, '⏳ ' + t('ack.waiting', { to: t('status.' + b.pending.status) })) : null,
    ack.status === 'disputed' ? el('div', { class: 'ack-state disputed small' },
      el('span', { class: 'grow' }, '⚠ ' + t('ack.disputed') + (ack.comment ? ': ' + ack.comment : '')),
      btn(t('ack.resolve'), onResolve, 'small')) : null,
    ack.status === 'confirmed' ? el('span', { class: 'ack-chip ok', title: t('ack.confirmed_hint') }, '✓ ' + t('ack.confirmed')) : null,
    ack.status === 'resolved' ? el('span', { class: 'ack-chip' }, '✔ ' + t('ack.resolved')) : null,
    b.protected && !ack.status ? el('span', { class: 'ack-chip', title: t('ack.final_hint') }, '🔒 ' + t('ack.final')) : null,
  ];
}

/** A no-show still uses up a paid visit, so it has to be confirmed. */
async function confirmStatus(status, name) {
  if (status !== 'no_show') return true;
  return confirmDialog(t('session.confirm_no_show', { name: name || '' }), '✗ ' + t('status.no_show'), true);
}

function statusSelect(bookingId, status, started, name, isProtected) {
  const options = started ? ['attended', 'no_show', 'excused', 'cancelled'] : ['booked', 'cancelled'];
  if (!options.includes(status)) options.unshift(status);
  return el('select', {
    class: 'status-select ' + status, 'aria-label': t('fields.status'),
    onchange: async (ev) => {
      const to = ev.target.value;
      ev.target.value = status;           // the list redraws with the real state afterwards
      await changeStatus(bookingId, status, to, name, isProtected);
    },
  }, options.map((o) => el('option', { value: o, selected: o === status }, t('status.' + o))));
}

// ------------------------------------------------------------------ popups: availability & one-off sessions

function slotPreview(start, end, step) {
  const out = [];
  if (!start || !end || !Number(step)) return out;
  for (let m = mins(start); m + Number(step) <= mins(end); m += Number(step)) out.push(hhmm(m));
  return out;
}

async function availabilityPopup() {
  const out = await api('GET', '/availability');
  const byDay = [0, 1, 2, 3, 4, 5, 6].map((i) => out.availability.filter((w) => w.weekday === i));
  const week = el('div', { class: 'avail-week' }, byDay.map((list, i) => el('div', { class: 'avail-day' + (i >= 5 ? ' weekend' : '') },
    el('div', { class: 'avail-dayname' }, weekdayName(i, 'short')),
    list.length ? list.map((w) => el('button', {
      type: 'button', class: 'avail-chip', onclick: () => popup(() => availabilityForm(w, out.defaults), { keep: true }),
      title: [w.title, w.location].filter(Boolean).join(' · '),
    }, el('strong', {}, w.start + '–' + w.end),
    el('span', { class: 'small' }, t('availability.chip', { n: w.slots, len: w.slot_minutes, cap: w.capacity })),
    w.location ? el('span', { class: 'small dim' }, w.location) : null)) : el('span', { class: 'muted small' }, '—'))));
  return [
    el('h3', {}, t('availability.title')),
    el('p', { class: 'muted small' }, t('availability.hint', { len: out.defaults.slot_minutes, cap: out.defaults.capacity })),
    week,
    el('div', { class: 'actions' }, btn('+ ' + t('availability.add'), () => popup(() => availabilityForm(null, out.defaults), { keep: true }), 'primary')),
  ];
}

function availabilityForm(w, defaults) {
  const editing = !!w;
  w = w || { weekday: null, start: '08:00', end: '12:00', slot_minutes: defaults.slot_minutes, capacity: defaults.capacity, location: '', title: '', valid_from: todayISO(), valid_to: null };
  const preview = el('div', { class: 'preview small' });
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      const body = { start: v.start, end: v.end, slot_minutes: Number(v.slot_minutes), capacity: Number(v.capacity), location: v.location, title: v.title, valid_to: v.valid_to || null };
      if (editing) body.weekday = Number(v.weekday);
      else {
        if (!v.weekdays || !v.weekdays.length) { toast(t('availability.pick_days'), 'error'); return; }
        body.weekdays = v.weekdays.map(Number);
        body.valid_from = v.valid_from || null;
      }
      const res = await act(() => (editing ? api('PATCH', '/availability/' + w.id, body) : api('POST', '/availability', body)),
        (r) => (r && (r.moved_bookings || r.cancelled_bookings)
          ? t('availability.changed_bookings', { moved: r.moved_bookings, cancelled: r.cancelled_bookings }) : t('common.saved')));
      if (res) backDialog();
    },
  },
  editing
    ? field(t('fields.weekday'), el('select', { name: 'weekday' }, [0, 1, 2, 3, 4, 5, 6].map((i) => el('option', { value: i, selected: i === w.weekday }, weekdayName(i)))))
    : el('div', { class: 'field' }, el('span', {}, t('availability.days')), el('div', { class: 'weekday-picks' },
      [0, 1, 2, 3, 4, 5, 6].map((i) => el('label', { class: 'wd' + (i >= 5 ? ' weekend' : '') },
        el('input', { type: 'checkbox', name: 'weekdays', value: i, 'data-multi': '1' }), el('span', {}, weekdayName(i, 'short')))))),
  el('div', { class: 'form cols' },
    field(t('availability.from'), timePicker('start', w.start)),
    field(t('availability.to'), timePicker('end', w.end)),
    field(t('availability.slot_len'), el('input', { name: 'slot_minutes', type: 'number', min: 15, max: 240, step: 5, value: w.slot_minutes, required: true })),
    field(t('availability.capacity'), el('input', { name: 'capacity', type: 'number', min: 1, max: defaults.max_capacity, value: w.capacity, required: true }))),
  preview,
  el('div', { class: 'form cols' },
    field(t('fields.title'), el('input', { name: 'title', value: w.title || '', maxlength: 100, placeholder: t('availability.title_ph') })),
    field(t('fields.location'), el('input', { name: 'location', value: w.location || '', maxlength: 100 }))),
  el('div', { class: 'form cols' },
    editing ? null : field(t('fields.valid_from'), datePicker('valid_from', w.valid_from || todayISO())),
    field(t('fields.valid_to'), datePicker('valid_to', w.valid_to || '', { optional: true, placeholder: t('fields.no_end') }))),
  editing ? el('p', { class: 'muted small' }, t('availability.edit_note')) : null,
  el('div', { class: 'actions' },
    editing ? btn(t('common.delete'), async () => {
      if (await confirmDialog(t('availability.confirm_delete', { day: weekdayName(w.weekday), time: w.start + '–' + w.end }), t('common.delete'), true) &&
        await act(() => api('DELETE', '/availability/' + w.id), (r) => t('availability.deleted', { n: r.cancelled_bookings }))) backDialog();
    }, 'danger') : null,
    el('span', { class: 'spacer' }),
    btn(t('common.cancel'), backDialog),
    el('button', { class: 'btn primary', type: 'submit' }, t('common.save'))));
  const updatePreview = () => {
    const v = formData(form);
    const slots = slotPreview(v.start, v.end, v.slot_minutes);
    preview.replaceChildren(slots.length
      ? el('span', {}, t('availability.preview', { n: slots.length }) + ' ', slots.map((x) => el('span', { class: 'chip' }, x)))
      : el('span', { class: 'warn-text' }, t('errors.window_too_short')));
  };
  form.addEventListener('input', updatePreview);
  updatePreview();
  return [el('h3', {}, editing ? t('availability.edit') : t('availability.add')), form];
}

function createSlotPopup(ds, start) {
  const len = S.settings.default_slot_minutes || 45;
  const cap = S.settings.default_capacity || S.settings.max_capacity || 8;
  const preview = el('div', { class: 'preview small' });
  const repeatBox = el('div', { class: 'repeat-box hidden' },
    el('div', { class: 'field' }, el('span', {}, t('availability.days')), el('div', { class: 'weekday-picks' },
      [0, 1, 2, 3, 4, 5, 6].map((i) => el('label', { class: 'wd' + (i >= 5 ? ' weekend' : '') },
        el('input', { type: 'checkbox', name: 'weekdays', value: i, 'data-multi': '1', checked: i === (parseISO(ds).getDay() + 6) % 7 }),
        el('span', {}, weekdayName(i, 'short')))))),
    field(t('slot.until'), datePicker('valid_to', '', { optional: true, placeholder: t('fields.no_end') })));
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      const common = { start: v.start, end: v.end, capacity: Number(v.capacity), location: v.location, title: v.title };
      let call;
      if (v.repeat) {
        if (!v.weekdays.length) { toast(t('availability.pick_days'), 'error'); return; }
        call = () => api('POST', '/availability', { ...common, slot_minutes: Number(v.slot_minutes), weekdays: v.weekdays.map(Number), valid_from: v.date, valid_to: v.valid_to || null });
      } else {
        call = () => api('POST', '/extra-sessions', { ...common, date: v.date, duration_min: Number(v.slot_minutes) });
      }
      if (await act(call, t('common.saved'))) backDialog();
    },
  },
  el('div', { class: 'form cols' },
    field(t('fields.date'), datePicker('date', ds, {
      onChange: (v) => {   // keep the repeat weekday in step with the date until the coach picks days
        if (!repeatBox.classList.contains('hidden')) return;
        const wd = (parseISO(v).getDay() + 6) % 7;
        repeatBox.querySelectorAll('input[name=weekdays]').forEach((c) => { c.checked = Number(c.value) === wd; });
      },
    })),
    field(t('availability.from'), timePicker('start', start)),
    field(t('availability.to'), timePicker('end', hhmm(Math.min(mins(start) + len, 23 * 60 + 55))))),
  el('div', { class: 'form cols' },
    field(t('availability.slot_len'), el('input', { name: 'slot_minutes', type: 'number', min: 15, max: 240, step: 5, value: len, required: true })),
    field(t('availability.capacity'), el('input', { name: 'capacity', type: 'number', min: 1, max: S.settings.max_capacity || 8, value: cap, required: true }))),
  preview,
  el('label', { class: 'check repeat-check' }, el('input', {
    type: 'checkbox', name: 'repeat', onchange: (ev) => repeatBox.classList.toggle('hidden', !ev.target.checked),
  }), el('strong', {}, '🔁 ' + t('slot.repeat'))),
  repeatBox,
  el('div', { class: 'form cols' },
    field(t('fields.title'), el('input', { name: 'title', maxlength: 100, placeholder: t('availability.title_ph') })),
    field(t('fields.location'), el('input', { name: 'location', maxlength: 100 }))),
  el('div', { class: 'actions' },
    btn(t('common.cancel'), backDialog),
    el('button', { class: 'btn primary', type: 'submit' }, t('common.create'))));
  const updatePreview = () => {
    const v = formData(form);
    const slots = slotPreview(v.start, v.end, v.slot_minutes);
    preview.replaceChildren(slots.length
      ? el('span', {}, t('availability.preview', { n: slots.length }) + ' ', slots.map((x) => el('span', { class: 'chip' }, x)))
      : el('span', { class: 'warn-text' }, t('errors.window_too_short')));
  };
  form.addEventListener('input', updatePreview);
  form.elements.start.addEventListener('change', () => {
    // keep the range one slot long while the coach is only moving the start
    const v = formData(form);
    if (v.start && mins(v.end || '00:00') <= mins(v.start)) form.elements.end._set(hhmm(Math.min(mins(v.start) + Number(v.slot_minutes || len), 23 * 60 + 55)));
    updatePreview();
  });
  updatePreview();
  return [el('h3', {}, t('slot.create_title')), form];
}

// ------------------------------------------------------------------ swimmer: my visits & account

async function myVisitsPopup() {
  const out = await api('GET', '/users/' + S.me.id);
  S.balance = out.balance;
  renderShell();
  const upcoming = out.bookings.filter((b) => b.status === 'booked').reverse();
  // visits needing an answer first, then the rest of the history
  const past = out.bookings.filter((b) => b.status !== 'booked' || b.pending)
    .sort((a, b) => Number(Boolean(b.pending || b.unseen || b.can_ack)) - Number(Boolean(a.pending || a.unseen || a.can_ack)));
  return [
    el('h3', {}, t('nav.visits')),
    creditsBar(out.balance),
    coachContact(),
    h2(t('visits.upcoming')),
    upcoming.length ? el('ul', { class: 'list' }, upcoming.map((b) => bookingRow(b, true))) : el('p', { class: 'muted' }, t('visits.none_upcoming')),
    h2(t('visits.history')),
    past.length ? el('ul', { class: 'list' }, past.slice(0, 50).map((b) => bookingRow(b, false))) : el('p', { class: 'muted' }, t('visits.none_history')),
    h2(t('visits.packages')),
    packagesList(out.balance.packages, false),
  ];
}

function coachContact() {
  const c = S.coach;
  if (!c) return null;
  return el('div', { class: 'coach-contact' }, avatar(c, 40),
    el('div', { class: 'grow' }, el('div', { class: 'muted small' }, t('coach.yours')), el('strong', {}, c.name)),
    c.phone ? el('a', { class: 'btn small', href: 'tel:' + c.phone }, '📞 ' + c.phone) : null,
    c.email ? el('a', { class: 'btn small ghost', href: 'mailto:' + c.email, title: c.email }, '✉️') : null);
}

function bookingRow(b, withCancel) {
  return el('li', {},
    el('span', { class: 'nowrap' }, el('strong', {}, fmtShort(b.date)), ' ', b.start + '–' + b.end),
    b.member ? el('span', { class: 'small members-tag' }, '👤 ' + b.member) : null,
    el('span', { class: 'muted small grow' }, [b.title, b.location].filter(Boolean).join(' · ')),
    badge(b.status, b),
    b.session_note && b.status === 'booked' ? el('div', { class: 'row-notes visit-note coach' }, '📢 ' + b.session_note) : null,
    visitNotes(b, { who: true }).some(Boolean) ? el('div', { class: 'row-notes' }, visitNotes(b, { who: true })) : null,
    withCancel ? null : visitAck(b),
    withCancel && b.can_cancel ? btn(t('book.cancel'), async () => {
      if (await confirmDialog(t('book.confirm_cancel', { when: fmtShort(b.date) + ' ' + b.start }), t('book.cancel'), true)) {
        act(() => api('DELETE', '/bookings/' + b.id), t('book.cancelled'));
      }
    }, 'small danger') : null);
}

function packagesList(packages, coachView) {
  if (!packages.length) return el('p', { class: 'muted' }, t('credits.no_packages'));
  return el('ul', { class: 'list' }, packages.map((p) => el('li', {},
    el('strong', {}, t('credits.package_visits', { n: p.visits })),
    el('span', { class: 'muted small' }, fmtDate(p.created_at.slice(0, 10))),
    p.valid_until ? el('span', { class: 'badge ' + (p.expired ? 'danger' : '') }, (p.expired ? t('credits.expired') : t('credits.valid_until')) + ' ' + fmtDate(p.valid_until)) : null,
    p.note ? el('span', { class: 'muted small' }, p.note) : null,
    el('span', { class: 'spacer' }),
    el('span', { class: 'small' }, t('credits.remaining', { n: p.remaining })),
    coachView ? btn('✕', async () => {
      if (await confirmDialog(t('credits.confirm_delete', { n: p.visits }), t('common.delete'), true)) {
        act(() => api('DELETE', '/packages/' + p.id), t('common.saved'));
      }
    }, 'small ghost danger') : null)));
}

async function accountPopup() {
  const me = (await api('GET', '/me')).user;
  const form = el('form', {
    class: 'form', onsubmit: (ev) => {
      ev.preventDefault();
      const v = formData(form);
      act(async () => { const out = await api('PATCH', '/me', v); S.me = out.user; }, t('common.saved'));
    },
  },
  isCoach() ? field(t('fields.name'), el('input', { name: 'name', value: me.name, required: true })) : null,
  el('div', { class: 'form cols' },
    field(t('fields.phone'), el('input', { name: 'phone', type: 'tel', value: me.phone || '' })),
    field(t('fields.email'), el('input', { name: 'email', type: 'email', value: me.email || '' }))),
  el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit' }, t('common.save'))));
  return [
    el('h3', {}, me.name, el('span', { class: 'muted' }, ' · ' + me.username)),
    avatarEditor(me),
    isCoach() ? null : coachContact(),
    form,
    el('details', { class: 'more' }, el('summary', {}, t('account.change_password')), passwordForm()),
    el('div', { class: 'actions' }, btn(t('nav.logout'), logout, 'danger')),
  ];
}

// ------------------------------------------------------------------ coach: settings

let settingsTab = 'general';

async function settingsPopup() {
  const items = [['general', t('settings.general')], ['cards', t('settings.cards')]];
  if (S.me.is_admin) items.push(['coaches', t('nav.coaches')]);
  let body;
  if (settingsTab === 'general') body = await settingsGeneral();
  else if (settingsTab === 'coaches' && S.me.is_admin) body = await settingsCoaches();
  else body = await settingsCards();
  return [el('h3', {}, t('settings.title')), tabs(items, settingsTab, (k) => { settingsTab = k; renderDialog(); }), body];
}

async function settingsCards() {
  const tpls = await loadTemplates();
  const form = el('form', {
    class: 'form cols', onsubmit: (ev) => {
      ev.preventDefault();
      const v = formData(form);
      act(() => api('POST', '/package-templates', { name: v.name, visits: Number(v.visits), valid_days: v.valid_days ? Number(v.valid_days) : null }), t('common.saved'));
    },
  },
  field(t('fields.name'), el('input', { name: 'name', required: true, placeholder: t('schedule.template_ph') })),
  field(t('fields.visits'), el('input', { name: 'visits', type: 'number', min: 1, max: 500, value: 10, required: true })),
  field(t('fields.valid_days'), el('input', { name: 'valid_days', type: 'number', min: 1, placeholder: t('fields.unlimited') })),
  el('button', { class: 'btn primary', type: 'submit' }, t('common.add')));
  return [
    el('p', { class: 'muted small' }, t('schedule.templates_hint')),
    tpls.length ? el('ul', { class: 'list' }, tpls.map((tp) => el('li', {},
      el('strong', {}, tp.name),
      el('span', { class: 'muted small grow' }, t('credits.package_visits', { n: tp.visits }) + (tp.valid_days ? ' · ' + t('schedule.valid_days', { n: tp.valid_days }) : '')),
      btn('✕', () => act(() => api('DELETE', '/package-templates/' + tp.id), t('common.deleted')), 'small ghost danger'))))
      : el('p', { class: 'muted' }, t('credits.no_templates')),
    form,
  ];
}

async function settingsGeneral() {
  const out = await api('GET', '/me/settings');
  const v = out.settings;
  const save = (key, value) => act(async () => {
    const r = await api('PUT', '/me/settings', { [key]: value });
    Object.assign(S.settings, r.settings);
  }, t('common.saved'));
  const num = (key, unit) => {
    const [lo, hi] = out.limits[key];
    return el('div', { class: 'rule' },
      el('div', { class: 'grow' }, el('strong', {}, t('rules.' + key)), el('div', { class: 'muted small' }, t('rules.' + key + '_hint'))),
      el('label', { class: 'rule-input' }, el('input', {
        type: 'number', min: lo, max: hi === null ? out.max_capacity : hi, value: v[key],
        onchange: (ev) => { if (ev.target.checkValidity()) save(key, Number(ev.target.value)); else ev.target.reportValidity(); },
      }), el('span', { class: 'muted small' }, unit)));
  };
  const flag = (key) => el('label', { class: 'rule check' },
    el('div', { class: 'grow' }, el('strong', {}, t('rules.' + key)), el('div', { class: 'muted small' }, t('rules.' + key + '_hint'))),
    el('input', { type: 'checkbox', class: 'switch', checked: v[key], onchange: (ev) => save(key, ev.target.checked) }));
  return [
    el('p', { class: 'muted small' }, t('rules.intro')),
    el('div', { class: 'rules' },
      h2(t('rules.swimmers')),
      flag('swimmers_can_book'),
      num('cancel_cutoff_hours', t('rules.u_hours')),
      num('booking_horizon_days', t('rules.u_days')),
      num('low_credit_threshold', t('rules.u_visits')),
      h2(t('rules.slots')),
      num('default_slot_minutes', t('common.min')),
      num('default_capacity', t('rules.u_places')),
      flag('skip_holidays')),
  ];
}

async function settingsCoaches() {
  const out = await api('GET', '/coaches');
  const form = el('form', {
    class: 'form cols', onsubmit: async (ev) => {
      ev.preventDefault();
      try {
        const r = await api('POST', '/coaches', formData(form));
        popup(() => credentialsView(r.user, r.password), { keep: true });
      } catch (e) { toast(errText(e), 'error'); }
    },
  },
  field(t('fields.name'), el('input', { name: 'name', required: true })),
  field(t('fields.username'), el('input', { name: 'username', required: true, autocapitalize: 'none' })),
  field(t('fields.phone'), el('input', { name: 'phone', type: 'tel' })),
  field(t('fields.email'), el('input', { name: 'email', type: 'email' })),
  el('label', { class: 'check' }, el('input', { type: 'checkbox', name: 'is_admin' }), t('coaches.admin')),
  el('button', { class: 'btn primary', type: 'submit' }, t('common.create')));
  return [
    el('ul', { class: 'list' }, out.coaches.map((c) => el('li', {},
      avatar(c, 30), el('strong', {}, c.name), el('span', { class: 'muted small grow' }, c.username),
      c.is_admin ? el('span', { class: 'badge' }, t('coaches.admin')) : null,
      c.active ? null : el('span', { class: 'badge danger' }, t('swimmers.inactive')),
      c.id !== S.me.id ? btn(t('swimmers.reset_password'), () => resetPassword(c), 'small ghost') : null,
      c.id !== S.me.id ? btn(c.active ? t('swimmers.deactivate') : t('swimmers.activate'),
        () => act(() => api('PATCH', '/users/' + c.id, { active: !c.active }), t('common.saved')), 'small ghost') : null))),
    h2(t('coaches.new')),
    form,
  ];
}

// ------------------------------------------------------------------ coach: swimmers page & popup

async function loadTemplates() {
  S.templates = (await api('GET', '/package-templates')).templates;
  return S.templates;
}

async function viewSwimmers() {
  const showInactive = localGet('showInactive') === '1';
  const [out] = await Promise.all([api('GET', '/swimmers' + (showInactive ? '?include_inactive=1' : '')), loadTemplates()]);
  const search = el('input', { type: 'search', placeholder: t('swimmers.search'), value: localGet('swSearch') || '', class: 'search' });
  const tbody = el('tbody');
  let filter = localGet('swFilter') || 'all';
  let sort = localGet('swSort') || 'name';
  const FILTERS = {
    all: () => true,
    low: (s) => s.balance.low && s.balance.total > 0,
    unpaid: (s) => s.balance.unpaid > 0,
    nocard: (s) => s.balance.total === 0,
  };
  const SORTS = {
    name: (a, b) => a.name.localeCompare(b.name, locale()),
    left: (a, b) => a.balance.available - b.balance.available || a.name.localeCompare(b.name, locale()),
    last: (a, b) => (b.last_visit || '').localeCompare(a.last_visit || ''),
  };
  const counts = Object.fromEntries(Object.entries(FILTERS).map(([k, f]) => [k, out.swimmers.filter(f).length]));
  const filterBar = el('div', { class: 'tabs sw-filters' });
  const drawFilters = () => filterBar.replaceChildren(...Object.keys(FILTERS).map((k) => el('button', {
    type: 'button', class: 'btn small' + (filter === k ? ' active' : '') + (k !== 'all' && counts[k] ? ' has-count' : ''),
    onclick: () => { filter = k; localSet('swFilter', k); drawFilters(); fill(); },
  }, t('swimmers.f_' + k), k !== 'all' && counts[k] ? el('span', { class: 'count' }, counts[k]) : null)));
  const sortSel = el('select', { class: 'auto', 'aria-label': t('swimmers.sort'), onchange: (ev) => { sort = ev.target.value; localSet('swSort', sort); fill(); } },
    Object.keys(SORTS).map((k) => el('option', { value: k, selected: k === sort }, t('swimmers.s_' + k))));
  const fill = () => {
    const q = search.value.trim().toLowerCase();
    localSet('swSearch', search.value);
    const rows = out.swimmers.filter((s) => (!q || (s.name + ' ' + s.username + ' ' + (s.phone || '') + ' ' + (s.members || []).join(' ')).toLowerCase().includes(q))
      && (FILTERS[filter] || FILTERS.all)(s)).sort(SORTS[sort] || SORTS.name);
    tbody.replaceChildren(...rows.map((s) => el('tr', { class: 'clickable' + (s.active ? '' : ' inactive'), onclick: () => popup(() => swimmerPopup(s.id)) },
      el('td', {}, el('div', { class: 'who' }, avatar(s, 36), el('div', {}, el('strong', {}, s.name),
        (s.members || []).length ? el('span', { class: 'badge joint', title: s.members.join(', ') }, '👥 ' + s.members.length) : null,
        el('div', { class: 'muted small' }, s.username, s.phone ? [' · ', el('a', { href: 'tel:' + s.phone, onclick: (ev) => ev.stopPropagation() }, s.phone)] : null)))),
      el('td', { class: 'num show-sm' }, creditsChip({ ...s.balance })),
      el('td', { class: 'num hide-sm' }, s.balance.used),
      el('td', { class: 'num hide-sm' }, s.balance.reserved),
      el('td', { class: 'num hide-sm' }, el('span', { class: 'badge ' + (s.balance.unpaid ? 'danger' : s.balance.low ? 'warn' : 'ok'), title: t('credits.left_short', { n: s.balance.available }) },
        s.balance.total), el('div', { class: 'tiny ' + (s.balance.low ? 'warn-text' : 'muted') }, s.balance.unpaid ? t('credits.unpaid', { n: s.balance.unpaid }) : t('credits.left_short', { n: s.balance.available }))),
      el('td', { class: 'small hide-sm' }, s.last_visit ? fmtShort(s.last_visit) : '—'),
      el('td', { class: 'small hide-sm' }, s.next_booking ? fmtShort(s.next_booking.date) + ' ' + s.next_booking.start : '—'),
      el('td', { class: 'num', onclick: (ev) => ev.stopPropagation() }, quickPackageButton(s)))));
    if (!rows.length) tbody.append(el('tr', {}, el('td', { colspan: 8, class: 'empty' }, t('swimmers.none'))));
  };
  search.addEventListener('input', fill);
  drawFilters();
  fill();
  render(
    el('div', { class: 'row between' }, el('h1', {}, t('swimmers.title', { n: out.swimmers.filter((s) => s.active).length })),
      el('div', { class: 'row' }, btn('📣 ' + t('msg.button'), () => popup(() => messagePopup('all')), 'small'),
        btn('+ ' + t('swimmers.new'), () => popup(newSwimmerPopup), 'primary'))),
    el('div', { class: 'row', style: 'margin-bottom:.4rem' }, search, sortSel,
      el('label', { class: 'check small' }, el('input', {
        type: 'checkbox', checked: showInactive, onchange: (ev) => { localSet('showInactive', ev.target.checked ? '1' : '0'); refresh(); },
      }), t('swimmers.show_inactive'))),
    filterBar,
    el('div', { class: 'card table-wrap' }, el('table', {},
      el('thead', {}, el('tr', {}, el('th', {}, t('fields.name')), el('th', { class: 'num show-sm' }, t('credits.chip_title')),
        el('th', { class: 'num hide-sm', title: t('swimmers.visited_hint') }, t('swimmers.visited')),
        el('th', { class: 'num hide-sm', title: t('swimmers.planned_hint') }, t('swimmers.planned')), el('th', { class: 'num hide-sm' }, t('swimmers.all')),
        el('th', { class: 'hide-sm' }, t('swimmers.last_visit')), el('th', { class: 'hide-sm' }, t('swimmers.next')), el('th', { class: 'num' }, t('credits.add_card')))),
      tbody)));
}

function cardHint(tp, swimmer) {
  return t('credits.add_hint', { card: cardLabel(tp), name: swimmer.name });
}

/** Adding paid visits must be deliberate: always confirm. */
async function addCard(swimmer, body, label) {
  const msg = t('credits.confirm_add', { card: label, name: swimmer.name });
  if (!(await confirmDialog(msg, t('credits.add_package')))) return null;
  return act(() => api('POST', '/packages', { swimmer_id: swimmer.id, ...body }), t('credits.added', { n: body.visits || label, name: swimmer.name }));
}
function cardLabel(tp) {
  // "10 посещения" already says it all; otherwise add the visit count, plus validity if any
  let label = '„' + tp.name + '“';
  if (!tp.name.includes(String(tp.visits))) label += ' (' + t('credits.package_visits', { n: tp.visits }) + ')';
  if (tp.valid_days) label += ', ' + t('schedule.valid_days', { n: tp.valid_days });
  return label;
}
const addTemplateCard = (swimmer, tp) => addCard(swimmer, { template_id: tp.id }, cardLabel(tp));

function quickPackageButton(s) {
  if (S.templates.length === 1) {
    const tp = S.templates[0];
    return el('button', { type: 'button', class: 'btn small card-btn', title: cardHint(tp, s), onclick: () => addTemplateCard(s, tp) }, '+ ' + tp.name);
  }
  return el('button', { type: 'button', class: 'btn small card-btn', title: t('credits.add_package') + ' · ' + s.name, onclick: () => popup(() => packagePopup(s)) },
    '+ ' + t('credits.add_card'));
}

function packagePopup(s) {
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      const body = { swimmer_id: s.id, visits: Number(v.visits), note: v.note };
      if (v.valid_days) body.valid_days = Number(v.valid_days);
      if (await addCard(s, body, '„' + t('credits.package_visits', { n: v.visits }) + '“' +
        (v.valid_days ? ', ' + t('schedule.valid_days', { n: v.valid_days }) : ''))) backDialog();
    },
  },
  S.templates.length ? el('div', { class: 'row' }, S.templates.map((tp) => el('button', {
    type: 'button', class: 'btn primary', title: cardHint(tp, s),
    onclick: async () => { if (await addTemplateCard(s, tp)) backDialog(); },
  }, '+ ' + tp.name))) : el('p', { class: 'muted small' }, t('credits.no_templates')),
  el('p', { class: 'muted small' }, t('credits.custom')),
  el('div', { class: 'form cols' },
    field(t('fields.visits'), el('input', { name: 'visits', type: 'number', min: 1, max: 500, required: true, value: 10 })),
    field(t('fields.valid_days'), el('input', { name: 'valid_days', type: 'number', min: 1, placeholder: t('fields.unlimited') }))),
  field(t('fields.note'), el('input', { name: 'note', maxlength: 300 })),
  el('div', { class: 'actions' }, btn(t('common.cancel'), backDialog), el('button', { class: 'btn primary', type: 'submit' }, t('common.add'))));
  return [el('h3', {}, t('credits.add_package') + ' · ' + s.name), form];
}

function credentialsView(user, password) {
  const text = t('swimmers.credentials_text', { url: location.origin, username: user.username, password });
  return [
    el('h3', {}, t('swimmers.credentials_title', { name: user.name })),
    el('p', { class: 'muted small' }, t('swimmers.credentials_hint')),
    el('div', { class: 'secret' }, text),
    el('div', { class: 'actions' },
      btn(t('common.copy'), async () => {
        try { await navigator.clipboard.writeText(text); toast(t('common.copied')); } catch (e) { toast(t('common.copy_failed'), 'error'); }
      }),
      btn(t('common.done'), closeDialog, 'primary')),
  ];
}

async function resetPassword(u) {
  if (!(await confirmDialog(t('swimmers.confirm_reset', { name: u.name }), t('swimmers.reset_password')))) return;
  try {
    const r = await api('POST', '/users/' + u.id + '/reset-password');
    popup(() => credentialsView(r.user, r.password), { keep: true });
  } catch (e) { toast(errText(e), 'error'); }
}

async function newSwimmerPopup() {
  const [coaches] = await Promise.all([S.me.is_admin ? api('GET', '/coaches').then((r) => r.coaches) : [], loadTemplates()]);
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      try {
        v.members = splitMembers(v.members);
        const out = await api('POST', '/swimmers', v);
        if (v.template_id) await api('POST', '/packages', { swimmer_id: out.user.id, template_id: v.template_id });
        if (v.comment && v.comment.trim()) await api('POST', '/users/' + out.user.id + '/comments', { text: v.comment });
        popup(() => credentialsView(out.user, out.password), { replace: true });
        refresh();
      } catch (e) { toast(errText(e), 'error'); }
    },
  },
  el('div', { class: 'form cols' },
    field(t('fields.name'), el('input', { name: 'name', required: true, maxlength: 100, autofocus: true })),
    field(t('fields.username'), el('input', { name: 'username', required: true, pattern: '[A-Za-z0-9._\\-]{3,32}', autocapitalize: 'none', title: t('swimmers.username_hint') })),
    field(t('fields.phone'), el('input', { name: 'phone', type: 'tel' })),
    field(t('fields.email'), el('input', { name: 'email', type: 'email' }))),
  coaches.length > 1 ? field(t('fields.coach'), el('select', { name: 'coach_id' },
    coaches.map((c) => el('option', { value: c.id, selected: c.id === S.me.id }, c.name)))) : null,
  S.templates.length ? field(t('credits.initial_package'), el('select', { name: 'template_id' },
    el('option', { value: '' }, '—'), S.templates.map((tp, i) => el('option', { value: tp.id, selected: i === 0 }, tp.name)))) : null,
  field(t('members.label'), el('input', { name: 'members', placeholder: t('members.placeholder') })),
  field(t('comments.first'), el('textarea', { name: 'comment', maxlength: 4000, placeholder: t('comments.placeholder') })),
  el('p', { class: 'muted small' }, t('swimmers.password_generated')),
  el('div', { class: 'actions' }, btn(t('common.cancel'), closeDialog), el('button', { class: 'btn primary', type: 'submit' }, t('common.create'))));
  return [el('h3', {}, t('swimmers.new')), form];
}

let swimmerTab = 'overview';

async function swimmerPopup(id) {
  const [out] = await Promise.all([api('GET', '/users/' + id), loadTemplates()]);
  const u = out.user;
  const items = [['overview', t('swimmer.overview')], ['visits', t('swimmer.visits')],
    ['comments', t('swimmer.comments') + (out.comments.length ? ' (' + out.comments.length + ')' : '')], ['details', t('swimmers.details')]];
  let body;
  if (swimmerTab === 'visits') body = swimmerVisits(out);
  else if (swimmerTab === 'comments') body = swimmerComments(out);
  else if (swimmerTab === 'details') body = await swimmerDetails(out);
  else body = swimmerOverview(out);
  return [
    el('div', { class: 'who big' }, avatar(u, 56), el('div', {},
      el('h3', {}, u.name, u.active ? null : el('span', { class: 'badge danger', style: 'margin-left:.5rem' }, t('swimmers.inactive'))),
      el('div', { class: 'muted small' }, [u.username, u.phone, u.email].filter(Boolean).join(' · ')))),
    tabs(items, swimmerTab, (k) => { swimmerTab = k; renderDialog(); }),
    body,
  ];
}

function swimmerOverview(out) {
  const u = out.user;
  const upcoming = out.bookings.filter((b) => b.status === 'booked').reverse().slice(0, 5);
  const last = out.comments[0];
  return [
    creditsBar(out.balance),
    el('div', { class: 'row', style: 'margin:.5rem 0' },
      S.templates.map((tp) => el('button', { type: 'button', class: 'btn small primary', title: cardHint(tp, u), onclick: () => addTemplateCard(u, tp) }, '+ ' + tp.name)),
      btn('+ ' + t('credits.custom_short'), () => popup(() => packagePopup(u), { keep: true }), 'small')),
    h2(t('visits.upcoming')),
    upcoming.length ? el('ul', { class: 'list' }, upcoming.map((b) => el('li', {},
      el('strong', { class: 'nowrap' }, fmtShort(b.date)), b.start, el('span', { class: 'grow' }), statusSelect(b.id, b.status, false))))
      : el('p', { class: 'muted' }, t('visits.none_upcoming')),
    last ? [h2(t('comments.latest')), commentItem(last, false)] : null,
  ];
}

function swimmerVisits(out) {
  const rows = out.bookings.filter((b) => b.status !== 'cancelled' || b.date >= todayISO());
  return [
    rows.length ? el('div', { class: 'table-wrap' }, el('table', {}, el('tbody', {}, rows.map((b) => el('tr', {},
      el('td', { class: 'nowrap' }, fmtShort(b.date)), el('td', {}, b.start + '–' + b.end, b.member ? el('div', { class: 'small members-tag' }, '👤 ' + b.member) : null,
        visitNotes(b, { who: true })),
      el('td', { class: 'small muted hide-sm' }, [b.title, b.location].filter(Boolean).join(' · ')),
      el('td', {}, byCoach(b) || b.pending ? badge(b.status, b) : statusSelect(b.id, b.status, bookingStarted(b), b.member || out.user.name, b.protected),
        coachAckMarks(b, () => act(() => api('POST', '/bookings/' + b.id + '/resolve'), t('ack.resolved'))),
        (b.history || []).length ? historyList(b.history) : null))))))
      : el('p', { class: 'muted' }, t('visits.none_history')),
    h2(t('visits.packages')),
    packagesList(out.balance.packages, true),
  ];
}

function commentItem(c, editable) {
  const text = el('div', { class: 'comment-text' }, c.text);
  const canEdit = editable && (c.author_id === S.me.id || S.me.is_admin);
  return el('div', { class: 'comment' },
    el('div', { class: 'comment-head' }, avatar({ id: c.author_id, name: c.author_name, avatar: c.author_avatar }, 24), el('strong', {}, c.author_name),
      el('span', { class: 'muted small' }, fmtTs(c.created_at) + (c.edited_at ? ' · ' + t('comments.edited') : '')),
      el('span', { class: 'spacer' }),
      canEdit ? btn('✎', () => {
        const box = el('textarea', { maxlength: 4000 }, c.text);
        text.replaceWith(el('div', { class: 'form' }, box, el('div', { class: 'actions' },
          btn(t('common.cancel'), () => renderDialog()),
          btn(t('common.save'), () => act(() => api('PATCH', '/comments/' + c.id, { text: box.value }), t('common.saved')), 'primary'))));
        box.focus();
      }, 'small ghost icon') : null,
      canEdit ? btn('✕', async () => {
        if (await confirmDialog(t('comments.confirm_delete'), t('common.delete'), true)) act(() => api('DELETE', '/comments/' + c.id), t('common.deleted'));
      }, 'small ghost icon danger') : null),
    text);
}

function swimmerComments(out) {
  const box = el('textarea', { name: 'text', maxlength: 4000, required: true, placeholder: t('comments.placeholder') });
  const form = el('form', {
    class: 'form', onsubmit: (ev) => {
      ev.preventDefault();
      act(() => api('POST', '/users/' + out.user.id + '/comments', { text: box.value }), t('common.saved'));
    },
  }, box, el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit' }, t('comments.add'))));
  return [
    el('p', { class: 'muted small' }, '🔒 ' + t('comments.private')),
    form,
    out.comments.length ? el('div', { class: 'comments' }, out.comments.map((c) => commentItem(c, true))) : el('p', { class: 'muted' }, t('comments.none')),
  ];
}

async function swimmerDetails(out) {
  const u = out.user;
  const coaches = S.me.is_admin ? (await api('GET', '/coaches')).coaches : [];
  const form = el('form', {
    class: 'form', onsubmit: (ev) => {
      ev.preventDefault();
      const v = formData(form);
      v.members = splitMembers(v.members);
      act(() => api('PATCH', '/users/' + u.id, v), t('common.saved'));
    },
  },
  el('div', { class: 'form cols' },
    field(t('fields.name'), el('input', { name: 'name', value: u.name, required: true })),
    field(t('fields.username'), el('input', { name: 'username', value: u.username, required: true, autocapitalize: 'none' })),
    field(t('fields.phone'), el('input', { name: 'phone', type: 'tel', value: u.phone || '' })),
    field(t('fields.email'), el('input', { name: 'email', type: 'email', value: u.email || '' })),
    coaches.length > 1 ? field(t('fields.coach'), el('select', { name: 'coach_id' },
      coaches.map((c) => el('option', { value: c.id, selected: c.id === u.coach_id }, c.name)))) : null),
  field(t('members.label'), el('input', { name: 'members', value: (u.members || []).join(', '), placeholder: t('members.placeholder') })),
  el('p', { class: 'muted small' }, t('members.hint', { n: S.settings.max_members || 3 })),
  field(t('fields.notes'), el('textarea', { name: 'notes' }, u.notes || '')),
  el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit' }, t('common.save'))));
  return [
    avatarEditor(u),
    form,
    el('div', { class: 'actions', style: 'margin-top:1rem' },
      btn(t('swimmers.reset_password'), () => resetPassword(u), 'ghost'),
      btn(u.active ? t('swimmers.deactivate') : t('swimmers.activate'), async () => {
        if (u.active && !(await confirmDialog(t('swimmers.confirm_deactivate', { name: u.name }), t('swimmers.deactivate'), true))) return;
        act(() => api('PATCH', '/users/' + u.id, { active: !u.active }), t('common.saved'));
      }, 'ghost' + (u.active ? ' danger' : ''))),
  ];
}

// ------------------------------------------------------------------ coach: statistics, reports, log

function statsRange(scope) {
  const today = parseISO(todayISO());
  const y = today.getFullYear(); const m = today.getMonth();
  switch (scope) {
    case 'last_month': return [new Date(y, m - 1, 1), new Date(y, m, 0)];
    case 'year': return [new Date(y, 0, 1), new Date(y, 11, 31)];
    case 'last30': return [addDays(today, -29), today];
    case 'custom': return [parseISO(localGet('statsFrom') || iso(new Date(y, m, 1))), parseISO(localGet('statsTo') || todayISO())];
    default: return [new Date(y, m, 1), new Date(y, m + 1, 0)];
  }
}

async function viewStats() {
  const tab = localGet('statsTab') || 'stats';
  const top = el('div', { class: 'row between' }, el('h1', {}, t('nav.stats')),
    tabs([['stats', t('stats.title')], ['insights', '💡 ' + t('insights.title')], ['reports', t('reports.title')], ['log', t('reports.audit')]], tab,
      (k) => { localSet('statsTab', k); refresh(); }));
  let body;
  if (tab === 'insights') body = await insightsBody();
  else if (tab === 'reports') body = await reportsBody();
  else if (tab === 'log') body = await auditBody();
  else body = await statsBody();
  render(top, body);
}

async function statsBody() {
  const scope = localGet('statsScope') || 'month';
  const [from, to] = statsRange(scope);
  const qs = '?from=' + iso(from) + '&to=' + iso(to);
  const st = await api('GET', '/stats' + qs);
  const custom = scope === 'custom' ? el('div', { class: 'row' },
    datePicker('from', iso(from), { onChange: (v) => { localSet('statsFrom', v); refresh(); } }), '–',
    datePicker('to', iso(to), { onChange: (v) => { localSet('statsTo', v); refresh(); } })) : null;
  const tile = (v, l) => el('div', { class: 'tile' }, el('div', { class: 'v' }, v), el('div', { class: 'l' }, l));
  const maxW = Math.max(1, ...st.per_weekday);
  const timeEntries = Object.entries(st.per_time);
  const maxT = Math.max(1, ...timeEntries.map(([, n]) => n));
  const bar = (label, n, max, cls) => el('div', { class: 'bar' + (cls ? ' ' + cls : '') },
    el('span', {}, label), el('div', { class: 'track' }, el('div', { class: 'fill', style: 'width:' + (n / max * 100) + '%' })), el('span', { class: 'num' }, n));
  return [
    el('div', { class: 'row', style: 'margin-bottom:.5rem' },
      tabs(['month', 'last_month', 'last30', 'year', 'custom'].map((sc) => [sc, t('stats.scope_' + sc)]), scope, (sc) => { localSet('statsScope', sc); refresh(); }),
      custom, el('span', { class: 'spacer' }),
      el('a', { class: 'btn small', href: '/api/v1/export.csv' + qs, download: '' }, '⬇ ' + t('stats.export'))),
    el('p', { class: 'muted small' }, fmtDate(iso(from)) + ' – ' + fmtDate(iso(to))),
    el('div', { class: 'tiles' },
      tile(st.totals.attended, t('status.attended')),
      tile(st.totals.no_show, t('status.no_show')),
      tile(st.totals.excused, t('status.excused')),
      tile(st.totals.cancelled, t('status.cancelled')),
      tile(st.attendance_rate === null ? '—' : Math.round(st.attendance_rate * 100) + '%', t('stats.attendance_rate')),
      tile(st.sessions_held, t('stats.sessions_held')),
      tile(st.avg_per_session ?? '—', t('stats.avg_per_session')),
      tile(st.occupancy === null || st.occupancy === undefined ? '—' : Math.round(st.occupancy * 100) + '%', t('stats.occupancy'))),
    el('div', { class: 'grid two' },
      el('div', { class: 'card' }, h2(t('stats.by_weekday')),
        el('div', { class: 'bars' }, st.per_weekday.map((n, i) => bar(weekdayName(i), n, maxW, i >= 5 ? 'weekend' : '')))),
      el('div', { class: 'card' }, h2(t('stats.by_time')),
        timeEntries.length ? el('div', { class: 'bars' }, timeEntries.map(([tm, n]) => bar(tm, n, maxT))) : el('p', { class: 'muted' }, t('stats.no_data')))),
    el('div', { class: 'card table-wrap' }, h2(t('stats.by_swimmer')),
      st.per_swimmer.length ? el('table', {},
        el('thead', {}, el('tr', {}, el('th', {}, t('fields.swimmer')),
          ['attended', 'no_show', 'excused', 'cancelled', 'booked'].map((k) => el('th', { class: 'num' }, t('status.' + k))))),
        el('tbody', {}, st.per_swimmer.map((r) => el('tr', { class: 'clickable', onclick: () => popup(() => swimmerPopup(r.swimmer_id)) },
          el('td', {}, r.name), ['attended', 'no_show', 'excused', 'cancelled', 'booked'].map((k) => el('td', { class: 'num' }, r[k]))))))
        : el('p', { class: 'muted' }, t('stats.no_data'))),
  ];
}

async function reportsBody() {
  const rep = await api('GET', '/reports');
  const ids = {};
  if (S.me.is_admin) for (const c of (await api('GET', '/coaches')).coaches) ids[c.username] = c.id;
  const q = (r) => (r.coach === S.me.username ? '' : '?coach_id=' + encodeURIComponent(ids[r.coach] || ''));
  const link = (r, kind, label) => el('a', { class: 'btn small', href: '/api/v1/reports/' + r.date + '/' + kind + q(r), target: '_blank', rel: 'noopener' }, label);
  return [
    el('div', { class: 'row between' }, el('p', { class: 'muted grow' }, t('reports.hint')),
      btn(t('reports.run_now'), () => act(() => api('POST', '/reports/run', {}), t('reports.generated')), 'primary')),
    el('div', { class: 'card' }, rep.reports.length ? el('ul', { class: 'list' }, rep.reports.slice(0, 60).map((r) => el('li', {},
      el('strong', { class: 'nowrap grow' }, fmtShort(r.date), S.me.is_admin ? el('span', { class: 'muted small' }, ' · ' + r.coach) : null),
      link(r, 'txt', t('reports.text')), link(r, 'visits', t('reports.visits_csv')), link(r, 'balances', t('reports.balances_csv')))))
      : el('p', { class: 'muted' }, t('reports.none'))),
  ];
}

async function auditBody() {
  const aud = await api('GET', '/audit?limit=200');
  return el('div', { class: 'card table-wrap' }, aud.entries.length ? el('table', {},
    el('tbody', {}, aud.entries.map((e) => el('tr', {},
      el('td', { class: 'small nowrap' }, fmtTs(e.ts)),
      el('td', { class: 'small' }, e.actor_name),
      el('td', { class: 'small' }, S.strings['audit.' + e.action] ? t('audit.' + e.action) : e.action),
      el('td', { class: 'small muted' }, e.target_name || (e.details && e.details.new ? t('status.' + e.details.new) : ''))))))
    : el('p', { class: 'muted' }, t('reports.no_audit')));
}

// ------------------------------------------------------------------ coach: Today (pool deck)

function sessionPhase(s) {
  const now = new Date();
  const start = new Date(s.date + 'T' + s.start);
  const end = new Date(s.date + 'T' + s.end);
  const minsTo = Math.round((start - now) / 60000);
  if (now >= end) return { key: 'done', text: t('today.done') };
  if (now >= start) return { key: 'live', text: t('today.live', { n: Math.max(1, Math.round((end - now) / 60000)) }) };
  return { key: 'next', text: minsTo < 60 ? t('today.in_min', { n: minsTo }) : t('today.at', { time: s.start }), checkin: minsTo <= (S.settings.checkin_early_minutes || 15) };
}

async function viewToday() {
  const ds = todayISO();
  const [data, sw] = await Promise.all([api('GET', '/sessions?from=' + ds + '&to=' + ds + '&detail=1'), api('GET', '/swimmers')]);
  const list = data.sessions.filter((s) => !s.cancelled);
  const all = list.flatMap((s) => s.attendees.filter((a) => a.status !== 'cancelled'));
  const here = all.filter((a) => a.status === 'attended' && a.checked).length;
  const noShow = all.filter((a) => a.status === 'no_show').length;
  const open = list.filter((s) => sessionPhase(s).key !== 'done');
  const done = list.filter((s) => sessionPhase(s).key === 'done');
  render(
    el('div', { class: 'today-head' },
      el('div', {}, el('h1', {}, '📍 ' + t('nav.today')), el('div', { class: 'muted' }, fmtDay(ds))),
      el('div', { class: 'row' }, btn('📣 ' + t('msg.to_today'), () => popup(() => messagePopup('day', ds)), 'small'))),
    el('div', { class: 'today-stats' },
      el('span', {}, el('strong', {}, list.length), ' ' + t('today.trainings')),
      el('span', {}, el('strong', {}, all.length), ' ' + t('today.booked')),
      el('span', { class: 'ok-text' }, el('strong', {}, here), ' ' + t('today.here')),
      noShow ? el('span', { class: 'warn-text' }, el('strong', {}, noShow), ' ' + t('status.no_show')) : null),
    data.holidays[ds] ? el('p', { class: 'notice info' }, '🎉 ' + data.holidays[ds][S.lang]) : null,
    list.length ? null : el('div', { class: 'card empty' }, t('today.none'), ' ',
      el('a', { href: '#/calendar' }, t('today.to_calendar'))),
    open.map((s, i) => todayCard(s, sw.swimmers, i === 0)),
    done.length ? el('details', { class: 'today-done', open: !open.length },
      el('summary', {}, t('today.finished', { n: done.length })), done.map((s) => todayCard(s, sw.swimmers, false))) : null);
}

function todayCard(s, swimmers, first) {
  const ph = sessionPhase(s);
  const listed = s.attendees.filter((a) => a.status !== 'cancelled' || a.pending);
  const canCheck = ph.key !== 'next' || ph.checkin;
  const late = ph.key === 'live' && (new Date() - new Date(s.date + 'T' + s.start)) > 5 * 60000;
  const rows = listed.map((a) => {
    const isHere = a.status === 'attended' && a.checked;
    const name = a.member || a.name;
    return el('div', { class: 'today-row' + (isHere ? ' here' : '') + (a.status === 'no_show' ? ' gone' : '') },
      avatar({ id: a.swimmer_id, name, avatar: a.avatar }, 40),
      el('div', { class: 'grow', onclick: () => popup(() => swimmerPopup(a.swimmer_id)) },
        el('strong', {}, name), a.member ? el('span', { class: 'muted small' }, ' · ' + a.name) : null,
        el('div', { class: 'small muted' },
          a.credits && a.credits.low ? el('span', { class: 'warn-text' }, '⚠ ' + t('credits.left_short', { n: a.credits.available }) + ' ') : null,
          a.swimmer_note ? '✉️ ' + a.swimmer_note : (a.last_comment ? '💬 ' + a.last_comment.text : ''))),
      late && !isHere && a.status !== 'no_show' && a.phone ? el('a', { class: 'btn small', href: 'tel:' + a.phone, title: a.phone }, '📞') : null,
      canCheck ? el('button', {
        type: 'button', class: 'checkin' + (isHere ? ' on' : ''),
        onclick: () => (isHere ? null : changeStatus(a.booking_id, a.status, 'attended', name, a.protected)),
      }, isHere ? '✓ ' + t('today.is_here') : t('today.check_in')) : el('span', { class: 'muted small' }, t('today.opens', { time: hhmm(Math.max(0, mins(s.start) - (S.settings.checkin_early_minutes || 15))) })),
      canCheck && !isHere && a.status !== 'no_show' ? btn('✗', () => changeStatus(a.booking_id, a.status, 'no_show', name, a.protected), 'small ghost danger icon') : null);
  });
  const unmarked = ph.key === 'done' ? listed.filter((a) => a.status === 'attended' && !a.checked) : [];
  const booked = new Set(listed.map((a) => a.swimmer_id + '|' + (a.member || '')));
  const avail = swimmers.filter((x) => x.coach_id === s.coach_id &&
    ((x.members || []).length ? x.members.some((m) => !booked.has(x.id + '|' + m)) : !booked.has(x.id + '|')));
  const walkIn = canCheck && avail.length ? combo({
    items: avail, placeholder: '+ ' + t('today.walk_in'),
    text: (x) => x.name + ' ' + x.username + ' ' + (x.members || []).join(' '),
    row: (x) => el('span', { class: 'opt-row' }, avatar(x, 26), el('span', { class: 'grow ellipsis' }, el('strong', {}, x.name)),
      el('span', { class: 'badge ' + (x.balance.available > 0 ? 'ok' : 'danger') }, t('credits.left_short', { n: x.balance.available }))),
    onChoose: async (x) => {
      const body = { session_key: s.key, swimmer_id: x.id, force: true };
      if ((x.members || []).length) {
        body.members = await pickMembers(x, x.members.filter((m) => !booked.has(x.id + '|' + m)));
        if (!body.members || !body.members.length) return;
      }
      act(async () => {
        const r = await api('POST', '/bookings', body);       // walk-in: book and check in at once
        for (const b of r.bookings) await api('PATCH', '/bookings/' + b.id, { status: 'attended' });
      }, t('today.walked_in', { name: x.name }));
    },
  }) : null;
  return el('section', { class: 'today-card ' + ph.key + (first ? ' first' : '') },
    el('div', { class: 'today-card-head' },
      el('strong', { class: 'agenda-time' }, s.start + '–' + s.end),
      el('span', { class: 'phase ' + ph.key }, ph.text),
      el('span', { class: 'muted small grow' }, [s.title, s.location].filter(Boolean).join(' · ')),
      el('span', { class: 'small nowrap' }, listed.length + '/' + s.capacity),
      btn('⋯', () => popup(() => sessionPopup(s.key, s.date)), 'small ghost icon')),
    s.note ? el('div', { class: 'visit-note coach small' }, '📢 ' + s.note) : null,
    unmarked.length ? el('div', { class: 'notice end-prompt' }, el('span', { class: 'grow' }, t('today.unmarked', { n: unmarked.length })),
      btn(t('today.all_here'), () => act(async () => {
        for (const a of unmarked) await api('PATCH', '/bookings/' + a.booking_id, { status: 'attended' });
      }, t('common.saved')), 'small primary')) : null,
    rows.length ? el('div', { class: 'today-rows' }, rows) : el('p', { class: 'muted' }, t('calendar.nobody')),
    walkIn ? el('div', { class: 'add-box' }, walkIn) : null);
}

// ------------------------------------------------------------------ coach: absence, messages, insights

async function absencePopup() {
  const out = await api('GET', '/absences');
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      if (v.to < v.from) { toast(t('errors.invalid_range'), 'error'); return; }
      if (!(await confirmDialog(t('absence.confirm', { from: fmtDate(v.from), to: fmtDate(v.to) }), t('absence.create'), true))) return;
      const r = await act(() => api('POST', '/absences', { from: v.from, to: v.to, reason: v.reason, extend_cards: v.extend }),
        (x) => t('absence.created', { s: x.absence.sessions, b: x.absence.cancelled_bookings, c: x.absence.extended.length }));
      if (r) renderDialog();
    },
  },
  el('div', { class: 'form cols' },
    field(t('absence.from'), datePicker('from', todayISO())),
    field(t('absence.to'), datePicker('to', todayISO()))),
  field(t('absence.reason'), el('input', { name: 'reason', maxlength: 200, placeholder: t('absence.reason_ph') })),
  el('label', { class: 'check' }, el('input', { type: 'checkbox', name: 'extend', checked: true }), t('absence.extend')),
  el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit' }, '🌴 ' + t('absence.create'))));
  return [
    el('h3', {}, '🌴 ' + t('absence.title')),
    el('p', { class: 'muted small' }, t('absence.hint')),
    form,
    out.absences.length ? [h2(t('absence.list')), el('ul', { class: 'list' }, out.absences.map((a) => el('li', {},
      el('strong', {}, fmtDate(a.from) + ' – ' + fmtDate(a.to)),
      el('span', { class: 'muted small grow' }, [a.reason, t('absence.summary', { c: a.extended.length, d: a.days })].filter(Boolean).join(' · ')),
      btn(t('absence.undo'), async () => {
        if (await confirmDialog(t('absence.undo_confirm'), t('absence.undo'), true)) act(() => api('DELETE', '/absences/' + a.id), t('absence.undone'));
      }, 'small ghost danger'))))] : null,
  ];
}

async function messagePopup(audience, day) {
  const sent = (await api('GET', '/messages')).messages.slice(0, 15);
  const dateField = field(t('msg.date'), datePicker('date', day || todayISO()));
  const sel = el('select', { name: 'audience', onchange: () => dateField.classList.toggle('hidden', sel.value !== 'day') },
    el('option', { value: 'all', selected: audience === 'all' }, t('msg.all')), el('option', { value: 'day', selected: audience === 'day' }, t('msg.day')));
  dateField.classList.toggle('hidden', audience !== 'day');
  const form = el('form', {
    class: 'form', onsubmit: async (ev) => {
      ev.preventDefault();
      const v = formData(form);
      if (await act(() => api('POST', '/messages', { text: v.text, audience: v.audience, date: v.date }),
        (r) => t('msg.sent', { n: r.message.recipients.length }))) renderDialog();
    },
  },
  el('textarea', { name: 'text', required: true, maxlength: 1000, placeholder: t('msg.placeholder') }),
  el('div', { class: 'form cols' }, field(t('msg.to'), sel), dateField),
  el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'submit' }, '📣 ' + t('msg.send'))));
  return [
    el('h3', {}, '📣 ' + t('msg.title')),
    el('p', { class: 'muted small' }, t('msg.hint')),
    form,
    sent.length ? [h2(t('msg.sent_list')), el('ul', { class: 'list' }, sent.map((m) => el('li', {},
      el('div', { class: 'grow' }, el('div', {}, m.text), el('div', { class: 'muted small' }, fmtTs(m.created_at) + ' · ' +
        (m.audience === 'day' ? t('msg.day_of', { date: fmtShort(m.date) }) : t('msg.all')))),
      el('span', { class: 'badge ' + (m.read_count === m.recipient_count ? 'ok' : ''), title: m.unread_names.join(', ') },
        t('msg.read', { r: m.read_count, n: m.recipient_count })))))] : null,
  ];
}

async function insightsBody() {
  const ins = await api('GET', '/insights');
  const person = (r, extra) => el('li', { class: 'clickable', onclick: () => popup(() => swimmerPopup(r.id)) },
    avatar(r, 30), el('strong', { class: 'grow' }, r.name), el('span', { class: 'muted small' }, extra),
    r.phone ? el('a', { class: 'btn small ghost', href: 'tel:' + r.phone, onclick: (ev) => ev.stopPropagation() }, '📞') : null);
  const section = (title, hint, rows) => el('div', { class: 'card' }, h2(title), el('p', { class: 'muted small' }, hint),
    rows.length ? el('ul', { class: 'list' }, rows) : el('p', { class: 'muted' }, t('insights.none')));
  const maxOcc = Math.max(0.01, ...ins.occupancy_by_weekday.map((x) => x || 0));
  return [
    el('div', { class: 'grid two' },
      section('😴 ' + t('insights.inactive'), t('insights.inactive_hint'),
        ins.inactive.map((r) => person(r, r.last_visit ? t('insights.last', { date: fmtShort(r.last_visit) }) : t('insights.never')))),
      section('⏰ ' + t('insights.expiring'), t('insights.expiring_hint'),
        ins.expiring.map((r) => person(r, t('insights.expires', { n: r.remaining, date: fmtShort(r.expires) })))),
      section('⚠ ' + t('insights.low'), t('insights.low_hint'),
        ins.low.map((r) => person(r, t('credits.left_short', { n: r.available })))),
      el('div', { class: 'card' }, h2('🔥 ' + t('insights.busiest')), el('p', { class: 'muted small' }, t('insights.weeks')),
        ins.busiest.length ? el('div', { class: 'row' }, ins.busiest.map(([tm, n]) => el('span', { class: 'chip booked big-chip' }, tm + ' · ' + n)))
          : el('p', { class: 'muted' }, t('stats.no_data')),
        h2(t('insights.occupancy')),
        el('div', { class: 'bars' }, ins.occupancy_by_weekday.map((x, i) => el('div', { class: 'bar' + (i >= 5 ? ' weekend' : '') },
          el('span', {}, weekdayName(i)), el('div', { class: 'track' }, el('div', { class: 'fill', style: 'width:' + ((x || 0) / maxOcc * 100) + '%' })),
          el('span', { class: 'num' }, x === null ? '—' : Math.round(x * 100) + '%')))))),
  ];
}

// ------------------------------------------------------------------ boot

async function boot() {
  try {
    const out = await api('GET', '/me');
    S.me = out.user;
    S.settings = { ...out.settings, today: out.today };
    S.coach = out.coach;
    if (S.me.lang && S.me.lang !== S.lang) await loadLang(S.me.lang);
  } catch (e) {
    S.me = null;
  }
  renderShell();
  await refresh();
}

(async function start() {
  applyTheme();
  await loadLang(S.lang);
  await boot();
  liveSync();
})();
