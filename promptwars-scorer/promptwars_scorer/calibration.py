"""Small-data calibration: engine score -> estimate of the real grader's score.

Design constraints, in order:

* The labeled set is tiny (single digits to a few dozen) and top-heavy. Anything
  flexible memorises it. So the models are deliberately rigid and shrunk.
* A correction is only switched ON when it beats BOTH dumb baselines out of
  sample (leave-one-out): "use the raw engine score" and "always guess the
  average real score". On compressed targets the second baseline is strong, and
  a model that cannot beat it has learned nothing.
* Otherwise the layer abstains and reports why. Abstaining is a valid result.

Models (all monotone non-decreasing, pure Python, no numpy/sklearn):
  offset    y = x + d            d = shrunk mean residual         (n >= 5)
  affine    y = a + b*x          b in [0, 1.5], shrunk toward 1   (n >= 8)
  isotonic  pool-adjacent-violators step fit                      (n >= 30)

Fitted per key ("composite" plus each parameter that has labels) and stored in
$PROMPTWARS_DATA_DIR/calibration.json.
"""

from __future__ import annotations

import json
import time
from statistics import mean

from . import records as rec_store
from .rubric import ENGINE_VERSION

MIN_N = 5
MIN_N_AFFINE = 8
MIN_N_ISOTONIC = 30
SHRINK_K = 1.0          # pseudo-observations pulling the correction toward "no change"
REQUIRED_GAIN = 0.05    # must beat the best baseline by 5% LOO MAE to switch on


def _clip(v: float) -> float:
    return max(0.0, min(100.0, v))


# ---- model fits: each returns a predictor f(x) -> y ---------------------------

def _fit_offset(xs, ys):
    n = len(xs)
    d = mean(y - x for x, y in zip(xs, ys)) * n / (n + SHRINK_K)
    return (lambda x: _clip(x + d)), {"type": "offset", "d": round(d, 3)}


def _fit_affine(xs, ys):
    n = len(xs)
    mx, my = mean(xs), mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b_ols = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 1e-9 else 1.0
    b = max(0.0, min(1.5, b_ols))
    w = n / (n + SHRINK_K)
    b = w * b + (1 - w) * 1.0            # shrink slope toward 1 (identity)
    a = my - b * mx
    return (lambda x: _clip(a + b * x)), {"type": "affine", "a": round(a, 3), "b": round(b, 3)}


def _fit_isotonic(xs, ys):
    pts = sorted(zip(xs, ys))
    blocks = [[x, x, y, 1] for x, y in pts]          # [x_lo, x_hi, mean_y, weight]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][2] > blocks[i + 1][2]:          # violation -> pool
            a, b = blocks[i], blocks[i + 1]
            w = a[3] + b[3]
            blocks[i] = [a[0], b[1], (a[2] * a[3] + b[2] * b[3]) / w, w]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    knots = [((b[0] + b[1]) / 2, b[2]) for b in blocks]

    def f(x):
        if x <= knots[0][0]:
            return _clip(knots[0][1])
        if x >= knots[-1][0]:
            return _clip(knots[-1][1])
        for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
            if x0 <= x <= x1:
                t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
                return _clip(y0 + t * (y1 - y0))
        return _clip(knots[-1][1])

    return f, {"type": "isotonic", "knots": [[round(a, 2), round(b, 2)] for a, b in knots]}


_FITTERS = (("offset", MIN_N, _fit_offset),
            ("affine", MIN_N_AFFINE, _fit_affine),
            ("isotonic", MIN_N_ISOTONIC, _fit_isotonic))


def _predictor(params: dict):
    t = params["type"]
    if t == "offset":
        return lambda x: _clip(x + params["d"])
    if t == "affine":
        return lambda x: _clip(params["a"] + params["b"] * x)
    knots = params["knots"]

    def f(x):
        if x <= knots[0][0]:
            return knots[0][1]
        if x >= knots[-1][0]:
            return knots[-1][1]
        for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
            if x0 <= x <= x1:
                return y0 + ((x - x0) / (x1 - x0) if x1 > x0 else 0) * (y1 - y0)
        return knots[-1][1]
    return f


def _loo_errors(xs, ys, fitter):
    errs = []
    for i in range(len(xs)):
        f, _ = fitter(xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:])
        errs.append(abs(f(xs[i]) - ys[i]))
    return errs


def _spearman(xs, ys) -> float | None:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return r
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / den, 3) if den else None


def fit_key(pairs: list[tuple[float, float]]) -> dict:
    """Fit one key from (engine, real) pairs and decide whether to enable it."""
    n = len(pairs)
    xs = [float(p[0]) for p in pairs]
    ys = [float(p[1]) for p in pairs]
    out: dict = {"n": n, "enabled": False}
    if n:
        out["engine_range"] = [min(xs), max(xs)]
        out["real_range"] = [min(ys), max(ys)]
        out["raw_mae"] = round(mean(abs(x - y) for x, y in zip(xs, ys)), 3)
        out["spearman"] = _spearman(xs, ys)
    if n < MIN_N:
        out["status"] = "insufficient_support"
        out["reason"] = f"{n} labeled submission(s); need at least {MIN_N} to fit anything."
        return out

    # Baseline 2: always guess the average real score (leave-one-out).
    mean_mae = mean(abs(mean(ys[:i] + ys[i + 1:]) - ys[i]) for i in range(n))
    out["mean_baseline_mae"] = round(mean_mae, 3)
    best_baseline = min(out["raw_mae"], mean_mae)

    best = None
    for name, min_n, fitter in _FITTERS:
        if n < min_n:
            continue
        errs = _loo_errors(xs, ys, fitter)
        mae = mean(errs)
        if best is None or mae < best[1]:
            best = (name, mae, errs, fitter)
    name, mae, errs, fitter = best
    out["model"] = name
    out["loo_mae"] = round(mae, 3)

    if mae >= best_baseline * (1 - REQUIRED_GAIN):
        out["status"] = "no_gain"
        which = "the raw engine score" if out["raw_mae"] <= mean_mae else "guessing the average"
        out["reason"] = (f"Best model ({name}) has held-out error {mae:.2f}, which does not beat "
                         f"{which} ({best_baseline:.2f}). Calibration stays off.")
        return out

    _, params = fitter(xs, ys)
    out.update({
        "enabled": True,
        "status": "active",
        "params": params,
        # 80th percentile of held-out absolute error: an honest +/- band.
        "band": round(sorted(errs)[min(n - 1, int(0.8 * n))], 2),
    })
    return out


def fit_all(records: list[dict] | None = None) -> dict:
    """Fit every key that has (engine, real) pairs and persist the result."""
    records = rec_store.load() if records is None else records
    keys: dict[str, list[tuple[float, float]]] = {k: [] for k in rec_store.SCORE_KEYS}
    for r in records:
        eng, h2s = r.get("engine") or {}, r.get("h2s") or {}
        for k in rec_store.SCORE_KEYS:
            x = eng.get("composite") if k == "composite" else (eng.get("params") or {}).get(k)
            if x is not None and k in h2s:
                keys[k].append((float(x), float(h2s[k])))
    model = {
        "version": time.strftime("cal_%Y%m%d_%H%M"),
        "engine_version": ENGINE_VERSION,
        "labeled_records": len([r for r in records if r.get("h2s")]),
        "evaluated_records": len([r for r in records if r.get("engine")]),
        "keys": {k: fit_key(v) for k, v in keys.items()},
    }
    (rec_store.data_dir() / "calibration.json").write_text(json.dumps(model, indent=2) + "\n")
    return model


def load_model() -> dict | None:
    p = rec_store.data_dir() / "calibration.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def apply(model: dict | None, key: str, x: float) -> dict:
    """Estimate for one key. Always returns a status; estimate is None when off."""
    k = ((model or {}).get("keys") or {}).get(key)
    if not k:
        return {"estimate": None, "status": "not_fitted",
                "reason": "No calibration has been fitted yet."}
    if not k.get("enabled"):
        return {"estimate": None, "status": k.get("status", "off"), "reason": k.get("reason", ""),
                "n": k.get("n", 0)}
    lo, hi = k["engine_range"]
    res = {"estimate": round(_predictor(k["params"])(x), 1), "band": k["band"], "status": "active",
           "n": k["n"], "model": k["model"], "loo_mae": k["loo_mae"],
           "version": (model or {}).get("version")}
    if x < lo - 5 or x > hi + 5:
        res["status"] = "extrapolated"
        res["reason"] = (f"Engine score {x:g} is outside the labeled range {lo:g}-{hi:g}; "
                         "treat the estimate as weak.")
    return res
