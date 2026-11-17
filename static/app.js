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
const data = { containers: [], images: [], volumes: [], networks: [] };
const collapsed = loadCollapsed();
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

  loadVersion();
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
  // Al arrancar y no al abrir el desplegable: el punto de «hay versión nueva»
  // tiene que salir sin que nadie vaya a mirar.
  refreshUpdate();
  setInterval(refreshUpdate, UPDATE_RECHECK_MS);

  q('pull-tag-input').addEventListener('keydown', e => { if (e.key === 'Enter') doPull(); });
  q('cp-new').addEventListener('keydown', e => { if (e.key === 'Enter') doChangePassword(); });
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeAllModals(); closeVersionPopover(); }
});

document.addEventListener('click', e => {
  if (!e.target.closest('#version-popover, #app-version-btn')) closeVersionPopover();
});

// ── Versión ────────────────────────────────────────────────────────────────
// /api/health es público, así que la versión se ve también en la pantalla de login
async function loadVersion() {
  try {
    const res = await fetch('/api/health');
    const d = await res.json();
    if (!d.version) return;
    document.querySelectorAll('[data-app-version]').forEach(el => { el.textContent = `v${d.version}`; });
  } catch (_) {}
}

// ── Actualización ──────────────────────────────────────────────────────────
// Desplegable de la versión de la cabecera. El botón no actualiza: deja una
// petición que recoge el vigilante del host (tools/actualizador/), que es quien
// ejecuta docker-update.sh. La app no puede hacerlo: recrearía su propio
// contenedor y se mataría a mitad.
const UPDATE_RECHECK_MS = 60 * 60 * 1000;
const UPDATE_POLL_MS = 5000;
const upd = { installed: null, published: null, hasNew: null, mine: null, poll: null, last: null, progress: null };

// Pasos de docker-update.sh, en el mismo orden que sus llamadas a `progreso`.
// El título del paso en curso lo manda el servidor (lleva la versión); estos
// son para pintar la lista completa. Si el script cambia el total, se pinta
// solo el paso en curso en vez de una lista que no casa.
const UPDATE_STEPS = [
  'Comprobar el estado local',
  'Descargar la versión nueva',
  'Construir la imagen',
  'Reiniciar con la versión nueva',
  'Comprobar que responde',
];
// Paso en el que DockerManager deja de responder: docker compose up recrea su contenedor.
const UPDATE_RESTART_STEP = 4;

// upd.progress: { phase, paso, total, titulo }.
// phase: wait (pedida, sin recoger), run, restart (sin respuesta), ok, fail.
function setUpdateProgress(next) {
  upd.progress = next ? { ...upd.progress, ...next } : null;
  paintUpdateProgress();
}

function progressPercent(p) {
  if (p.phase === 'ok') return 100;
  if (p.phase === 'wait' || !p.paso) return 3;
  // El paso en curso cuenta como medio hecho: la barra se mueve al empezarlo.
  return Math.round(((p.paso - 0.5) / p.total) * 100);
}

function progressHeadline(p) {
  if (p.phase === 'wait') return 'Petición enviada: esperando a que el servidor empiece';
  if (p.phase === 'ok') return 'Actualización terminada';
  if (p.phase === 'fail') {
    return p.paso ? `Falló en el paso ${p.paso} de ${p.total} · ${UPDATE_STEPS[p.paso - 1] || p.titulo || ''}`
      : 'La actualización falló antes de empezar';
  }
  if (p.phase === 'restart') return `Paso ${p.paso} de ${p.total} · Reiniciando: DockerManager no responde mientras tanto`;
  if (!p.paso) return 'Actualizando…';
  return `Paso ${p.paso} de ${p.total} · ${p.titulo || UPDATE_STEPS[p.paso - 1] || ''}`;
}

function renderUpdateSteps(p) {
  if (!p.total || p.total !== UPDATE_STEPS.length) return '';
  return `<ol class="upd-steps">${UPDATE_STEPS.map((label, i) => {
    const n = i + 1;
    let cls = '';
    if (p.phase === 'ok' || (p.paso && n < p.paso)) cls = 'done';
    else if (n === p.paso) cls = p.phase === 'fail' ? 'failed' : 'active';
    return `<li class="${cls}">${esc(label)}</li>`;
  }).join('')}</ol>`;
}

function paintUpdateProgress() {
  const p = upd.progress;
  ['vp-progress', 'um-progress'].forEach(id => {
    const el = q(id);
    el.classList.toggle('hidden', !p);
    if (!p) { el.innerHTML = ''; return; }
    const pct = progressPercent(p);
    el.dataset.phase = p.phase;
    // El desplegable es estrecho: barra y paso en curso; la lista va en el modal.
    el.innerHTML = `
      <div class="upd-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"
           aria-label="Progreso de la actualización"><span style="width:${pct}%"></span></div>
      <div class="upd-head">${esc(progressHeadline(p))}</div>
      ${el.classList.contains('compact') ? '' : renderUpdateSteps(p)}`;
  });
}

// Traduce el estado del servidor mientras dura la actualización.
function progressFromStatus(d) {
  const pr = d.progreso;
  if (pr) setUpdateProgress({ phase: 'run', paso: pr.paso, total: pr.total, titulo: pr.titulo });
  else if (!upd.progress || !['run', 'restart'].includes(upd.progress.phase)) {
    // Sin progreso: aún sin recoger, o un docker-update.sh anterior que no lo escribe.
    setUpdateProgress({ phase: 'wait', paso: 0, total: UPDATE_STEPS.length, titulo: '' });
  }
}

function toggleVersionPopover(e) {
  e.stopPropagation();
  const open = q('version-popover').classList.toggle('hidden') === false;
  q('app-version-btn').setAttribute('aria-expanded', String(open));
  if (open) refreshUpdate();
}

function closeVersionPopover() {
  q('version-popover').classList.add('hidden');
  q('app-version-btn').setAttribute('aria-expanded', 'false');
}

function updateMsg(text, type = '') {
  // El mismo mensaje en el desplegable y en el modal: se puede comprobar desde los dos.
  ['vp-msg', 'um-msg'].forEach(id => {
    const el = q(id);
    el.textContent = text;
    el.className = `vp-msg ${type}`;
  });
}

function paintRemote(r) {
  const row = q('vp-remote-row'), tag = q('vp-tag');
  upd.published = r && r.publicada || null;
  upd.hasNew = r && r.hayNueva !== undefined ? r.hayNueva : null;

  if (!r || !r.activo) { row.classList.add('hidden'); return; }
  row.classList.remove('hidden');

  if (!r.publicada) {
    // Sin dato: se dice, en vez de un guion que se confunde con «al día».
    q('vp-remote').textContent = 'no se pudo comprobar';
    row.title = r.error || '';
    tag.classList.add('hidden');
    return;
  }
  q('vp-remote').textContent = r.publicada;
  row.title = r.error ? `${r.error}. Último dato: ${new Date(r.comprobado).toLocaleString()}` : '';

  if (upd.hasNew === null) { tag.classList.add('hidden'); return; }
  tag.classList.remove('hidden');
  tag.textContent = upd.hasNew ? 'Hay actualización' : 'Al día';
  tag.classList.toggle('has-update', upd.hasNew);
}

function paintUpdate(d) {
  upd.last = d;
  upd.installed = d.version;
  q('vp-version').textContent = d.version || '—';
  paintRemote(d.remota);

  const warning = q('vp-warning');
  const failed = !d.enMarcha && d.ultimo && d.ultimo.estado === 'fallo';
  if (!d.vigilanteVisto) {
    warning.innerHTML = 'El vigilante de actualizaciones no da señales de vida. Instálalo una vez en el servidor con ' +
      '<code>sudo ./tools/actualizador/instalar-vigilante.sh</code>; mientras tanto, actualiza por SSH con <code>./docker-update.sh</code>.';
  } else if (failed) {
    warning.textContent = `La última actualización falló${d.ultimo.codigo != null ? ` (código ${d.ultimo.codigo})` : ''}. ` +
      'Sigue en marcha la versión anterior; el detalle está debajo.';
  }
  warning.classList.toggle('hidden', d.vigilanteVisto && !failed);

  const detail = q('vp-detail');
  const hasDetail = Boolean(d.ultimo && d.ultimo.detalle);
  detail.classList.toggle('hidden', !hasDetail);
  if (hasDetail) q('vp-detail-text').textContent = d.ultimo.detalle;

  const btn = q('vp-update-btn');
  btn.textContent = upd.hasNew && upd.published ? `Actualizar a la ${upd.published}` : 'Actualizar ahora';
  btn.disabled = Boolean(d.enMarcha) || !d.vigilanteVisto;
  q('vp-check-btn').disabled = Boolean(d.enMarcha);

  const marker = q('app-version-btn');
  marker.classList.toggle('has-update', upd.hasNew === true);
  marker.title = upd.hasNew === true ? `Hay una versión nueva: la ${upd.published}` : 'Versión de DockerManager';

  if (!q('update-modal').classList.contains('hidden')) paintUpdateModal(d);

  if (d.enMarcha) progressFromStatus(d);

  if (d.enMarcha && !upd.poll) {
    // Otra pestaña (o una recarga a mitad) pidió la actualización: se sigue igual.
    upd.mine = d.solicitadaEn;
    updateMsg('Actualizando… DockerManager se reiniciará');
    startUpdatePoll();
  }
}

async function fetchUpdate(force = false) {
  const res = await fetch('/api/actualizacion' + (force ? '?refrescar=1' : ''));
  if (!res.ok) throw new Error(await apiError(res));
  return res.json();
}

async function refreshUpdate() {
  try { paintUpdate(await fetchUpdate()); } catch (_) {}
}

async function checkUpdate() {
  q('vp-check-btn').disabled = true;
  q('um-check-btn').disabled = true;
  updateMsg('Comprobando…');
  try {
    const d = await fetchUpdate(true);
    paintUpdate(d);
    const r = d.remota || {};
    if (!r.activo) updateMsg('La comprobación de versión está desactivada en config.ini');
    else if (r.error && !r.publicada) updateMsg(r.error, 'error');
    else if (r.hayNueva) updateMsg(`Hay una versión nueva: la ${r.publicada}`, 'ok');
    else if (r.hayNueva === false) updateMsg('Estás en la última versión publicada', 'ok');
    else updateMsg('No se pudo comparar con la versión publicada');
  } catch (err) {
    updateMsg(`Error: ${err.message}`, 'error');
  } finally {
    q('vp-check-btn').disabled = Boolean(upd.poll);
    q('um-check-btn').disabled = Boolean(upd.poll);
  }
}

// «Actualizar» del desplegable no actualiza: abre el modal, que dice a qué
// versión se va, qué trae y qué va a pasar, y es ahí donde se confirma.
function requestUpdate() {
  closeVersionPopover();
  updateMsg('');
  if (upd.last) paintUpdateModal(upd.last);
  showModal('update-modal');
  refreshUpdate();
}

// «hace un momento», «hace 5 min», «hace 3 h»: la caché de GitHub dura horas,
// y saber de cuándo es el dato importa más que la hora exacta.
function timeAgo(iso) {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms)) return '';
  const min = Math.floor(ms / 60000);
  if (min < 1) return 'hace un momento';
  if (min < 60) return `hace ${min} min`;
  const h = Math.floor(min / 60);
  return h < 48 ? `hace ${h} h` : `hace ${Math.floor(h / 24)} días`;
}

// Markdown en línea del CHANGELOG (**negrita** y `código`). Se escapa antes:
// el texto viene de GitHub y no se mete HTML ajeno en la página.
function inlineMd(text) {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

function renderReleaseNotes(r) {
  const notes = r.novedades || [];
  const title = notes.length > 1
    ? `Novedades desde la ${esc(upd.installed)} (${notes.length} versiones)`
    : 'Novedades';
  if (!notes.length) {
    const why = r.errorNovedades
      ? `No se pudieron leer las notas de la versión: ${esc(r.errorNovedades)}`
      : 'El CHANGELOG publicado no tiene notas para esta versión.';
    return `<div class="um-notes-title">${title}</div><div class="um-notes-empty">${why}</div>`;
  }
  return `<div class="um-notes-title">${title}</div>` + notes.map(n => `
    <div class="um-release">
      <div class="um-release-head"><strong>${esc(n.version)}</strong>${n.fecha ? `<span>${esc(n.fecha)}</span>` : ''}</div>
      ${n.grupos.map(g => `
        ${g.titulo ? `<div class="um-group">${esc(g.titulo)}</div>` : ''}
        <ul>${g.elementos.map(e => `<li>${inlineMd(e)}</li>`).join('')}</ul>`).join('')}
    </div>`).join('');
}

function paintUpdateModal(d) {
  const r = d.remota || {};
  const tag = q('um-tag');
  q('um-installed').textContent = d.version || '—';
  q('um-published').textContent = r.publicada || (r.activo ? '?' : '—');
  q('um-checked').textContent = r.comprobado ? `Comprobado ${timeAgo(r.comprobado)}` : '';

  let status;
  if (!r.activo) status = 'La comprobación de versión está desactivada en config.ini.';
  else if (!r.publicada) status = r.error || 'No se pudo comprobar la versión publicada.';
  else if (upd.hasNew) status = `Hay una versión nueva: la ${r.publicada}.`;
  else if (upd.hasNew === false) status = 'Tienes la última versión publicada.';
  else status = 'No se pudo comparar con la versión publicada.';
  q('um-status-text').textContent = status;
  tag.classList.toggle('hidden', upd.hasNew === null);
  tag.textContent = upd.hasNew ? 'Hay actualización' : 'Al día';
  tag.classList.toggle('has-update', upd.hasNew === true);

  const notesEl = q('um-notes');
  notesEl.classList.toggle('hidden', upd.hasNew !== true);
  if (upd.hasNew === true) notesEl.innerHTML = renderReleaseNotes(r);

  const restart = 'DockerManager se reiniciará: estará un par de ' +
    'minutos sin responder. Si la versión nueva no arranca, el servidor vuelve solo a la anterior.';
  q('um-explain').textContent = upd.hasNew === false
    ? `Ya estás en la última publicada (${d.version}): se volverá a construir la misma versión, sin novedades. ${restart}`
    : restart;

  // El aviso (vigilante ausente, último fallo) es el mismo que el del desplegable.
  const warning = q('um-warning'), vpWarning = q('vp-warning');
  warning.innerHTML = vpWarning.innerHTML;
  warning.classList.toggle('hidden', vpWarning.classList.contains('hidden'));

  const btn = q('um-update-btn');
  btn.textContent = upd.hasNew && r.publicada ? `Actualizar a la ${r.publicada}`
    : upd.hasNew === false ? 'Reinstalar igualmente' : 'Actualizar ahora';
  btn.disabled = Boolean(d.enMarcha) || !d.vigilanteVisto;
  q('um-check-btn').disabled = Boolean(d.enMarcha) || Boolean(upd.poll);
}

async function confirmUpdate() {
  q('um-update-btn').disabled = true;
  q('vp-update-btn').disabled = true;
  try {
    const res = await fetch('/api/actualizacion', { method: 'POST' });
    if (!res.ok) throw new Error(await apiError(res));
    const d = await res.json();
    // Lo que quedase de una actualización anterior (un fallo) no se mezcla con esta.
    if (d.aceptada) setUpdateProgress(null);
    paintUpdate(d);
    if (!d.aceptada) { updateMsg('Ya hay una actualización en marcha'); return; }
    upd.mine = d.solicitadaEn;
    updateMsg('Petición enviada. Esperando al vigilante del servidor…');
    startUpdatePoll();
  } catch (err) {
    updateMsg(`Error: ${err.message}`, 'error');
    refreshUpdate();
  }
}

function stopUpdatePoll() {
  clearInterval(upd.poll);
  upd.poll = null;
}

function startUpdatePoll() {
  stopUpdatePoll();
  const before = upd.installed;
  upd.poll = setInterval(async () => {
    let d;
    try {
      const res = await fetch('/api/actualizacion');
      if (res.status === 401) { location.reload(); return; }
      d = await res.json();
    } catch (_) {
      // Mientras se recrea el contenedor no responde nadie: es la señal de que va en serio.
      // Ocurre en el paso de reinicio aunque no se llegara a ver su progreso.
      const p = upd.progress || {};
      setUpdateProgress({ phase: 'restart', total: p.total || UPDATE_STEPS.length,
        paso: Math.max(p.paso || 0, UPDATE_RESTART_STEP), titulo: '' });
      updateMsg('Reiniciando… esperando a que vuelva');
      return;
    }

    const mine = d.ultimo && d.ultimo.solicitadaEn === upd.mine;
    if (d.version && before && d.version !== before) {
      stopUpdatePoll();
      setUpdateProgress({ phase: 'ok' });
      updateMsg(`Actualizado a la ${d.version}. Recargando…`, 'ok');
      setTimeout(() => location.reload(), 2000);
    } else if (mine && d.ultimo.estado === 'ok') {
      stopUpdatePoll();
      setUpdateProgress({ phase: 'ok' });
      updateMsg('Actualización terminada. Recargando…', 'ok');
      setTimeout(() => location.reload(), 2000);
    } else if (mine && d.ultimo.estado === 'fallo') {
      stopUpdatePoll();
      if (upd.progress) setUpdateProgress({ phase: 'fail' });
      paintUpdate(d);
      updateMsg('La actualización falló; sigue la versión anterior', 'error');
    } else if (!d.enMarcha) {
      stopUpdatePoll();
      setUpdateProgress(null);
      paintUpdate(d);
      updateMsg('Nadie recogió la petición a tiempo. ¿Está en marcha el vigilante?', 'error');
    } else {
      paintUpdate(d);
      updateMsg('Actualizando… DockerManager se reiniciará');
    }
  }, UPDATE_POLL_MS);
}

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
    q('settings-btn').classList.remove('hidden');
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
    q('settings-btn').classList.remove('hidden');
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
    q('settings-btn').classList.remove('hidden');
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

// ── Ajustes ────────────────────────────────────────────────────────────────
// Solo lectura de /api/ajustes (config.ini y .env) con las acciones que ya
// había sueltas por la cabecera. Cada sección es una función que devuelve las
// tarjetas, así añadir una es añadir una entrada a SETTINGS_SECTIONS.
const SETTINGS_KEY = 'dm-settings-section';
const settingsState = { data: null, section: null };

const ST_ICONS = {
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  send: '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/>',
  bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  tag: '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><path d="M7.5 7.5h.01"/>',
  git: '<circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="8" r="2.5"/><path d="M6 8.5v7M18 10.5c0 4-6 3-11 5.5"/>',
  server: '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><path d="M6 7h.01M6 17h.01"/>',
  temp: '<path d="M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v5M14 11v5"/>',
};

const SETTINGS_SECTIONS = {
  cuenta: { title: 'Cuenta y seguridad', render: settingsAccount },
  avisos: { title: 'Avisos', render: settingsAlerts },
  actualizaciones: { title: 'Actualizaciones', render: settingsUpdates },
  servidor: { title: 'Servidor y limpieza', render: settingsServer },
};

const TELEGRAM_STATUS = {
  activo: ['ok', 'Listo.', 'Los avisos se envían por Telegram.'],
  sin_configurar: ['', 'Sin configurar.', 'Pon el token del bot y tu ID abajo y pulsa Guardar.'],
  incompleto: ['warn', 'Incompleto.', 'Falta el token o los IDs; sin los dos, Telegram no arranca.'],
  error: ['err', 'No arrancó.', 'El token y los IDs están, pero algún valor no es válido. El motivo exacto está en el log de la app.'],
};

// Origen de cada valor de Telegram: el .env manda sobre lo guardado en la web.
const TG_FROM_ENV = stPill('Desde .env');
const TG_ENV_HELP = 'Definido en el <code>.env</code>, que tiene prioridad. Para gestionarlo desde aquí, quítalo de ahí y reinicia.';

function savedSettingsSection() {
  try {
    const s = localStorage.getItem(SETTINGS_KEY);
    return s in SETTINGS_SECTIONS ? s : 'cuenta';
  } catch (_) { return 'cuenta'; }
}

async function openSettings() {
  showModal('settings-modal');
  showSettingsSection(settingsState.section || savedSettingsSection());
  await loadSettings();
  // El estado de la versión lo guarda el desplegable de la cabecera (upd);
  // se refresca para que la sección Actualizaciones no enseñe uno viejo.
  await refreshUpdate();
  renderSettings();
}

async function loadSettings() {
  const errEl = q('st-error');
  errEl.classList.add('hidden');
  try {
    const res = await fetch('/api/ajustes');
    if (!res.ok) throw new Error(await apiError(res));
    settingsState.data = await res.json();
  } catch (err) {
    errEl.textContent = `No se pudieron cargar los ajustes: ${err.message}`;
    errEl.classList.remove('hidden');
  }
  renderSettings();
}

function showSettingsSection(key) {
  settingsState.section = key;
  try { localStorage.setItem(SETTINGS_KEY, key); } catch (_) {}
  document.querySelectorAll('#settings-modal .st-nav-item').forEach(b => {
    const on = b.dataset.section === key;
    b.classList.toggle('active', on);
    if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  q('st-heading').textContent = SETTINGS_SECTIONS[key].title;
  renderSettings();
}

function renderSettings() {
  const body = q('st-body');
  const d = settingsState.data;
  if (!d) { body.innerHTML = '<p class="st-loading">Cargando…</p>'; return; }
  body.innerHTML = SETTINGS_SECTIONS[settingsState.section].render(d).join('');
}

// ── Ajustes: piezas ────────────────────────────────────────────────────────
// Todo valor que viene del servidor pasa por esc(); los textos fijos no.
function stCard({ icon, title, desc, source, body }) {
  return `<section class="st-card">
    <div class="st-card-head">
      <span class="st-card-icon"><svg viewBox="0 0 24 24" aria-hidden="true">${ST_ICONS[icon]}</svg></span>
      <div><h3 class="st-card-title">${title}</h3>${desc ? `<p class="st-card-desc">${desc}</p>` : ''}</div>
      ${source ? `<span class="st-source">${source}</span>` : ''}
    </div>
    <div class="st-card-body">${body}</div>
  </section>`;
}

function stRow(title, sub, right = '') {
  return `<div class="st-row">
    <div class="st-row-main"><div class="st-row-title">${title}</div>${sub ? `<div class="st-row-sub">${sub}</div>` : ''}</div>
    ${right}
  </div>`;
}

function stValue(v) { return `<span class="st-row-value">${esc(v)}</span>`; }
function stList(rows) { return `<div class="st-list">${rows.join('')}</div>`; }
function stPill(text, level = '') { return `<span class="st-pill ${level}">${text}</span>`; }
function stYesNo(v) { return v ? 'sí' : 'no'; }

function stStep(n, done, title, sub, right = '') {
  const mark = `<span class="st-step ${done ? 'done' : ''}" aria-label="${done ? 'Hecho' : 'Pendiente'}">${done ? '&#x2713;' : n}</span>`;
  return `<div class="st-row">${mark}
    <div class="st-row-main"><div class="st-row-title">${title}</div><div class="st-row-sub">${sub}</div></div>
    ${right}
  </div>`;
}

// ── Ajustes: secciones ─────────────────────────────────────────────────────
function settingsAccount(d) {
  const c = d.cuenta;
  return [
    stCard({
      icon: 'user', title: 'Cuenta',
      desc: 'Administrador único de DockerManager. El usuario se elige en el primer arranque y no se puede cambiar desde aquí.',
      source: 'data/config.json',
      body: stList([
        stRow('Usuario', null, stValue(c.usuario || '—')),
        stRow('Contraseña', 'Se guarda como hash bcrypt. Al cambiarla se avisa por Telegram si la categoría «seguridad» está activa.',
          '<button type="button" onclick="openChangePassword()">Cambiar contraseña</button>'),
      ]),
    }),
    stCard({
      icon: 'lock', title: 'Sesión y acceso',
      desc: 'Duración de la sesión y bloqueo por intentos fallidos de login.',
      source: 'config.ini › [security]',
      body: stList([
        stRow('Duración de la sesión', 'Pasado este tiempo hay que volver a entrar.', stValue(`${c.sesionDias} días`)),
        stRow('Intentos antes de bloquear', 'Logins fallidos seguidos desde la misma IP.', stValue(c.intentosMaximos)),
        stRow('Tiempo de bloqueo', null, stValue(`${c.bloqueoMinutos} min`)),
        stRow('Coste bcrypt', 'Solo afecta a las contraseñas que se guarden a partir de ahora.', stValue(c.costeBcrypt)),
      ]),
    }),
  ];
}

function settingsAlerts(d) {
  const t = d.telegram;
  const [level, label, text] = TELEGRAM_STATUS[t.estado] || ['', esc(t.estado), ''];
  const active = t.estado === 'activo';
  const tokenFromEnv = t.origenToken === 'env';
  const idsFromEnv = t.origenIds === 'env';
  const botLink = t.bot
    ? `<a href="https://t.me/${encodeURIComponent(t.bot)}" target="_blank" rel="noopener noreferrer">@${esc(t.bot)}</a>` : '';

  // Token: nunca llega al navegador. Si ya hay uno guardado, el campo solo
  // aparece al pulsar «Reemplazar»; vacío al guardar = se conserva el actual.
  let tokenSub, tokenRight = '';
  if (tokenFromEnv) {
    tokenSub = TG_ENV_HELP;
    tokenRight = TG_FROM_ENV;
  } else if (t.tokenConfigurado && !settingsState.tgReplaceToken) {
    tokenSub = `${stPill('Configurado', 'ok')} ${botLink ? `Bot ${botLink}.` : ''} El valor no se muestra nunca aquí.`;
    tokenRight = '<button type="button" onclick="settingsReplaceToken()">Reemplazar</button>';
  } else {
    tokenSub = 'Crea un bot con <a href="https://t.me/BotFather" target="_blank" rel="noopener noreferrer">@BotFather</a> (<code>/newbot</code>) y pega aquí el token. Se comprueba con Telegram antes de guardarlo.';
    tokenRight = `<input id="st-tg-token" class="form-input st-input" type="password" autocomplete="off" spellcheck="false"
      placeholder="123456789:AA…" aria-label="Token del bot">`;
  }

  const idsList = t.ids.length ? `Reciben avisos y pueden usar comandos: ${t.ids.map(i => `<code>${esc(i)}</code>`).join(', ')}.` : '';
  const idsSub = t.errorIds ? `<span class="st-pill err">${esc(t.errorIds)}</span>`
    : idsFromEnv ? `${TG_ENV_HELP} ${idsList}`
    : 'Separados por comas. Consigue tu ID con <a href="https://t.me/userinfobot" target="_blank" rel="noopener noreferrer">@userinfobot</a>. Cualquier otro que escriba al bot se ignora.';
  const idsRight = idsFromEnv ? TG_FROM_ENV
    : `<input id="st-tg-ids" class="form-input st-input" type="text" inputmode="numeric" autocomplete="off" spellcheck="false"
        placeholder="123456789, 987654321" value="${esc(t.ids.join(', '))}" aria-label="IDs autorizados">`;

  const editable = !tokenFromEnv || !idsFromEnv;
  const connection = stCard({
    icon: 'send', title: 'Telegram',
    desc: 'Bot para recibir avisos en el móvil y consultar el estado con comandos de solo lectura. Qué avisos llegan se elige en la tarjeta de al lado.',
    source: tokenFromEnv && idsFromEnv ? '.env' : 'data/telegram-config.json',
    body: `
      <div class="st-status"><span class="st-dot ${level}"></span><span><b>${label}</b> ${text}</span></div>
      ${stList([
        stStep(1, t.tokenConfigurado, 'Token del bot', tokenSub, tokenRight),
        stStep(2, t.ids.length > 0, 'IDs autorizados', idsSub, idsRight),
        stStep(3, false, 'Abrir el chat con el bot',
          `Abre el chat con ${botLink || 'tu bot'} y pulsa <b>Iniciar</b>. Un bot no puede escribir primero a nadie: sin este paso los avisos no llegan. Si otra persona le escribe, lo ignora.`),
        stStep(4, false, 'Comprobar', 'Manda un mensaje de prueba a los IDs autorizados, sin depender de las categorías activas.',
          `<button id="st-tg-test" type="button" onclick="sendTelegramTest()" ${active ? '' : 'disabled'}>Enviar prueba</button>`),
      ])}
      ${editable || t.guardadoWeb ? `<div class="st-actions">
        ${editable ? '<button id="st-tg-save" type="button" class="btn-accent" onclick="saveTelegramConnection()">Guardar y aplicar</button>' : ''}
        ${settingsState.tgReplaceToken ? '<button type="button" onclick="settingsReplaceToken(false)">Cancelar</button>' : ''}
        ${t.guardadoWeb ? '<button type="button" onclick="resetTelegramConfig()">Borrar configuración guardada</button>' : ''}
      </div>` : ''}`,
  });

  const r = t.rangos || {};
  const num = (id, key, value) => {
    const [min, max] = r[key] || [];
    return `<input id="${id}" class="form-input st-num" type="number" step="1" min="${esc(min)}" max="${esc(max)}" value="${esc(value)}">`;
  };
  const categories = stCard({
    icon: 'bell', title: 'Qué se avisa',
    desc: 'Categorías activas y umbrales de los avisos. Lo que guardes aquí tiene prioridad sobre config.ini.',
    source: t.guardadoWeb ? 'data/telegram-config.json' : 'config.ini › [telegram]',
    body: `
      ${t.errorCategorias ? `<div class="st-help err">${esc(t.errorCategorias)}</div>` : ''}
      ${stList(t.categorias.map(c => stRow(esc(c.id), esc(c.descripcion),
        `<input class="st-check" type="checkbox" data-tg-cat="${esc(c.id)}" ${c.activa ? 'checked' : ''} aria-label="Avisar de ${esc(c.id)}">`)))}
      ${stList([
        stRow('Disco lleno', 'Porcentaje de uso a partir del que se avisa.',
          `<span class="st-inline">${num('st-tg-disco', 'discoAvisoPct', t.discoAvisoPct)} %</span>`),
        stRow('Bucle de reinicios', 'Caídas de un mismo contenedor dentro de la ventana.',
          `<span class="st-inline">${num('st-tg-reinicios', 'reiniciosBucle', t.reiniciosBucle)} en
           ${num('st-tg-ventana', 'ventanaBucleMinutos', t.ventanaBucleMinutos)} min</span>`),
        stRow('Comprobación de recursos', 'Cada cuánto se miran temperatura, disco y versión nueva.',
          `<span class="st-inline">${num('st-tg-intervalo', 'intervaloSegundos', t.intervaloSegundos)} s</span>`),
        stRow('Timeout de la API', null,
          `<span class="st-inline">${num('st-tg-timeout', 'timeoutSegundos', t.timeoutSegundos)} s</span>`),
      ])}
      <div class="st-actions">
        <button id="st-tg-alerts-save" type="button" class="btn-accent" onclick="saveTelegramAlerts()">Guardar y aplicar</button>
      </div>`,
  });
  return [connection, categories];
}

function settingsUpdates(d) {
  const a = d.actualizacion;
  const u = upd.last;
  let tag = stPill('Sin comprobar');
  if (!a.comprobar) tag = stPill('Comprobación desactivada');
  else if (upd.hasNew === true) tag = stPill('Hay actualización', 'warn');
  else if (upd.hasNew === false) tag = stPill('Al día', 'ok');

  const watcher = !u ? stValue('—')
    : u.vigilanteVisto ? stPill('Activo', 'ok') : stPill('No da señales', 'err');

  return [
    stCard({
      icon: 'tag', title: 'Versión',
      desc: 'La app solo comprueba y deja la petición; quien actualiza es el vigilante instalado en el host.',
      body: `
        ${stList([
          stRow('Instalada', null, stValue(d.version)),
          stRow('Publicada', `En la rama <code>${esc(a.rama)}</code> de GitHub.`, `${tag} ${stValue(upd.published || '—')}`),
          stRow('Vigilante del host', u && !u.vigilanteVisto
            ? 'Instálalo una vez con <code>sudo ./tools/actualizador/instalar-vigilante.sh</code>.' : null, watcher),
        ])}
        <div class="st-actions">
          <button id="st-upd-check" type="button" onclick="settingsCheckUpdate()" ${a.comprobar ? '' : 'disabled'}>Comprobar ahora</button>
          <button type="button" class="btn-accent" onclick="requestUpdate()">Actualizar…</button>
        </div>`,
    }),
    stCard({
      icon: 'git', title: 'Origen',
      desc: 'De dónde se lee la versión publicada.',
      source: 'config.ini › [actualizacion]',
      body: stList([
        stRow('Comprobar versiones', null, stValue(stYesNo(a.comprobar))),
        stRow('Repositorio', null, stValue(a.repositorio)),
        stRow('Rama', null, stValue(a.rama)),
        stRow('Caché de la respuesta', '«Comprobar ahora» se la salta.', stValue(`${a.cacheHoras} h`)),
        stRow('Timeout', null, stValue(`${a.timeoutSegundos} s`)),
      ]),
    }),
  ];
}

function settingsServer(d) {
  const s = d.servidor, l = d.limpieza;
  const exposed = s.bindHost === '0.0.0.0';
  return [
    stCard({
      icon: 'server', title: 'Servidor web',
      desc: 'Dónde escucha DockerManager. Las variables PORT y HOST del entorno tienen prioridad sobre config.ini.',
      source: 'config.ini › [server]',
      body: stList([
        stRow('Puerto', null, stValue(s.puerto)),
        stRow('Dirección', exposed ? 'Accesible desde cualquier interfaz de red.' : null, stValue(s.bindHost)),
      ]),
    }),
    stCard({
      icon: 'temp', title: 'Panel Servidor',
      desc: 'Umbrales de color de la temperatura cuando el sensor no publica los suyos. La crítica también dispara el aviso de recursos.',
      source: 'config.ini › [servidor]',
      body: stList([
        stRow('Temperatura de aviso', null, stValue(`${s.temperaturaAviso} °C`)),
        stRow('Temperatura crítica', null, stValue(`${s.temperaturaCritica} °C`)),
        stRow('Ruta medida como disco', null, stValue(s.rutaDisco)),
      ]),
    }),
    stCard({
      icon: 'trash', title: 'Limpieza',
      desc: 'Imágenes y volúmenes que no usa ningún contenedor, ni siquiera uno parado.',
      source: 'config.ini › [limpieza]',
      body: `
        ${stList([
          stRow('Antigüedad propuesta', 'Valor inicial de la ventana «Limpiar». 0 = cualquier antigüedad.', stValue(`${l.diasPorDefecto} días`)),
          stRow('Antigüedad máxima', null, stValue(`${l.diasMaximos} días`)),
        ])}
        <div class="st-actions"><button type="button" onclick="settingsOpenCleanup()">Abrir Limpiar</button></div>`,
    }),
  ];
}

// ── Ajustes: acciones ──────────────────────────────────────────────────────
async function sendTelegramTest() {
  const btn = q('st-tg-test');
  btn.disabled = true;
  try {
    const res = await fetch('/api/ajustes/telegram/prueba', { method: 'POST' });
    if (!res.ok) throw new Error(await apiError(res));
    toast('Prueba enviada: debería llegar en unos segundos', 'success');
  } catch (err) {
    toast(`Telegram: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

function settingsReplaceToken(on = true) {
  settingsState.tgReplaceToken = on;
  renderSettings();
  if (on) q('st-tg-token')?.focus();
}

async function putTelegramSettings(body, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/ajustes/telegram', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await apiError(res));
    const r = await res.json();
    settingsState.tgReplaceToken = false;
    toast(r.activo ? 'Telegram guardado y aplicado' : 'Guardado. Telegram sigue inactivo: falta el token o los IDs', r.activo ? 'success' : 'info');
    await loadSettings();
  } catch (err) {
    toast(`Telegram: ${err.message}`, 'error');
    if (btn) btn.disabled = false;
  }
}

function saveTelegramConnection() {
  const body = {};
  const token = q('st-tg-token')?.value.trim();
  if (token) body.token = token;
  const ids = q('st-tg-ids');
  if (ids) body.ids = ids.value.trim();
  if (!Object.keys(body).length) { toast('No hay nada que guardar', 'info'); return; }
  putTelegramSettings(body, q('st-tg-save'));
}

function saveTelegramAlerts() {
  const body = {
    categorias: [...document.querySelectorAll('#st-body [data-tg-cat]')].filter(c => c.checked).map(c => c.dataset.tgCat),
  };
  const fields = {
    discoAvisoPct: 'st-tg-disco', reiniciosBucle: 'st-tg-reinicios', ventanaBucleMinutos: 'st-tg-ventana',
    intervaloSegundos: 'st-tg-intervalo', timeoutSegundos: 'st-tg-timeout',
  };
  for (const [key, id] of Object.entries(fields)) {
    const value = Number(q(id).value);
    if (!Number.isInteger(value)) { toast('Los umbrales deben ser números enteros', 'error'); q(id).focus(); return; }
    body[key] = value;
  }
  putTelegramSettings(body, q('st-tg-alerts-save'));
}

async function resetTelegramConfig() {
  if (!confirm('¿Borrar la configuración de Telegram guardada desde la web? Volverán a aplicarse el .env y config.ini.')) return;
  try {
    const res = await fetch('/api/ajustes/telegram', { method: 'DELETE' });
    if (!res.ok) throw new Error(await apiError(res));
    settingsState.tgReplaceToken = false;
    toast('Configuración de Telegram borrada', 'success');
    await loadSettings();
  } catch (err) {
    toast(`Telegram: ${err.message}`, 'error');
  }
}

async function settingsCheckUpdate() {
  q('st-upd-check').disabled = true;
  await checkUpdate();
  renderSettings();
}

function settingsOpenCleanup() {
  closeModal('settings-modal');
  openCleanup();
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
async function apiError(res) {
  try { const b = await res.json(); return b.detail || `HTTP ${res.status}`; }
  catch (_) { return `HTTP ${res.status}`; }
}

function loadCollapsed() {
  try { return JSON.parse(localStorage.getItem('dm-collapsed') || '{}'); } catch (_) { return {}; }
}

function toggleGroup(key) {
  collapsed[key] = !collapsed[key];
  try { localStorage.setItem('dm-collapsed', JSON.stringify(collapsed)); } catch (_) {}
  renderContainers(data.containers);
  renderImages(data.images);
  renderVolumes(data.volumes);
}
async function loadAll() {
  await Promise.all([loadContainers(), loadImages(), loadVolumes(), loadNetworks()]);
}

async function loadContainers() {
  try {
    const res = await fetch('/api/containers');
    if (!res.ok) throw new Error(await apiError(res));
    const list = await res.json();
    checkContainerAlerts(list);
    renderContainers(list);
    // Los grupos de imágenes y volúmenes dependen de los contenedores
    if (data.images.length) renderImages(data.images);
    if (data.volumes.length) renderVolumes(data.volumes);
    renderSummary();
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
    if (!res.ok) throw new Error(await apiError(res));
    renderImages(await res.json());
    renderSummary();
  } catch (err) {
    q('images-body').innerHTML =
      `<tr><td colspan="6" class="empty" style="color:var(--red)">${esc(err.message)}</td></tr>`;
  }
}

async function loadVolumes() {
  try {
    const res = await fetch('/api/volumes');
    if (!res.ok) throw new Error(await apiError(res));
    renderVolumes(await res.json());
    renderSummary();
  } catch (err) {
    q('volumes-body').innerHTML =
      `<tr><td colspan="6" class="empty" style="color:var(--red)">${esc(err.message)}</td></tr>`;
  }
}

async function loadNetworks() {
  try {
    const res = await fetch('/api/networks');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderNetworks(await res.json());
    renderSummary();
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
      `<span class="hstat">Disco <b>${d.disk_pct}%</b> <small>${fmtBytes(d.disk_used)}/${fmtBytes(d.disk_total)}</small></span>` +
      (d.cpu_temp != null
        ? `<span class="hstat">Temp <b class="${levelOf(d.cpu_temp, d.temp_warn, d.temp_crit)}">${d.cpu_temp.toFixed(0)}°C</b></span>`
        : '');
  } catch (_) {}
}

// ── Rendering ──────────────────────────────────────────────────────────────
function renderSummary() {
  const c = data.containers;
  const running = c.filter(x => x.status === 'running').length;
  const unusedImgs = data.images.filter(i => !i.used_by.length).length;
  const unusedVols = data.volumes.filter(v => !v.used_by.length).length;
  const card = (label, value, sub, cls = '') =>
    `<div class="sum-card ${cls}"><div class="sum-val">${value}</div><div class="sum-label">${label}</div><div class="sum-sub">${sub}</div></div>`;
  q('summary').innerHTML =
    card('Contenedores', `${running}<small>/${c.length}</small>`, 'corriendo / total', running === c.length ? '' : 'warn') +
    card('Stacks', new Set(c.map(x => x.project).filter(Boolean)).size, 'proyectos compose') +
    card('Imágenes', data.images.length, `${unusedImgs} sin usar`) +
    card('Volúmenes', data.volumes.length, `${unusedVols} sin usar`) +
    card('Redes', data.networks.length, 'definidas');
}

// Cabecera de grupo colapsable, común a las tres tablas
function groupRow(key, title, meta, cols, extra = '') {
  const closed = !!collapsed[key];
  return `<tr class="group-row" data-key="${esc(key)}"><td colspan="${cols}">
    <div class="group-head">
      <button class="group-toggle" data-key="${esc(key)}" onclick="toggleGroup(this.dataset.key)" aria-expanded="${!closed}" title="${closed ? 'Expandir' : 'Contraer'}">${closed ? '&#9656;' : '&#9662;'}</button>
      <span class="group-title">${esc(title)}</span>
      <span class="group-meta">${meta}</span>
      <span class="group-extra">${extra}</span>
    </div></td></tr>`;
}

// Agrupación: el stack de compose si existe; si no, el primer segmento del nombre
// ("PorfolioManager-caddy" → "porfoliomanager"). Así las distintas versiones y
// servicios de una misma app quedan juntos aunque no se lanzaran con compose.
const groupTitles = new Map();
function nameToken(name) { return String(name).split(/[-_.]/)[0]; }
function groupKeyOf(x) { return `grp:${(x.project || nameToken(x.name)).toLowerCase()}`; }
function rebuildGroupTitles() {
  groupTitles.clear();
  [...data.containers].sort((a, b) => a.name.localeCompare(b.name)).forEach(c => {
    const k = groupKeyOf(c);
    if (!groupTitles.has(k)) groupTitles.set(k, c.project || nameToken(c.name));
  });
}
function groupTitleOf(key, fallback) { return groupTitles.get(key) || fallback || key.slice(4); }

// Grupo de un recurso sin contenedores: por nombre contra los grupos conocidos
// (el más largo gana: "porfoliomanager_caddy_data" → porfoliomanager)
function matchGroupByName(name) {
  const n = String(name).toLowerCase();
  let best = null;
  for (const k of groupTitles.keys()) {
    const stem = k.slice(4);
    if (n.startsWith(stem) && (!best || stem.length > best.length)) best = stem;
  }
  return best ? `grp:${best}` : null;
}
function imageRepo(img) {
  const t = img.tags[0];
  return t ? t.replace(/:[^:/]*$/, '').split('/').pop() : '';
}
function volumeOwner(v) {
  const label = v.labels && v.labels['com.docker.compose.project'];
  return label ? `grp:${label.toLowerCase()}` : matchGroupByName(v.name);
}

function userChips(users) {
  if (!users.length) return '<span class="dim">—</span>';
  return users.map(u =>
    `<span class="port-tag chip-${u.status === 'running' ? 'up' : 'down'}" title="${esc(u.status)}">${esc(u.name)}</span>`
  ).join('');
}

// Agrupa por el primer usuario del recurso; si nadie lo usa, por nombre; si no, "Sin usar"
function groupByOwner(items, fallbackKey) {
  const groups = new Map();
  for (const it of items) {
    const first = it.used_by[0];
    let key = first ? groupKeyOf(first) : fallbackKey(it);
    const unused = !key;
    if (!key) key = 'unused';
    if (!groups.has(key)) {
      groups.set(key, { key, unused,
        title: unused ? 'Sin usar' : groupTitleOf(key, first && (first.project || nameToken(first.name))),
        items: [] });
    }
    groups.get(key).items.push(it);
  }
  return [...groups.values()].sort((a, b) => (a.unused - b.unused) || a.title.localeCompare(b.title));
}

function plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

function containerRow(c) {
  const isRunning = c.status === 'running';
  const isPaused = c.status === 'paused';
  const cls = STATUS_CLASS[c.status] || 's-other';
  const label = STATUS_LABELS[c.status] || c.status;
  const health = c.health
    ? ` <span class="health health-${esc(c.health)}" title="Healthcheck: ${esc(c.health)}">${c.health === 'healthy' ? '&#10003;' : c.health === 'unhealthy' ? '&#10007;' : '&#8230;'}</span>` : '';

  // Docker publica el mismo puerto una vez por familia IP (v4 y v6): deduplicamos
  const portEntries = [...new Map(
    Object.entries(c.ports || {}).flatMap(([k, v]) =>
      v.map(p => [`${p}:${k}`, { host: p, container: k.split('/')[0] }])
    )
  ).values()];
  const ports = portEntries.length
    ? portEntries.map(p =>
        `<a class="port-tag port-link" href="http://${location.hostname}:${esc(p.host)}" target="_blank" rel="noopener" title="Abrir puerto ${esc(p.host)}">${esc(p.host)}&#8594;${esc(p.container)}</a>`
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
    acts = [op(3, 'btn-yellow', '&#x25B6;', 'unpause', 'Reanudar'), logBtn, inspBtn, rmBtn];
  } else if (isRunning) {
    acts = [op(1, 'btn-red',    '&#x23F9;', 'stop',    'Detener'),
            op(2, 'btn-yellow', '&#x21BA;', 'restart', 'Reiniciar'),
            op(3, 'btn-yellow', '&#x23F8;', 'pause',   'Pausar'),
            logBtn, statsBtn, inspBtn, procsBtn, termBtn];
  } else {
    acts = [op(1, 'btn-green', '&#x25B6;', 'start', 'Iniciar'), logBtn, inspBtn, rmBtn];
  }
  // El separador final solo si hay algo detrás de él
  acts = acts.join('') + sep(4) + (acts.includes(rmBtn) ? sep(10) : '');

  const statsCell = isRunning
    ? `<span class="mini-stats" id="mstat-${c.id}">—</span>`
    : `<span class="dim">—</span>`;
  const service = c.service && c.service !== c.name
    ? `<span class="svc-tag" title="Servicio compose">${esc(c.service)}</span>` : '';

  return `<tr data-name="${esc((c.name + ' ' + (c.project || '') + ' ' + c.image).toLowerCase())}" data-state="${isRunning ? 'running' : 'stopped'}" data-group="${esc(containerGroup(c))}">
    <td title="${esc(c.name)}"><span class="mono cell-name">${esc(c.name)}</span>${service}</td>
    <td title="${esc(c.image)}"><span class="mono dim">${esc(c.image)}</span></td>
    <td><span class="status-dot ${cls}">${label}</span>${health}</td>
    <td>${ports}</td>
    <td>${statsCell}</td>
    <td class="col-actions"><div class="actions actions-grid">${acts}</div></td>
  </tr>`;
}

function containerGroup(c) { return groupKeyOf(c); }

function renderContainers(list) {
  data.containers = list;
  q('container-count').textContent = `${list.length} total`;
  if (!list.length) {
    q('containers-body').innerHTML = '<tr><td colspan="6" class="empty">Sin contenedores</td></tr>';
    return;
  }

  rebuildGroupTitles();
  const groups = new Map();
  [...list].sort((a, b) => a.name.localeCompare(b.name)).forEach(c => {
    const k = groupKeyOf(c);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(c);
  });

  const meta = items => {
    const up = items.filter(c => c.status === 'running').length;
    const cls = up === items.length ? 's-running' : up ? 's-paused' : 's-exited';
    return `<span class="status-dot ${cls}">${up}/${items.length} corriendo</span>`;
  };

  let html = '';
  [...groups.keys()].sort((a, b) => groupTitleOf(a).localeCompare(groupTitleOf(b))).forEach(key => {
    const items = groups.get(key);
    const k = esc(key);
    const bulk = items.length < 2 ? '' :
      `<button class="btn-xs btn-green" onclick="groupAction(this.dataset.k,'start')" data-k="${k}" title="Iniciar todos">&#x25B6; Iniciar</button>` +
      `<button class="btn-xs btn-yellow" onclick="groupAction(this.dataset.k,'restart')" data-k="${k}" title="Reiniciar todos">&#x21BA; Reiniciar</button>` +
      `<button class="btn-xs btn-red" onclick="groupAction(this.dataset.k,'stop')" data-k="${k}" title="Detener todos">&#x23F9; Detener</button>`;
    html += groupRow(key, groupTitleOf(key), `${plural(items.length, 'contenedor', 'contenedores')} · ${meta(items)}`, 6, bulk);
    if (!collapsed[key]) html += items.map(containerRow).join('');
  });
  q('containers-body').innerHTML = html;

  list.filter(c => c.status === 'running' && !collapsed[containerGroup(c)])
      .forEach(c => loadMiniStats(c.id));
  filterContainers();
}

async function groupAction(key, op) {
  const verb = { start: 'iniciar', stop: 'detener', restart: 'reiniciar' }[op];
  const title = groupTitleOf(key);
  const targets = data.containers.filter(c => groupKeyOf(c) === key &&
    (op === 'start' ? c.status !== 'running' : c.status === 'running'));
  if (!targets.length) { toast(`${title}: nada que ${verb}`, 'info'); return; }
  if (op !== 'start' && !confirm(`¿${verb[0].toUpperCase() + verb.slice(1)} ${plural(targets.length, 'contenedor', 'contenedores')} de "${title}"?`)) return;
  toast(`${title}: ${verb}…`, 'info');
  const results = await Promise.allSettled(targets.map(async c => {
    const res = await fetch(`/api/containers/${c.id}/${op}`, { method: 'POST' });
    if (!res.ok) throw new Error(`${c.name}: ${await apiError(res)}`);
  }));
  const failed = results.filter(r => r.status === 'rejected');
  if (failed.length) toast(`Error: ${failed.map(f => f.reason.message).join('; ')}`, 'error');
  else toast(`${title}: completado`, 'success');
  setTimeout(loadContainers, 800);
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
  const text = (q('filter-input').value || '').toLowerCase();
  const state = q('status-filter').value;
  const filtering = !!(text || state);
  const visible = new Set();
  document.querySelectorAll('#containers-body tr[data-name]').forEach(row => {
    const show = row.dataset.name.includes(text) && (!state || row.dataset.state === state);
    row.style.display = show ? '' : 'none';
    if (show) visible.add(row.dataset.group);
  });
  // Filtrando, se ocultan las cabeceras de grupos sin coincidencias
  document.querySelectorAll('#containers-body tr.group-row').forEach(row => {
    row.style.display = !filtering || visible.has(row.dataset.key) ? '' : 'none';
  });
}

function renderImages(list) {
  data.images = list;
  const unused = list.filter(i => !i.used_by.length).length;
  q('image-count').textContent = `${list.length} total` + (unused ? ` · ${unused} sin usar` : '');
  if (!list.length) {
    q('images-body').innerHTML = '<tr><td colspan="6" class="empty">Sin imágenes</td></tr>';
    return;
  }
  const row = img => {
    const tags = img.tags.length
      ? img.tags.map(t => `<span class="port-tag">${esc(t)}</span>`).join(' ')
      : '<span class="dim">sin tag</span>';
    return `<tr>
      <td>${tags}</td>
      <td><span class="mono dim">${esc(img.id)}</span></td>
      <td>${img.size_mb} MB</td>
      <td>${userChips(img.used_by)}</td>
      <td>${img.created}</td>
      <td class="col-actions"><div class="actions">
        <button class="icon-btn" title="Historial" aria-label="Historial" onclick="openHistory('${esc(img.id)}','${esc(img.tags[0] || img.id)}')">&#x1F4DC;</button>
        <button class="icon-btn btn-red" title="Eliminar" aria-label="Eliminar" onclick="removeImage('${esc(img.id)}')">&#x1F5D1;</button>
      </div></td>
    </tr>`;
  };
  q('images-body').innerHTML = groupByOwner(list, img => matchGroupByName(imageRepo(img))).map(g => {
    const key = `img:${g.key}`;
    const mb = g.items.reduce((t, i) => t + i.size_mb, 0).toFixed(1);
    const old = g.items.filter(i => !i.used_by.length);
    const bulk = old.length
      ? `<button class="btn-xs btn-red" onclick="removeGroupImages(this.dataset.k)" data-k="${esc(g.key)}" title="Borrar las imágenes del grupo que no usa ningún contenedor">&#x1F5D1; Borrar sin usar (${old.length})</button>`
      : '';
    return groupRow(key, g.title, `${plural(g.items.length, 'imagen', 'imágenes')} · ${mb} MB`, 6, bulk) +
      (collapsed[key] ? '' : g.items.map(row).join(''));
  }).join('');
}

// Borra las imágenes sin contenedores de un grupo (versiones antiguas de una app).
// En serie: borrados paralelos de imágenes que comparten capas chocan en Docker.
async function removeGroupImages(groupKey) {
  const group = groupByOwner(data.images, img => matchGroupByName(imageRepo(img)))
    .find(g => g.key === groupKey);
  const targets = group ? group.items.filter(i => !i.used_by.length) : [];
  if (!targets.length) { toast('Nada que borrar', 'info'); return; }
  const mb = targets.reduce((t, i) => t + i.size_mb, 0).toFixed(1);
  if (!confirm(`¿Borrar ${plural(targets.length, 'imagen', 'imágenes')} sin usar de "${group.title}" (${mb} MB)?`)) return;
  toast(`${group.title}: borrando…`, 'info');
  const errors = [];
  for (const img of targets) {
    // Con varios tags Docker exige force para borrar por ID; ya está comprobado que no la usa nadie
    const force = img.tags.length > 1 ? '?force=true' : '';
    try {
      const res = await fetch(`/api/images/${img.id}${force}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(await apiError(res));
    } catch (err) {
      errors.push(`${img.tags[0] || img.id}: ${err.message}`);
    }
  }
  const done = targets.length - errors.length;
  if (errors.length) toast(`${plural(done, 'imagen borrada', 'imágenes borradas')}; errores: ${errors.join('; ')}`, 'error');
  else toast(`${group.title}: ${plural(done, 'imagen borrada', 'imágenes borradas')}`, 'success');
  setTimeout(loadImages, 600);
}

function renderVolumes(list) {
  data.volumes = list;
  const unused = list.filter(v => !v.used_by.length).length;
  q('volume-count').textContent = `${list.length} total` + (unused ? ` · ${unused} sin usar` : '');
  if (!list.length) {
    q('volumes-body').innerHTML = '<tr><td colspan="6" class="empty">Sin volúmenes</td></tr>';
    return;
  }
  const row = v => `<tr>
    <td title="${esc(v.name)}"><span class="mono">${esc(v.name)}</span></td>
    <td><span class="dim">${esc(v.driver)}</span></td>
    <td title="${esc(v.mountpoint)}"><span class="mono dim" style="font-size:11px">${esc(v.mountpoint)}</span></td>
    <td>${userChips(v.used_by)}</td>
    <td>${v.created || '—'}</td>
    <td class="col-actions"><div class="actions">
      <button class="icon-btn btn-red" title="Eliminar" aria-label="Eliminar" onclick="removeVolume('${esc(v.name)}')">&#x1F5D1;</button>
    </div></td>
  </tr>`;
  q('volumes-body').innerHTML = groupByOwner(list, volumeOwner).map(g => {
    const key = `vol:${g.key}`;
    return groupRow(key, g.title, `${plural(g.items.length, 'volumen', 'volúmenes')}`, 6) +
      (collapsed[key] ? '' : g.items.map(row).join(''));
  }).join('');
}

function renderNetworks(list) {
  data.networks = list;
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
  if (!confirm('¿Eliminar esta imagen? Si la usa algún contenedor, Docker la rechazará.')) return;
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
  if (!confirm(`¿Eliminar volumen "${name}"? Los datos se perderán de forma permanente.`)) return;
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
        ${kv('DockerManager', 'v' + d.app_version)} ${kv('Docker versión', d.docker_version)} ${kv('API versión', d.api_version)}
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

// ── Servidor ───────────────────────────────────────────────────────────────
// Métricas del host (/api/system/metrics). Solo se consultan con el modal
// abierto: el resto del tiempo basta con la barra de la cabecera.
const SERVER_REFRESH_MS = 3000;
const SERVER_PCT_WARN = 70;
const SERVER_PCT_CRIT = 90;
const serverState = { timer: null, chart: null, buf: { labels: [], cpu: [], mem: [], temp: [] } };

function openServer() {
  closeServer();
  serverState.buf = { labels: [], cpu: [], mem: [], temp: [] };
  q('srv-cards').innerHTML = '<p class="dim">Cargando...</p>';
  ['srv-cores', 'srv-temps', 'srv-mem', 'srv-sys'].forEach(id => { q(id).innerHTML = ''; });
  q('srv-updated').textContent = '';
  showModal('server-modal');
  initServerChart();
  serverState.timer = setInterval(loadServerMetrics, SERVER_REFRESH_MS);
  loadServerMetrics();
}

function closeServer() {
  if (serverState.timer) { clearInterval(serverState.timer); serverState.timer = null; }
  if (serverState.chart) { serverState.chart.destroy(); serverState.chart = null; }
}

async function loadServerMetrics() {
  try {
    const res = await fetch('/api/system/metrics');
    if (!res.ok) throw new Error(await apiError(res));
    const d = await res.json();
    // La respuesta puede llegar después de cerrar el modal.
    if (!serverState.timer) return;
    renderServer(d);
    pushServerChart(d);
    q('srv-updated').textContent = 'Actualizado ' + clockTime();
  } catch (err) {
    q('srv-updated').textContent = 'Error: ' + err.message;
  }
}

function levelOf(val, warn, crit) {
  if (val == null) return '';
  return val >= crit ? 'crit' : val >= warn ? 'warn' : '';
}

function srvBar(pct, level) {
  const cls = level === 'crit' ? 'bar-danger' : level === 'warn' ? 'bar-warn' : 'bar-cpu';
  return `<div class="bar-wrap"><div class="bar ${cls}" style="width:${Math.min(100, Math.max(0, pct))}%"></div></div>`;
}

function srvCard(label, value, sub, level = '') {
  return `<div class="sum-card srv-card ${level}"><div class="sum-val">${value}</div>` +
    `<div class="sum-label">${label}</div><div class="sum-sub">${sub}</div></div>`;
}

function renderServer(d) {
  const { cpu, memory: mem, swap, disk } = d;
  const temp = d.cpu_temp;
  const load = cpu.load_avg;
  const cores = cpu.cores_logical || 1;
  const freq = cpu.freq_mhz ? ` · ${cpu.freq_mhz} MHz` : '';
  q('srv-cards').innerHTML =
    srvCard('CPU', `${cpu.pct.toFixed(1)}<small>%</small>`, `${plural(cores, 'núcleo', 'núcleos')}${freq}`,
      levelOf(cpu.pct, SERVER_PCT_WARN, SERVER_PCT_CRIT)) +
    // Carga por encima del número de núcleos = hay procesos esperando CPU.
    srvCard('Carga', load ? load[0].toFixed(2) : '—', load ? `5m ${load[1].toFixed(2)} · 15m ${load[2].toFixed(2)}` : 'no disponible',
      load ? levelOf(load[0] / cores * 100, SERVER_PCT_WARN, 100) : '') +
    srvCard('RAM', `${mem.pct.toFixed(1)}<small>%</small>`, `${fmtBytes(mem.used)} / ${fmtBytes(mem.total)}`,
      levelOf(mem.pct, SERVER_PCT_WARN, SERVER_PCT_CRIT)) +
    srvCard('Swap', swap.total ? `${swap.pct.toFixed(1)}<small>%</small>` : '—',
      swap.total ? `${fmtBytes(swap.used)} / ${fmtBytes(swap.total)}` : 'sin swap',
      swap.total ? levelOf(swap.pct, SERVER_PCT_WARN, SERVER_PCT_CRIT) : '') +
    srvCard('Disco', `${disk.pct.toFixed(1)}<small>%</small>`, `${fmtBytes(disk.used)} / ${fmtBytes(disk.total)}`,
      levelOf(disk.pct, SERVER_PCT_WARN, SERVER_PCT_CRIT)) +
    srvCard('Temp. CPU', temp != null ? `${temp.toFixed(0)}<small>°C</small>` : '—',
      temp != null ? 'sensor más caliente' : 'sin sensor de CPU',
      levelOf(temp, d.thresholds.temp_warn, d.thresholds.temp_crit)) +
    srvCard('Encendido', fmtUptime(d.uptime_seconds), 'desde ' + new Date(d.boot_time).toLocaleString('es'));

  q('srv-cores').innerHTML = cpu.per_core.map((pct, i) =>
    `<div class="srv-row"><span class="srv-row-k">#${i}</span>` +
    `${srvBar(pct, levelOf(pct, SERVER_PCT_WARN, SERVER_PCT_CRIT))}<span class="srv-row-v">${pct.toFixed(0)}%</span></div>`
  ).join('');

  q('srv-temps').innerHTML = d.temperatures.length
    ? d.temperatures.map(t =>
        `<div class="srv-row" title="${esc(t.sensor)} · aviso ${t.high}°C · crítica ${t.critical}°C">` +
        `<span class="srv-row-k">${esc(t.label)}</span>` +
        `${srvBar(t.current / t.critical * 100, levelOf(t.current, t.high, t.critical))}` +
        `<span class="srv-row-v">${t.current.toFixed(1)}°C</span></div>`).join('') +
      d.fans.map(f => kv(f.label, `${f.rpm} RPM`)).join('')
    : '<p class="dim">El hardware no publica sensores de temperatura (habitual en máquinas virtuales).</p>';

  q('srv-mem').innerHTML =
    kv('Total', fmtBytes(mem.total)) + kv('En uso', fmtBytes(mem.used)) +
    kv('Disponible', fmtBytes(mem.available)) +
    (mem.cached != null ? kv('Caché', fmtBytes(mem.cached)) : '') +
    (mem.buffers != null ? kv('Buffers', fmtBytes(mem.buffers)) : '') +
    kv('Swap', swap.total ? `${fmtBytes(swap.used)} / ${fmtBytes(swap.total)}` : 'sin swap');

  q('srv-sys').innerHTML =
    kv('Disco medido', disk.path) + kv('Libre', fmtBytes(disk.free)) +
    kv('Lectura', fmtRate(disk.read_bps)) + kv('Escritura', fmtRate(disk.write_bps)) +
    kv('Núcleos', `${cores} lógicos${cpu.cores_physical ? ` · ${cpu.cores_physical} físicos` : ''}`) +
    (cpu.freq_mhz ? kv('Frecuencia', `${cpu.freq_mhz} MHz${cpu.freq_max_mhz ? ` (máx. ${cpu.freq_max_mhz})` : ''}`) : '') +
    (load ? kv('Carga 1/5/15', load.map(v => v.toFixed(2)).join(' · ')) : '');
}

function initServerChart() {
  const b = serverState.buf;
  const line = (label, data, color, fill) => ({
    label, data, borderColor: color, backgroundColor: fill, fill: true,
    tension: 0.4, pointRadius: 0, borderWidth: 2, spanGaps: true,
  });
  serverState.chart = new Chart(q('srv-chart').getContext('2d'), {
    type: 'line',
    data: {
      labels: b.labels,
      datasets: [
        line('CPU %', b.cpu, '#58a6ff', 'rgba(88,166,255,0.08)'),
        line('RAM %', b.mem, '#3fb950', 'rgba(63,185,80,0.08)'),
        line('Temp. CPU °C', b.temp, '#f85149', 'rgba(248,81,73,0.05)'),
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 0 },
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { display: false },
        // Porcentajes y grados comparten eje: las temperaturas de trabajo
        // caen dentro de 0-100 y así no hace falta un segundo eje.
        y: { min: 0, max: 100, ticks: { color: '#8b949e', font: { size: 11 } }, grid: { color: '#21262d' } },
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

function pushServerChart(d) {
  const b = serverState.buf;
  b.labels.push(clockTime()); b.cpu.push(d.cpu.pct); b.mem.push(d.memory.pct); b.temp.push(d.cpu_temp);
  if (b.labels.length > MAX_CHART_PTS) { b.labels.shift(); b.cpu.shift(); b.mem.shift(); b.temp.shift(); }
  if (serverState.chart) serverState.chart.update('none');
}

function clockTime() {
  return new Date().toLocaleTimeString('es', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function fmtRate(bps) { return bps == null ? '—' : fmtBytes(bps) + '/s'; }

function fmtUptime(secs) {
  const days = Math.floor(secs / 86400);
  const hours = Math.floor(secs % 86400 / 3600);
  const mins = Math.floor(secs % 3600 / 60);
  return days ? `${days}<small>d</small> ${hours}<small>h</small>` : `${hours}<small>h</small> ${mins}<small>m</small>`;
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

// ── Limpieza selectiva ─────────────────────────────────────────────────────
// Vista previa de imágenes y volúmenes en desuso con cierta antigüedad. El
// servidor recalcula la lista al borrar, así que lo mostrado es orientativo.
let cleanupData = null;

async function openCleanup() {
  cleanupData = null;
  q('cleanup-days').value = '';
  showModal('cleanup-modal');
  await loadCleanup();
}

async function loadCleanup() {
  const days = q('cleanup-days').value.trim();
  q('cleanup-list').innerHTML = '<span class="dim">Buscando...</span>';
  q('cleanup-run-btn').disabled = true;
  try {
    const res = await fetch('/api/limpieza' + (days ? `?dias=${encodeURIComponent(days)}` : ''));
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || JSON.stringify(d));
    cleanupData = d;
    q('cleanup-days').value = d.dias;
    renderCleanup();
  } catch (err) {
    cleanupData = null;
    q('cleanup-list').innerHTML = `<span style="color:var(--red)">Error: ${esc(err.message)}</span>`;
  }
}

function cleanupTypes() {
  const types = [];
  if (q('cleanup-images').checked) types.push('imagenes');
  if (q('cleanup-volumes').checked) types.push('volumenes');
  return types;
}

function renderCleanup() {
  if (!cleanupData) return;
  const types = cleanupTypes();
  const imgs = types.includes('imagenes') ? cleanupData.imagenes : [];
  const vols = types.includes('volumenes') ? cleanupData.volumenes : [];
  const mb = imgs.reduce((t, i) => t + i.size_mb, 0).toFixed(1);
  const imgRows = imgs.map(i => `<tr>
    <td>${i.tags.length ? i.tags.map(t => `<span class="port-tag">${esc(t)}</span>`).join(' ') : '<span class="dim">sin tag</span>'}</td>
    <td><span class="mono dim">${esc(i.id)}</span></td><td>${i.size_mb} MB</td><td>${esc(i.created)}</td></tr>`).join('');
  const volRows = vols.map(v => `<tr>
    <td><span class="mono">${esc(v.name)}</span></td>
    <td><span class="dim">${esc(v.driver)}</span></td><td>—</td><td>${esc(v.created)}</td></tr>`).join('');
  q('cleanup-list').innerHTML = (imgs.length || vols.length)
    ? `<p class="dim" style="font-size:12px;margin:8px 0">${plural(imgs.length, 'imagen', 'imágenes')} (${mb} MB) · ${plural(vols.length, 'volumen', 'volúmenes')}</p>
       <div class="table-wrapper"><table><tbody>${imgRows}${volRows}</tbody></table></div>`
    : '<p class="dim" style="margin-top:8px">Nada que limpiar con estos filtros.</p>';
  q('cleanup-run-btn').disabled = !(imgs.length || vols.length);
}

async function runCleanup() {
  const types = cleanupTypes();
  if (!cleanupData || !types.length) return;
  const warn = types.includes('volumenes') ? ' Los datos de los volúmenes se perderán de forma permanente.' : '';
  if (!confirm(`¿Borrar los recursos listados?${warn}`)) return;
  q('cleanup-run-btn').disabled = true;
  try {
    const res = await fetch('/api/limpieza', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dias: cleanupData.dias, tipos: types }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || JSON.stringify(d));
    const msg = `Limpieza: ${plural(d.images_deleted.length, 'imagen', 'imágenes')}, ` +
      `${plural(d.volumes_deleted.length, 'volumen', 'volúmenes')} · ${d.freed_mb} MB`;
    if (d.errors.length) {
      toast(`${msg} · ${d.errors.length} no se pudieron borrar: ${d.errors.map(e => e.item).join(', ')}`, 'error');
    } else {
      toast(msg, 'success');
    }
    setTimeout(loadAll, 600);
    await loadCleanup();
  } catch (err) {
    toast(`Error: ${err.message}`, 'error');
    q('cleanup-run-btn').disabled = false;
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
  if (id === 'server-modal') closeServer();
}

function closeAllModals() {
  ['setup-modal','login-modal','log-modal','stats-modal','inspect-modal','proc-modal',
   'sysinfo-modal','server-modal','pull-modal','term-modal','create-modal','history-modal','chpass-modal','cleanup-modal',
   'update-modal','settings-modal']
    .forEach(id => q(id).classList.add('hidden'));
  if (statsWs) { statsWs.close(); statsWs = null; }
  if (statsChart) { statsChart.destroy(); statsChart = null; }
  if (logWs) { logWs.close(); logWs = null; }
  if (termWs) { termWs.close(); termWs = null; }
  if (term) { term.dispose(); term = null; }
  if (termResizeObserver) { termResizeObserver.disconnect(); termResizeObserver = null; }
  currentProcId = null;
  closeServer();
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
