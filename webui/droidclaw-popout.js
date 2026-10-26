/*
 * Android Control pop-out: a small floating, draggable, resizable window that
 * shows only the phone screen, so you can watch the device while the chat
 * runs. It survives closing the Android Control modal and page reloads.
 *
 * Loaded globally through the page-head extension.
 */
(function () {
  'use strict';
  if (window.DroidclawPopout) return;

  var STATE_KEY = 'dc_popout_v1';
  var PANEL_PATH = '/plugins/droidclaw/webui/device-panel.html';
  var MIN_W = 160, MIN_H = 240, MARGIN = 8;

  function load() {
    try { return JSON.parse(localStorage.getItem(STATE_KEY) || '{}') || {}; } catch (e) { return {}; }
  }

  function save(state) {
    try { localStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch (e) {}
  }

  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html) node.innerHTML = html;
    return node;
  }

  var state = load();
  var root = null;
  var viewer = null;

  function clampBox(box) {
    var vw = window.innerWidth, vh = window.innerHeight;
    box.w = Math.max(MIN_W, Math.min(box.w || 260, vw - 2 * MARGIN));
    box.h = Math.max(MIN_H, Math.min(box.h || 520, vh - 2 * MARGIN));
    box.x = Math.max(MARGIN, Math.min(box.x == null ? vw - box.w - 24 : box.x, vw - box.w - MARGIN));
    box.y = Math.max(MARGIN, Math.min(box.y == null ? vh - box.h - 96 : box.y, vh - box.h - MARGIN));
    return box;
  }

  function applyBox() {
    if (!root) return;
    clampBox(state);
    root.style.left = state.x + 'px';
    root.style.top = state.y + 'px';
    root.style.width = state.w + 'px';
    root.style.height = state.h + 'px';
  }

  function renderStatus(st) {
    if (!root) return;
    var label = root.querySelector('.dcp-status');
    var text = st.mode === 'stream' ? st.fps + ' fps'
      : st.mode === 'poll' ? st.fps + ' fps (screenshots)'
      : st.mode === 'connecting' ? 'connecting…' : '';
    label.textContent = text;
    label.title = st.message || text;
    root.classList.toggle('dcp-live', st.mode === 'stream' || st.mode === 'poll');
  }

  function build() {
    root = el('div', 'dcp-window');
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-label', 'Android screen');
    var fpsOptions = ['12', '24', '30', '40', '60'].map(function (v) {
      return '<option value="' + v + '">' + v + '</option>';
    }).join('');
    root.innerHTML =
      '<div class="dcp-header" title="Drag to move">' +
        '<span class="dcp-dot"></span>' +
        '<span class="dcp-title"></span>' +
        '<span class="dcp-status"></span>' +
        '<select class="dcp-fps" title="Frame rate" aria-label="Frame rate">' + fpsOptions + '</select>' +
        '<button class="dcp-btn" data-act="dock" title="Back to the full Android Control panel">&#x2922;</button>' +
        '<button class="dcp-btn" data-act="close" title="Close">&#x2715;</button>' +
      '</div>' +
      '<div class="dcp-body"></div>' +
      '<div class="dcp-footer">' +
        '<button class="dcp-btn" data-act="back" title="Back">&#x25C0;</button>' +
        '<button class="dcp-btn" data-act="home" title="Home">&#x25CF;</button>' +
        '<button class="dcp-btn" data-act="recents" title="Recent apps">&#x25A0;</button>' +
      '</div>' +
      '<div class="dcp-resize" title="Drag to resize"></div>';
    document.body.appendChild(root);

    root.querySelector('.dcp-title').textContent = state.label || 'Android';
    var fps = root.querySelector('.dcp-fps');
    fps.value = String(state.fps || 30);
    fps.addEventListener('change', function () {
      state.fps = Number(fps.value);
      save(state);
      try { localStorage.setItem('dc_preview_fps', fps.value); } catch (e) {}
      if (viewer) viewer.setFps(state.fps);
    });
    root.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-act]');
      if (!btn) return;
      var act = btn.getAttribute('data-act');
      if (act === 'close') api.close();
      else if (act === 'dock') api.dock();
      else if (viewer) viewer.key(act);
    });

    bindDrag(root.querySelector('.dcp-header'), function (dx, dy, start) {
      state.x = start.x + dx;
      state.y = start.y + dy;
      applyBox();
    });
    bindDrag(root.querySelector('.dcp-resize'), function (dx, dy, start) {
      state.w = start.w + dx;
      state.h = start.h + dy;
      applyBox();
    });
    applyBox();
  }

  function bindDrag(handle, onMove) {
    handle.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || e.target.closest('select, button:not(.dcp-resize)')) return;
      e.preventDefault();
      var start = { x: state.x, y: state.y, w: state.w, h: state.h, px: e.clientX, py: e.clientY };
      root.classList.add('dcp-dragging');
      try { handle.setPointerCapture(e.pointerId); } catch (err) {}
      function move(ev) { onMove(ev.clientX - start.px, ev.clientY - start.py, start); }
      function up() {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', up);
        root.classList.remove('dcp-dragging');
        save(state);
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', up);
    });
  }

  function whenViewerReady(cb, tries) {
    if (window.DroidclawViewer) return cb();
    if ((tries || 0) > 100) return;
    setTimeout(function () { whenViewerReady(cb, (tries || 0) + 1); }, 50);
  }

  var api = {
    /** Open (or retarget) the floating window. opts: {device, label, fps} */
    open: function (opts) {
      opts = opts || {};
      if ('device' in opts) state.device = opts.device || '';
      if (opts.label) state.label = opts.label;
      if (opts.fps && opts.fps !== 'off') state.fps = Number(opts.fps);
      state.open = true;
      save(state);
      if (!root) build();
      root.querySelector('.dcp-title').textContent = state.label || 'Android';
      root.querySelector('.dcp-fps').value = String(state.fps || 30);
      whenViewerReady(function () {
        if (!root) return;
        if (!viewer) {
          viewer = new window.DroidclawViewer(root.querySelector('.dcp-body'), {
            device: function () { return state.device || ''; },
            fps: state.fps || 30,
            onStatus: renderStatus
          });
        }
        viewer.fps = state.fps || 30;
        viewer.start();
      });
      return api;
    },
    close: function () {
      state.open = false;
      save(state);
      if (viewer) { viewer.destroy(); viewer = null; }
      if (root) { root.remove(); root = null; }
    },
    /** Close the pop-out and return to the full panel. */
    dock: function () {
      api.close();
      if (typeof window.openModal === 'function') window.openModal(PANEL_PATH + '?dc=' + Date.now());
    },
    isOpen: function () { return !!root; },
    state: function () { return JSON.parse(JSON.stringify(state)); }
  };

  window.addEventListener('resize', function () { if (root) { applyBox(); } });
  document.addEventListener('visibilitychange', function () {
    // Don't stream to a hidden tab; resume when it is visible again.
    if (!viewer) return;
    if (document.hidden) viewer.stop(true);
    else viewer.start();
  });

  window.DroidclawPopout = api;

  function restore() {
    if (state.open) api.open({});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', restore);
  else restore();
})();
