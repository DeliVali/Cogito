// <liquid-orb state="idle|thinking|responding|working|approval|done|error" size="120">
// COGITO's liquid orb, canvas 2D, no dependencies.
// Ported verbatim from the design canvas (COGITO v2, export_cogito_v2/liquid-orb.js). Only
// its comments were translated to English (CONTRIBUTING §1). Keep the drawing code as is:
// the visual is the reason it was reused.
// - Outline = a polar path with slow harmonics (liquid) + a light ripple while responding.
// - Interior: 4 palette blobs drifting + a current of light; specular and shadow are fixed
//   (the 3D volume itself does not move).
// - State change: continuous interpolation of parameters and palette (~300ms), no cuts.
// - Pull (like Mario 64's face): a point is grabbed and dragged; that stretch of the outline
//   stretches toward the pointer like a jelly nose. On release it snaps back, overshoots and
//   wobbles. A click without a drag is only a tap.
// - Pauses on document.hidden and when off-screen; prefers-reduced-motion = one static frame.
(function () {
  if (customElements.get('liquid-orb')) return;
  const rgb = h => [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16));
  const BLUE = ['#ffffff', '#e4eefa', '#bcd0ec', '#8aa6d2', '#6582b4'];
  const S = {
    idle:       { pal: ['#ffffff', '#edf1f7', '#cfd9e7', '#a3b2c8', '#8293ad'], amp: .012, flow: .15, talk: 0, dash: 0, pulse: 0, ok: 0, sat: 0, err: 0, br: 6.5 },
    thinking:   { pal: BLUE, amp: .026, flow: .8, talk: 0, dash: 1, pulse: 0, ok: 0, sat: 0, err: 0, br: 3.4 },
    responding: { pal: BLUE, amp: .03, flow: 1.0, talk: 1, dash: 0, pulse: 0, ok: 0, sat: 0, err: 0, br: 3 },
    working:    { pal: BLUE, amp: .024, flow: .95, talk: 0, dash: 0, pulse: 0, ok: 0, sat: 1, err: 0, br: 4 },
    approval:   { pal: ['#ffffff', '#f8f0dc', '#ead5a0', '#c9a95a', '#a3843c'], amp: .02, flow: .35, talk: 0, dash: 0, pulse: 1, ok: 0, sat: 0, err: 0, br: 5 },
    done:       { pal: ['#ffffff', '#e6f6f0', '#bfe6d6', '#86c7ae', '#5fa68d'], amp: .012, flow: .22, talk: 0, dash: 0, pulse: 0, ok: 1, sat: 0, err: 0, br: 6 },
    error:      { pal: ['#ffffff', '#f9e8e5', '#ecbfb8', '#cf8a81', '#ad645b'], amp: .016, flow: .25, talk: 0, dash: 0, pulse: 0, ok: 0, sat: 0, err: 1, br: 5 },
  };
  const KEYS = ['amp', 'flow', 'talk', 'dash', 'pulse', 'ok', 'sat', 'err', 'br'];
  const reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const TAU = Math.PI * 2;

  class LiquidOrb extends HTMLElement {
    static get observedAttributes() { return ['state', 'size']; }
    constructor() {
      super();
      this.cv = document.createElement('canvas');
      this.cv.style.display = 'block';
      this.attachShadow({ mode: 'open' }).appendChild(this.cv);
      this.ctx = this.cv.getContext('2d');
      this.t = Math.random() * 100;
      this.env = 0; this.visible = true; this.cur = null;
      this._loop = this._loop.bind(this);
      this.cv.style.pointerEvents = 'none';
      this.g = { lx: 0, ly: 0, lvx: 0, lvy: 0, ta: 0, held: false, a: 0, sw: .6, gx: 0, gy: 0, px: 0, py: 0, x: 0, y: 0, vx: 0, vy: 0, m2: 0, v2: 0, m3: 0, v3: 0, sx: 0, vs: 0, rip: [] };
      const at = e => { const r = this.getBoundingClientRect(), m = r.width / 2; return { x: e.clientX - r.left - m, y: e.clientY - r.top - m, R: r.width * .34 }; };
      this.addEventListener('pointerdown', e => {
        if (reduced) return; const p = at(e), d = Math.hypot(p.x, p.y); if (d > p.R * 1.12) return;
        const g = this.g, f = Math.min(1, d / p.R); g.held = true; g.a = Math.atan2(p.y, p.x); g.ta = g.a; g.sw = .5 + .45 * (1 - f);
        g.gx = p.x; g.gy = p.y; g.px = p.x; g.py = p.y;
        g.vx += Math.cos(g.a) * p.R * .25; g.vy += Math.sin(g.a) * p.R * .25; g.v2 += .15;
        try { this.setPointerCapture(e.pointerId); } catch (_) {}
      });
      this.addEventListener('pointermove', e => { const g = this.g; if (!g.held) return; const p = at(e); g.px = p.x; g.py = p.y; });
      const up = () => { const g = this.g; if (!g.held) return; g.held = false; const R = this.size * .34, L = Math.hypot(g.x, g.y) / R; g.v2 -= .9 * L + .2; g.v3 += .4 * L; g.vs += .7 * L + .15; g.rip.push({ x: (g.gx + g.x) / R, y: (g.gy + g.y) / R, t: 0 }); if (g.rip.length > 3) g.rip.shift(); };
      ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(n => this.addEventListener(n, up));
      this._setTarget();
    }
    get state() { return this.getAttribute('state') || 'idle'; }
    set state(v) { this.setAttribute('state', v); }
    get size() { return +this.getAttribute('size') || 120; }
    set size(v) { this.setAttribute('size', v); }
    attributeChangedCallback(n) { if (n === 'size') this._resize(); else this._setTarget(); if (reduced) this._draw(0); }
    connectedCallback() {
      this.style.display = 'inline-block'; this.style.touchAction = 'none';
      this._resize();
      this.visible = true; this.lastCheck = 0;
      this.last = performance.now();
      this._draw(0);
      if (!reduced) this.raf = requestAnimationFrame(this._loop);
    }
    disconnectedCallback() { cancelAnimationFrame(this.raf); }
    _setTarget() {
      if (this.state === 'done' && this._last !== 'done') this.doneT = this.t;
      this._last = this.state;
      this.target = S[this.state] || S.idle;
      this.tp = this.target.pal.map(rgb);
      if (!this.cur) this.cur = Object.assign({}, this.target, { pal: this.target.pal.map(rgb) });
    }
    _resize() {
      const s = this.size, d = Math.min(window.devicePixelRatio || 1, 2);
      const P = 1.6, o = s * .3;
      this.cv.width = s * P * d; this.cv.height = s * P * d;
      this.cv.style.width = this.cv.style.height = s * P + 'px'; this.cv.style.margin = -o + 'px';
      if (this.isConnected) this.style.width = this.style.height = s + 'px';
      this.ctx.setTransform(d, 0, 0, d, o * d, o * d);
    }
    _loop(now) {
      this.raf = requestAnimationFrame(this._loop);
      const dt = Math.min(.05, (now - this.last) / 1000); this.last = now;
      if (document.hidden) return;
      if (now - this.lastCheck > 500) {
        this.lastCheck = now;
        const r = this.getBoundingClientRect();
        this.visible = !(r.width && (r.bottom < -50 || r.top > innerHeight + 50 || r.right < -50 || r.left > innerWidth + 50));
      }
      if (!this.visible) return;
      this.t += dt; this._draw(dt);
    }
    _draw(dt) {
      const c = this.ctx, s = this.size, m = s / 2, R = s * .34, t = this.t, cur = this.cur;
      const k = reduced ? 1 : 1 - Math.pow(.06, dt);
      KEYS.forEach(key => { cur[key] += (this.target[key] - cur[key]) * k; });
      cur.pal.forEach((p, i) => { for (let j = 0; j < 3; j++) p[j] += (this.tp[i][j] - p[j]) * k; });
      const col = (i, a) => `rgba(${cur.pal[i].map(Math.round).join(',')},${a == null ? 1 : a})`;

      // "speaking" envelope (light): fast attack, slow decay
      const raw = cur.talk * Math.max(0, .5 + .5 * Math.sin(t * 5.3) * Math.sin(t * 2.1 + .7) + .25 * Math.sin(t * 11.7));
      this.env += (raw - this.env) * (raw > this.env ? .3 : .06);
      const env = Math.max(0, Math.min(1, this.env));

      c.clearRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      c.save();
      const jz = this.g.sx * .06; c.translate(m, m + R); c.scale(1 + jz, 1 - jz); c.translate(-m, -m - R);
      if (cur.pulse > .01) {
        const gp = cur.pulse * (.28 + .22 * Math.sin(t * TAU / 1.6));
        const hg = c.createRadialGradient(m, m, R * .85, m, m, R * 1.42);
        hg.addColorStop(0, `rgba(226,184,72,${gp})`); hg.addColorStop(1, 'rgba(226,184,72,0)');
        c.fillStyle = hg; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      }
      if (cur.err > .01) {
        const gl = cur.err * (.35 + .25 * Math.sin(t * TAU / 2.6));
        const hg = c.createRadialGradient(m, m, R * .8, m, m, R * 1.45);
        hg.addColorStop(0, `rgba(220,120,110,${gl})`); hg.addColorStop(1, 'rgba(220,120,110,0)');
        c.fillStyle = hg; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      }

      // pull: follows the pointer while held; on release, a soft spring that overshoots and wobbles (jelly)
      const gs = this.g;
      if (dt > 0) {
        let tx = 0, ty = 0;
        if (gs.held) { tx = gs.px - gs.gx; ty = gs.py - gs.gy; const L = Math.hypot(tx, ty), mx = R * .3; if (L > 0) { const k2 = mx * Math.tanh(L / mx) / L; tx *= k2; ty *= k2; } }
        const kk = gs.held ? 55 : 70, cc = gs.held ? 11 : 3.4;
        gs.vx += (kk * (tx - gs.x) - cc * gs.vx) * dt; gs.x += gs.vx * dt;
        gs.vy += (kk * (ty - gs.y) - cc * gs.vy) * dt; gs.y += gs.vy * dt;
        gs.v2 += (-80 * gs.m2 - 2 * gs.v2) * dt; gs.m2 += gs.v2 * dt;
        gs.v3 += (-140 * gs.m3 - 2.6 * gs.v3) * dt; gs.m3 += gs.v3 * dt;
        gs.vs += (-55 * gs.sx - 2 * gs.vs) * dt; gs.sx += gs.vs * dt;
        if (gs.held && Math.hypot(tx, ty) > R * .03) { const da0 = Math.atan2(gs.gy + gs.y, gs.gx + gs.x); gs.ta = da0; }
        let dA = gs.ta - gs.a; dA = Math.atan2(Math.sin(dA), Math.cos(dA)); gs.a += dA * Math.min(1, dt * 4);
        gs.lvx += (30 * (gs.x - gs.lx) - 4 * gs.lvx) * dt; gs.lx += gs.lvx * dt;
        gs.lvy += (30 * (gs.y - gs.ly) - 4 * gs.lvy) * dt; gs.ly += gs.lvy * dt;
        gs.rip.forEach(r => r.t += dt); gs.rip = gs.rip.filter(r => r.t < 1.1);
      }
      const GL = Math.hypot(gs.x, gs.y), pw = 1.6 + 1.2 * (1 - gs.sw);
      const pull = a => { let df = a - gs.a; df = Math.atan2(Math.sin(df), Math.cos(df)); const w = Math.pow((1 + Math.cos(df)) / 2, pw); return { w, modes: gs.m2 * .05 * Math.cos(2 * df) + gs.m3 * .025 * Math.cos(3 * df + .6) }; };
      const breathe = .016 * Math.sin(t * TAU / cur.br) + cur.sat * .032 * Math.sin(t * TAU / 1.2);
      const N = 128, pts = [];
      const wp = .6 * Math.sin(t * .37), wq = .5 * Math.sin(t * .29 + 1.7);
      for (let i = 0; i < N; i++) {
        const a = i / N * TAU;
        const w = cur.amp * (.55 * Math.sin(3 * a + t * .9 + wp) + .35 * Math.sin(5 * a - t * 1.3 + 1.3 + wq) + .45 * Math.sin(2 * a + t * .55 + 2.1 - wp))
          + env * .045 * (.6 * Math.sin(4 * a + t * 3.1) + .4 * Math.sin(6 * a - t * 3.7 + .8));
        const pl = pull(a), r = R * (1 + breathe + w + pl.modes) - GL * .12 * (1 - pl.w);
        pts.push([m + Math.cos(a) * r + gs.x * pl.w, m + Math.sin(a) * r + gs.y * pl.w]);
      }
      const path = () => {
        c.beginPath();
        const L = pts.length, p0 = pts[L - 1], p1 = pts[0];
        c.moveTo((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2);
        for (let i = 0; i < L; i++) { const a = pts[i], b = pts[(i + 1) % L]; c.quadraticCurveTo(a[0], a[1], (a[0] + b[0]) / 2, (a[1] + b[1]) / 2); }
        c.closePath();
      };

      // cast shadow
      c.save(); c.shadowColor = 'rgba(40,60,110,.2)'; c.shadowBlur = s * .07; c.shadowOffsetY = s * .035;
      path(); c.fillStyle = col(2, .6); c.fill(); c.restore();

      // liquid interior
      c.save(); path(); c.clip();
      let g = c.createRadialGradient(m - R * .3, m - R * .42, R * .04, m, m, R * 1.2);
      [[0, 0, .95], [.26, 1, .85], [.58, 2, .78], [.86, 3, .82], [1, 4, .9]].forEach(([o, i, a]) => g.addColorStop(o, col(i, a)));
      c.fillStyle = g; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      const F = t * cur.flow, pushX = gs.lx * .9, pushY = gs.ly * .9;
      [[1, .55, .9, 0], [4, .6, .8, 2.1], [2, .5, .7, 4.2], [0, .35, .55, 1.1]].forEach(([pi, al, rr, off], j) => {
        const a = F * (.55 + j * .17) + off;
        const x = m + Math.cos(a) * R * .42 + pushX * (1 - j * .18), y = m + Math.sin(a * 1.27 + j) * R * .38 + pushY * (1 - j * .18);
        const rad = R * rr * (1 + .12 * Math.sin(F * 1.3 + j));
        const bg = c.createRadialGradient(x, y, 0, x, y, rad);
        bg.addColorStop(0, col(pi, al * .7)); bg.addColorStop(1, col(pi, 0));
        c.fillStyle = bg; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      });
      // water surface inside the glass
      const dt0 = this.t - (this.doneT || 0), ez = x => 1 - Math.pow(1 - Math.max(0, Math.min(1, x)), 3);
      const lvl = m + R * .22, wa = R * (.03 + .035 * cur.flow);
      c.beginPath(); c.moveTo(m - R * 1.2, m + R * 1.2);
      for (let x = -1.2; x <= 1.21; x += .05) c.lineTo(m + x * R, lvl + Math.sin(x * 3.2 + t * 1.3 * (.4 + cur.flow)) * wa + Math.sin(x * 5.1 - t * .9) * wa * .4);
      c.lineTo(m + R * 1.2, m + R * 1.2); c.closePath();
      c.fillStyle = col(3, .14); c.fill();
      // currents of light (a second one appears while "working", and everything flows faster)
      c.save(); c.filter = `blur(${Math.max(1, R * .07)}px)`; c.lineCap = 'round'; c.lineWidth = R * .16;
      c.strokeStyle = 'rgba(255,255,255,.34)';
      c.beginPath(); c.arc(m + Math.sin(F * .7) * R * .1, m + R * .08, R * .52, F * .9, F * .9 + 1.5); c.stroke();
      if (cur.sat > .01) {
        c.strokeStyle = `rgba(255,255,255,${.28 * cur.sat})`; c.lineWidth = R * .12;
        c.beginPath(); c.arc(m - Math.sin(F * .6) * R * .08, m - R * .05, R * .36, -F * 1.3, -F * 1.3 + 1.2); c.stroke();
      }
      c.restore();
      gs.rip.forEach(rp => { const p = rp.t / 1.1, rr = R * (.1 + 1.5 * p), al = .45 * (1 - p) * (1 - p); c.save(); c.filter = 'blur(' + Math.max(.5, R * .02) + 'px)'; c.strokeStyle = 'rgba(255,255,255,' + al + ')'; c.lineWidth = R * (.09 * (1 - p) + .02); c.beginPath(); c.arc(m + rp.x * R, m + rp.y * R, rr, 0, TAU); c.stroke(); c.restore(); });
      if (GL > 1) { const nx = m + Math.cos(gs.a) * R * .7 + gs.x * .8, ny = m + Math.sin(gs.a) * R * .7 + gs.y * .8, ng = c.createRadialGradient(nx, ny, 0, nx, ny, R * .5); ng.addColorStop(0, 'rgba(255,255,255,' + Math.min(.4, GL / R * .5) + ')'); ng.addColorStop(1, 'rgba(255,255,255,0)'); c.fillStyle = ng; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6); }
      g = c.createRadialGradient(m - R * .1, m - R * .15, R * .55, m, m, R * 1.08);
      g.addColorStop(0, col(3, 0)); g.addColorStop(1, col(3, .38));
      c.fillStyle = g; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      g = c.createRadialGradient(m + R * .2, m + R * .75, 0, m + R * .2, m + R * .75, R * .5);
      g.addColorStop(0, 'rgba(255,255,255,.55)'); g.addColorStop(1, 'rgba(255,255,255,0)');
      c.fillStyle = g; c.fillRect(-s * .3, -s * .3, s * 1.6, s * 1.6);
      // glass rim: lit on top, fading toward the bottom
      g = c.createLinearGradient(0, m - R, 0, m + R);
      g.addColorStop(0, 'rgba(255,255,255,.95)'); g.addColorStop(.5, 'rgba(255,255,255,.15)'); g.addColorStop(1, 'rgba(255,255,255,.5)');
      path(); c.strokeStyle = g; c.lineWidth = Math.max(1.5, R * .05); c.stroke();
      c.restore();

      // fixed specular highlight
      c.save(); c.translate(m - R * .33 + pushX * .35, m - R * .44 + pushY * .35); c.rotate(-.55); c.scale(1, .6);
      g = c.createRadialGradient(0, 0, 0, 0, 0, R * .38);
      g.addColorStop(0, 'rgba(255,255,255,.8)'); g.addColorStop(.45, 'rgba(255,255,255,.28)'); g.addColorStop(1, 'rgba(255,255,255,0)');
      c.fillStyle = g; c.beginPath(); c.arc(0, 0, R * .38, 0, TAU); c.fill(); c.restore();
      c.save(); c.translate(m - R * .4 + pushX * .35, m - R * .52 + pushY * .35); c.rotate(-.55); c.scale(1, .45);
      c.fillStyle = 'rgba(255,255,255,.92)'; c.beginPath(); c.arc(0, 0, R * .13, 0, TAU); c.fill(); c.restore();
      path(); c.strokeStyle = col(4, .28); c.lineWidth = 1; c.stroke();
      if (cur.pulse > .01) { path(); c.strokeStyle = `rgba(240,200,90,${(.45 + .4 * Math.sin(t * TAU / 1.6)) * cur.pulse})`; c.lineWidth = Math.max(1.5, R * .06); c.stroke(); }
      if (cur.err > .01) { path(); c.fillStyle = `rgba(255,236,232,${cur.err * (.12 + .12 * Math.sin(t * TAU / 2.6))})`; c.fill(); }

      // per-state overlays
      if (cur.dash > .01) {
        c.save(); c.setLineDash([s * .022, s * .03]); c.lineDashOffset = -t * s * .06;
        c.strokeStyle = `rgba(60,90,160,${.6 * cur.dash})`; c.lineWidth = 1.5;
        c.beginPath(); c.arc(m, m, R * 1.3, 0, TAU); c.stroke(); c.restore();
      }
      if (cur.ok > .01) {
        const p = (dt0 % 3.4) / 3.4;
        if (p < .3) {
          const sp = ez(p / .3);
          c.save(); path(); c.clip(); c.translate(m, m); c.rotate(-.75);
          const x = (-1.4 + 2.8 * sp) * R, lg = c.createLinearGradient(x - R * .28, 0, x + R * .28, 0);
          lg.addColorStop(0, 'rgba(255,255,255,0)'); lg.addColorStop(.5, `rgba(255,255,255,${.8 * cur.ok})`); lg.addColorStop(1, 'rgba(255,255,255,0)');
          c.fillStyle = lg; c.fillRect(-R * 1.6, -R * 1.6, R * 3.2, R * 3.2); c.restore();
        }
        [[.66, -.66, 0], [.95, -.18, .08], [.28, -1.0, .15]].forEach(([sx, sy, d]) => {
          const q = p - .2 - d; if (q <= 0 || q >= .32) return;
          const a = Math.sin(q / .32 * Math.PI) * cur.ok, x = m + sx * R * 1.1, y = m + sy * R * 1.1, r = R * .16 * a;
          c.save(); c.translate(x, y); c.rotate(q * 2); c.fillStyle = `rgba(255,255,255,${a})`;
          c.shadowColor = 'rgba(160,230,200,.9)'; c.shadowBlur = R * .12;
          c.beginPath(); c.moveTo(0, -r); c.quadraticCurveTo(0, 0, r, 0); c.quadraticCurveTo(0, 0, 0, r); c.quadraticCurveTo(0, 0, -r, 0); c.quadraticCurveTo(0, 0, 0, -r); c.fill();
          c.restore();
        });
      }
      c.restore();
    }
  }
  customElements.define('liquid-orb', LiquidOrb);
})();
