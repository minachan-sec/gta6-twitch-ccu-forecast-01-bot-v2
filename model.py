"""GTA VI Twitch CCU forecaster v2: ensemble kernel + per-hour regression.

Method (fresh design, validated on practice + leave-one-out backtest):
  For each hour h = 2..24, forecast the log-ratio ln(viewers_h / hour1) as:
    median = 0.3 * kernel-weighted median + 0.7 * WLS regression prediction
  where the kernel weights past launches by similarity of log(hour-1 size)
  (Gaussian, bandwidth 1.0), x3 for the same unlock class (rolling midnight
  vs simultaneous global), and x0.3 for launches flagged early-access
  contaminated or weak tier. The regression is a per-hour weighted least
  squares fit of log-ratio on log(hour-1), which extrapolates the size
  trend beyond the biggest comparable (GTA VI's hour-1 will likely exceed
  every comparable's). Spread: asymmetric normal, per-hour sigma from the
  weighted residual std around the ensemble median (upper/lower separately),
  smoothed across hours, scaled 0.8, floored at 0.07*sqrt(h-1).

  Measured: practice 0.170 (v1: 0.222, house: 0.387); LOO over clean
  launches 0.430 (v1: 0.453); LOO on big launches 0.170 (v1: 0.188).
"""
import json
import math
import re
from datetime import datetime

Z = (-1.645, -0.674, 0.0, 0.674, 1.645)
LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)

# Tuned constants (from backtest; see README).
BW = 1.0            # kernel bandwidth on log(hour-1)
UNLOCK_BOOST = 3.0  # weight multiplier for same unlock class
DOWNWEIGHT = 0.3    # weight multiplier for contaminated/weak-tier launches
W_REG = 0.7         # regression weight in the median blend
SPREAD_SCALE = 0.8  # global sigma scale (empirical spreads run wide)
SPREAD_FLOOR = 0.07  # sigma floor coefficient: floor = 0.07 * sqrt(h - 1)
SIGMA_SMOOTH = 0.5  # 3-hour moving-average strength for sigma curves


def _parse(t):
    return datetime.fromisoformat(t.replace('Z', '+00:00'))


def _tier(text):
    m = re.search(r'Tier:\s*([a-z-]+)', text or '')
    return m.group(1) if m else 'none'


def _uclass(text):
    u = (text or '').lower()
    if 'roll' in u or 'midnight local' in u:
        return 'rolling'
    if 'simultaneous' in u or 'global unlock' in u or 'single global' in u:
        return 'simultaneous'
    return 'other'


def _shapes(case):
    """Extract usable launch shapes from the case's comparables.

    Returns a list of dicts: v1, ratios (23 log-ratios, None where missing),
    tier, uclass. Sorted by name for determinism.
    """
    out = []
    for c in sorted(case.get('comparables') or [], key=lambda x: x.get('name') or ''):
        try:
            ds = _parse(c['day_start'])
        except Exception:
            continue
        by_hour = {}
        for p in c.get('hourly') or []:
            try:
                h = int((_parse(p['from']) - ds).total_seconds() // 3600)
                v = p.get('viewers')
            except Exception:
                continue
            if -400 <= h < 96 and isinstance(v, (int, float)) and v is not None:
                by_hour[h] = v
        if 0 not in by_hour or not by_hour[0] or by_hour[0] < 1:
            continue
        v1 = by_hour[0]
        ratios = []
        for h in range(1, 24):
            v = by_hour.get(h)
            ratios.append(math.log(v / v1) if v is not None and v >= 1 else None)
        if sum(1 for r in ratios if r is not None) < 18:
            continue
        out.append({'v1': v1, 'ratios': ratios,
                    'tier': _tier(c.get('unlock') or ''),
                    'uclass': _uclass(c.get('unlock') or '')})
    return out


def _weights(p1, target_cls, pool):
    lv = math.log(max(p1, 1))
    ws = []
    for o in pool:
        w = math.exp(-((lv - math.log(o['v1'])) ** 2) / (2 * BW * BW))
        if o['uclass'] == target_cls:
            w *= UNLOCK_BOOST
        if o['tier'] != 'comparable':
            w *= DOWNWEIGHT
        ws.append(w)
    return ws


def _wmedian(pts):
    """Weighted median of (value, weight) pairs."""
    pts = sorted(pts)
    tot = sum(w for _, w in pts)
    if tot <= 0:
        return pts[len(pts) // 2][0]
    acc = 0.0
    for r, w in pts:
        acc += w
        if acc >= 0.5 * tot:
            return r
    return pts[-1][0]


def _wls(xs, ys, ws):
    """Weighted least squares y ~ a + b x. Returns (a, b) or None.

    The slope is clamped to [-2, 1]: the size trend is modestly negative
    (bigger launches have flatter shapes), and an extreme slope fitted from
    a few noisy points would explode when extrapolated to GTA VI's hour-1,
    which sits beyond every comparable.
    """
    sw = sum(ws)
    if sw <= 0 or len(xs) < 3:
        return None
    mx = sum(w * x for x, w in zip(xs, ws)) / sw
    my = sum(w * y for y, w in zip(ys, ws)) / sw
    sxx = sum(w * (x - mx) ** 2 for x, w in zip(xs, ws))
    if sxx <= 1e-9:
        return (my, 0.0)
    sxy = sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws))
    b = sxy / sxx
    b = max(-2.0, min(1.0, b))
    return (my - b * mx, b)


def _wstd_below_above(pts, med):
    """Weighted std of residuals above and below the median, separately."""
    up = [(r - med, w) for r, w in pts if r >= med]
    dn = [(r - med, w) for r, w in pts if r < med]

    def _std(seg):
        s = sum(w for _, w in seg)
        if s <= 0:
            return None
        return math.sqrt(sum(w * d * d for d, w in seg) / s)

    su, sd = _std(up), _std(dn)
    if su is None:
        su = sd
    if sd is None:
        sd = su
    return su, sd


def _smooth3(v, strength):
    out = []
    for i in range(len(v)):
        lo = v[i - 1] if i > 0 else v[i]
        hi = v[i + 1] if i < len(v) - 1 else v[i]
        out.append((1 - strength) * v[i] + strength * (lo + v[i] + hi) / 3.0)
    return out


def _flat_rows(p1):
    """House-style fallback: flat at hour-1 with widening spread."""
    rows = []
    for h in range(2, 25):
        s = 0.15 * math.sqrt(h - 1)
        rows.append([p1 * math.exp(z * s) for z in Z])
    return rows


def forecast(case):
    """Forecast one case. Returns 23 rows (hours 2..24) of 5 quantiles."""
    try:
        known = [k for k in case.get('known') or [] if k.get('hour') == 1]
        p1 = float(known[0]['viewers']) if known else 1.0
    except Exception:
        p1 = 1.0
    if not (p1 > 0) or not math.isfinite(p1):
        p1 = 1.0
    pool = _shapes(case)
    if not pool:
        return _flat_rows(p1)
    target_cls = _uclass(case.get('notes') or '')
    lv = math.log(p1)
    ws = _weights(p1, target_cls, pool)
    meds, sig_up, sig_dn = [], [], []
    for i in range(23):
        pts = [(o['ratios'][i], w) for o, w in zip(pool, ws)
               if o['ratios'][i] is not None]
        if not pts:
            meds.append(0.0)
            f = SPREAD_FLOOR * math.sqrt(i + 1)
            sig_up.append(f)
            sig_dn.append(f)
            continue
        med_k = _wmedian(pts)
        xs = [math.log(o['v1']) for o in pool if o['ratios'][i] is not None]
        ys = [o['ratios'][i] for o in pool if o['ratios'][i] is not None]
        w2 = [w for o, w in zip(pool, ws) if o['ratios'][i] is not None]
        fit = _wls(xs, ys, w2)
        med_r = fit[0] + fit[1] * lv if fit else med_k
        med = (1 - W_REG) * med_k + W_REG * med_r
        meds.append(med)
        su, sd = _wstd_below_above(pts, med)
        f = SPREAD_FLOOR * math.sqrt(i + 1)
        sig_up.append(su if su is not None else f)
        sig_dn.append(sd if sd is not None else f)
    sig_up = _smooth3(sig_up, SIGMA_SMOOTH)
    sig_dn = _smooth3(sig_dn, SIGMA_SMOOTH)
    # Live-stream concentration (final case only; null in practice): when one
    # stream holds most of the category, the count is more fragile (one
    # streamer ending a broadcast drops it sharply), so widen the spread a
    # little. Unvalidated signal: capped at +15%, no effect when absent.
    widen = 1.0
    try:
        streams = case.get('streams') or []
        top = max((float(s.get('viewers') or 0) for s in streams), default=0.0)
        if top > 0 and p1 > 0:
            conc = top / p1
            widen = min(1.15, 1.0 + 0.5 * max(0.0, conc - 0.4))
    except Exception:
        widen = 1.0
    rows = []
    for i in range(23):
        fl = SPREAD_FLOOR * math.sqrt(i + 1)
        su = max(fl, sig_up[i] * SPREAD_SCALE * widen)
        sd = max(fl, sig_dn[i] * SPREAD_SCALE * widen)
        qs = [meds[i] - 1.645 * sd, meds[i] - 0.674 * sd, meds[i],
              meds[i] + 0.674 * su, meds[i] + 1.645 * su]
        vals = []
        prev = 0.0
        for q in qs:
            v = p1 * math.exp(q)
            if not math.isfinite(v) or v <= 0:
                v = 1.0
            v = min(v, 1e9)
            v = max(v, prev)
            vals.append(v)
            prev = v
        rows.append(vals)
    return rows
