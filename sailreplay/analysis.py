"""
Race analysis for Vakaros logs without a wind sensor.

Turns one or more parsed VKX logs into a single race model (plain dict, JSON-ready)
used by the replay page and the CSV exports.

True wind direction (TWD) is estimated from the boats themselves, in this order:
  1. a value you give (--twd)
  2. the bisector of the fleet's tacks and gybes (wind axis), with the upwind end
     chosen from which way the fleet sails after the gun (or which side is slower)
  3. the perpendicular to the pinged start line
The estimate is time-varying: each tack re-measures the wind, so persistent shifts show up.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import polar
from .vkx import VkxLog

KT_TO_MS = 0.514444
PALETTE = ["#e4572e", "#29a3e0", "#f3c613", "#5bc06b", "#b07ce8", "#ff8fb1",
           "#ffffff", "#ff9f1c", "#2ec4b6", "#c0c0c0"]


# --------------------------------------------------------------------------- helpers
def wrap180(a):
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


def circ_mean(deg, w=None):
    r = np.radians(np.asarray(deg, dtype=float))
    w = np.ones_like(r) if w is None else np.asarray(w, dtype=float)
    return math.degrees(math.atan2(np.nansum(w * np.sin(r)), np.nansum(w * np.cos(r)))) % 360


def circ_smooth(deg, n):
    """Centred moving circular mean over n samples, NaN aware."""
    r = np.radians(deg)
    s, c = np.sin(r), np.cos(r)
    return np.degrees(np.arctan2(_nan_movmean(s, n), _nan_movmean(c, n))) % 360


def _nan_movmean(x, n):
    n = max(1, int(n))
    if n == 1:
        return x.copy()
    v = np.where(np.isnan(x), 0.0, x)
    m = (~np.isnan(x)).astype(float)
    k = np.ones(n)
    num = np.convolve(v, k, mode="same")
    den = np.convolve(m, k, mode="same")
    out = num / np.where(den == 0, np.nan, den)
    out[np.isnan(x)] = np.nan
    return out


def _nan_movmedian(x, n):
    n = max(1, int(n)) | 1
    pad = n // 2
    xp = np.pad(x, pad, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(xp, n)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return np.nanmedian(win, axis=1)


def _r(x, nd=2):
    """Round floats for JSON, NaN -> None."""
    if x is None:
        return None
    if isinstance(x, (list, tuple, np.ndarray)):
        return [_r(v, nd) for v in x]
    try:
        f = float(x)
    except (TypeError, ValueError):
        return x
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


# --------------------------------------------------------------------------- data prep
@dataclass
class BoatSeries:
    name: str
    color: str
    log: VkxLog
    lat: np.ndarray = None
    lon: np.ndarray = None
    x: np.ndarray = None
    y: np.ndarray = None
    sog: np.ndarray = None
    cog: np.ndarray = None
    hdg: np.ndarray = None
    heel: np.ndarray = None
    pitch: np.ndarray = None
    valid: np.ndarray = None


def find_guns(logs: list[VkxLog]) -> list[int]:
    """Gun times (ms) from race timer events across all boats, clustered within 30 s."""
    cands = []
    for log in logs:
        for e in log.timer:
            if e["event"] == "race_start":
                cands.append(e["t"])
            elif e["event"] in ("start", "sync") and e["value_s"] > 0:
                cands.append(e["t"] + e["value_s"] * 1000)
    cands.sort()
    guns: list[list[int]] = []
    for c in cands:
        if guns and c - guns[-1][-1] < 30_000:
            guns[-1].append(c)
        else:
            guns.append([c])
    return [int(np.median(g)) for g in guns]


def _resample(b: BoatSeries, grid_ms: np.ndarray, heel_offset: float, max_gap_ms=4000):
    tr = b.log.track
    t = np.array([r["t"] for r in tr], dtype=np.int64)
    out_n = len(grid_ms)
    if len(t) < 2:
        b.valid = np.zeros(out_n, bool)
        for k in ("lat", "lon", "sog", "cog", "hdg", "heel", "pitch"):
            setattr(b, k, np.full(out_n, np.nan))
        return
    def col(k):
        return np.array([r[k] for r in tr], dtype=float)
    idx = np.searchsorted(t, grid_ms)
    lo = np.clip(idx - 1, 0, len(t) - 1)
    hi = np.clip(idx, 0, len(t) - 1)
    near = np.minimum(np.abs(grid_ms - t[lo]), np.abs(t[hi] - grid_ms))
    valid = (grid_ms >= t[0]) & (grid_ms <= t[-1]) & (near <= max_gap_ms)
    tf = t.astype(float)
    g = grid_ms.astype(float)
    b.lat = np.interp(g, tf, col("lat"))
    b.lon = np.interp(g, tf, col("lon"))
    b.sog = np.interp(g, tf, col("sog_kt"))
    b.cog = np.interp(g, tf, np.unwrap(np.radians(col("cog")))) * 180 / np.pi % 360
    b.hdg = np.interp(g, tf, np.unwrap(np.radians(col("heading")))) * 180 / np.pi % 360
    b.heel = np.interp(g, tf, col("heel")) - heel_offset
    b.pitch = np.interp(g, tf, col("pitch"))
    for k in ("lat", "lon", "sog", "cog", "hdg", "heel", "pitch"):
        a = getattr(b, k)
        a[~valid] = np.nan
    b.valid = valid


# --------------------------------------------------------------------------- manoeuvre bootstrap
def _turns_from_cog(cog, sog, hz):
    """Find big course changes (tacks/gybes) without knowing the wind. Returns (idx, bisector, turn)."""
    out = []
    n = len(cog)
    if n < hz * 60:
        return out
    cs = circ_smooth(cog, max(1, int(2 * hz)))
    w = int(8 * hz)
    st = int(18 * hz)
    uw = np.unwrap(np.radians(np.nan_to_num(cs, nan=0.0))) * 180 / np.pi
    i = w + st
    last = -10**9
    while i < n - w - st:
        if np.isnan(cs[i - w - st:i + w + st]).any() or np.nanmean(sog[i - w - st:i + w + st]) < 1.5:
            i += 1
            continue
        d = uw[i + w] - uw[i - w]
        if abs(d) > 55 and abs(d) < 160 and i - last > 20 * hz:
            before = uw[i - w - st:i - w]
            after = uw[i + w:i + w + st]
            if before.std() < 10 and after.std() < 10:
                # refine to the point of maximum turn rate
                seg = np.abs(np.diff(uw[i - w:i + w]))
                j = i - w + int(np.argmax(seg))
                b0, a0 = before.mean(), after.mean()
                out.append((j, (b0 + (a0 - b0) / 2) % 360, a0 - b0))
                last = i
                i += int(20 * hz)
                continue
        i += 1
    return out



# --------------------------------------------------------------------------- race detection
def _course_chains(log: VkxLog, hz: float = 1.0):
    """Find races in one boat's log from how it sails: a beat, then a run, then more legs,
    with no long pause in between. Returns a list of dicts with start/finish ms and legs."""
    if len(log.track) < 600:
        return []
    grid = np.arange(log.start_ms, log.end_ms, int(1000 / hz), dtype=np.int64)
    n = len(grid)
    s = BoatSeries("b", "#fff", log)
    _resample(s, grid, 0.0)
    lat0 = float(np.nanmean(s.lat))
    s.x = (s.lon - np.nanmean(s.lon)) * 111_320 * math.cos(math.radians(lat0))
    s.y = (s.lat - lat0) * 111_320
    twd, _, _ = _estimate_wind([s], n, hz, None, None, None, [log], [])
    if twd is None:
        return []
    cs = circ_smooth(s.cog, max(1, int(3 * hz)))
    vmg = s.sog * np.cos(np.radians(wrap180(twd - cs)))
    v = _nan_movmean(np.nan_to_num(vmg), int(40 * hz))
    lab = np.where(v > 1.0, 1, np.where(v < -1.0, -1, 0))
    legs = [[int(l), a, b] for l, a, b in _segments(list(lab), int(30 * hz)) if l != 0 and b - a >= 40 * hz]
    merged = []
    for L in legs:                                   # same direction with a short pause = one leg
        if merged and merged[-1][0] == L[0] and L[1] - merged[-1][2] <= 60 * hz:
            merged[-1][2] = L[2]
        else:
            merged.append(L)
    def joined(prev, nxt):
        # legs belong to one race if the gap between them is a rounding (75 s), or a reach
        # (triangle and gold-cup courses) sailed with purpose for up to 5 minutes
        gap = nxt[1] - prev[2]
        if gap <= 75 * hz:
            return True
        if gap <= 300 * hz:
            a, b = prev[2], nxt[1]
            g = s.sog[a:b]
            if not len(g) or np.nanmedian(g) < 2.5:
                return False
            path = np.nansum(np.hypot(np.diff(s.x[a:b]), np.diff(s.y[a:b])))
            net = math.hypot(s.x[b - 1] - s.x[a], s.y[b - 1] - s.y[a])
            return path > 0 and net / path >= 0.7               # a reach is sailed straight; milling is not
        return False

    chains, cur = [], []
    for L in merged:
        if cur and L[0] != cur[-1][0] and joined(cur[-1], L):
            cur.append(L)
        else:
            if cur:
                chains.append(cur)
            cur = [L]
    if cur:
        chains.append(cur)
    p = np.cumsum(np.nan_to_num(vmg) * KT_TO_MS / hz)
    out = []
    for c in chains:
        # a race opens with a real beat followed straight away by a real run
        while len(c) >= 2 and not (c[0][0] == 1 and c[0][2] - c[0][1] >= 150 * hz
                                   and c[1][0] == -1 and c[1][2] - c[1][1] >= 150 * hz):
            c = c[1:]
        if len(c) < 2 or c[0][0] != 1:
            continue
        while len(c) > 2 and c[-1][0] == -1 and c[-1][2] - c[-1][1] < 90 * hz:
            c = c[:-1]                               # bearing away after finishing
        a, b = c[0][1], c[-1][2]
        # a race is sailed: a parked or drifting boat (GPS wander, a boat on a trailer) is not racing
        seg = s.sog[a:b]
        if not len(seg) or np.nanmedian(seg) < 2.0 or np.nanmean(seg > 2.0) < 0.6:
            continue
        # sharpen the start: first moment the boat is properly going upwind
        first = vmg[a:c[0][2]]
        target = 0.6 * np.nanmedian(first) if np.isfinite(np.nanmedian(first)) else 1.0
        lo = max(0, a - int(40 * hz))
        st = next((i for i in range(lo, a + int(40 * hz)) if vmg[i] > target and np.nanmean(vmg[i:i + int(15 * hz)]) > target), a)
        # Finish at the line off the committee boat. Upwind finish: where the last beat stops gaining
        # ground to windward. Downwind finish (courses L and Z): where the last run stops losing it.
        e0, e1 = c[-1][1], min(n, c[-1][2] + int(30 * hz))
        if e1 > e0:
            b = e0 + int(np.nanargmax(p[e0:e1]) if c[-1][0] == 1 else np.nanargmin(p[e0:e1]))
        marks = [{"kind": "start", "lat": float(s.lat[st]), "lon": float(s.lon[st]), "t": int(grid[st])}]
        for L in c:
            seg = p[L[1]:L[2]]
            if not len(seg):
                continue
            j = L[1] + int(np.argmax(seg) if L[0] == 1 else np.argmin(seg))
            if L is not c[-1]:
                marks.append({"kind": "windward" if L[0] == 1 else "leeward",
                              "lat": float(s.lat[j]), "lon": float(s.lon[j]), "t": int(grid[j])})
        course, wing = _course_letter(c, s, p, hz, wrap180(twd - cs))
        if wing is not None:
            marks.append({"kind": "wing", "lat": float(s.lat[wing]), "lon": float(s.lon[wing]), "t": int(grid[wing])})
        fb = min(b, n - 1)
        marks.append({"kind": "finish", "lat": float(s.lat[fb]), "lon": float(s.lon[fb]), "t": int(grid[fb])})
        out.append({"start_ms": int(grid[st]), "finish_ms": int(grid[fb]),
                    "legs": ["up" if L[0] == 1 else "down" for L in c], "course": course,
                    "marks": marks, "twd": float(circ_mean(twd[a:b]))})
    return out


# CYC Frostbite Sailing Instructions, section 7: course letters by the legs sailed.
COURSES = {
    "W": "Windward–Leeward–Finish",
    "W2": "Windward–Leeward twice around",
    "L": "Windward–Finish",
    "M": "Modified Gold Cup",
    "Z": "Z pattern",
}


def _course_letter(c, s, p, hz, twa=None):
    """Course letter from the pattern of legs. The fleet doesn't sail triangles. A gold-cup lap (M)
    can't be told apart from a run sailed with one gybe, so it reads as W2; set it per race in
    site.json ("courses") if needed."""
    seq = tuple(L[0] for L in c)
    return {(1, -1): "L", (1, -1, 1): "W", (1, -1, 1, -1): "Z", (1, -1, 1, -1, 1): "W2"}.get(seq, ""), None


def find_races(boats: list[tuple[str, VkxLog]]) -> list[dict]:
    """All races in a set of logs. Uses the Atlas race timer when anyone used it,
    otherwise spots races from the sailing (beat, run, ... with no long stop)."""
    logs = [l for _, l in boats]
    guns = find_guns(logs)
    races = []
    if guns:
        for k, g in enumerate(guns):
            nxt = guns[k + 1] if k + 1 < len(guns) else None
            fin = {}
            for name, log in boats:
                ends = [e["t"] for e in log.timer if e["event"] == "race_end" and e["t"] > g and (nxt is None or e["t"] < nxt)]
                if ends:
                    fin[name] = min(ends)
            # a timer started on the dock or in the car is not a race: someone has to sail after the gun
            sailed = False
            for _, log in boats:
                after = [r for r in log.track if g <= r["t"] <= g + 20 * 60_000 and r["sog_kt"] >= 2.0]
                if len(after) >= 180 * max(1, (log.log_rate_hz or 2)) // 2:
                    sailed = True
                    break
            if sailed:
                races.append({"start_ms": g, "estimated": False, "finish": fin, "marks": [], "source": "timer"})
        if races:
            return races
    found = []                                       # (start, name, chain)
    for name, log in boats:
        for c in _course_chains(log):
            found.append((c["start_ms"], name, c))
    found.sort(key=lambda f: f[0])
    for st, name, c in found:
        hit = next((r for r in races if abs(r["_starts"][0] - st) < 240_000), None)
        if hit is None:
            hit = {"_starts": [], "finish": {}, "_chains": [], "estimated": True, "source": "course"}
            races.append(hit)
        if name in hit["finish"]:
            continue
        hit["_starts"].append(st)
        hit.setdefault("_onset", {})[name] = st
        hit["finish"][name] = c["finish_ms"]
        hit["_chains"].append(c)
    for r in races:
        r["start_ms"] = int(np.median(r.pop("_starts")))
        chains = r.pop("_chains")
        best = max(chains, key=lambda c: len(c["legs"]))
        r["legs"] = best["legs"]
        letters = [c.get("course") for c in chains if c.get("course")]
        r["course_letter"] = max(set(letters), key=letters.count) if letters else ""
        r["twd"] = circ_mean([c["twd"] for c in chains])
        # marks: average each boat's rounding spot for the same mark
        marks = []
        for k, m in enumerate(best["marks"]):
            pts = [c["marks"][k] for c in chains if len(c["marks"]) > k and c["marks"][k]["kind"] == m["kind"]]
            marks.append({"kind": m["kind"], "lat": float(np.mean([q["lat"] for q in pts])),
                          "lon": float(np.mean([q["lon"] for q in pts])), "boats": len(pts)})
        r["marks"] = marks
        if r["marks"]:
            _fit_line(r, boats)
        r.pop("_onset", None)
    races.sort(key=lambda r: r["start_ms"])
    return races



def _fit_line(race: dict, boats: list[tuple[str, VkxLog]], hz: float = 2.0) -> None:
    """Start and finish share one line square to the wind, with the committee boat in the middle:
    pin ... RC is the start, RC ... finish mark is the finish (upwind finish).
    Place that line where the fleet started and finished, then time each boat's start and
    finish as the moment it crossed it. The finish is confirmed by the drop in speed after it."""
    T = math.radians(race["twd"])
    ux, uy = math.sin(T), math.cos(T)                  # upwind
    rx, ry = math.cos(T), -math.sin(T)                 # to the right, looking upwind
    lat0 = next(m["lat"] for m in race["marks"])
    lon0 = next(m["lon"] for m in race["marks"])
    mlon = 111_320 * math.cos(math.radians(lat0))
    sgn = -1.0 if (race.get("legs") or ["up"])[-1] == "down" else 1.0   # downwind finish on courses L and Z
    per = {}
    for name, log in boats:
        if name not in race["finish"]:
            continue
        a_ms, f_ms = race["_onset"][name], race["finish"][name]
        grid = np.arange(a_ms - 120_000, f_ms + 90_000, int(1000 / hz), dtype=np.int64)
        bs = BoatSeries(name, "", log)
        _resample(bs, grid, 0.0)
        x, y = (bs.lon - lon0) * mlon, (bs.lat - lat0) * 111_320
        q = x * ux + y * uy
        ia = int(120 * hz)
        ifin = int((f_ms - grid[0]) / (1000 / hz))
        # finish: on the last leg, near its furthest point, the first clear drop in speed
        lo, hi = max(ia, ifin - int(150 * hz)), min(len(grid) - 1, ifin + int(60 * hz))
        qf = sgn * q
        qmax = np.nanmax(qf[lo:hi]) if hi > lo else np.nan
        sm = _nan_movmean(bs.sog, int(6 * hz))
        drop = None
        for i in range(lo, hi - int(15 * hz)):
            if qf[i] < qmax - 40 or np.isnan(sm[i]):
                continue
            before = np.nanmean(sm[max(0, i - int(30 * hz)):i])
            after = np.nanmean(sm[i:i + int(15 * hz)])
            if before > 2 and after < 0.72 * before:
                drop = i
                break
        if drop is None:
            drop = lo + int(np.nanargmax(qf[lo:hi])) if hi > lo else ifin
        per[name] = dict(grid=grid, x=x, y=y, q=q, ia=ia, drop=drop)
    if not per:
        return
    if sgn > 0:
        # Upwind finish: boats cross, carry on for a few seconds, then ease off. That drop places
        # the line better than the start, where boats are still building speed below it.
        qline = float(np.nanmedian([v["q"][v["drop"]] for v in per.values()])) - 8.0
    else:
        qline = float(np.nanmedian([v["q"][v["ia"]] for v in per.values()] + [v["q"][v["drop"]] for v in per.values()]))

    def crossing(v, a, b, prefer, d=1.0):
        q = d * v["q"]
        ql = d * qline
        xs = [i for i in range(max(1, a), min(len(q) - 1, b)) if q[i - 1] < ql <= q[i]]
        if not xs:
            return None
        return min(xs, key=lambda i: abs(i - prefer))

    starts, fins, a_s, a_f = [], {}, [], []
    for name, v in per.items():
        i_s = crossing(v, v["ia"] - int(60 * hz), v["ia"] + int(45 * hz), v["ia"]) or v["ia"]
        i_f = crossing(v, v["drop"] - int(90 * hz), v["drop"] + int(5 * hz), v["drop"], sgn) or v["drop"]
        starts.append(int(v["grid"][i_s]))
        fins[name] = int(v["grid"][i_f])
        a_s.append(float(v["x"][i_s] * rx + v["y"][i_s] * ry))
        a_f.append(float(v["x"][i_f] * rx + v["y"][i_f] * ry))
    if np.mean(a_f) >= np.mean(a_s):
        rc = (max(a_s) + min(a_f)) / 2 if max(a_s) < min(a_f) else (np.mean(a_s) + np.mean(a_f)) / 2
        pin, fmark = min(a_s) - 20, max(a_f) + 20
    else:
        rc = (min(a_s) + max(a_f)) / 2 if min(a_s) > max(a_f) else (np.mean(a_s) + np.mean(a_f)) / 2
        pin, fmark = max(a_s) + 20, min(a_f) - 20

    def ll(a):
        X, Y = qline * ux + a * rx, qline * uy + a * ry
        return [lat0 + Y / 111_320, lon0 + X / mlon]
    race["start_ms"] = int(np.median(starts))
    race["finish"] = fins
    race["line_est"] = {"pin_end": ll(pin), "rc": ll(rc), "finish_end": ll(fmark),
                        "start_len_m": round(abs(rc - pin)), "finish_len_m": round(abs(fmark - rc))}
    for m in race["marks"]:
        if m["kind"] == "start":
            m["lat"], m["lon"] = ll(float(np.mean(a_s)))
        elif m["kind"] == "finish":
            m["lat"], m["lon"] = ll(float(np.mean(a_f)))

def course_changes(races: list[dict]) -> None:
    """Annotate each race with course length and how far each mark moved since the previous race."""
    prev = None
    for r in races:
        w = next((m for m in r["marks"] if m["kind"] == "windward"), None)
        lw = next((m for m in r["marks"] if m["kind"] == "leeward"), None)
        info = {}
        if w and lw:
            mlon = 111_320 * math.cos(math.radians(w["lat"]))
            dx, dy = (w["lon"] - lw["lon"]) * mlon, (w["lat"] - lw["lat"]) * 111_320
            info["length_m"] = round(math.hypot(dx, dy))
            info["axis"] = round(math.degrees(math.atan2(dx, dy)) % 360)
        if prev:
            for kind in ("windward", "leeward"):
                a = next((m for m in r["marks"] if m["kind"] == kind), None)
                b = next((m for m in prev["marks"] if m["kind"] == kind), None)
                if a and b:
                    mlon = 111_320 * math.cos(math.radians(a["lat"]))
                    dx, dy = (a["lon"] - b["lon"]) * mlon, (a["lat"] - b["lat"]) * 111_320
                    d = math.hypot(dx, dy)
                    info[f"{kind}_moved_m"] = round(d)
                    info[f"{kind}_moved_dir"] = round(math.degrees(math.atan2(dx, dy)) % 360) if d >= 1 else None
        r["course"] = info
        prev = r


# --------------------------------------------------------------------------- main entry
def build_race(boats: list[tuple[str, VkxLog]], *, twd: float | None = None, gun_ms: int | None = None,
               race_index: int = 0, hz: float = 2.0, pre_start_s: float = 180.0,
               heel_offsets: dict | None = None, title: str | None = None,
               colors: dict | None = None, race: dict | None = None,
               not_before_ms: int | None = None) -> dict:
    heel_offsets = heel_offsets or {}
    colors = colors or {}
    logs = [l for _, l in boats]
    notes: list[str] = []
    series = [BoatSeries(name, colors.get(name, PALETTE[i % len(PALETTE)]), log) for i, (name, log) in enumerate(boats)]
    series = [s for s in series if s.log.track]
    if not series:
        raise ValueError("None of the logs contain GPS positions.")

    # ---- race window ---------------------------------------------------------------
    guns = find_guns(logs)
    finishes = {}
    if race is not None and gun_ms is None:
        gun_ms = race["start_ms"]
        finishes = dict(race.get("finish") or {})
        if race.get("estimated"):
            notes.append("No race timer was used, so the start is estimated from when the boat began the first beat, "
                         "and the finish from the end of the last leg.")
        guns = [gun_ms]
    if gun_ms is None and guns:
        if race_index >= len(guns):
            raise ValueError(f"Race {race_index + 1} requested but only {len(guns)} start(s) found.")
        gun_ms = guns[race_index]
        if len(guns) > 1:
            notes.append(f"{len(guns)} starts found in these logs; showing race {race_index + 1}.")
    data_start = min(s.log.start_ms for s in series)
    data_end = max(s.log.end_ms for s in series)
    if gun_ms is not None and finishes:
        t0 = max(data_start, gun_ms - int(pre_start_s * 1000), (not_before_ms or 0))
        t1 = min(data_end, max(finishes.values()) + 60_000)
    elif gun_ms is not None:
        t0 = max(data_start, gun_ms - int(pre_start_s * 1000))
        nxt = [g for g in guns if g > gun_ms + 60_000]
        cap = (nxt[0] - int(pre_start_s * 1000)) if nxt else data_end
        ends = []
        for s in series:
            fin = [e["t"] for e in s.log.timer if e["event"] == "race_end" and gun_ms < e["t"] <= cap]
            ends.append(min(fin) + 90_000 if fin else min(cap, s.log.end_ms))
        t1 = min(cap, max(ends))
    else:
        notes.append("No race start found on the timers, so the whole session is shown and start analysis is skipped.")
        t0, t1 = data_start, data_end

    step = int(round(1000 / hz))
    grid = np.arange(t0, t1 + 1, step, dtype=np.int64)
    n = len(grid)
    for s in series:
        _resample(s, grid, heel_offsets.get(s.name, 0.0))

    lat0 = float(np.nanmean(np.concatenate([s.lat for s in series])))
    lon0 = float(np.nanmean(np.concatenate([s.lon for s in series])))
    mlat, mlon = 111_320.0, 111_320.0 * math.cos(math.radians(lat0))
    for s in series:
        s.x = (s.lon - lon0) * mlon
        s.y = (s.lat - lat0) * mlat

    gun_i = int((gun_ms - t0) // step) if gun_ms is not None and t0 <= gun_ms <= t1 else None

    # ---- start line ----------------------------------------------------------------
    line = None
    pings = [p for l in logs for p in l.line if gun_ms is None or p["t"] <= gun_ms]
    pin = [p for p in pings if p["end"] == "pin"]
    rc = [p for p in pings if p["end"] == "boat"]
    if pin and rc:
        pin, rc = max(pin, key=lambda p: p["t"]), max(rc, key=lambda p: p["t"])
        line = {"pin": [pin["lat"], pin["lon"]], "boat": [rc["lat"], rc["lon"]],
                "pin_xy": ((pin["lon"] - lon0) * mlon, (pin["lat"] - lat0) * mlat),
                "boat_xy": ((rc["lon"] - lon0) * mlon, (rc["lat"] - lat0) * mlat)}

    # ---- wind ------------------------------------------------------------------------
    twd_series, twd_global, twd_source = _estimate_wind(series, n, hz, gun_i, line, twd, logs, notes)

    # ---- per-boat analysis ---------------------------------------------------------------
    if twd_global is not None:
        ux, uy = math.sin(math.radians(twd_global)), math.cos(math.radians(twd_global))
    else:
        ux, uy = 0.0, 0.0
    ref = (0.0, 0.0)
    if line:
        ref = ((line["pin_xy"][0] + line["boat_xy"][0]) / 2, (line["pin_xy"][1] + line["boat_xy"][1]) / 2)
    out_boats = []
    for s in series:
        out_boats.append(_analyse_boat(s, grid, hz, gun_i, twd_series, (ux, uy), ref, line, gun_ms, finishes.get(s.name)))

    _rank(out_boats)

    # fleet wind speed estimate and the race-average wind direction
    tws_all = np.vstack([b.pop("_tws") for b in out_boats])
    racing_any = np.any(np.vstack([b.pop("_racing") for b in out_boats]), axis=0)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            tws_fleet = np.nanmedian(tws_all, axis=0)
    tws_smooth = _nan_movmedian(tws_fleet, int(90 * hz))
    tws_smooth[~racing_any] = np.nan
    wind_speed = _tws_range(tws_fleet)
    ups = [l for b in out_boats for l in b["legs"] if l["type"] == "Upwind" and l.get("tws") is not None]
    dns = [l for b in out_boats for l in b["legs"] if l["type"] == "Downwind" and l.get("tws") is not None]
    wind_speed["up"] = _r(np.median([l["tws"] for l in ups]), 1) if ups else None
    wind_speed["down"] = _r(np.median([l["tws"] for l in dns]), 1) if dns else None
    wind_speed["words"] = polar.describe(wind_speed.get("tws_lo") or np.nan, wind_speed.get("tws") or np.nan, wind_speed.get("tws_hi") or np.nan)
    wind_speed["model"] = polar.NAME
    twd_avg = None
    if twd_series is not None:
        sel = racing_any if racing_any.any() else np.ones(n, bool)
        twd_avg = circ_mean(twd_series[sel])
    if line is not None:
        line = {"pin": line["pin"], "boat": line["boat"],
                "length_m": _r(math.dist(line["pin_xy"], line["boat_xy"]), 0)}
    wind_every = max(1, int(10 * hz))
    return {
        "meta": {
            "title": title or "Race replay",
            "t0_ms": int(t0), "hz": hz, "n": n,
            "gun_ms": int(gun_ms) if gun_ms is not None else None,
            "gun_s": _r((gun_ms - t0) / 1000, 1) if gun_ms is not None else None,
            "origin": [lat0, lon0],
            "twd_global": _r(twd_global, 0),
            "twd_source": twd_source,
            "notes": notes,
            "has_wind_sensor": any(l.wind for l in logs),
            "start_estimated": bool(race and race.get("estimated")),
            "course": (race or {}).get("course"),
            "course_letter": (race or {}).get("course_letter", ""),
            "marks": [{"kind": m["kind"], "lat": _r(m["lat"], 6), "lon": _r(m["lon"], 6)} for m in (race or {}).get("marks", [])],
            "line_est": (race or {}).get("line_est") if line is None else None,
        },
        "line": line,
        "wind": {"every": wind_every, "twd": _r(twd_series[::wind_every], 0) if twd_series is not None else None,
                 "twd_avg": _r(twd_avg, 0), "tws": _r(tws_smooth[::wind_every], 1), "speed": wind_speed},
        "boats": out_boats,
    }


def _estimate_wind(series, n, hz, gun_i, line, twd_manual, logs, notes):
    if twd_manual is not None:
        return np.full(n, float(twd_manual)), float(twd_manual), "manual"

    # bootstrap from course changes (after the gun when we know it)
    turns = []
    for s in series:
        cog = s.cog.copy()
        if gun_i is not None:
            cog[:gun_i] = np.nan
        for j, bis, turn in _turns_from_cog(cog, s.sog, hz):
            turns.append((j, bis, turn))
    # Atlas tack references (Shift Angle rows) are direct tack headings
    shift_axis = None
    for log in logs:
        st = [r for r in log.shifts if r["tack"] == "starboard"]
        pt = [r for r in log.shifts if r["tack"] == "port"]
        if st and pt:
            a, b = st[-1]["heading"], pt[-1]["heading"]
            d = wrap180(b - a)
            if 60 <= abs(d) <= 120:
                shift_axis = (a + d / 2) % 360

    axis = None
    if len(turns) >= 2:
        axis = circ_mean([2 * t[1] for t in turns]) / 2           # axis, ambiguous by 180
    elif shift_axis is not None:
        axis = shift_axis
    elif line:
        dx = line["boat_xy"][0] - line["pin_xy"][0]
        dy = line["boat_xy"][1] - line["pin_xy"][1]
        axis = (math.degrees(math.atan2(dx, dy)) - 90) % 360
    if axis is None:
        notes.append("Not enough tacks or gybes to estimate the wind. Pass --twd to set it.")
        return None, None, "none"

    # resolve which end of the axis the wind blows from
    cands = [axis, (axis + 180) % 360]
    source = "tacks"
    if shift_axis is not None and len(turns) < 2:
        source = "atlas tack headings"
    elif len(turns) < 2:
        source = "start line"
    choice = None
    if gun_i is not None:
        a, b = gun_i + int(20 * hz), gun_i + int(120 * hz)
        vx, vy = [], []
        for s in series:
            if b < n and not np.isnan(s.x[a]) and not np.isnan(s.x[b]):
                vx.append(s.x[b] - s.x[a]); vy.append(s.y[b] - s.y[a])
        if vx:
            mv = (np.mean(vx), np.mean(vy))
            scores = [mv[0] * math.sin(math.radians(c)) + mv[1] * math.cos(math.radians(c)) for c in cands]
            choice = cands[int(np.argmax(scores))]
    if choice is None:
        # upwind sailing is slower than downwind on a Melges 15
        sp = []
        for c in cands:
            v = []
            for s in series:
                rel = np.abs(wrap180(s.cog - c))
                v.append(s.sog[(rel < 60) & (s.sog > 1.5)])
            v = np.concatenate(v)
            sp.append(np.nanmean(v) if len(v) else np.inf)
        choice = cands[int(np.argmin(sp))]
        notes.append("Wind end chosen from boat speeds (upwind slower). Check the wind arrow looks right.")
    twd_global = choice

    # time-varying: tacks re-measure the wind
    pts = [(j, (bis if abs(wrap180(bis - twd_global)) < 40 else None)) for j, bis, _ in turns]
    pts += [(j, (bis + 180) % 360) for j, bis, _ in turns if abs(wrap180(bis - twd_global - 180)) < 40]
    pts = [(j, b) for j, b in pts if b is not None]
    twd = np.full(n, twd_global)
    if len(pts) >= 3:
        js = np.array([p[0] for p in pts], dtype=float)
        dv = wrap180(np.array([p[1] for p in pts]) - twd_global)
        sigma = 150 * hz
        idx = np.arange(n, dtype=float)
        w = np.exp(-0.5 * ((idx[:, None] - js[None, :]) / sigma) ** 2)
        wsum = w.sum(axis=1)
        dev = (w * dv[None, :]).sum(axis=1) / np.where(wsum < 1e-6, 1, wsum)
        dev = np.where(wsum < 0.05, 0.0, dev) * np.minimum(1, wsum / 0.5)
        twd = (twd_global + np.clip(dev, -25, 25)) % 360
    return twd, twd_global, source


def _segments(mask_vals, min_len):
    """Run-length encode an array of labels, merging runs shorter than min_len into neighbours."""
    runs = []
    if len(mask_vals) == 0:
        return runs
    cur, start = mask_vals[0], 0
    for i in range(1, len(mask_vals)):
        if mask_vals[i] != cur:
            runs.append([cur, start, i])
            cur, start = mask_vals[i], i
    runs.append([cur, start, len(mask_vals)])
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for k, (lab, a, b) in enumerate(runs):
            if b - a < min_len:
                if k > 0:
                    runs[k - 1][2] = b
                else:
                    runs[k + 1][1] = a
                runs.pop(k)
                changed = True
                break
        merged = [runs[0]]
        for r in runs[1:]:
            if r[0] == merged[-1][0]:
                merged[-1][2] = r[2]
            else:
                merged.append(r)
        runs = merged
    return runs


def _analyse_boat(s: BoatSeries, grid, hz, gun_i, twd, u, ref, line, gun_ms, finish_ms=None):
    n = len(grid)
    dt = 1.0 / hz
    sog = s.sog
    cog_s = circ_smooth(s.cog, max(1, int(2 * hz)))
    res = {"name": s.name, "color": s.color, "source": s.log.source}
    have_wind = twd is not None
    if have_wind:
        twa = wrap180(twd - cog_s)
        vmg = sog * np.cos(np.radians(twa))
        atwa = _nan_movmedian(np.abs(twa), int(9 * hz))
        mode = np.full(n, "", dtype=object)
        mode[atwa < 70] = "up"
        mode[(atwa >= 70) & (atwa <= 110)] = "reach"
        mode[atwa > 110] = "down"
        mode[(sog < 1.5) | np.isnan(sog)] = "slow"
        vmg_abs = np.where(mode == "down", -vmg, vmg)
        side = np.sign(twa)
        heel_lw = -s.heel * side
        # Atlas mounted a few degrees off the centreline shows as a constant heading-vs-course gap.
        # Leeway flips sign with the tack, so the average of the two tacks is the mounting error.
        dh = wrap180(s.hdg - cog_s)
        upm = (mode == "up") & (sog > 2.5)
        st, pt = upm & (side > 0), upm & (side < 0)
        if st.sum() > 60 * hz and pt.sum() > 60 * hz:
            ms_, mp_ = float(np.nanmedian(dh[st])), float(np.nanmedian(dh[pt]))
            hdg_offset, leeway = (ms_ + mp_) / 2, (ms_ - mp_) / 2
        else:
            hdg_offset, leeway = float("nan"), float("nan")
        hdg_fixed = (s.hdg - (hdg_offset if np.isfinite(hdg_offset) and abs(hdg_offset) > 2 else 0)) % 360
        prog = (s.x - ref[0]) * u[0] + (s.y - ref[1]) * u[1]
    else:
        twa = vmg = vmg_abs = heel_lw = prog = np.full(n, np.nan)
        mode = np.where(sog >= 1.5, "sail", "slow").astype(object)
        side = np.zeros(n)
        hdg_fixed, hdg_offset, leeway = s.hdg, float("nan"), float("nan")

    racing = np.zeros(n, bool)
    finish_i = None
    fin = [e["t"] for e in s.log.timer if e["event"] == "race_end" and (gun_ms is None or e["t"] > gun_ms)]
    if finish_ms is not None:
        fin = [finish_ms]
    if fin:
        fi = int((min(fin) - grid[0]) // int(1000 / hz))
        if 0 <= fi < n:
            finish_i = fi
    a0 = gun_i if gun_i is not None else 0
    a1 = finish_i if finish_i is not None else n
    racing[a0:a1] = True
    racing &= s.valid

    mg = np.zeros(n)
    if have_wind:
        inc = np.where(racing & np.isin(mode, ["up", "down", "reach"]), np.nan_to_num(np.maximum(vmg_abs, -50)) * KT_TO_MS * dt, 0)
        mg = np.cumsum(inc)
    dist = np.zeros(n)
    stepd = np.hypot(np.diff(s.x, prepend=np.nan), np.diff(s.y, prepend=np.nan))
    dist = np.cumsum(np.where(racing & ~np.isnan(stepd), stepd, 0))

    # ---- manoeuvres -------------------------------------------------------------------
    man = []
    if have_wind:
        committed = np.zeros(n)
        cur = 0.0
        for i in range(n):
            a = twa[i]
            if not np.isnan(a) and 20 < abs(a) < 160 and sog[i] > 1.0:
                cur = math.copysign(1, a)
            committed[i] = cur
        changes = [i for i in range(1, n) if committed[i] != committed[i - 1] and committed[i - 1] != 0]
        for c in changes:
            # find actual crossing near c (where twa crosses 0 or 180)
            lo = max(1, c - int(15 * hz))
            seg = np.abs(twa[lo:c + 1])
            if np.all(np.isnan(seg)):
                continue
            near0 = np.nanmin(seg)
            near180 = np.nanmax(seg)
            kind = "tack" if near0 < 180 - near180 else "gybe"
            if kind == "tack":
                tm = lo + int(np.nanargmin(seg))
            else:
                tm = lo + int(np.nanargmax(seg))
            if gun_i is not None and tm < gun_i + int(10 * hz):
                continue                                   # pre-start and the turn up at the gun
            if finish_i is not None and tm > finish_i:
                continue
            # what was the boat doing before and after? same mode = tack/gybe, different = rounding
            def _mode(a, b):
                w = [x for x in mode[max(0, a):min(n, b)] if x in ("up", "down", "reach")]
                return max(set(w), key=w.count) if w else None
            mb, ma = _mode(tm - int(25 * hz), tm - int(6 * hz)), _mode(tm + int(10 * hz), tm + int(30 * hz))
            if mb is None or ma is None:
                continue
            if mb != ma or mb == "reach":
                continue                                   # mark roundings come from the legs below
            kind = "tack" if mb == "up" else "gybe"
            m = _manoeuvre_metrics(s, tm, kind, sog, vmg_abs, cog_s, hz, n)
            if m:
                # drop manoeuvres that were undone quickly (wobbles)
                if man and tm - man[-1]["i"] < 8 * hz:
                    man.pop()
                    continue
                man.append(m)
    for m in man:
        m["t"] = _r(m.pop("i") / hz, 1)

    # ---- wind speed from boat speed (no sensor) ----------------------------------------
    tws_est = np.full(n, np.nan)
    if have_wind:
        steady = racing & np.isin(mode, ["up", "down"])
        for m in man:
            i = int(m["t"] * hz)
            steady[max(0, i - int(10 * hz)):min(n, i + int(20 * hz))] = False
        sog_s = _nan_movmean(sog, int(20 * hz))
        est = polar.wind_from_speed(sog_s, _nan_movmedian(np.abs(twa), int(9 * hz)))
        tws_est = np.where(steady, est, np.nan)

    # ---- legs -------------------------------------------------------------------------
    legs = []
    if have_wind:
        lab = np.where(racing, mode, "-")
        lab = np.where(lab == "slow", "-", lab)
        for k, (l, a, b) in enumerate(_segments(list(lab), int(45 * hz))):
            if l in ("-", "") or b - a < 30 * hz:
                continue
            sl = slice(a, b)
            ms = [m for m in man if a / hz <= m["t"] < b / hz and m["kind"] in ("tack", "gybe")]
            legs.append({
                "type": {"up": "Upwind", "down": "Downwind", "reach": "Reach"}.get(l, l),
                "start": _r(a / hz, 1), "end": _r(b / hz, 1),
                "duration_s": _r((b - a) / hz, 0),
                "distance_m": _r(dist[b - 1] - dist[a], 0),
                "avg_sog": _r(np.nanmean(sog[sl])),
                "avg_vmg": _r(np.nanmean(vmg_abs[sl])),
                "avg_twa": _r(np.nanmean(np.abs(twa[sl])), 0),
                "avg_heel": _r(np.nanmean(heel_lw[sl]), 1),
                **_tws_range(tws_est[sl]),
                "manoeuvres": len(ms),
            })

    roundings = []
    for prev, nxt in zip(legs, legs[1:]):
        if prev["type"] != nxt["type"]:
            i = int(nxt["start"] * hz)
            roundings.append({"t": nxt["start"], "kind": "rounding",
                              "label": ("Windward" if prev["type"] == "Upwind" else "Leeward") + " mark",
                              "entry_sog": _r(sog[max(0, i - int(10 * hz))]), "lat": _r(s.lat[i], 6), "lon": _r(s.lon[i], 6)})
    man = sorted(man + roundings, key=lambda m: m["t"])

    # ---- start ------------------------------------------------------------------------
    start = None
    if gun_i is not None and gun_i < n and s.valid[gun_i]:
        start = {"sog_at_gun": _r(sog[gun_i]), "sog_10s": _r(sog[min(n - 1, gun_i + int(10 * hz))])}
        if line:
            px, py = line["pin_xy"]
            bx, by = line["boat_xy"]
            lx, ly = bx - px, by - py
            L = math.hypot(lx, ly)
            nx, ny = ly / L, -lx / L                                     # normal
            if have_wind and nx * u[0] + ny * u[1] < 0:
                nx, ny = -nx, -ny                                        # point to course side
            elif not have_wind:
                # course side = where boats are 60 s after the gun
                j = min(n - 1, gun_i + int(60 * hz))
                if not np.isnan(s.x[j]) and ((s.x[j] - px) * nx + (s.y[j] - py) * ny) < 0:
                    nx, ny = -nx, -ny
            d = (s.x - px) * nx + (s.y - py) * ny
            along = ((s.x - px) * lx + (s.y - py) * ly) / (L * L)
            dg = d[gun_i]
            start["dist_to_line_m"] = _r(-dg, 1)                         # + = behind the line
            start["ocs"] = bool(dg > 0.5)
            start["pos_at_gun"] = _r(np.clip(along[gun_i], -0.5, 1.5) * 100, 0)
            cross = None
            for i in range(gun_i, min(n - 1, gun_i + int(180 * hz))):
                if not np.isnan(d[i]) and d[i] <= 0 < d[i + 1]:
                    cross = i
                    break
            if cross is not None:
                start["cross_s"] = _r((cross - gun_i) / hz, 1)
                start["cross_pos"] = _r(np.clip(along[cross], -0.5, 1.5) * 100, 0)
                start["sog_cross"] = _r(sog[cross])
            elif start["ocs"]:
                start["cross_s"] = None
        j = min(n - 1, gun_i + int(120 * hz))
        start["height_2min_m"] = _r(prog[j], 0) if have_wind else None

    # ---- summary ----------------------------------------------------------------------
    up = racing & (mode == "up")
    dn = racing & (mode == "down")
    tacks = [m for m in man if m["kind"] == "tack"]
    gybes = [m for m in man if m["kind"] == "gybe"]
    summary = {
        "finish_s": _r(finish_i / hz, 1) if finish_i is not None else None,
        "elapsed_s": _r((finish_i - gun_i) / hz, 0) if finish_i is not None and gun_i is not None else None,
        "distance_m": _r(dist[-1], 0),
        "max_sog": _r(np.nanmax(np.where(racing, sog, np.nan)) if racing.any() else np.nan),
        "avg_sog": _r(np.nanmean(sog[racing]) if racing.any() else np.nan),
        "up_vmg": _r(np.nanmean(vmg_abs[up]) if up.any() else np.nan),
        "up_sog": _r(np.nanmean(sog[up]) if up.any() else np.nan),
        "up_twa": _r(np.nanmean(np.abs(twa[up])) if up.any() else np.nan, 0),
        "up_heel": _r(np.nanmean(heel_lw[up]) if up.any() else np.nan, 1),
        "dn_vmg": _r(np.nanmean(vmg_abs[dn]) if dn.any() else np.nan),
        "dn_sog": _r(np.nanmean(sog[dn]) if dn.any() else np.nan),
        "dn_twa": _r(np.nanmean(np.abs(twa[dn])) if dn.any() else np.nan, 0),
        "tacks": len(tacks), "gybes": len(gybes),
        "tack_loss_m": _r(np.nanmedian([m["loss_m"] for m in tacks if m["loss_m"] is not None]) if tacks else np.nan, 1),
        "gybe_loss_m": _r(np.nanmedian([m["loss_m"] for m in gybes if m["loss_m"] is not None]) if gybes else np.nan, 1),
        "tack_recover_s": _r(np.nanmedian([m["recover_s"] for m in tacks if m["recover_s"] is not None]) if tacks else np.nan, 1),
        "made_good_m": _r(mg[-1], 0),
        "heading_offset": _r(hdg_offset, 1),
        "up_leeway": _r(leeway, 1),
    }
    if up.any():
        summary["heel_vs_vmg"] = _heel_bins(heel_lw[up], vmg_abs[up])

    res.update({
        "track": {
            "lat": _r(s.lat, 6), "lon": _r(s.lon, 6),
            "sog": _r(sog, 2), "cog": _r(cog_s, 0), "hdg": _r(hdg_fixed, 0),
            "heel": _r(heel_lw if have_wind else s.heel, 1),
            "twa": _r(twa, 0), "vmg": _r(vmg_abs, 2),
            "mg": _r(mg, 0), "mode": [m if m else "" for m in mode.tolist()],
        },
        "manoeuvres": man, "legs": legs, "start": start, "summary": summary,
        "_tws": tws_est, "_racing": racing,
    })
    return res


def _tws_range(est):
    v = np.asarray(est, float)
    v = v[np.isfinite(v)]
    if len(v) < 20:
        return {"tws": None, "tws_lo": None, "tws_hi": None}
    lo, mid, hi = np.percentile(v, [15, 50, 85])
    return {"tws": _r(mid, 1), "tws_lo": _r(lo, 1), "tws_hi": _r(hi, 1)}


def _heel_bins(heel, vmg):
    ok = ~np.isnan(heel) & ~np.isnan(vmg)
    heel, vmg = heel[ok], vmg[ok]
    out = []
    for lo in range(-6, 22, 2):
        m = (heel >= lo) & (heel < lo + 2)
        if m.sum() >= 10:
            out.append([lo + 1, _r(np.mean(vmg[m])), int(m.sum())])
    return out


def _manoeuvre_metrics(s, tm, kind, sog, vmg_abs, cog, hz, n):
    def idx(sec):
        return tm + int(round(sec * hz))
    a, b = idx(-20), idx(-6)
    c, d = idx(-6), idx(24)
    if a < 0 or d >= n:
        return None
    pre = sog[a:b]
    if np.isnan(pre).mean() > 0.3:
        return None
    entry = float(np.nanmean(pre))
    entry_vmg = float(np.nanmean(vmg_abs[a:b]))
    win = sog[c:idx(15)]
    vmin = float(np.nanmin(win)) if not np.all(np.isnan(win)) else float("nan")
    rec = None
    for i in range(tm, min(n, idx(40))):
        if sog[i] >= 0.9 * entry:
            rec = (i - tm) / hz
            break
    loss = float(np.nansum((entry_vmg - vmg_abs[c:d]) * KT_TO_MS / hz))
    turn = float(wrap180(circ_mean(cog[idx(8):idx(20)]) - circ_mean(cog[a:b])))
    return {
        "i": tm, "kind": kind,
        "entry_sog": _r(entry), "min_sog": _r(vmin),
        "drop_pct": _r((1 - vmin / entry) * 100 if entry > 0 else np.nan, 0),
        "recover_s": _r(rec, 1), "loss_m": _r(loss, 1), "turn": _r(abs(turn), 0),
        "lat": _r(s.lat[tm], 6), "lon": _r(s.lon[tm], 6),
    }


def _rank(boats):
    hs = [b["start"]["height_2min_m"] for b in boats if b["start"] and b["start"].get("height_2min_m") is not None]
    for b in boats:
        if b["start"] and b["start"].get("height_2min_m") is not None:
            b["start"]["height_2min_m"] = _r(b["start"]["height_2min_m"] - max(hs), 0)   # 0 = best in fleet
    def key(b):
        f = b["summary"]["elapsed_s"]
        return (0, f) if f is not None else (1, -(b["summary"]["made_good_m"] or 0))
    for pos, b in enumerate(sorted(boats, key=key), 1):
        b["summary"]["position"] = pos


# --------------------------------------------------------------------------- exports
def write_race_csvs(race: dict, out_dir) -> list:
    import csv
    from datetime import datetime, timezone
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    meta = race["meta"]
    written = []
    tcols = ["sog", "cog", "hdg", "heel", "twa", "vmg", "mg", "mode"]
    for b in race["boats"]:
        stem = b["name"].lower().replace(" ", "_")
        p = out / f"{stem}_race.csv"
        tr = b["track"]
        every = max(1, int(meta["hz"]))
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_utc", "race_time_s", "lat", "lon", "sog_kt", "cog", "heading", "heel_leeward",
                        "twa", "vmg_kt", "made_good_m", "mode"])
            for i in range(0, meta["n"], every):
                if tr["lat"][i] is None:
                    continue
                ts = datetime.fromtimestamp((meta["t0_ms"] + i * 1000 / meta["hz"]) / 1000, tz=timezone.utc)
                rt = i / meta["hz"] - (meta["gun_s"] or 0)
                w.writerow([ts.isoformat(timespec="seconds"), round(rt, 1), tr["lat"][i], tr["lon"][i]] +
                           [tr[k][i] for k in tcols])
        written.append(p)
    p = out / "manoeuvres.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["boat", "kind", "race_time_s", "entry_sog", "min_sog", "drop_pct", "recover_s", "loss_m", "turn_deg"])
        for b in race["boats"]:
            for m in b["manoeuvres"]:
                w.writerow([b["name"], m.get("label", m["kind"]), _r(m["t"] - (meta["gun_s"] or 0), 1), m.get("entry_sog"),
                            m.get("min_sog"), m.get("drop_pct"), m.get("recover_s"), m.get("loss_m"), m.get("turn")])
    written.append(p)
    p = out / "summary.csv"
    keys = ["position", "elapsed_s", "distance_m", "avg_sog", "max_sog", "up_vmg", "up_sog", "up_twa", "up_heel",
            "dn_vmg", "dn_sog", "dn_twa", "tacks", "tack_loss_m", "tack_recover_s", "gybes", "gybe_loss_m"]
    skeys = ["sog_at_gun", "dist_to_line_m", "ocs", "pos_at_gun", "cross_s", "cross_pos", "height_2min_m"]
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["boat"] + keys + ["start_" + k for k in skeys])
        for b in sorted(race["boats"], key=lambda b: b["summary"]["position"]):
            st = b["start"] or {}
            w.writerow([b["name"]] + [b["summary"].get(k) for k in keys] + [st.get(k) for k in skeys])
    written.append(p)
    return written


# --------------------------------------------------------------------------- whole day
def build_day(boats: list[tuple[str, VkxLog]], *, title: str | None = None, **kw) -> dict:
    """Every race in the logs, each as its own race model, plus a day summary."""
    from datetime import datetime
    races = find_races(boats)
    if not races:
        raise ValueError("No races found: no race timer, and no beat-then-run pattern in the sailing.")
    course_changes(races)
    out, prev_end = [], None
    first = min(l.start_ms for _, l in boats if l.track)
    day_title = title or datetime.fromtimestamp(first / 1000).strftime("%a %d %b %Y")
    for k, r in enumerate(races, 1):
        rr = build_race(boats, race=r, title=f"Race {k}", not_before_ms=(prev_end + 30_000) if prev_end else None, **kw)
        rr["meta"]["race_no"] = k
        rr["meta"]["day_title"] = day_title
        out.append(rr)
        prev_end = max(r["finish"].values()) if r.get("finish") else None
    rows = []
    for k, (r, rr) in enumerate(zip(races, out), 1):
        bs = sorted(rr["boats"], key=lambda b: b["summary"]["position"])
        win = bs[0]
        s = win["summary"]
        rows.append({
            "race": k, "start_ms": r["start_ms"], "estimated": r.get("estimated", False),
            "course_letter": r.get("course_letter", ""),
            "elapsed_s": s["elapsed_s"], "winner": win["name"], "boats": len(bs),
            "twd": rr["wind"].get("twd_avg") if rr["wind"].get("twd_avg") is not None else rr["meta"]["twd_global"],
            "course": r.get("course") or {},
            "up_vmg": s["up_vmg"], "dn_vmg": s["dn_vmg"], "tacks": s["tacks"], "gybes": s["gybes"],
            "tack_loss_m": s["tack_loss_m"],
            "tws": rr["wind"]["speed"].get("tws"), "tws_lo": rr["wind"]["speed"].get("tws_lo"),
            "tws_hi": rr["wind"]["speed"].get("tws_hi"),
            "results": [{"name": b["name"], "pos": b["summary"]["position"], "elapsed_s": b["summary"]["elapsed_s"],
                         "sailed": b["summary"]["elapsed_s"] is not None or (b["summary"]["distance_m"] or 0) >= 300}
                        for b in bs],
        })
    return {"day": {"title": day_title, "races": rows}, "races": out}
