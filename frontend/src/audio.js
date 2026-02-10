/**
 * Audio / notification sound helpers.
 */
import { isMuted } from "./state.js";

let _audioCtx = null;

function _getAudioCtx() {
  if (!_audioCtx) {
    try {
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (e) {
      return null;
    }
  }
  return _audioCtx;
}

export function playMsgSound() {
  if (isMuted.value) return;
  const ctx = _getAudioCtx();
  if (!ctx) return;
  const now = ctx.currentTime;
  const g = ctx.createGain();
  g.connect(ctx.destination);
  g.gain.setValueAtTime(0.15, now);
  g.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
  const o1 = ctx.createOscillator();
  o1.type = "sine"; o1.frequency.value = 800;
  o1.connect(g); o1.start(now); o1.stop(now + 0.08);
  const o2 = ctx.createOscillator();
  o2.type = "sine"; o2.frequency.value = 1000;
  o2.connect(g); o2.start(now + 0.1); o2.stop(now + 0.18);
}

export function playTaskSound() {
  if (isMuted.value) return;
  const ctx = _getAudioCtx();
  if (!ctx) return;
  const now = ctx.currentTime;
  [523.25, 659.25, 783.99].forEach((freq, i) => {
    const t = now + i * 0.15;
    const g = ctx.createGain();
    g.connect(ctx.destination);
    g.gain.setValueAtTime(0.12, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.15);
    const o = ctx.createOscillator();
    o.type = "sine"; o.frequency.value = freq;
    o.connect(g); o.start(t); o.stop(t + 0.15);
  });
}
