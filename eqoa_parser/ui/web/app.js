/* EQOA Parser web UI. The Python side (web_app.py) pushes JSON messages into window.eqoa.push(msg); this file only draws.
   Messages: snapshot | live | closed | finished | test | status (see WebState in web_app.py). */
(function () {
'use strict';
const $ = (s) => document.querySelector(s);
const fmt = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US'));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const short = (n) => (n == null ? '—' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? (n / 1e3).toFixed(1) + 'K' : fmt(n));
const rate = (v) => (v >= 100 ? fmt(Math.round(v)) : v ? v.toFixed(1) : '—');
const clock = (s) => { s = Math.max(0, Math.floor(s)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
const api = () => (window.pywebview && window.pywebview.api) || null;
const call = (name, ...args) => { const a = api(); return a && a[name] ? a[name](...args) : Promise.resolve(null); };

const S = {fights: [], live: null, last: null, tests: [], run: null, status: {}, settings: {}};
const UI = {mode: 'dps', tab: 'fights', expanded: false, selFight: null, selTest: 0, marking: false, testMsg: '', paneKey: ''};
const MODES = [['dps', 'DPS'], ['hps', 'HPS'], ['dtps', 'DTPS']];
const TABS = [['fights', 'FIGHTS'], ['detail', 'DETAIL'], ['tests', 'TESTS'], ['run', 'RUN']];

function bracket(label) { return '[ ' + label + ' ]'; }
function seg(el, items, cur, cb) {
  el.innerHTML = '';
  items.forEach(([k, l]) => {
    const b = document.createElement('button');
    b.className = 'btn' + (k === cur ? ' on' : '');
    b.textContent = bracket(l);
    b.onclick = () => cb(k);
    el.appendChild(b);
  });
}

/* ---- theme ---- */
function effectiveTheme() {
  return document.documentElement.dataset.theme || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}
function applyTheme(t) {
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  $('#xTheme').textContent = bracket(effectiveTheme() === 'dark' ? 'LIGHT' : 'DARK');
}

/* ---- compact meter ---- */
function currentFight() { return S.live || S.last; }
function modeVal(actor, mode) { return actor ? actor[mode] || 0 : 0; }
function barRow(name, v, max, pet) {
  const pct = max > 0 ? Math.round(v / max * 100) : 0;
  return `<div class="row${pet ? ' pet' : ''}"><i class="fill" style="width:${pct}%${pct ? '' : ';border:0'}"></i><span class="nm">${esc(name)}</span><span class="v">${v ? rate(v) : '—'}</span></div>`;
}
function groupSub(g, mode) { return mode === 'dps' ? g.combined_dps : mode === 'hps' ? g.hps : g.dtps; }

function renderCompact() {
  const f = currentFight(), mode = UI.mode, st = S.status || {};
  seg($('#modeC'), MODES, mode, (k) => { UI.mode = k; renderCompact(); });
  const live = !!S.live;
  let label = 'IDLE · WAITING FOR COMBAT';
  if (f) label = '#' + f.id + ' · ' + (f.target || 'NO TARGET').toUpperCase();
  else if (st.state === 'finished') label = 'SOURCE ENDED';
  $('#cLabel').innerHTML = `<i class="dot${live ? ' live' : st.error ? ' err' : st.connection ? ' ok' : ''}" id="cDot"></i>` + esc(label);
  $('#cTimer').textContent = f ? clock(f.duration) : '';
  $('#heroU').textContent = mode.toUpperCase() + ' · COMBINED';
  $('#heroC').textContent = f ? rate(f.totals[mode]) .replace('—', '0') : '0';
  $('#idleNote').textContent = f ? (st.state === 'finished' && !live ? 'SOURCE ENDED' : '') : (st.connection || st.label || '');
  $('#idleNote').classList.toggle('hidden', !$('#idleNote').textContent);
  let html = '';
  if (f) {
    const gs = f.groups.filter((g) => g.player.active || g.pet.active);
    const shown = gs.length ? gs : f.groups;
    let max = 0;
    shown.forEach((g) => { max = Math.max(max, modeVal(g.player, mode), modeVal(g.pet, mode)); });
    html = shown.slice().sort((a, b) => modeVal(b.player, mode) - modeVal(a.player, mode)).map((g) => {
      const head = shown.length > 1
        ? `<div class="emuhead"><span class="lbl">${esc(g.name).toUpperCase()}</span><span class="lbl dim">${rate(groupSub(g, mode)).replace('—', '0')} ${mode.toUpperCase()}</span></div>` : '';
      const rows = barRow(g.player.name, modeVal(g.player, mode), max, false) +
        (g.pet.active ? barRow(g.pet.name, modeVal(g.pet, mode), max, true) : '');
      return `<div class="emu">${head}${rows}</div>`;
    }).join('');
  }
  $('#emus').innerHTML = html;
  const foot = [];
  if (f) { foot.push('DMG ' + fmt(f.totals.damage), 'MAX HIT ' + fmt(f.totals.max_hit)); }
  const xp = S.run && S.run.progress_totals ? S.run.progress_totals.xp : 0;
  if (xp) foot.push('XP +' + short(xp));
  $('#cFoot').innerHTML = foot.map((x) => `<span>${esc(x)}</span>`).join('') || '<span>NO FIGHTS YET</span>';
  $('#cFoot').classList.toggle('hidden', false);
  const err = $('#cErr');
  err.textContent = st.error || '';
  err.classList.toggle('hidden', !st.error);
  $('#cPin').classList.toggle('on', !!S.settings.topmost);
  fitSoon();
}

let fitLast = 0, fitTimer = null;
function fitSoon() {
  if (UI.expanded || fitTimer) return;
  fitTimer = setTimeout(() => {
    fitTimer = null;
    if (UI.expanded) return;
    const h = $('#compact').offsetHeight;
    if (h && Math.abs(h - fitLast) > 1) { fitLast = h; call('fit', h); }
  }, 60);
}

/* ---- expanded view ---- */
function fightList() {
  const l = S.fights.slice();
  if (S.live) l.unshift(Object.assign({}, S.live, {uid: 'live', prev: false}));
  return l;
}
function selectedFight() {
  const l = fightList();
  if (!l.length) return null;
  return l.find((f) => f.uid === UI.selFight) || l[0];
}
function tagFor(f) {
  if (f.uid === 'live') return '<span class="tag bad">LIVE</span>';
  return f.prev ? '<span class="tag">PREV</span>' : '<span class="tag ok">DONE</span>';
}
function charsCell(f) {
  const parts = f.groups.filter((g) => g.combined_total > 0).map((g) =>
    esc(g.name) + ' ' + rate(g.player.dps).replace('—', '0') + (g.pet.total > 0 ? '+' + rate(g.pet.dps) : ''));
  return parts.length ? parts.join(' · ') : '—';
}
function table(head, rows, rightFrom) {
  return '<table><tr>' + head.map((h, i) => `<th${i >= rightFrom ? ' class="r"' : ''}>${h}</th>`).join('') + '</tr>' + rows + '</table>';
}

function paneFights() {
  const l = fightList();
  if (!l.length) return '<div class="empty">NO FIGHTS YET · WAITING FOR COMBAT</div>';
  const sel = selectedFight();
  const rows = l.slice(0, 200).map((f) => `<tr class="click${f.prev ? ' prev' : ''}${sel && sel.uid === f.uid ? ' sel' : ''}" data-uid="${esc(f.uid)}">
    <td class="m">${f.prev ? esc(f.run_id || 'prev').slice(-6) : 'now'}</td><td class="m">${f.id}</td><td>${esc(f.target || '—')}</td>
    <td class="r">${clock(f.duration)}</td><td class="sub">${charsCell(f)}</td><td>${tagFor(f)}</td></tr>`).join('');
  return table(['RUN', '#', 'TARGET', 'DUR', 'CHARACTERS · DPS (+PET)', ''], rows, 99);
}

function paneDetail() {
  const f = selectedFight();
  if (!f) return '<div class="empty">SELECT A FIGHT</div>';
  const rows = f.rows.map((r) => `<tr class="kind-${esc(r.kind)}"><td>${esc(r.label)}</td><td class="r">${fmt(r.total)}</td><td class="r">${rate(r.dps).replace('—', '0')}</td>
    <td class="r">${r.max_hit == null ? '' : fmt(r.max_hit)}</td><td class="r">${esc(r.taken)}</td><td class="r">${esc(r.healed)}</td></tr>`).join('');
  return `<div class="lbl" style="margin-bottom:8px">FIGHT ${f.id} · ${esc((f.target || 'NO TARGET').toUpperCase())} · ${clock(f.duration)}</div>` +
    table(['ACTOR', 'DMG', 'DPS', 'MAX', 'TAKEN', 'HEALED'], rows, 1) +
    f.lines.map((x) => `<p class="sub sp">${esc(x)}</p>`).join('') +
    `<p class="sub sp">Top abilities: ${esc(f.abilities)}</p>`;
}

function testRows() {
  if (!S.tests.length) return '<div class="empty">NO TESTS YET</div>';
  const best = Math.max(...S.tests.map((t) => t.dps));
  const rows = S.tests.map((t, i) => `<tr class="click${i === UI.selTest ? ' sel' : ''}" data-i="${i}"><td>${esc(t.label)}${t.complete ? '' : ' <span class="tag warn">EARLY</span>'}</td>
    <td class="r">${t.length_s}s</td><td class="r${t.dps === best ? ' ok' : ''}">${rate(t.dps)}</td><td class="r">${fmt(t.hits)}</td><td class="r">${fmt(t.max_hit)}</td><td>${esc(t.pets.join(', ') || '—')}</td></tr>`).join('');
  const t = S.tests[Math.min(UI.selTest, S.tests.length - 1)];
  const det = t.rows.map((r) => `<tr class="kind-${esc(r.kind)}"><td>${esc(r.label)}</td><td class="r">${fmt(r.total)}</td><td class="r">${rate(r.dps).replace('—', '0')}</td><td class="r">${r.max_hit == null ? '' : fmt(r.max_hit)}</td></tr>`).join('');
  return table(['TEST', 'LEN', 'DPS', 'HITS', 'BIGGEST', 'PET'], rows, 1) +
    `<div class="lbl" style="margin:12px 0 6px">${esc(t.label.toUpperCase())} · ${fmt(t.total)} IN ${t.length_s}S</div>` +
    table(['CHARACTER', 'TOTAL', 'DPS', 'MAX HIT'], det, 1);
}

function buildTestsShell() {
  $('#pane').innerHTML = `<div class="form"><span class="lbl">TIMED TEST</span>
    <input id="tLabel" value="test" maxlength="40" aria-label="Test label"><input class="s" id="tSecs" value="180" aria-label="Length in seconds"><span class="dim">SEC</span>
    <button class="btn acc" id="tGo">[ START ]</button></div>
    <div class="sub" id="tMsg"></div><div id="tList"></div>`;
  $('#tGo').onclick = () => {
    if (S.status.test_active) { call('cancel_test'); return; }
    call('start_test', $('#tLabel').value, parseFloat($('#tSecs').value)).then((r) => {
      UI.testMsg = r && !r.ok ? r.error : '';
      renderTestsLive();
    });
  };
}
function renderTestsLive() {
  const st = S.status, active = !!st.test_active;
  $('#tGo').textContent = bracket(active ? 'CANCEL' : 'START');
  $('#tGo').disabled = !st.can_test;
  $('#tLabel').disabled = $('#tSecs').disabled = active;
  const m = $('#tMsg');
  m.textContent = UI.testMsg || (active ? (st.test || '') : (st.can_test ? 'The clock starts at your first damage.' : 'Tests are not available.'));
  m.className = 'sub' + (UI.testMsg ? ' bad' : '');
  const list = $('#tList'), top = list.scrollTop;
  list.innerHTML = testRows();
  list.scrollTop = top;
  list.querySelectorAll('tr[data-i]').forEach((tr) => { tr.onclick = () => { UI.selTest = +tr.dataset.i; renderTestsLive(); }; });
}

function paneRun() {
  const r = S.run;
  if (!r) return '<div class="empty">NO RUN DATA YET</div>';
  const t = r.progress_totals, ph = t.per_hour;
  let h = `<div class="stats">
    <div class="stat"><div class="k">TUNAR / HR</div><div class="v">${short(ph.tunar)}</div></div>
    <div class="stat"><div class="k">XP / HR</div><div class="v">${short(ph.xp)}</div></div>
    <div class="stat"><div class="k">KILLS</div><div class="v">${fmt(t.kills)}</div></div></div>`;
  const best = r.best ? ` · best #${r.best.id} ${esc(r.best.target || '—')} (${rate(r.best.dps)} dps)` : '';
  h += `<p class="sub">Fights ${r.fight_count} · total damage ${fmt(r.total_damage)} · average DPS ${rate(r.average_dps).replace('—', '0')}${best}</p>`;
  if (r.characters.length) {
    h += '<div class="lbl" style="margin:12px 0 6px">CHARACTERS</div>' + table(['CHARACTER', 'DAMAGE', 'DPS', 'BEST FIGHT'],
      r.characters.map((c) => `<tr class="${c.name === 'All characters' ? 'kind-all' : ''}"><td>${esc(c.name)}</td><td class="r">${fmt(c.total)}</td><td class="r">${rate(c.dps)}</td>
      <td class="r">${c.best_fight_id == null ? '—' : '#' + c.best_fight_id + ' (' + rate(c.best_dps) + ')'}</td></tr>`).join(''), 1);
  }
  if (r.progress.length) {
    h += '<div class="lbl" style="margin:12px 0 6px">PROGRESS</div>' + table(['CHARACTER', 'KILLS', 'XP', 'MASTERY', 'TUNAR'],
      r.progress.map((p) => `<tr><td>${esc(p.name)}</td><td class="r">${fmt(p.kills)}</td><td class="r">${fmt(p.xp)}</td><td class="r">${fmt(p.mastery)}</td><td class="r">${fmt(p.tunar)}</td></tr>`).join(''), 1);
    h += `<p class="sub sp">Per hour use the run's elapsed time (${clock(r.elapsed)}). ${r.progress.map((p) => esc(p.text)).join(' · ')}</p>`;
  }
  return h;
}

function renderStatus() {
  const st = S.status || {};
  const parts = [st.label, st.connection, st.pine, st.record, st.test].filter(Boolean);
  const bar = $('#statusbar');
  bar.innerHTML = (st.error ? `<div class="bad" style="white-space:normal">ERROR: ${esc(st.error)}</div>` : '') + parts.map((x) => `<div>${esc(x)}</div>`).join('');
  bar.title = st.error || parts.join('\n');
  const word = S.live ? 'LIVE' : st.state === 'finished' ? 'SOURCE ENDED' : st.connection ? 'CONNECTED' : 'IDLE';
  const label = word + (st.pine ? ' · ' + st.pine.toUpperCase() : '');
  $('#xLabel').innerHTML = `<i class="dot ${S.live ? 'live' : st.error ? 'err' : st.connection ? 'ok' : ''}"></i>${esc(label)}`;
  $('#xPin').classList.toggle('on', !!S.settings.topmost);
}

function renderTabs() {
  const t = $('#tabs');
  t.innerHTML = '';
  const left = document.createElement('span'); left.className = 'seg';
  seg(left, TABS, UI.tab, (k) => { UI.tab = k; UI.paneKey = ''; renderExpanded(); });
  t.appendChild(left);
  const right = document.createElement('span'); right.className = 'ctl'; right.style.marginLeft = 'auto';
  const st = S.status || {};
  if (st.can_record) {
    if (UI.marking) {
      right.innerHTML = '<input id="mNote" value="mark" maxlength="60" style="width:110px" class="form-in" aria-label="Mark note">';
    }
    const rec = document.createElement('button');
    rec.className = 'btn' + (st.recording ? ' acc' : ''); rec.textContent = bracket(st.recording ? 'STOP REC' : 'RECORD');
    rec.onclick = () => call('toggle_record').then((r) => { if (r && !r.ok) { UI.testMsg = r.error; } });
    const mk = document.createElement('button');
    mk.className = 'btn'; mk.textContent = bracket(UI.marking ? 'SAVE' : 'MARK'); mk.disabled = !st.recording;
    mk.onclick = () => {
      if (!UI.marking) { UI.marking = true; renderTabs(); const i = $('#mNote'); if (i) { i.focus(); i.select(); } return; }
      const note = ($('#mNote') || {}).value || 'mark'; UI.marking = false; call('mark', note); renderTabs();
    };
    right.appendChild(rec); right.appendChild(mk);
  }
  t.appendChild(right);
  UI.tabsKey = JSON.stringify([UI.tab, st.can_record, st.recording, UI.marking]);
}

function renderExpanded() {
  const st = S.status || {};
  const key = JSON.stringify([UI.tab, st.can_record, st.recording, UI.marking]);
  if (key !== UI.tabsKey) renderTabs();
  renderStatus();
  const pane = $('#pane'), top = pane.scrollTop;
  if (UI.tab === 'tests') {
    if (UI.paneKey !== 'tests') { buildTestsShell(); UI.paneKey = 'tests'; }
    renderTestsLive();
    return;
  }
  UI.paneKey = UI.tab;
  pane.innerHTML = UI.tab === 'fights' ? paneFights() : UI.tab === 'detail' ? paneDetail() : paneRun();
  pane.scrollTop = top;
  pane.querySelectorAll('tr[data-uid]').forEach((tr) => {
    tr.onclick = () => { UI.selFight = tr.dataset.uid; UI.tab = 'detail'; UI.paneKey = ''; renderExpanded(); };
  });
}

function render() { if (UI.expanded) renderExpanded(); else renderCompact(); }

function setExpanded(on) {
  UI.expanded = on;
  document.body.className = on ? 'expanded' : 'compact';
  $('#compact').classList.toggle('hidden', on);
  $('#expanded').classList.toggle('hidden', !on);
  $('#expanded').style.display = on ? 'flex' : 'none';
  UI.paneKey = ''; UI.tabsKey = '';
  call('set_view', on).then(() => { fitLast = 0; render(); });
  render();
}

/* ---- messages from Python ---- */
let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; render(); });
}

window.eqoa = {
  /* Apply one message to the state; returns true if the layout was switched (which already rendered). */
  apply(m) {
    switch (m.type) {
      case 'snapshot':
        Object.assign(S, {fights: m.fights, live: m.live, last: m.last, tests: m.tests, run: m.run, status: m.status, settings: m.settings});
        applyTheme(m.settings.theme);
        if (m.settings.expanded !== UI.expanded) { setExpanded(m.settings.expanded); return true; }
        break;
      case 'live': S.live = m.fight; break;
      case 'closed': S.fights.unshift(m.fight); S.live = null; S.last = m.fight; S.run = m.run; break;
      case 'finished': S.run = m.run; S.live = null; break;
      case 'test': S.tests.unshift(m.result); UI.selTest = 0; break;
      case 'status': S.status = m.status; break;
    }
    return false;
  },
  /* One call per pump cycle from Python; draw once per animation frame however many messages arrive. */
  pushBatch(ms) {
    for (const m of ms) this.apply(m);
    scheduleRender();
  },
  push(m) { this.pushBatch([m]); },
};

/* ---- controls ---- */
function togglePin() { S.settings.topmost = !S.settings.topmost; call('set_topmost', S.settings.topmost); render(); }
$('#expand').onclick = () => setExpanded(true);
$('#collapse').onclick = () => setExpanded(false);
['cMin', 'xMin'].forEach((id) => { $('#' + id).onclick = () => call('minimize'); });
['cClose', 'xClose'].forEach((id) => { $('#' + id).onclick = () => call('close'); });
['cPin', 'xPin'].forEach((id) => { $('#' + id).onclick = togglePin; });
$('#xTheme').onclick = () => {
  const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
  applyTheme(next); call('set_theme', next);
};
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme());
document.addEventListener('contextmenu', (e) => e.preventDefault());

applyTheme();
renderCompact();
window.addEventListener('pywebviewready', () => call('ready'));
if (api()) call('ready');
})();
