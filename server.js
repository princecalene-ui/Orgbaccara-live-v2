/**
 * ORG BACC — serveur minimal
 * GET  /api/learn
 * POST /api/learn              { option, win, gameNum? }
 * POST /api/learn/consume-flip { option }
 * POST /api/learn/reset
 * GET  /api/health
 *
 *   npm i express cors && node server.js
 *   → http://localhost:3847
 */
const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 3847;
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
const LEARN_FILE = path.join(DATA_DIR, 'learn_state.json');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });

function emptyLearn() {
  return { streaks: {}, forceFlip: {}, totalResolved: 0, totalWins: 0, seen: {}, updatedAt: null, version: 1 };
}
function loadLearn() {
  try {
    if (!fs.existsSync(LEARN_FILE)) return emptyLearn();
    return Object.assign(emptyLearn(), JSON.parse(fs.readFileSync(LEARN_FILE, 'utf8')));
  } catch (e) { return emptyLearn(); }
}
function saveLearn(state) {
  state.updatedAt = new Date().toISOString();
  state.version = (state.version || 0) + 1;
  const keys = Object.keys(state.seen || {});
  if (keys.length > 2000) {
    const keep = keys.slice(-1500);
    const next = {};
    keep.forEach(k => { next[k] = true; });
    state.seen = next;
  }
  fs.writeFileSync(LEARN_FILE, JSON.stringify(state, null, 2), 'utf8');
  return state;
}
function normalizeOption(opt) {
  if (!opt) return 'OPT1';
  const a = String(opt).toUpperCase();
  if (a.includes('3CARTES') || a === 'OPT2') return 'OPT2';
  if (a.includes('TOTAL') || a === 'OPT3') return 'OPT3';
  if (a.includes('ALLINV') || a.includes('PARITÉ') || a.includes('PARITE') || a === 'OPT4') return 'OPT4';
  if (a.includes('ALGO') || a === 'OPT1' || a.includes('ENSEIGNE')) return 'OPT1';
  if (a.startsWith('OPT')) return a.slice(0, 4);
  return a.slice(0, 12);
}
function applyOutcome(state, option, win, gameNum) {
  const opt = normalizeOption(option);
  if (gameNum != null && gameNum !== '') {
    const key = `${opt}:${gameNum}`;
    if (state.seen[key]) return { state, applied: false, reason: 'duplicate' };
    state.seen[key] = true;
  }
  if (!state.streaks[opt]) state.streaks[opt] = { loss: 0, win: 0 };
  state.totalResolved = (state.totalResolved || 0) + 1;
  if (win) {
    state.totalWins = (state.totalWins || 0) + 1;
    state.streaks[opt].win = (state.streaks[opt].win || 0) + 1;
    state.streaks[opt].loss = 0;
    state.forceFlip[opt] = false;
  } else {
    state.streaks[opt].loss = (state.streaks[opt].loss || 0) + 1;
    state.streaks[opt].win = 0;
    if (state.streaks[opt].loss >= 2) state.forceFlip[opt] = true;
  }
  return { state, applied: true };
}
function publicLearn(state) {
  return {
    streaks: state.streaks || {},
    forceFlip: state.forceFlip || {},
    totalResolved: state.totalResolved || 0,
    totalWins: state.totalWins || 0,
    updatedAt: state.updatedAt,
    version: state.version || 0
  };
}

const app = express();
app.use(cors({ origin: true }));
app.use(express.json({ limit: '64kb' }));

app.get('/api/learn', (_req, res) => {
  res.json({ ok: true, learn: publicLearn(loadLearn()) });
});

app.post('/api/learn', (req, res) => {
  try {
    let state = loadLearn();
    const body = req.body || {};
    const batch = Array.isArray(body.outcomes) ? body.outcomes : [body];
    let applied = 0;
    const details = [];
    for (const item of batch) {
      if (!item || typeof item.win !== 'boolean') continue;
      const option = item.option || item.algoUsed || item.key || 'OPT1';
      const r = applyOutcome(state, option, item.win, item.gameNum);
      if (r.applied) applied++;
      details.push({ option: normalizeOption(option), win: item.win, gameNum: item.gameNum ?? null, applied: r.applied, reason: r.reason || null });
    }
    if (applied > 0) state = saveLearn(state);
    res.json({ ok: true, applied, details, learn: publicLearn(state) });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message || 'server_error' });
  }
});

app.post('/api/learn/consume-flip', (req, res) => {
  try {
    let state = loadLearn();
    const opt = normalizeOption((req.body && req.body.option) || 'OPT1');
    const had = !!(state.forceFlip && state.forceFlip[opt]);
    if (had) {
      state.forceFlip[opt] = false;
      if (state.streaks[opt]) state.streaks[opt].loss = 0;
      state = saveLearn(state);
    }
    res.json({ ok: true, consumed: had, learn: publicLearn(state) });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post('/api/learn/reset', (req, res) => {
  const token = process.env.LEARN_RESET_TOKEN;
  if (token && (req.headers['x-reset-token'] !== token) && (req.body && req.body.token !== token)) {
    return res.status(403).json({ ok: false, error: 'forbidden' });
  }
  res.json({ ok: true, learn: publicLearn(saveLearn(emptyLearn())) });
});

app.get('/api/health', (_req, res) => {
  res.json({ ok: true, service: 'org-bacc-learn', time: new Date().toISOString() });
});

app.use(express.static(__dirname));
app.listen(PORT, () => {
  console.log(`[ORG BACC] learn API on :${PORT}`);
  console.log(`  data → ${LEARN_FILE}`);
});
