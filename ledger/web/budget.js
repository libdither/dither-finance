// The budget model's math: no DOM, no data. Shared by the portal and the standalone dashboard.
const Budget = (() => {
const sum = xs => xs.reduce((a, b) => a + b, 0);

// CRRA utility on $K, so the numbers stay in range
const util = (c, g) => { c /= 1000; return Math.abs(g - 1) < 1e-9 ? Math.log(c) : Math.pow(c, 1 - g) / (1 - g); };

/* Returns. Everything is in today's dollars: nominal rates are deflated by inflation. Savings are a
   mix of cash and stocks; with risk aversion g the return worth planning on is the certainty
   equivalent  r = share·μ + (1−share)·r_cash − ½·g·share²·σ²  (Merton): the expected return less
   a charge for its volatility. Dividends are part of μ, not extra income. */
function rates(P, g = P.gamma) {
  const inf = P.inflation / 100, real = x => (1 + x / 100) / (1 + inf) - 1;
  const rc = real(P.cashRate), mu = real(P.stockReturn), sg = P.stockVol / 100, s = Math.min(1, Math.max(0, P.stockShare / 100));
  return {rc, mu, sg, s, exp: s * mu + (1 - s) * rc, r: s * mu + (1 - s) * rc - 0.5 * g * s * s * sg * sg,
          star: sg > 0 ? (mu - rc) / (g * sg * sg) : 1};
}

/* Income besides the career is a list of streams, each believed with some chance. A stream either
   turns out real (pays its amount a year for its years) or not; that, like the career scenario,
   becomes clear after the uncertain period. States = scenarios × on/off of each uncertain stream. */
function states(P) {
  const ps = P.scenarios.map(s => Math.max(0, +s.p || 0)), ptot = sum(ps) || 1;
  const sure = P.streams.filter(x => (+x.p || 0) >= 100), unsure = P.streams.filter(x => (+x.p || 0) > 0 && (+x.p || 0) < 100).slice(0, 5);
  const out = [];
  P.scenarios.forEach((sc, i) => {
    for (let mask = 0; mask < 1 << unsure.length; mask++) {
      let p = ps[i] / ptot; const on = [...sure];
      unsure.forEach((x, j) => { const q = x.p / 100; if (mask >> j & 1) { p *= q; on.push(x); } else p *= 1 - q; });
      if (p > 0) out.push({i, sc, p, on});
    }
  });
  return out;
}
const streamAt = (on, t) => { let s = 0; for (const x of on) if (t < (+x.years || 0)) s += +x.amount || 0; return s; };

/* Two-stage life-cycle consumption under uncertainty.
   Stage 1 (now until the uncertainty clears, T1 years): spend c a year, flat; no borrowing, and the
   emergency reserve is never planned away. Stage 2 (after it clears): spend the state's total
   resources (wealth + present value of its income) along the CRRA-optimal path to the horizon.
   c maximizes expected discounted utility over the states.

   Everything that doesn't depend on c is worked out once (prepare): each state's income in stage 1
   and the present value of stage 2, and the stage-2 utility sum, which for CRRA factors as
   util(c0)·Σ βᵏ⁺ᵀ¹ Gᵏ⁽¹⁻ᵍ⁾ (log utility: log c0·Σβ + log G·Σkβ). evaluate is then a few steps per state. */
function prepare(P, g = P.gamma) {
  const r = rates(P, g).r, beta = 1 / (1 + P.rho / 100), T1 = Math.max(1, Math.round(P.resolveYears));
  const N = Math.max(T1 + 1, P.horizonAge - P.age), work = Math.max(0, P.retireAge - P.age - T1);
  const G = Math.pow((1 + r) * beta, 1 / g), n2 = N - T1, logU = Math.abs(g - 1) < 1e-9;
  let ann = 0, S = 0, L0 = 0, L1 = 0, B1 = 0;
  for (let k = 0; k < n2; k++) {
    ann += Math.pow(G / (1 + r), k);
    const b = Math.pow(beta, T1 + k);
    if (logU) { L0 += b; L1 += k * b; } else S += b * Math.pow(G, k * (1 - g));
  }
  for (let t = 0; t < T1; t++) B1 += Math.pow(beta, t);
  const ST = states(P).map(st => {
    const inc1 = []; for (let t = 0; t < T1; t++) inc1.push((+st.sc.y1 || 0) + streamAt(st.on, t));
    let pv = 0;
    for (let k = 0; k < n2; k++) pv += ((k < work ? (+st.sc.y2 || 0) * Math.pow(1 + (+st.sc.growth || 0) / 100, k) : 0) + streamAt(st.on, T1 + k)) / Math.pow(1 + r, k);
    return {...st, inc1, pv};
  });
  return {P, g, r, T1, ann, S, L0, L1, B1, logG: Math.log(G), logU, ST, w0: P.wealth - P.reserveMonths * P.essentials, nsc: P.scenarios.length};
}
function evaluate(X, c) {
  let V = 0; const after = new Array(X.nsc).fill(0), pi = new Array(X.nsc).fill(0);
  const u1 = util(c, X.g) * X.B1;
  for (const st of X.ST) {
    let w = X.w0;
    for (let t = 0; t < X.T1; t++) {
      w += st.inc1[t] - c;
      if (w < -1e-6) return {V: -Infinity};
      w *= 1 + X.r;
    }
    const c0 = (w + st.pv) / X.ann;
    if (!(c0 > 0)) return {V: -Infinity};
    const u2 = X.logU ? Math.log(c0 / 1000) * X.L0 + X.logG * X.L1 : util(c0, X.g) * X.S;
    V += st.p * (u1 + u2);
    after[st.i] += st.p * c0; pi[st.i] += st.p;
  }
  return {V, after: after.map((a, i) => pi[i] ? a / pi[i] : 0)};
}
function solve(P, g = P.gamma) {
  const X = prepare(P, g);
  if (!X.ST.length) return null;
  let lo = 0, hi = Math.max(1, P.wealth) + 10 * Math.max(1, ...P.scenarios.map(s => +s.y1 || 0)) + 10 * sum(P.streams.map(x => +x.amount || 0));
  for (let i = 0; i < 80; i++) { const m = (lo + hi) / 2; if (evaluate(X, m).V > -Infinity) lo = m; else hi = m; }
  let a = 1, b = Math.max(1.0001, lo * 0.9999);
  if (evaluate(X, a).V === -Infinity) return null;   // even spending nothing breaks the reserve
  const phi = (Math.sqrt(5) - 1) / 2;
  for (let i = 0; i < 100; i++) {
    const x1 = b - phi * (b - a), x2 = a + phi * (b - a);
    if (evaluate(X, x1).V < evaluate(X, x2).V) a = x1; else b = x2;
  }
  const c = (a + b) / 2;
  return {c, after: evaluate(X, c).after, cmax: lo};
}

/* Simulated lives under the plan: draw the scenario and streams by their chances and each year's
   stock return (lognormal). Spend c until the uncertainty clears; after that, re-solve every year
   from actual wealth, which is what following the model over time means. Each state's income, its
   present value from every year on, and the annuity factor are tabled once (backward sums). */
function simulate(P, c, n = 500) {
  let seed = 12345; const rnd = () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
    t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
  const gauss = () => Math.sqrt(-2 * Math.log(1 - rnd())) * Math.cos(2 * Math.PI * rnd());
  const R = rates(P), T1 = Math.max(1, Math.round(P.resolveYears));
  const N = Math.max(T1 + 1, P.horizonAge - P.age), beta = 1 / (1 + P.rho / 100);
  const G = Math.pow((1 + R.r) * beta, 1 / P.gamma), s2 = Math.log(1 + R.sg * R.sg / Math.pow(1 + R.mu, 2)), m = Math.log(1 + R.mu) - s2 / 2, sd = Math.sqrt(s2);
  const q = G / (1 + R.r), ANN = new Float64Array(N + 1);
  for (let t = N - 1; t >= 0; t--) ANN[t] = 1 + q * ANN[t + 1];
  const ST = states(P).map(st => {
    const inc = new Float64Array(N), PV = new Float64Array(N + 1);
    for (let t = 0; t < N; t++) inc[t] = (t < T1 ? (+st.sc.y1 || 0) : P.age + t < P.retireAge ? (+st.sc.y2 || 0) * Math.pow(1 + (+st.sc.growth || 0) / 100, t - T1) : 0) + streamAt(st.on, t);
    for (let t = N - 1; t >= 0; t--) PV[t] = inc[t] + PV[t + 1] / (1 + R.r);
    return {p: st.p, inc, PV};
  });
  const cum = []; let acc = 0; for (const st of ST) cum.push(acc += st.p);
  const reserve = P.reserveMonths * P.essentials;
  const spendY = [...Array(N)].map(() => new Float64Array(n)), wealthY = [...Array(N)].map(() => new Float64Array(n));
  for (let k = 0; k < n; k++) {
    const u = rnd() * acc, st = ST[cum.findIndex(x => x >= u)];
    let w = P.wealth - reserve;
    for (let t = 0; t < N; t++) {
      const ct = t < T1 ? Math.max(0, Math.min(c, w + st.inc[t])) : Math.max(0, (w + st.PV[t]) / ANN[t]);
      spendY[t][k] = ct / 12;
      const ret = R.s * (Math.exp(m + sd * gauss()) - 1) + (1 - R.s) * R.rc;
      w = (w + st.inc[t] - ct) * (1 + ret);
      wealthY[t][k] = w + reserve;
    }
  }
  const band = Y => Y.map(xs => { xs.sort(); return [0.1, 0.25, 0.5, 0.75, 0.9].map(p => xs[Math.min(xs.length - 1, Math.floor(p * xs.length))]); });
  return {spend: band(spendY), wealth: band(wealthY)};
}

// Solves are cached by the parameters, so switching pages or redrawing never re-solves.
const memo = new Map();
function cached(kind, P, g, f) {
  const k = kind + "|" + g + "|" + JSON.stringify(P);
  if (!memo.has(k)) { if (memo.size > 200) memo.clear(); memo.set(k, f()); }
  return memo.get(k);
}
return {
  rates, states, streamAt, util,
  solve: (P, g = P.gamma) => cached("solve", P, g, () => solve(P, g)),
  simulate: (P, c) => cached("sim", P, c, () => simulate(P, c)),
  _raw: {solve, simulate},
};
})();
if (typeof module !== "undefined") module.exports = Budget;
