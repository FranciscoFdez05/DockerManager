'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let logWs = null;
let statsWs = null;
let termWs = null;
let term = null;
let fitAddon = null;
let termResizeObserver = null;
let statsChart = null;
let currentProcId = null;
let currentLogId = null;
let currentLogName = null;
let prevContainerStates = {};
const chartBuf = { labels: [], cpu: [], mem: [] };
const MAX_CHART_PTS = 60;

const STATUS_LABELS = {
  running: 'Corriendo', exited: 'Detenido', paused: 'Pausado',
  restarting: 'Reiniciando', dead: 'Muerto', created: 'Creado',
};
const STATUS_CLASS = {
  running: 's-running', exited: 's-exited', dead: 's-exited',
  paused: 's-paused', restarting: 's-restarting',
};

// ── Bootstrap ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Enter en setup
  ['setup-user','setup-pass','setup-confirm'].forEach(id =>
    q(id).addEventListener('keydown', e => { if (e.key === 'Enter') doSetup(); })
  );
  // Enter en login
  ['login-user','login-pass'].forEach(id =>
    q(id).addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); })
  );

  const ok = await checkAuth();
  if (!ok) return;
  boot();
});

function boot() {
  requestNotifPermission();
  loadAll();
  setInterval(loadContainers, 12000);
  setInterval(loadHostStats, 4000);
  loadHostStats();

  q('pull-tag-input').addEventListener('keydown', e => { if (e.key === 'Enter') doPull(); });
  q('cp-new').addEventListener('keydown', e => { if (e.key === 'Enter') doChangePassword(); });
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeAllModals();
});

// ── Auth ───────────────────────────────────────────────────────────────────
async function checkAuth() {
  try {
    const res = await fetch('/api/auth/status');
    const d = await res.json();
    if (d.setup_needed) {
      showModal('setup-modal');
      setTimeout(() => q('setup-user').focus(), 50);
      return false;
    }
    if (!d.authenticated) {
      showModal('login-modal');
      setTimeout(() => q('login-user').focus(), 50);
      return false;
    }
    q('logout-btn').classList.remove('hidden');
    q('chpass-btn').classList.remove('hidden');
    return true;
  } catch (_) {
    return true;
  }
}

async function doSetup() {
  const username = q('setup-user').value.trim();
  const password = q('setup-pass').value;
  const confirm  = q('setup-confirm').value;
  const errEl    = q('setup-error');
  errEl.classList.add('hidden');

  try {
    const res = await fetch('/api/auth/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, confirm }),
    });
    const d = await res.json();
    if (!res.ok) {
      errEl.textContent = d.detail || 'Error';
      errEl.classList.remove('hidden');
      return;
    }
    closeAllModals();
    q('logout-btn').classList.remove('hidden');
    q('chpass-btn').classList.remove('hidden');
    toast(`Cuenta creada. Bienvenido, ${username}`, 'success');
    boot();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  }
}

async function doLogin() {
  const username = q('login-user').value.trim();
  const password = q('login-pass').value;
  const errEl    = q('login-error');
  errEl.classList.add('hidden');

  try {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const d = await res.json();
    if (!res.ok) {
      errEl.textContent = d.detail || 'Credenciales incorrectas';
      errEl.classList.remove('hidden');
      q('login-pass').value = '';
      q('login-pass').focus();
      return;
    }
    closeAllModals();
    q('logout-btn').classList.remove('hidden');
    q('chpass-btn').classList.remove('hidden');
    boot();
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  }
}

async function doLogout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  location.reload();
}

function openChangePassword() {
  q('cp-current').value = '';
  q('cp-new').value = '';
  q('cp-confirm').value = '';
  q('cp-error').classList.add('hidden');
  showModal('chpass-modal');
  setTimeout(() => q('cp-current').focus(), 50);
}

async function doChangePassword() {
  const current  = q('cp-current').value;
  const new_pass = q('cp-new').value;
  const confirm  = q('cp-confirm').value;
  const errEl    = q('cp-error');
  errEl.classList.add('hidden');

  try {
    const res = await fetch('/api/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current, new_password: new_pass, confirm }),
    });
    const d = await res.json();
    if (!res.ok) {
      errEl.textContent = d.detail || 'Error';
      errEl.classList.remove('hidden');
      return;
    }
    closeModal('chpass-modal');
    toast('Contraseña actualizada', 'success');
  } catch (err) {
    errEl.textContent = err.message;
    errEl.classList.remove('hidden');
  }
}

// ── Notifications ──────────────────────────────────────────────────────────
function requestNotifPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function checkContainerAlerts(list) {
  list.forEach(c => {
    const prev = prevContainerStates[c.id];
    if (prev === 'running' && c.status !== 'running') {
      fireNotif(`Contenedor caído: ${c.name}`, `Estado: ${STATUS_LABELS[c.status] || c.status}`);
    }
  });
  prevContainerStates = Object.fromEntries(list.map(c => [c.id, c.status]));
}

function fireNotif(title, body) {
  if (Notification.permission === 'granted') {
    new Notification(title, { body, icon: '/favicon.svg' });
  }
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadContainers(), loadImages(), loadVolumes(), loadNetworks()]);
}

async function loadContainers() {
  try {
    const res = await fetch('/api/containers');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();
    checkContainerAlerts(list);
    renderContainers(list);
    setDockerStatus(true);
  } catch (err) {
    setDockerStatus(false);
    q('containers-body').innerHTML =
      `<tr><td colspan="6" class="empty" style="color:var(--red)">Error: ${esc(err.message)}</td></tr>`;
  }
}

async function loadImages() {
  try {
    const res = await fetch('/api/images');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderImages(await res.json());
  } catch (err) {
    q('images-body').innerHTML =
      `<tr><td colspan="5" class="empty" style="color:var(--red)">${esc(err.message)}</td></tr>`;
  }
}

async function loadVolumes() {
  try {
    const res = await fetch('/api/volumes');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderVolumes(await res.json());
  } catch (err) {
    q('volumes-body').innerHTML =
      `<tr><td colspan="5" class="empty" style="color:var(--red)">${esc(err.message)}</td></tr>`;
  }
}

async function loadNetworks() {
  try {
    const res = await fetch('/api/networks');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderNetworks(await res.json());
  } catch (err) {
    q('networks-body').innerHTML =
      `<tr><td colspan="6" class="empty" style="color:var(--red)">${esc(err.message)}</td></tr>`;
  }
}

async function loadHostStats() {
  try {
    const res = await fetch('/api/system/host');
    if (!res.ok) return;
    const d = await res.json();
    q('host-stats-bar').innerHTML =
      `<span class="hstat">CPU <b>${d.cpu_pct.toFixed(1)}%</b></span>` +
      `<span class="hstat">RAM <b>${d.mem_pct.toFixed(1)}%</b> <small>${fmtBytes(d.mem_used)}/${fmtBytes(d.mem_total)}</small></span>` +
      `<span class="hstat">Disco <b>${d.disk_pct}%</b> <small>${fmtBytes(d.disk_used)}/${fmtBytes(d.disk_total)}</small></span>`;
  } catch (_) {}
}

// ── Rendering ──────────────────────────────────────────────────────────────
function renderContainers(list) {
  q('container-count').textContent = `${list.length} total`;
  if (!list.length) {
    q('containers-body').innerHTML = '<tr><td colspan="6" class="empty">Sin contenedores</td></tr>';
    return;
  }

  q('containers-body').innerHTML = list.map(c => {
    const isRunning = c.status === 'running';
    const isPaused = c.status === 'paused';
    const cls = STATUS_CLASS[c.status] || 's-other';
    const label = STATUS_LABELS[c.status] || c.status;

    // Docker publica el mismo puerto una vez por familia IP (v4 y v6): deduplicamos
    const portEntries = [...new Map(
      Object.entries(c.ports || {}).flatMap(([k, v]) =>
        v.map(p => [`${p}:${k}`, { host: p, container: k.split('/')[0] }])
      )
    ).values()];
    const ports = portEntries.length
      ? portEntries.map(p =>
          `<a class="port-tag port-link" href="http://${location.hostname}:${esc(p.host)}" target="_blank" title="Abrir puerto ${esc(p.host)}">${esc(p.host)}&#8594;${esc(p.container)}</a>`
        ).join('')
      : '<span class="dim">—</span>';

    // Cada acción va en una ranura fija de la rejilla → columnas alineadas entre filas
    const icon = (slot, cls, glyph, title, onclick) =>
      `<button class="icon-btn ${cls}" style="grid-column:${slot}" title="${title}" aria-label="${title}" onclick="${onclick}">${glyph}</button>`;
    const op = (slot, cls, glyph, name, title) =>
      icon(slot, cls, glyph, title, `action('${c.id}','${name}')`);

    const logBtn   = icon(5, 'btn-accent', '&#x1F4CB;', 'Logs',      `openLogs('${c.id}','${esc(c.name)}')`);
    const statsBtn = icon(6, 'btn-accent', '&#x1F4CA;', 'Stats',     `openStats('${c.id}','${esc(c.name)}')`);
    const inspBtn  = icon(7, '',           '&#x1F50D;', 'Inspect',   `openInspect('${c.id}','${esc(c.name)}')`);
    const procsBtn = icon(8, '',           '&#x1F9F5;', 'Procesos',  `openProcs('${c.id}','${esc(c.name)}')`);
    const termBtn  = icon(9, 'btn-yellow', '&#x1F5A5;', 'Terminal',  `openTerminal('${c.id}','${esc(c.name)}')`);
    const rmBtn    = op(11, 'btn-red', '&#x1F5D1;', 'remove', 'Eliminar');
    const sep      = col => `<span class="act-sep" style="grid-column:${col}"></span>`;

    let acts;
    if (isPaused) {
      acts = [op(3, 'btn-yellow', '&#x25B6;', 'unpause', 'Reanudar'),
              logBtn, inspBtn, rmBtn];
    } else if (isRunning) {
      acts = [op(1, 'btn-red',    '&#x23F9;', 'stop',    'Detener'),
              op(2, 'btn-yellow', '&#x21BA;', 'restart', 'Reiniciar'),
              op(3, 'btn-yellow', '&#x23F8;', 'pause',   'Pausar'),
              logBtn, statsBtn, inspBtn, procsBtn, termBtn];
    } else {
      acts = [op(1, 'btn-green', '&#x25B6;', 'start', 'Iniciar'),
              logBtn, inspBtn, rmBtn];
    }
    // El separador final solo si hay algo detrás de él
    acts = acts.join('') + sep(4) + (acts.includes(rmBtn) ? sep(10) : '');

    const statsCell = isRunning
      ? `<span class="mini-stats" id="mstat-${c.id}">—</span>`
      : `<span class="dim">—</span>`;

    return `<tr data-name="${esc(c.name.toLowerCase())}">
      <td title="${esc(c.name)}"><span class="mono cell-name">${esc(c.name)}</span></td>
      <td title="${esc(c.image)}"><span class="mono dim">${esc(c.image)}</span></td>
      <td><span class="status-dot ${cls}">${label}</span></td>
      <td>${ports}</td>
      <td>${statsCell}</td>
      <td class="col-actions"><div class="actions actions-grid">${acts}</div></td>
    </tr>`;
  }).join('');

  list.filter(c => c.status === 'running').forEach(c => loadMiniStats(c.id));
  filterContainers();
}

async function loadMiniStats(id) {
  try {
    const res = await fetch(`/api/containers/${id}/stats_once`);
    if (!res.ok) return;
    const d = await res.json();
    const el = document.getElementById(`mstat-${id}`);
    if (el) el.textContent = `CPU ${d.cpu_pct.toFixed(1)}% | RAM ${fmtBytes(d.mem_usage)}`;
  } catch (_) {}
}

function filterContainers() {
  const q_val = (q('filter-input').value || '').toLowerCase();
  document.querySelectorAll('#containers-body tr[data-name]').forEach(row => {
    row.style.display = row.dataset.name.includes(q_val) ? '' : 'none';
  });
}

function renderImages(list) {
  q('image-count').textContent = `${list.length} total`;
  if (!list.length) {
    q('images-body').innerHTML = '<tr><td colspan="5" class="empty">Sin imágenes</td></tr>';
    return;
  }
  q('images-body').innerHTML = list.map(img => {
    const tags = img.tags.length
      ? img.tags.map(t => `<span class="port-tag">${esc(t)}</span>`).join(' ')
      : '<span class="dim">sin tag</span>';
    return `<tr>
      <td>${tags}</td>
      <td><span class="mono dim">${esc(img.id)}</span></td>
      <td>${img.size_mb} MB</td>
      <td>${img.created}</td>
      <td class="col-actions"><div class="actions">
        <button class="icon-btn" title="Historial" aria-label="Historial" onclick="openHistory('${esc(img.id)}','${esc(img.tags[0] || img.id)}')">&#x1F4DC;</button>
        <button class="icon-btn btn-red" title="Eliminar" aria-label="Eliminar" onclick="removeImage('${esc(img.id)}')">&#x1F5D1;</button>
      </div></td>
    </tr>`;
  }).join('');
}

function renderVolumes(list) {
  q('volume-count').textContent = `${list.length} total`;
  if (!list.length) {
    q('volumes-body').innerHTML = '<tr><td colspan="5" class="empty">Sin volúmenes</td></tr>';
    return;
  }
  q('volumes-body').innerHTML = list.map(v => `<tr>
    <td><span class="mono">${esc(v.name)}</span></td>
    <td><span class="dim">${esc(v.driver)}</span></td>
    <td title="${esc(v.mountpoint)}"><span class="mono dim" style="font-size:11px">${esc(v.mountpoint)}</span></td>
    <td>${v.created || '—'}</td>
    <td class="col-actions"><div class="actions">
      <button class="icon-btn btn-red" title="Eliminar" aria-label="Eliminar" onclick="removeVolume('${esc(v.name)}')">&#x1F5D1;</button>
    </div></td>
  </tr>`).join('');
}

function renderNetworks(list) {
  q('network-count').textContent = `${list.length} total`;
  if (!list.length) {
    q('networks-body').innerHTML = '<tr><td colspan="6" class="empty">Sin redes</td></tr>';
    return;
  }
  q('networks-body').innerHTML = list.map(n => `<tr>
    <td><span class="mono">${esc(n.name)}</span></td>
    <td><span class="dim">${esc(n.driver)}</span></td>
    <td><span class="mono dim">${esc(n.subnet) || '—'}</span></td>
    <td>${esc(n.scope)}</td>
    <td>${n.containers}</td>
    <td class="col-actions"><div class="actions">
      <button class="icon-btn btn-red" title="Eliminar" aria-label="Eliminar" onclick="removeNetwork('${esc(n.id)}','${esc(n.name)}')">&#x1F5D1;</button>
    </div></td>
  </tr>`).join('');
}

// ── Container actions ──────────────────────────────────────────────────────
async function action(id, op) {
  if (op === 'remove' && !confirm('¿Eliminar este contenedor?')) return;
  const method = op === 'remove' ? 'DELETE' : 'POST';
  const url = op === 'remove' ? `/api/containers/${id}` : `/api/containers/${id}/${op}`;
  try {
    const res = await fetch(url, { method });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || JSON.stringify(body));
    const labels = { start:'iniciado', stop:'detenido', restart:'reiniciado',
                     remove:'eliminado', pause:'pausado', unpause:'reanudado' };
    toast(`Contenedor ${labels[op]}`, 'success');
    setTimeout(loadContainers, 1200);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

async function removeImage(id) {
  if (!confirm('¿Eliminar esta imagen?')) return;
  try {
    const res = await fetch(`/api/images/${id}`, { method: 'DELETE' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || JSON.stringify(body));
    toast('Imagen eliminada', 'success');
    setTimeout(loadImages, 600);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

async function removeVolume(name) {
  if (!confirm(`¿Eliminar volumen "${name}"?`)) return;
  try {
    const res = await fetch(`/api/volumes/${encodeURIComponent(name)}`, { method: 'DELETE' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || JSON.stringify(body));
    toast('Volumen eliminado', 'success');
    setTimeout(loadVolumes, 600);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

async function removeNetwork(id, name) {
  if (!confirm(`¿Eliminar red "${name}"?`)) return;
  try {
    const res = await fetch(`/api/networks/${id}`, { method: 'DELETE' });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || JSON.stringify(body));
    toast('Red eliminada', 'success');
    setTimeout(loadNetworks, 600);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

// ── Log modal ──────────────────────────────────────────────────────────────
function openLogs(id, name) {
  currentLogId = id;
  currentLogName = name;
  q('log-container-name').textContent = name;
  const out = q('log-output');
  out.textContent = '';
  showModal('log-modal');
  if (logWs) logWs.close();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  logWs = new WebSocket(`${proto}//${location.host}/ws/containers/${id}/logs`);
  logWs.onmessage = ({ data }) => {
    if (!data) return; // keepalive vacío
    const autoScroll = q('autoscroll-toggle').checked;
    out.textContent += data;
    if (autoScroll) out.scrollTop = out.scrollHeight;
  };
  logWs.onerror = () => { out.textContent += '\n[Error de conexión]\n'; };
  logWs.onclose = () => { out.textContent += '\n[Stream cerrado]\n'; };
}

function clearLogs() { q('log-output').textContent = ''; }

function downloadLogs() {
  if (!currentLogId) return;
  window.open(`/api/containers/${currentLogId}/logs/download`, '_blank');
}

// ── Stats modal ────────────────────────────────────────────────────────────
function openStats(id, name) {
  q('stats-container-name').textContent = name;
  chartBuf.labels = []; chartBuf.cpu = []; chartBuf.mem = [];
  showModal('stats-modal');
  if (statsChart) { statsChart.destroy(); statsChart = null; }
  initChart();
  if (statsWs) statsWs.close();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  statsWs = new WebSocket(`${proto}//${location.host}/ws/containers/${id}/stats`);
  statsWs.onmessage = ({ data }) => {
    try {
      const d = JSON.parse(data);
      if (d.error) return;
      setBar('stats-cpu-bar', d.cpu_pct, 100);
      setBar('stats-mem-bar', d.mem_pct, 100);
      q('stats-cpu-val').textContent = `${d.cpu_pct.toFixed(2)}%`;
      q('stats-mem-val').textContent =
        `${fmtBytes(d.mem_usage)} / ${fmtBytes(d.mem_limit)} (${d.mem_pct.toFixed(1)}%)`;
      q('stats-net-rx').textContent = fmtBytes(d.net_rx);
      q('stats-net-tx').textContent = fmtBytes(d.net_tx);
      pushChart(d.cpu_pct, d.mem_pct);
    } catch (_) {}
  };
  statsWs.onerror = () => toast('Error conectando stats', 'error');
}

function closeStatsModal() {
  closeModal('stats-modal');
  if (statsWs) { statsWs.close(); statsWs = null; }
  if (statsChart) { statsChart.destroy(); statsChart = null; }
}

function setBar(id, val, max) {
  const el = document.getElementById(id);
  if (!el) return;
  const pct = Math.min(100, (val / max) * 100);
  el.style.width = pct + '%';
  el.className = `bar ${pct > 80 ? 'bar-danger' : pct > 50 ? 'bar-warn' : (id.includes('cpu') ? 'bar-cpu' : 'bar-mem')}`;
}

function initChart() {
  const canvas = q('stats-chart');
  if (!canvas) return;
  statsChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: chartBuf.labels,
      datasets: [
        { label: 'CPU %', data: chartBuf.cpu, borderColor: '#58a6ff',
          backgroundColor: 'rgba(88,166,255,0.08)', fill: true, tension: 0.4,
          pointRadius: 0, borderWidth: 2 },
        { label: 'RAM %', data: chartBuf.mem, borderColor: '#3fb950',
          backgroundColor: 'rgba(63,185,80,0.08)', fill: true, tension: 0.4,
          pointRadius: 0, borderWidth: 2 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 0 },
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { display: false },
        y: { min: 0, max: 100,
          ticks: { color: '#8b949e', font: { size: 11 }, callback: v => v + '%' },
          grid: { color: '#21262d' } },
      },
      plugins: {
        legend: { labels: { color: '#8b949e', font: { size: 11 }, boxWidth: 12 } },
        tooltip: {
          backgroundColor: '#161b22', borderColor: '#30363d', borderWidth: 1,
          titleColor: '#8b949e', bodyColor: '#c9d1d9',
        },
      },
    },
  });
}

function pushChart(cpu, mem) {
  const t = new Date().toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  chartBuf.labels.push(t); chartBuf.cpu.push(cpu); chartBuf.mem.push(mem);
  if (chartBuf.labels.length > MAX_CHART_PTS) {
    chartBuf.labels.shift(); chartBuf.cpu.shift(); chartBuf.mem.shift();
  }
  if (statsChart) statsChart.update('none');
}

// ── Inspect modal ──────────────────────────────────────────────────────────
async function openInspect(id, name) {
  q('inspect-container-name').textContent = name;
  q('inspect-body').innerHTML = '<p class="dim" style="padding:20px">Cargando...</p>';
  showModal('inspect-modal');
  try {
    const [inspRes, netsRes] = await Promise.all([
      fetch(`/api/containers/${id}/inspect`),
      fetch('/api/networks'),
    ]);
    const d = await inspRes.json();
    const allNets = await netsRes.json();

    const nets = Object.entries(d.networks).map(([n, cfg]) =>
      `<div class="kv">
        <span class="kv-k">${esc(n)}</span>
        <span class="kv-v">IP: ${esc(cfg.ip || '—')} &nbsp;GW: ${esc(cfg.gateway || '—')} &nbsp;MAC: ${esc(cfg.mac || '—')}
          &nbsp;<button class="btn-xs btn-red" onclick="netDisconnect('${id}','${esc(n)}')">Desconectar</button>
        </span>
      </div>`
    ).join('') || '<span class="dim">Sin redes</span>';

    const netOptions = allNets.map(n =>
      `<option value="${esc(n.name)}">${esc(n.name)}</option>`
    ).join('');

    const mounts = d.mounts.map(m =>
      `<div class="kv"><span class="kv-k">${esc(m.type)}</span>
       <span class="kv-v mono" style="font-size:11px">${esc(m.source)} → ${esc(m.destination)} [${m.rw ? 'rw' : 'ro'}]</span></div>`
    ).join('') || '<span class="dim">Sin mounts</span>';

    const envs = (d.env || []).map(e => `<div class="env-line">${esc(e)}</div>`).join('')
      || '<span class="dim">Sin variables</span>';

    const memLimit = d.memory_limit > 0 ? fmtBytes(d.memory_limit) : 'Sin límite';

    q('inspect-body').innerHTML = `
      <div class="inspect-grid">
        <div class="inspect-section">
          <h3>General</h3>
          ${kv('ID', d.id)} ${kv('Hostname', d.hostname)} ${kv('Estado', d.status)}
          ${kv('Creado', d.created)} ${kv('Iniciado', d.started_at)}
          ${kv('Restart policy', d.restart_policy)}
          ${kv('Límite RAM', memLimit)} ${kv('CPU shares', d.cpu_shares || 'Por defecto')}
        </div>
        <div class="inspect-section">
          <h3>Redes</h3>
          ${nets}
          <div style="margin-top:10px;display:flex;gap:6px;align-items:center">
            <select id="net-connect-select" class="form-input form-select" style="flex:1">
              ${netOptions}
            </select>
            <button class="btn-green btn-xs" onclick="netConnect('${id}')">Conectar</button>
          </div>
        </div>
        <div class="inspect-section">
          <h3>Mounts</h3>${mounts}
        </div>
        <div class="inspect-section full-width">
          <h3>Variables de entorno</h3>
          <div class="env-block">${envs}</div>
        </div>
      </div>`;
  } catch (err) {
    q('inspect-body').innerHTML =
      `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
  }
}

async function netConnect(containerId) {
  const network = q('net-connect-select').value;
  if (!network) return;
  try {
    const res = await fetch(`/api/containers/${containerId}/networks/connect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ network }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail);
    toast(`Conectado a ${network}`, 'success');
    const name = q('inspect-container-name').textContent;
    openInspect(containerId, name);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

async function netDisconnect(containerId, network) {
  try {
    const res = await fetch(`/api/containers/${containerId}/networks/disconnect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ network }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail);
    toast(`Desconectado de ${network}`, 'success');
    const name = q('inspect-container-name').textContent;
    openInspect(containerId, name);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

function kv(label, value) {
  return `<div class="kv"><span class="kv-k">${esc(label)}</span><span class="kv-v">${esc(String(value ?? '—'))}</span></div>`;
}

// ── Processes modal ────────────────────────────────────────────────────────
async function openProcs(id, name) {
  currentProcId = id;
  q('proc-container-name').textContent = name;
  q('proc-body').innerHTML = '<p class="dim" style="padding:20px">Cargando...</p>';
  showModal('proc-modal');
  await fetchProcs(id);
}

async function refreshProcs() { if (currentProcId) await fetchProcs(currentProcId); }

async function fetchProcs(id) {
  try {
    const res = await fetch(`/api/containers/${id}/processes`);
    if (!res.ok) { const b = await res.json(); throw new Error(b.detail || `HTTP ${res.status}`); }
    const d = await res.json();
    const header = `<tr>${(d.titles || []).map(t => `<th>${esc(t)}</th>`).join('')}</tr>`;
    const rows = (d.processes || []).map(p => `<tr>${p.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('');
    q('proc-body').innerHTML =
      `<div class="table-wrapper"><table><thead>${header}</thead><tbody>${rows}</tbody></table></div>`;
  } catch (err) {
    q('proc-body').innerHTML = `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
  }
}

// ── Terminal modal ─────────────────────────────────────────────────────────
function openTerminal(id, name) {
  q('term-container-name').textContent = name;
  q('term-status').textContent = 'Conectando...';
  showModal('term-modal');

  if (termWs) termWs.close();
  if (term) { term.dispose(); term = null; }
  if (termResizeObserver) { termResizeObserver.disconnect(); termResizeObserver = null; }

  const container = q('terminal-container');
  container.innerHTML = '';

  term = new Terminal({
    cursorBlink: true,
    fontSize: 13,
    fontFamily: "'Cascadia Code', 'Fira Code', Consolas, monospace",
    theme: {
      background: '#010409', foreground: '#c9d1d9', cursor: '#58a6ff',
      black: '#0d1117', red: '#f85149', green: '#3fb950', yellow: '#d29922',
      blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#b1bac4',
      brightBlack: '#6e7681', brightWhite: '#f0f6fc',
    },
    scrollback: 5000,
  });

  fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(container);
  setTimeout(() => fitAddon.fit(), 50);

  termResizeObserver = new ResizeObserver(() => {
    if (fitAddon) fitAddon.fit();
  });
  termResizeObserver.observe(container);

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  termWs = new WebSocket(`${proto}//${location.host}/ws/containers/${id}/exec`);
  termWs.binaryType = 'arraybuffer';

  termWs.onopen = () => {
    q('term-status').textContent = 'Conectado';
    if (term && termWs.readyState === WebSocket.OPEN) {
      termWs.send(JSON.stringify({ type: 'resize', rows: term.rows, cols: term.cols }));
    }
  };

  termWs.onmessage = (e) => {
    if (!term) return;
    if (e.data instanceof ArrayBuffer) {
      term.write(new Uint8Array(e.data));
    } else {
      term.write(e.data);
    }
  };

  termWs.onclose = () => {
    q('term-status').textContent = 'Desconectado';
    if (term) term.write('\r\n\x1b[33m[Sesión cerrada]\x1b[0m\r\n');
  };

  termWs.onerror = () => {
    if (term) term.write('\r\n\x1b[31m[Error de conexión]\x1b[0m\r\n');
  };

  term.onData(data => {
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(new TextEncoder().encode(data));
    }
  });

  term.onResize(({ rows, cols }) => {
    if (termWs && termWs.readyState === WebSocket.OPEN) {
      termWs.send(JSON.stringify({ type: 'resize', rows, cols }));
    }
  });
}

function closeTerminal() {
  closeModal('term-modal');
  if (termWs) { termWs.close(); termWs = null; }
  if (term) { term.dispose(); term = null; }
  if (termResizeObserver) { termResizeObserver.disconnect(); termResizeObserver = null; }
}

// ── Create container modal ─────────────────────────────────────────────────
async function openCreate() {
  q('c-image').value = ''; q('c-name').value = '';
  q('c-command').value = '';
  q('c-restart').value = 'unless-stopped';
  q('ports-list').innerHTML = '';
  q('env-list').innerHTML = '';
  q('vol-list').innerHTML = '';
  q('create-error').classList.add('hidden');

  // Populate networks
  try {
    const res = await fetch('/api/networks');
    const nets = await res.json();
    q('c-network').innerHTML = '<option value="">Por defecto</option>' +
      nets.map(n => `<option value="${esc(n.name)}">${esc(n.name)}</option>`).join('');
  } catch (_) {}

  showModal('create-modal');
  setTimeout(() => q('c-image').focus(), 50);
}

function addRow(listId, placeholder) {
  const list = q(listId);
  const row = document.createElement('div');
  row.className = 'dynamic-row';
  row.innerHTML = `
    <input type="text" class="form-input" placeholder="${esc(placeholder)}" />
    <button class="btn-red btn-xs" onclick="this.parentElement.remove()">&#x2715;</button>`;
  list.appendChild(row);
  row.querySelector('input').focus();
}

function getRows(listId) {
  return [...q(listId).querySelectorAll('input')].map(i => i.value).filter(v => v.trim());
}

async function submitCreate() {
  const image = q('c-image').value.trim();
  if (!image) {
    showCreateError('La imagen es obligatoria');
    return;
  }
  q('create-error').classList.add('hidden');

  const body = {
    image,
    name: q('c-name').value.trim() || undefined,
    restart_policy: q('c-restart').value,
    network: q('c-network').value || undefined,
    command: q('c-command').value.trim() || undefined,
    ports: getRows('ports-list'),
    env: getRows('env-list'),
    volumes: getRows('vol-list'),
  };

  try {
    const res = await fetch('/api/containers/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || JSON.stringify(d));
    toast(`Contenedor "${d.name}" creado`, 'success');
    closeModal('create-modal');
    setTimeout(loadContainers, 800);
  } catch (err) {
    showCreateError(err.message);
  }
}

function showCreateError(msg) {
  const el = q('create-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

// ── Image history modal ────────────────────────────────────────────────────
async function openHistory(id, name) {
  q('history-image-name').textContent = name;
  q('history-body').innerHTML = '<p class="dim" style="padding:20px">Cargando...</p>';
  showModal('history-modal');
  try {
    const res = await fetch(`/api/images/${id}/history`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const list = await res.json();
    const rows = list.map(h => `<tr>
      <td>${h.tags.length ? h.tags.map(t => `<span class="port-tag">${esc(t)}</span>`).join('') : '<span class="dim">—</span>'}</td>
      <td><span class="mono" style="font-size:11px">${esc(h.created_by || '<vacío>')}</span></td>
      <td style="white-space:nowrap">${h.size > 0 ? fmtBytes(h.size) : '<span class="dim">0 B</span>'}</td>
    </tr>`).join('');
    q('history-body').innerHTML = `<div class="table-wrapper">
      <table><thead><tr><th>Tag</th><th>Comando</th><th>Tamaño</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  } catch (err) {
    q('history-body').innerHTML =
      `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
  }
}

// ── System info ────────────────────────────────────────────────────────────
async function openSystemInfo() {
  q('sysinfo-body').innerHTML = '<p class="dim" style="padding:20px">Cargando...</p>';
  showModal('sysinfo-modal');
  try {
    const res = await fetch('/api/system/info');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    q('sysinfo-body').innerHTML = `
      <div class="inspect-section" style="padding:20px">
        ${kv('Hostname', d.hostname)} ${kv('IP del host', d.host_ip)}
        ${kv('Docker versión', d.docker_version)} ${kv('API versión', d.api_version)}
        ${kv('Sistema operativo', d.os)} ${kv('Kernel', d.kernel)}
        ${kv('Arquitectura', d.arch)} ${kv('CPUs', d.cpus)}
        ${kv('Memoria total', d.memory_gb + ' GB')}
        ${kv('Contenedores corriendo', d.containers_running)}
        ${kv('Contenedores detenidos', d.containers_stopped)}
        ${kv('Imágenes', d.images)}
        ${kv('Storage driver', d.storage_driver)}
        ${kv('Logging driver', d.logging_driver)}
        ${d.host_ip ? `<div style="margin-top:14px">
          <a class="port-tag port-link" href="http://${d.host_ip}" target="_blank">
            Abrir http://${d.host_ip} en navegador
          </a></div>` : ''}
      </div>`;
  } catch (err) {
    q('sysinfo-body').innerHTML =
      `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
  }
}

// ── Prune ──────────────────────────────────────────────────────────────────
async function confirmPrune() {
  if (!confirm('¿Eliminar contenedores parados, imágenes sin usar, volúmenes y redes huérfanas?')) return;
  try {
    const res = await fetch('/api/system/prune', { method: 'POST' });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail);
    toast(`Prune OK — ${d.freed_mb} MB liberados`, 'success');
    setTimeout(loadAll, 800);
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
  }
}

// ── Pull image ─────────────────────────────────────────────────────────────
function openPullModal() {
  q('pull-tag-input').value = '';
  q('pull-status').textContent = '';
  showModal('pull-modal');
  setTimeout(() => q('pull-tag-input').focus(), 50);
}

async function doPull() {
  const tag = q('pull-tag-input').value.trim();
  if (!tag) return;
  q('pull-status').innerHTML = '<span class="dim">Descargando... (puede tardar)</span>';
  try {
    const res = await fetch('/api/images/pull', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tag }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || JSON.stringify(d));
    q('pull-status').innerHTML = `<span style="color:var(--green)">✓ ${esc(tag)} descargada</span>`;
    toast(`Imagen ${tag} descargada`, 'success');
    setTimeout(loadImages, 600);
  } catch (err) {
    q('pull-status').innerHTML = `<span style="color:var(--red)">Error: ${esc(err.message)}</span>`;
    toast(`Error: ${err.message}`, 'error');
  }
}

// ── Modal helpers ──────────────────────────────────────────────────────────
function showModal(id) { q(id).classList.remove('hidden'); }

function closeModal(id) {
  q(id).classList.add('hidden');
  if (id === 'stats-modal') closeStatsModal();
  if (id === 'log-modal') { if (logWs) { logWs.close(); logWs = null; } }
  if (id === 'term-modal') closeTerminal();
  if (id === 'proc-modal') currentProcId = null;
}

function closeAllModals() {
  ['setup-modal','login-modal','log-modal','stats-modal','inspect-modal','proc-modal',
   'sysinfo-modal','pull-modal','term-modal','create-modal','history-modal','chpass-modal']
    .forEach(id => q(id).classList.add('hidden'));
  if (statsWs) { statsWs.close(); statsWs = null; }
  if (statsChart) { statsChart.destroy(); statsChart = null; }
  if (logWs) { logWs.close(); logWs = null; }
  if (termWs) { termWs.close(); termWs = null; }
  if (term) { term.dispose(); term = null; }
  if (termResizeObserver) { termResizeObserver.disconnect(); termResizeObserver = null; }
  currentProcId = null;
}

// ── UI helpers ─────────────────────────────────────────────────────────────
function setDockerStatus(ok) {
  const el = q('docker-status');
  el.textContent = ok ? '● Docker conectado' : '● Docker desconectado';
  el.className = 'status-badge ' + (ok ? 'ok' : 'err');
}

function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  q('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function fmtBytes(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 ** 2) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1024 ** 3) return (bytes / 1024 ** 2).toFixed(1) + ' MB';
  return (bytes / 1024 ** 3).toFixed(2) + ' GB';
}

function q(id) { return document.getElementById(id); }

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
