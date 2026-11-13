"""Price-only setup scanners for the Signals page. Every rule is plain arithmetic on a name's own price file,
so the same rules run for the US and India and nothing depends on TMLE.

Setups (key -> label):
  reclaim   Stage 4->1 reclaim: first close back above the 200 EMA after a deep, long decline (or a flat base at the 200 that breaks out)
  earnings  gap + volume on a name already at/above a rising-or-flat 200 EMA
  cup       (kept in render.py) 15-40% cup, plus a stage chip
  vcp       successive weekly contractions, tightening, near the pivot
  breakdown Stage 4 breakdown: a 100%+ run that never lost the 50/200, now losing them on volume
"""
import datetime


def ema(vals, w):
    k, e, out = 2.0 / (w + 1), None, []
    for x in vals:
        if x is not None:
            e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _clean(p):
    c = p.get("close") or []
    if len(c) < 150 or c[-1] is None:
        return None
    return p


def stage_chip(c, e50, e200, n):
    """Plain stage read from the 50/200 EMAs: 2 = above both with a rising 200, 4 = below a falling 200,
    3 = above the 200 but under the 50, 1 = anything else near a flat 200."""
    if e200[n] is None or e50[n] is None or n < 30 or e200[n - 20] is None:
        return None
    slope = e200[n] / e200[n - 20] - 1
    if c[n] > e50[n] > e200[n] and slope > 0.005:
        return 2
    if c[n] < e200[n] and slope < -0.005:
        return 4
    if c[n] > e200[n] and c[n] < e50[n]:
        return 3
    return 1


def reclaim(p, n=None, max_age=20, deep=0.40, below_need=60, look=120):
    c, h, v, d = p["close"], p.get("high") or [], p.get("volume") or [], p["dates"]
    n = len(c) - 1 if n is None else n
    if n < 220:
        return None
    E = ema(c, 200)
    # most recent upward cross inside the window, after a deep, long decline
    for i in range(n, max(n - max_age, 200) - 1, -1):
        if c[i] is None or c[i - 1] is None or not (c[i] > E[i] and c[i - 1] <= E[i - 1]):
            continue
        below = sum(1 for j in range(i - look, i) if c[j] is not None and E[j] is not None and c[j] < E[j])
        w0 = max(0, i - 300)
        seg = [x for x in c[w0:i] if x is not None]
        if not seg:
            continue
        pk = max(range(w0, i), key=lambda j: c[j] if c[j] is not None else -1)
        tr = min(c[j] for j in range(pk, i) if c[j] is not None)
        dd = 1 - tr / c[pk]
        if below >= below_need and dd >= deep:
            again = any(c[j] is not None and c[j - 1] is not None and c[j] > E[j] and c[j - 1] <= E[j - 1] for j in range(max(1, i - 120), i - 5))
            return {"kind": "reclaim", "sub": "Re-reclaim" if again else "First reclaim", "daysAgo": n - i, "firedDate": d[i],
                    "declinePct": round(-dd * 100), "sessionsBelow": below,
                    "above200": c[n] > E[n], "extPct": round((c[n] / E[n] - 1) * 100, 1)}
        break
    # a flat base sitting on the 200 that breaks out on volume (AMD / DELL style)
    E50 = ema(c, 50)
    for i in range(n, max(n - 10, 80) - 1, -1):
        base = range(i - 45, i - 3)
        if any(c[j] is None or E[j] is None for j in base):
            continue
        if max(abs(c[j] / E[j] - 1) for j in base) > 0.14:
            continue
        if abs(E[i - 3] / E[i - 33] - 1) > 0.06:
            continue
        top = max(h[j] if h[j] is not None else c[j] for j in base)
        vol30 = [x for x in v[max(0, i - 31):i] if x]
        if c[i] is not None and c[i] > top and c[i] > E[i] and c[i - 1] <= top and vol30 and (v[i] or 0) >= 1.5 * sum(vol30) / len(vol30):
            return {"kind": "reclaim", "sub": "Base breakout at the 200", "daysAgo": n - i, "firedDate": d[i],
                    "declinePct": None, "sessionsBelow": None, "above200": c[n] > E[n], "extPct": round((c[n] / E[n] - 1) * 100, 1)}
    return None


def _weekly(p):
    W = {}
    for i, dt in enumerate(p["dates"]):
        y, w, _ = datetime.date.fromisoformat(dt).isocalendar()
        r = W.setdefault((y, w), {"d": dt, "h": -1e18, "l": 1e18, "c": None, "i": i})
        if p["high"][i] is not None:
            r["h"] = max(r["h"], p["high"][i])
        if p["low"][i] is not None:
            r["l"] = min(r["l"], p["low"][i])
        r["c"], r["d"], r["i"] = p["close"][i], dt, i
    return [W[k] for k in sorted(W)]


def _swings(W, thr):
    pts, dirn, hi, lo = [], 0, (0, W[0]["h"]), (0, W[0]["l"])
    for i in range(1, len(W)):
        if W[i]["h"] > hi[1]:
            hi = (i, W[i]["h"])
        if W[i]["l"] < lo[1]:
            lo = (i, W[i]["l"])
        if dirn >= 0 and W[i]["l"] <= hi[1] * (1 - thr) and i > hi[0]:
            pts.append(("H",) + hi)
            dirn, lo, hi = -1, (i, W[i]["l"]), (i, W[i]["h"])
        elif dirn <= 0 and W[i]["h"] >= lo[1] * (1 + thr) and i > lo[0]:
            pts.append(("L",) + lo)
            dirn, hi, lo = 1, (i, W[i]["h"]), (i, W[i]["l"])
    return pts


def vcp(p, n=None, thr=0.10, max_decline=0.40, grow=1.25, last_max=0.18, near=0.05):
    """Weekly swings of >=10%: each contraction no more than 25% deeper than the one before, the last under 15%,
    above the 50 and 200 EMA, and within 5% of the pivot. 'breakout' once it closes above the pivot."""
    c, d = p["close"], p["dates"]
    n = len(c) - 1 if n is None else n
    if n < 250:
        return None
    q = {k: (p[k][:n + 1] if p.get(k) else []) for k in ("dates", "open", "high", "low", "close", "volume")}
    W = _weekly(q)
    W = W[-70:]
    pts = _swings(W, thr)
    if len(pts) < 4:
        return None
    # contractions = each swing high followed by its low; the base starts at the highest swing high in the window
    highs = [x for x in pts if x[0] == "H"]
    if not highs:
        return None
    base_i = max(range(len(pts)), key=lambda j: pts[j][2] if pts[j][0] == "H" else -1)
    seq = []
    for a, b in zip(pts[base_i:], pts[base_i + 1:]):
        if a[0] == "H" and b[0] == "L":
            seq.append((a, b, 1 - b[2] / a[2]))
    if len(seq) < 2:
        return None
    # a lower low than the base start's first low by too much breaks the pattern
    depths = [s[2] for s in seq[-5:]]
    if depths[0] < 0.12 or depths[-1] > last_max:
        return None
    for x, y in zip(depths, depths[1:]):
        if y > x * grow:
            return None
    E50, E200 = ema(c, 50), ema(c, 200)
    if c[n] < E50[n] or c[n] < E200[n]:
        return None
    # the prior decline must be shallow, otherwise it is a Stage 4->1 name, not a VCP
    seg = [x for x in c[max(0, n - 252):n + 1] if x is not None]
    run_max, worst = 0, 0
    for x in seg:
        run_max = max(run_max, x)
        worst = max(worst, 1 - x / run_max)
    if worst >= max_decline + 0.0001 and depths[0] < 0.12:
        return None
    if worst >= 0.55:
        return None
    # pivot: the highest high of the base before the latest week
    last_low_w = seq[-1][1][1]
    pivot = max(seq[-1][0][2], max(W[j]["h"] for j in range(last_low_w, len(W) - 1)) if last_low_w < len(W) - 1 else 0)
    dist = c[n] / pivot - 1
    broke = c[n] > pivot and any(c[j] is not None and c[j] <= pivot for j in range(n - 10, n))
    if not broke and not (-near <= dist <= 0):
        return None
    return {"kind": "vcp", "sub": "Breakout" if broke else "Forming", "pivot": round(pivot, 2), "distPct": round(dist * 100, 1),
            "contractions": [round(-x * 100, 1) for x in depths], "declinePct": round(-worst * 100),
            "firedDate": d[n] if broke else None, "daysAgo": 0 if broke else None}


def breakdown(p, n=None, run_min=1.0, max_age=90, vol_mult=1.3, min_off=0.12):
    """Stage 4 breakdown: a run of at least +100% that held its 200 EMA, peaked above its 50, and has since closed
    under the 50 on volume. Shown for up to 90 sessions after the first close under the 50, with the days since, so fresh breaks sort first."""
    c, v, d = p["close"], p.get("volume") or [], p["dates"]
    n = len(c) - 1 if n is None else n
    if n < 150:
        return None
    E50, E200 = ema(c, 50), ema(c, 200)
    pk = max(range(max(0, n - 70), n + 1), key=lambda j: c[j] if c[j] is not None else -1)
    if pk >= n - 2 or c[pk] is None or E50[pk] is None or c[pk] < E50[pk]:
        return None
    w0 = max(0, pk - 300)
    lo = min(x for x in c[w0:pk + 1] if x is not None)
    if lo <= 0 or c[pk] / lo - 1 < run_min:
        return None
    run = range(max(w0 + 5, pk - 150), pk)
    bad200 = sum(1 for j in run if c[j] is not None and E200[j] is not None and c[j] < E200[j] * 0.97)
    if run and bad200 / len(run) > 0.10:
        return None
    if c[n] is None or c[n] >= E50[n] or c[n] / c[pk] - 1 > -min_off:
        return None
    hit = next((i for i in range(pk + 1, n + 1) if c[i] is not None and E50[i] is not None and c[i] < E50[i]), None)
    if hit is None or n - hit > max_age:
        return None
    on_vol = None
    for i in range(hit, n + 1):
        vol30 = [x for x in v[max(0, i - 31):i] if x]
        if c[i] is not None and c[i] < E50[i] and vol30 and (v[i] or 0) >= vol_mult * sum(vol30) / len(vol30):
            on_vol = i
            break
    if on_vol is None:
        return None
    return {"kind": "breakdown", "sub": "Below the 200" if c[n] < E200[n] else "Below the 50", "daysAgo": n - hit, "firedDate": d[hit],
            "runPct": round((c[pk] / lo - 1) * 100), "offPeakPct": round((c[n] / c[pk] - 1) * 100, 1), "volDate": d[on_vol]}
