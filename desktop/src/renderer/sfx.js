'use strict';

/**
 * The design's interface sounds, generated with WebAudio and ported from the canvas'
 * `sfx()` (`COGITO v2.dc.html`): click, open, close, select, send, cancel, done, window.
 * Same oscillators, frequencies, envelopes and volumes; nothing is loaded from a file.
 *
 * Best effort: with no AudioContext (a test page, a locked-down renderer) or a suspended
 * one, the result is silence, never an error.
 */

let context = null;

function audio() {
  if (context) return context;
  const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Context) return null;
  try {
    context = new Context();
  } catch {
    return null;
  }
  return context;
}

function tone(ac, t, f0, f1, dur, type, vol) {
  const o = ac.createOscillator();
  const g = ac.createGain();
  o.type = type;
  o.frequency.setValueAtTime(f0, t);
  o.frequency.exponentialRampToValueAtTime(f1, t + dur);
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(vol, t + 0.006);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  o.connect(g).connect(ac.destination);
  o.start(t);
  o.stop(t + dur + 0.02);
}

const SOUNDS = {
  click(ac, t) {
    const o = ac.createOscillator();
    const f = ac.createBiquadFilter();
    const g = ac.createGain();
    o.type = 'sine';
    o.frequency.setValueAtTime(420, t);
    o.frequency.exponentialRampToValueAtTime(980, t + 0.035);
    f.type = 'lowpass';
    f.frequency.value = 1800;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.09, t + 0.004);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
    o.connect(f).connect(g).connect(ac.destination);
    o.start(t);
    o.stop(t + 0.07);
  },
  open(ac, t) {
    tone(ac, t, 660, 700, 0.09, 'triangle', 0.06);
    tone(ac, t + 0.045, 990, 1040, 0.12, 'sine', 0.05);
  },
  close(ac, t) {
    tone(ac, t, 990, 940, 0.08, 'sine', 0.045);
    tone(ac, t + 0.04, 660, 620, 0.1, 'triangle', 0.05);
  },
  select(ac, t) {
    tone(ac, t, 520, 1180, 0.09, 'sine', 0.06);
    tone(ac, t + 0.07, 900, 1760, 0.11, 'sine', 0.04);
    tone(ac, t + 0.16, 1760, 1755, 0.22, 'sine', 0.018);
  },
  send(ac, t) {
    tone(ac, t, 420, 1250, 0.14, 'sine', 0.06);
    tone(ac, t + 0.1, 1250, 1600, 0.16, 'sine', 0.03);
  },
  cancel(ac, t) {
    tone(ac, t, 784, 780, 0.09, 'sine', 0.055);
    tone(ac, t, 1568, 1560, 0.06, 'sine', 0.015);
    tone(ac, t + 0.11, 523, 440, 0.38, 'sine', 0.06);
    tone(ac, t + 0.11, 262, 220, 0.38, 'triangle', 0.02);
  },
  done(ac, t) {
    [880, 1108, 1320].forEach((f, i) => tone(ac, t + i * 0.07, f, f, 0.3, 'sine', 0.045));
  },
  window(ac, t) {
    tone(ac, t, 300, 900, 0.22, 'sine', 0.05);
    tone(ac, t + 0.12, 1200, 1200, 0.25, 'sine', 0.025);
  },
};

/** Play one of the design's sounds by name. Unknown names are silence. */
function sfx(kind) {
  const play = SOUNDS[kind];
  if (!play) return;
  const ac = audio();
  if (!ac) return;
  try {
    if (ac.state === 'suspended') ac.resume().catch(() => {});
    play(ac, ac.currentTime + 0.005);
  } catch {
    // A sound that cannot play is not worth an error in the thread.
  }
}

export { sfx, SOUNDS };
