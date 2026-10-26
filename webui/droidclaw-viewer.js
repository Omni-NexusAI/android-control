/*
 * Android Control live screen viewer.
 *
 * Streams H.264 from the device (api/stream -> scrcpy-server) and decodes it
 * with WebCodecs onto a <canvas>. Falls back to polling downscaled JPEG
 * screenshots when WebCodecs or the stream is unavailable. Pointer input on
 * the canvas is sent to api/control: click = tap, hold = long press,
 * drag = swipe.
 *
 * Loaded as a classic script by the panel and the pop-out; safe to include
 * more than once.
 */
(function () {
  'use strict';
  if (window.DroidclawViewer) return;

  var API = '/api/plugins/droidclaw/';
  var FPS_CHOICES = [12, 24, 30, 40, 60];
  var PKT = { SESSION: 1, CONFIG: 2, FRAME: 3, KEEPALIVE: 4, HELLO: 5 };

  var csrfToken = null;
  var csrfPromise = null;

  function getCsrfToken() {
    if (csrfToken) return Promise.resolve(csrfToken);
    if (csrfPromise) return csrfPromise;
    csrfPromise = fetch('/api/csrf_token', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (res) { csrfToken = res.token || ''; return csrfToken; })
      .finally(function () { csrfPromise = null; });
    return csrfPromise;
  }

  function post(endpoint, body, signal) {
    return getCsrfToken().then(function (token) {
      return fetch(API + endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
        body: JSON.stringify(body || {}),
        signal: signal
      });
    });
  }

  function api(endpoint, body) {
    return post(endpoint, body).then(function (r) { return r.json(); });
  }

  function hex2(n) { return ('0' + n.toString(16)).slice(-2); }

  // "avc1.PPCCLL" from the SPS inside an Annex-B config packet.
  function codecFromConfig(bytes) {
    for (var i = 0; i + 4 < bytes.length; i++) {
      var start = 0;
      if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1) start = i + 3;
      else if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 0 && bytes[i + 3] === 1) start = i + 4;
      if (start && (bytes[start] & 0x1f) === 7 && start + 3 < bytes.length) {
        return 'avc1.' + hex2(bytes[start + 1]) + hex2(bytes[start + 2]) + hex2(bytes[start + 3]);
      }
    }
    return 'avc1.42e01f';
  }

  function concat(a, b) {
    var out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  function nearestFps(value) {
    var n = Number(value) || 30;
    return FPS_CHOICES.reduce(function (best, c) { return Math.abs(c - n) < Math.abs(best - n) ? c : best; }, 30);
  }

  /**
   * @param {HTMLElement} container element the canvas is placed into
   * @param {object} opts { device: string|function, fps, maxSize, mode: 'auto'|'stream'|'poll', interactive, onStatus }
   */
  function Viewer(container, opts) {
    opts = opts || {};
    this.container = container;
    this.device = opts.device || '';
    this.fps = nearestFps(opts.fps || 30);
    this.maxSize = opts.maxSize || 1024;
    this.mode = opts.mode || 'auto';
    this.interactive = opts.interactive !== false;
    this.onStatus = opts.onStatus || function () {};
    this.running = false;
    this.activeMode = 'stopped';
    this.frameW = 0;
    this.frameH = 0;
    this._frameTimes = [];
    this._generation = 0;
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'dcv-canvas';
    this.canvas.width = 9;
    this.canvas.height = 16;
    this.ctx = this.canvas.getContext('2d');
    container.appendChild(this.canvas);
    if (this.interactive) this._bindInput();
  }

  Viewer.prototype._deviceId = function () {
    return typeof this.device === 'function' ? (this.device() || '') : (this.device || '');
  };

  Viewer.prototype._status = function (message) {
    this.statusMessage = message || '';
    this.onStatus({
      mode: this.activeMode,
      fps: this.measuredFps(),
      target: this.fps,
      width: this.frameW,
      height: this.frameH,
      message: this.statusMessage
    });
  };

  Viewer.prototype.measuredFps = function () {
    var t = this._frameTimes;
    if (t.length < 2) return 0;
    return Math.round(((t.length - 1) * 1000) / Math.max(1, t[t.length - 1] - t[0]));
  };

  Viewer.prototype._countFrame = function () {
    var now = performance.now();
    this._frameTimes.push(now);
    while (this._frameTimes.length && now - this._frameTimes[0] > 2000) this._frameTimes.shift();
  };

  Viewer.prototype._resize = function (w, h) {
    if (!w || !h) return;
    this.frameW = w;
    this.frameH = h;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
  };

  Viewer.prototype.start = function () {
    this.stop(true);
    this.running = true;
    var gen = ++this._generation;
    var wantStream = this.mode !== 'poll' && typeof window.VideoDecoder === 'function' && window.isSecureContext !== false;
    if (wantStream) this._startStream(gen, 0);
    else this._startPoll(gen, this.mode === 'stream' ? 'WebCodecs is not available in this browser' : '');
  };

  Viewer.prototype.stop = function (silent) {
    this.running = false;
    this._generation++;
    if (this._abort) { try { this._abort.abort(); } catch (e) {} this._abort = null; }
    if (this._pingTimer) { clearInterval(this._pingTimer); this._pingTimer = null; }
    if (this._viewerId) { api('stream', { action: 'close', viewer: this._viewerId }).catch(function () {}); this._viewerId = null; }
    if (this._decoder) { try { this._decoder.close(); } catch (e) {} this._decoder = null; }
    if (this._pollTimer) { clearTimeout(this._pollTimer); this._pollTimer = null; }
    this.activeMode = 'stopped';
    this._frameTimes = [];
    if (!silent) this._status('');
  };

  Viewer.prototype.destroy = function () {
    this.stop(true);
    if (this._unbind) this._unbind();
    if (this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas);
  };

  Viewer.prototype.setFps = function (fps) {
    this.fps = nearestFps(fps);
    if (this.running) this.start();
  };

  Viewer.prototype.setDevice = function (device) {
    this.device = device || '';
    if (this.running) this.start();
  };

  // ---- H.264 stream ------------------------------------------------------

  Viewer.prototype._startStream = function (gen, attempt) {
    var self = this;
    var abort = new AbortController();
    this._abort = abort;
    this.activeMode = 'connecting';
    this._status('Starting live stream…');
    post('stream', { device: this._deviceId(), fps: this.fps, max_size: this.maxSize }, abort.signal)
      .then(function (resp) {
        if (gen !== self._generation) return null;
        var type = resp.headers.get('Content-Type') || '';
        if (!resp.ok || type.indexOf('application/json') !== -1) {
          return resp.json().catch(function () { return {}; }).then(function (data) {
            throw new Error((data && data.message) || ('stream unavailable (' + resp.status + ')'));
          });
        }
        self.activeMode = 'stream';
        return self._readStream(resp.body.getReader(), gen);
      })
      .then(function () {
        if (gen !== self._generation || !self.running) return;
        // Stream ended (fps change from another viewer, device hiccup):
        // reconnect, unless it keeps dying right away.
        var now = performance.now();
        self._drops = (self._drops || []).filter(function (t) { return now - t < 10000; });
        self._drops.push(now);
        if (self._drops.length > 3) {
          self._startPoll(gen, 'Live stream keeps dropping; using screenshots');
          return;
        }
        setTimeout(function () { if (gen === self._generation && self.running) self._startStream(gen, 0); }, 500);
      })
      .catch(function (err) {
        if (gen !== self._generation || !self.running || abort.signal.aborted) return;
        if (attempt < 2 && self.activeMode === 'stream') {
          setTimeout(function () { if (gen === self._generation) self._startStream(gen, attempt + 1); }, 800);
          return;
        }
        self._startPoll(gen, 'Live stream unavailable (' + (err && err.message ? err.message : err) + '); using screenshots');
      });
  };

  Viewer.prototype._makeDecoder = function (codec, gen) {
    var self = this;
    if (this._decoder) { try { this._decoder.close(); } catch (e) {} }
    var decoder = new VideoDecoder({
      output: function (frame) {
        if (gen === self._generation) {
          self._resize(frame.displayWidth, frame.displayHeight);
          self.ctx.drawImage(frame, 0, 0, self.canvas.width, self.canvas.height);
          self._countFrame();
        }
        frame.close();
      },
      error: function (e) {
        self._needKey = true;
        self._decoderError = String(e && e.message ? e.message : e);
      }
    });
    decoder.configure({ codec: codec, optimizeForLatency: true, hardwareAcceleration: 'no-preference' });
    this._decoder = decoder;
    this._needKey = true;
  };

  Viewer.prototype._readStream = function (reader, gen) {
    var self = this;
    var buffer = new Uint8Array(0);
    var config = null;
    var lastStatus = 0;

    function handle(type, flags, pts, payload) {
      if (type === PKT.HELLO) {
        self._viewerId = new TextDecoder().decode(payload);
        if (self._pingTimer) clearInterval(self._pingTimer);
        self._pingTimer = setInterval(function () {
          if (self._viewerId) api('stream', { action: 'ping', viewer: self._viewerId }).catch(function () {});
        }, 3000);
      } else if (type === PKT.SESSION) {
        var dv = new DataView(payload.buffer, payload.byteOffset, 8);
        self._resize(dv.getUint32(0), dv.getUint32(4));
      } else if (type === PKT.CONFIG) {
        config = payload.slice();
        self._makeDecoder(codecFromConfig(config), gen);
      } else if (type === PKT.FRAME && self._decoder && self._decoder.state === 'configured') {
        var key = (flags & 1) === 1;
        if (!key && (self._needKey || self._decoder.decodeQueueSize > 6)) {
          if (!self._needKey) {
            self._needKey = true;
            api('stream', { action: 'keyframe', device: self._deviceId() }).catch(function () {});
          }
          return;
        }
        var data = key && config ? concat(config, payload) : payload;
        try {
          self._decoder.decode(new EncodedVideoChunk({ type: key ? 'key' : 'delta', timestamp: pts, data: data }));
          if (key) self._needKey = false;
        } catch (e) {
          self._needKey = true;
        }
      }
      var now = performance.now();
      if (now - lastStatus > 1000) { lastStatus = now; self._status(''); }
    }

    function pump() {
      return reader.read().then(function (chunk) {
        if (chunk.done || gen !== self._generation) return;
        buffer = buffer.length ? concat(buffer, chunk.value) : chunk.value;
        var offset = 0;
        while (buffer.length - offset >= 16) {
          var dv = new DataView(buffer.buffer, buffer.byteOffset + offset, 16);
          var type = dv.getUint8(0);
          var flags = dv.getUint8(1);
          var len = dv.getUint32(4);
          var pts = Number(dv.getBigUint64(8));
          if (buffer.length - offset - 16 < len) break;
          handle(type, flags, pts, buffer.subarray(offset + 16, offset + 16 + len));
          offset += 16 + len;
        }
        buffer = offset ? buffer.slice(offset) : buffer;
        return pump();
      });
    }
    return pump();
  };

  // ---- screenshot polling fallback ----------------------------------------

  Viewer.prototype._startPoll = function (gen, reason) {
    var self = this;
    if (this._abort) { try { this._abort.abort(); } catch (e) {} this._abort = null; }
    this.activeMode = 'poll';
    this._status(reason || '');
    var img = new Image();

    function tick() {
      if (gen !== self._generation || !self.running) return;
      if (document.hidden) { self._pollTimer = setTimeout(tick, 1000); return; }
      var started = performance.now();
      api('screenshot', { device: self._deviceId(), inline: true, max_edge: self.maxSize, quality: 70 })
        .then(function (data) {
          if (gen !== self._generation) return;
          if (!data.success) {
            self._status(data.message || 'Screenshot failed');
            return new Promise(function (r) { setTimeout(r, 1500); });
          }
          return new Promise(function (resolve) {
            img.onload = function () {
              if (gen === self._generation) {
                self._resize(img.naturalWidth, img.naturalHeight);
                self.ctx.drawImage(img, 0, 0, self.canvas.width, self.canvas.height);
                self._countFrame();
                self._status(self.statusMessage);
              }
              resolve();
            };
            img.onerror = resolve;
            img.src = data.data_url;
          });
        })
        .catch(function () { return new Promise(function (r) { setTimeout(r, 1500); }); })
        .then(function () {
          if (gen !== self._generation || !self.running) return;
          var wait = Math.max(0, 1000 / self.fps - (performance.now() - started));
          self._pollTimer = setTimeout(tick, wait);
        });
    }
    tick();
  };

  // ---- input ---------------------------------------------------------------

  // Map a pointer event to 0..1 coordinates inside the drawn frame
  // (the canvas is letterboxed with object-fit: contain).
  Viewer.prototype.normalize = function (clientX, clientY) {
    var rect = this.canvas.getBoundingClientRect();
    var cw = this.canvas.width, ch = this.canvas.height;
    if (!rect.width || !rect.height || !cw || !ch) return null;
    var scale = Math.min(rect.width / cw, rect.height / ch);
    var drawnW = cw * scale, drawnH = ch * scale;
    var left = rect.left + (rect.width - drawnW) / 2;
    var top = rect.top + (rect.height - drawnH) / 2;
    var x = (clientX - left) / drawnW, y = (clientY - top) / drawnH;
    if (x < 0 || y < 0 || x > 1 || y > 1) return null;
    return { x: x, y: y };
  };

  Viewer.prototype.send = function (body) {
    body.device = this._deviceId();
    body.frame_w = this.frameW;
    body.frame_h = this.frameH;
    return api('control', body);
  };

  Viewer.prototype.key = function (name) {
    return this.send({ action: name });
  };

  Viewer.prototype._bindInput = function () {
    var self = this;
    var down = null;
    this.canvas.style.touchAction = 'none';
    function onDown(e) {
      if (e.button !== undefined && e.button !== 0) return;
      var p = self.normalize(e.clientX, e.clientY);
      if (!p) return;
      down = { p: p, t: performance.now(), x: e.clientX, y: e.clientY };
      try { self.canvas.setPointerCapture(e.pointerId); } catch (err) {}
      e.preventDefault();
    }
    function onUp(e) {
      if (!down) return;
      var start = down;
      down = null;
      var p = self.normalize(e.clientX, e.clientY) || start.p;
      var moved = Math.hypot(e.clientX - start.x, e.clientY - start.y);
      var held = performance.now() - start.t;
      var req;
      if (moved < 8) {
        req = { action: held >= 550 ? 'longpress' : 'tap', x: start.p.x, y: start.p.y };
      } else {
        req = { action: 'swipe', x: start.p.x, y: start.p.y, x2: p.x, y2: p.y, duration: Math.min(Math.max(held / 1000, 0.12), 1.5) };
      }
      self.canvas.classList.add('dcv-busy');
      self.send(req).catch(function () {}).then(function () { self.canvas.classList.remove('dcv-busy'); });
    }
    function onCancel() { down = null; }
    this.canvas.addEventListener('pointerdown', onDown);
    this.canvas.addEventListener('pointerup', onUp);
    this.canvas.addEventListener('pointercancel', onCancel);
    this.canvas.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    this._unbind = function () {
      self.canvas.removeEventListener('pointerdown', onDown);
      self.canvas.removeEventListener('pointerup', onUp);
      self.canvas.removeEventListener('pointercancel', onCancel);
    };
  };

  Viewer.FPS_CHOICES = FPS_CHOICES;
  Viewer.api = api;
  Viewer.codecFromConfig = codecFromConfig;
  window.DroidclawViewer = Viewer;
})();
