'use strict';

let logWs = null;
let statsWs = null;
let currentProcId = null;

const STATUS_LABELS = {
  running:    'Corriendo',
  exited:     'Detenido',
  paused:     'Pausado',
  restarting: 'Reiniciando',
  dead:       'Muerto',
  created:    'Creado',
};

const STATUS_CLASS = {
  running:    's-running',
  exited:     's-exited',
  dead:       's-exited',
  paused:     's-paused',
  restarting: 's-restarting',
};

// ── Bootstrap ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadAll();
  setInterval(loadContainers, 12000);
  setInterval(loadHostStats, 4000);
  loadHostStats();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeAllModals();
});

// ── Data loading ───────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadContainers(), loadImages(), loadVolumes(), loadNetworks()]);
}

async function loadContainers() {
  try {
    const res = await fetch('/api/containers');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderContainers(await res.json());
    setDockerStatus(true);
  } catch (err) {
    setDockerStatus(false);
    q('containers-body').innerHTML =
      `<tr><td colspan="6" class="empty" style="color:var(--red)">Error al conectar con Docker: ${esc(err.message)}</td></tr>`;
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
    const isPaused  = c.status === 'paused';
    const cls = STATUS_CLASS[c.status] || 's-other';
    const label = STATUS_LABELS[c.status] || c.status;

    const portEntries = Object.entries(c.ports || {}).flatMap(([k, v]) => v.map(p => ({ host: p, container: k.split('/')[0] })));
    const ports = portEntries.length
      ? portEntries.map(p =>
          `<a class="port-tag port-link" href="http://${location.hostname}:${esc(p.host)}" target="_blank" title="Abrir en navegador">${esc(p.host)}&#8594;${esc(p.container)}</a>`
        ).join('')
      : '<span class="dim">—</span>';

    const startBtn   = `<button class="btn-green"  onclick="action('${c.id}','start')">&#x25B6; Start</button>`;
    const stopBtn    = `<button class="btn-red"    onclick="action('${c.id}','stop')">&#x23F9; Stop</button>`;
    const restBtn    = `<button class="btn-yellow" onclick="action('${c.id}','restart')">&#x21BA; Restart</button>`;
    const pauseBtn   = `<button class="btn-yellow" onclick="action('${c.id}','pause')">&#x23F8; Pause</button>`;
    const unpauseBtn = `<button class="btn-yellow" onclick="action('${c.id}','unpause')">&#x25B6; Unpause</button>`;
    const logsBtn    = `<button class="btn-accent" onclick="openLogs('${c.id}','${esc(c.name)}')">&#x1F4CB; Logs</button>`;
    const statsBtn   = `<button class="btn-accent" onclick="openStats('${c.id}','${esc(c.name)}')">&#x1F4CA; Stats</button>`;
    const inspectBtn = `<button onclick="openInspect('${c.id}','${esc(c.name)}')">&#x1F50D; Inspect</button>`;
    const procsBtn   = `<button onclick="openProcs('${c.id}','${esc(c.name)}')">&#x1F9F5; Procesos</button>`;
    const rmBtn      = `<button class="btn-red"    onclick="action('${c.id}','remove')">&#x1F5D1; Remove</button>`;

    let acts;
    if (isPaused) {
      acts = [unpauseBtn, logsBtn, inspectBtn, rmBtn].join('');
    } else if (isRunning) {
      acts = [stopBtn, restBtn, pauseBtn, logsBtn, statsBtn, inspectBtn, procsBtn].join('');
    } else {
      acts = [startBtn, logsBtn, inspectBtn, rmBtn].join('');
    }

    const statsCell = isRunning
      ? `<span class="mini-stats" id="mstat-${c.id}">—</span>`
      : `<span class="dim">—</span>`;

    return `<tr>
      <td><span class="mono">${esc(c.name)}</span></td>
      <td><span class="mono dim">${esc(c.image)}</span></td>
      <td><span class="status-dot ${cls}">${label}</span></td>
      <td>${ports}</td>
      <td>${statsCell}</td>
      <td><div class="actions">${acts}</div></td>
    </tr>`;
  }).join('');

  // Load mini-stats for running containers
  list.filter(c => c.status === 'running').forEach(c => loadMiniStats(c.id));
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
      <td><div class="actions">
        <button class="btn-red" onclick="removeImage('${esc(img.id)}')">&#x1F5D1; Remove</button>
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
    <td><span class="mono dim" style="font-size:11px">${esc(v.mountpoint)}</span></td>
    <td>${v.created || '—'}</td>
    <td><div class="actions">
      <button class="btn-red" onclick="removeVolume('${esc(v.name)}')">&#x1F5D1; Remove</button>
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
    <td><div class="actions">
      <button class="btn-red" onclick="removeNetwork('${esc(n.id)}','${esc(n.name)}')">&#x1F5D1; Remove</button>
    </div></td>
  </tr>`).join('');
}

// ── Container actions ──────────────────────────────────────────────────────
async function action(id, op) {
  if (op === 'remove' && !confirm('¿Eliminar este contenedor?')) return;

  const method = op === 'remove' ? 'DELETE' : 'POST';
  const url = op === 'remove'
    ? `/api/containers/${id}`
    : `/api/containers/${id}/${op}`;

  try {
    const res = await fetch(url, { method });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || JSON.stringify(body));
    const labels = { start: 'iniciado', stop: 'detenido', restart: 'reiniciado', remove: 'eliminado', pause: 'pausado', unpause: 'reanudado' };
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
  q('log-container-name').textContent = name;
  const out = q('log-output');
  out.textContent = '';
  showModal('log-modal');

  if (logWs) logWs.close();

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  logWs = new WebSocket(`${proto}//${location.host}/ws/containers/${id}/logs`);

  logWs.onmessage = ({ data }) => {
    const autoScroll = q('autoscroll-toggle').checked;
    out.textContent += data;
    if (autoScroll) out.scrollTop = out.scrollHeight;
  };

  logWs.onerror = () => { out.textContent += '\n[Error de conexión]\n'; };
  logWs.onclose = () => { out.textContent += '\n[Stream cerrado]\n'; };
}

function clearLogs() {
  q('log-output').textContent = '';
}

// ── Stats modal ────────────────────────────────────────────────────────────
function openStats(id, name) {
  q('stats-container-name').textContent = name;
  showModal('stats-modal');
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
      q('stats-mem-val').textContent = `${fmtBytes(d.mem_usage)} / ${fmtBytes(d.mem_limit)} (${d.mem_pct.toFixed(1)}%)`;
      q('stats-net-rx').textContent = fmtBytes(d.net_rx);
      q('stats-net-tx').textContent = fmtBytes(d.net_tx);
    } catch (_) {}
  };
  statsWs.onerror = () => toast('Error conectando stats', 'error');
}

function closeStatsModal() {
  closeModal('stats-modal');
  if (statsWs) { statsWs.close(); statsWs = null; }
}

function setBar(id, val, max) {
  const el = q(id);
  const pct = Math.min(100, (val / max) * 100);
  el.style.width = pct + '%';
  el.className = `bar ${pct > 80 ? 'bar-danger' : pct > 50 ? 'bar-warn' : (id.includes('cpu') ? 'bar-cpu' : 'bar-mem')}`;
}

// ── Inspect modal ──────────────────────────────────────────────────────────
async function openInspect(id, name) {
  q('inspect-container-name').textContent = name;
  q('inspect-body').innerHTML = '<p class="dim" style="padding:20px">Cargando...</p>';
  showModal('inspect-modal');
  try {
    const res = await fetch(`/api/containers/${id}/inspect`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();

    const nets = Object.entries(d.networks).map(([n, cfg]) =>
      `<div class="kv"><span class="kv-k">${esc(n)}</span><span class="kv-v">IP: ${esc(cfg.ip)} | GW: ${esc(cfg.gateway)} | MAC: ${esc(cfg.mac)}</span></div>`
    ).join('') || '<span class="dim">Sin redes</span>';

    const mounts = d.mounts.map(m =>
      `<div class="kv"><span class="kv-k">${esc(m.type)}</span><span class="kv-v mono" style="font-size:11px">${esc(m.source)} → ${esc(m.destination)} [${m.rw ? 'rw' : 'ro'}]</span></div>`
    ).join('') || '<span class="dim">Sin mounts</span>';

    const envs = (d.env || []).map(e =>
      `<div class="env-line">${esc(e)}</div>`
    ).join('') || '<span class="dim">Sin variables</span>';

    const memLimit = d.memory_limit > 0 ? fmtBytes(d.memory_limit) : 'Sin límite';

    q('inspect-body').innerHTML = `
      <div class="inspect-grid">
        <div class="inspect-section">
          <h3>General</h3>
          ${kv('ID', d.id)}
          ${kv('Hostname', d.hostname)}
          ${kv('Estado', d.status)}
          ${kv('Creado', d.created)}
          ${kv('Iniciado', d.started_at)}
          ${kv('Restart policy', d.restart_policy)}
          ${kv('Límite RAM', memLimit)}
          ${kv('CPU shares', d.cpu_shares || 'Por defecto')}
        </div>
        <div class="inspect-section">
          <h3>Redes</h3>
          ${nets}
        </div>
        <div class="inspect-section">
          <h3>Mounts</h3>
          ${mounts}
        </div>
        <div class="inspect-section full-width">
          <h3>Variables de entorno</h3>
          <div class="env-block">${envs}</div>
        </div>
      </div>`;
  } catch (err) {
    q('inspect-body').innerHTML = `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
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

async function refreshProcs() {
  if (currentProcId) await fetchProcs(currentProcId);
}

async function fetchProcs(id) {
  try {
    const res = await fetch(`/api/containers/${id}/processes`);
    if (!res.ok) {
      const b = await res.json();
      throw new Error(b.detail || `HTTP ${res.status}`);
    }
    const d = await res.json();
    const header = `<tr>${(d.titles || []).map(t => `<th>${esc(t)}</th>`).join('')}</tr>`;
    const rows = (d.processes || []).map(p =>
      `<tr>${p.map(cell => `<td>${esc(cell)}</td>`).join('')}</tr>`
    ).join('');
    q('proc-body').innerHTML = `<div class="table-wrapper"><table><thead>${header}</thead><tbody>${rows}</tbody></table></div>`;
  } catch (err) {
    q('proc-body').innerHTML = `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
  }
}

// ── System info modal ──────────────────────────────────────────────────────
async function openSystemInfo() {
  q('sysinfo-body').innerHTML = '<p class="dim" style="padding:20px">Cargando...</p>';
  showModal('sysinfo-modal');
  try {
    const res = await fetch('/api/system/info');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    const addr = d.host_ip ? `http://${d.host_ip}` : '—';
    q('sysinfo-body').innerHTML = `
      <div class="inspect-section" style="padding:20px">
        ${kv('Hostname', d.hostname)}
        ${kv('IP del host', d.host_ip)}
        ${kv('Dirección web', addr !== '—' ? addr : '—')}
        ${kv('Docker versión', d.docker_version)}
        ${kv('API versión', d.api_version)}
        ${kv('Sistema operativo', d.os)}
        ${kv('Kernel', d.kernel)}
        ${kv('Arquitectura', d.arch)}
        ${kv('CPUs', d.cpus)}
        ${kv('Memoria total', d.memory_gb + ' GB')}
        ${kv('Contenedores corriendo', d.containers_running)}
        ${kv('Contenedores detenidos', d.containers_stopped)}
        ${kv('Imágenes', d.images)}
        ${kv('Storage driver', d.storage_driver)}
        ${kv('Logging driver', d.logging_driver)}
        ${d.host_ip ? `<div style="margin-top:14px"><a class="port-tag port-link" href="http://${d.host_ip}" target="_blank">Abrir ${d.host_ip} en navegador</a></div>` : ''}
      </div>`;
  } catch (err) {
    q('sysinfo-body').innerHTML = `<p style="color:var(--red);padding:20px">Error: ${esc(err.message)}</p>`;
  }
}

// ── Prune ──────────────────────────────────────────────────────────────────
async function confirmPrune() {
  if (!confirm('¿Eliminar contenedores parados, imágenes sin usar, volúmenes y redes huérfanas?')) return;
  try {
    const res = await fetch('/api/system/prune', { method: 'POST' });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail);
    toast(`Prune OK — liberados ${d.freed_mb} MB`, 'success');
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

q('pull-tag-input') && document.addEventListener('DOMContentLoaded', () => {
  q('pull-tag-input').addEventListener('keydown', e => { if (e.key === 'Enter') doPull(); });
});

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
function showModal(id) {
  q(id).classList.remove('hidden');
}

function closeModal(id) {
  q(id).classList.add('hidden');
  if (id === 'stats-modal') { if (statsWs) { statsWs.close(); statsWs = null; } }
  if (id === 'log-modal')   { if (logWs)   { logWs.close();   logWs   = null; } }
  if (id === 'proc-modal')  { currentProcId = null; }
}

function closeAllModals() {
  ['log-modal','stats-modal','inspect-modal','proc-modal','sysinfo-modal','pull-modal'].forEach(closeModal);
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
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
