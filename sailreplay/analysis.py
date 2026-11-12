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

from . import local, polar
from .vkx import VkxLog

KT_TO_MS = 0.514444
# seconds before and after head to wind (tack) or dead downwind (gybe) left out of steady-sailing averages
MANOEUVRE_WINDOW_S = {"tack": (5.0, 12.0), "gybe": (6.0, 15.0)}
# Upwind, a Melges 15 is near top speed once it is powered up, so wind read back from upwind speed is
# mostly noise above this (a boat 0.2 kt slow reads 3 kt less wind) and biased low. When a boat's
# downwind legs say the wind was at least this, its upwind readings are left out of the wind estimate.
UPWIND_TWS_TRUSTED_BELOW = 10.0
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
    """Gun times (ms) from race timer events across all boats.

    Timers disagree: people start the countdown late and do not always sync it on the next
    signal. So timer guns within 90 s of each other are one start, and the gun is taken from the
    boats that synced their timer when any did (a synced timer is right on the signal), otherwise
    the median."""
    cands = []                                         # (gun ms, synced)
    for log in logs:
        ev = sorted(log.timer, key=lambda e: e["t"])
        for k, e in enumerate(ev):
            if e["event"] == "race_start":
                # synced if a sync came after the last countdown start before this gun
                last_start = max([f["t"] for f in ev[:k] if f["event"] == "start"] or [e["t"] - 600_000])
                synced = any(f["event"] == "sync" and last_start <= f["t"] < e["t"] for f in ev[:k])
                cands.append((e["t"], synced))
            elif e["event"] in ("start", "sync") and e["value_s"] > 0:
                gun = e["t"] + e["value_s"] * 1000
                # a later sync, restart or the gun itself before this countdown ran out replaces it
                if any(f["event"] in ("start", "sync", "race_start", "reset") and e["t"] < f["t"] <= gun for f in ev[k + 1:]):
                    continue
                cands.append((gun, e["event"] == "sync"))
    cands.sort()
    guns: list[list] = []
    for c in cands:
        if guns and c[0] - guns[-1][-1][0] < 90_000:
            guns[-1].append(c)
        else:
            guns.append([c])
    out = []
    for g in guns:
        synced = [t for t, sy in g if sy]
        alltimes = [t for t, _ in g]
        # with three or more timers the middle one is safest; with fewer, trust a synced one
        if len(alltimes) >= 3:
            out.append(int(np.median(alltimes)))
        else:
            out.append(int(np.median(synced if synced else alltimes)))
    return out


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
def _course_chains(log: VkxLog, hz: float = 1.0, guns=None):
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
        # (the gold-cup wing mark) sailed with purpose for up to 5 minutes
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

    def net(L):
        a, b = L[1], min(n - 1, L[2] - 1)
        return s.x[b] - s.x[a], s.y[b] - s.y[a]

    def same_course(prev, nxt):
        # consecutive legs of a windward-leeward race run back and forth along one line: the next
        # leg heads back roughly the way the last one came (within 55 degrees) and is not wildly
        # longer. Sailing home on a reach after the last race fails this.
        (px, py), (nx, ny) = net(prev), net(nxt)
        lp, ln = math.hypot(px, py), math.hypot(nx, ny)
        if lp < 30 or ln < 30:
            return True
        ang = math.degrees(math.acos(max(-1.0, min(1.0, -(px * nx + py * ny) / (lp * ln)))))
        return ang <= 55 and ln <= 3.0 * lp

    def new_start(L):
        # a beat that sets off right at a race-timer gun is the start of a new race, even when the
        # boat sailed straight from the last finish into the next start
        if not guns or L[0] != 1:
            return False
        t = grid[L[1]]
        return any(g - 150_000 <= t <= g + 30_000 for g in guns)

    chains, cur = [], []
    for L in merged:
        if cur and L[0] != cur[-1][0] and joined(cur[-1], L) and same_course(cur[-1], L) and not new_start(L):
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
        # (a short run, as on a small course in a breeze, counts when another beat follows it)
        while len(c) >= 2 and not (c[0][0] == 1 and c[0][2] - c[0][1] >= 150 * hz and c[1][0] == -1
                                   and (c[1][2] - c[1][1] >= 150 * hz
                                        or (c[1][2] - c[1][1] >= 75 * hz and len(c) >= 3))):
            c = c[1:]
        if len(c) < 2 or c[0][0] != 1:
            continue
        while len(c) > 2 and c[-1][0] == -1 and (c[-1][2] - c[-1][1] < 90 * hz
                                                 or math.hypot(*net(c[-1])) < 0.6 * max(
                                                     [math.hypot(*net(L)) for L in c[:-1] if L[0] == -1] or [0])):
            c = c[:-1]     # bearing away after finishing: a real last run is as long as the other runs
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
        offsets, reach_m = [], []
        for li, L in enumerate(c):
            seg = p[L[1]:L[2]]
            if not len(seg):
                continue
            j = L[1] + int(np.argmax(seg) if L[0] == 1 else np.argmin(seg))
            if L is not c[-1] and L[0] == 1:
                o = _offset_after(s, j, hz, wrap180(twd - cs))
                if o is not None:
                    reach_m.append(o["d"])
                    if True:                     # kept even when it doesn't look like an offset: used only on days that have them
                        offsets.append({"kind": "offset", "lat": o["lat"], "lon": o["lon"], "t": int(grid[o["k"]]),
                                        "leg": li, "d": o["d"], "turn": o["turn"], "ok": o["ok"]})
            if L is not c[-1]:
                mk = {"kind": "windward" if L[0] == 1 else "leeward",
                      "lat": float(s.lat[j]), "lon": float(s.lon[j]), "t": int(grid[j]), "leg": li}
                if L[0] == -1:
                    mk["rounded"] = _rounding_side(s.cog, j, hz)
                # the boat passes beside the mark, not over it: put the mark a few metres inside
                # the turn (to port for the usual port rounding, to starboard at a starboard gate)
                if np.isfinite(cs[j]):
                    side = 90.0 if mk.get("rounded") == "starboard" else -90.0
                    mk["lat"], mk["lon"] = _offset(mk["lat"], mk["lon"], cs[j] + side, MARK_INSIDE_M)
                marks.append(mk)
        course, wing = _course_letter(c, s, p, hz, wrap180(twd - cs))
        if wing is not None:
            marks.append({"kind": "wing", "lat": float(s.lat[wing]), "lon": float(s.lon[wing]), "t": int(grid[wing])})
        fb = min(b, n - 1)
        marks.append({"kind": "finish", "lat": float(s.lat[fb]), "lon": float(s.lon[fb]), "t": int(grid[fb])})
        out.append({"start_ms": int(grid[st]), "finish_ms": int(grid[fb]),
                    "legs": ["up" if L[0] == 1 else "down" for L in c], "course": course,
                    "_legs": [(L[0], int(grid[L[1]]), int(grid[min(n - 1, L[2])]), round(math.hypot(*net(L)))) for L in c],
                    "marks": marks, "offsets": offsets, "reach_m": reach_m, "twd": float(circ_mean(twd[a:b]))})
    return out


def _offset_after(s, j, hz, twa):
    """The windward offset: a second, smaller mark a short reach past the windward mark. After
    rounding, the boat reaches (wind abeam) for a few seconds, then bears away onto the run.
    Returns the spot where the boat settled on the run ({lat, lon, k, d: metres from the windward mark,
    ok: it looks like an offset}), or None when it can't be read."""
    n = len(s.lat)
    at = np.abs(_nan_movmedian(twa, max(1, int(3 * hz))))
    end = min(n - 1, j + int(75 * hz))
    k1 = next((i for i in range(j, end) if np.isfinite(at[i]) and at[i] > 60), None)       # round the windward mark
    if k1 is None:
        return None
    hold = int(6 * hz)
    k2 = next((i for i in range(k1, end - hold) if np.all(at[i:i + hold] >= 125)), None)   # settled on the run
    if k2 is None:
        return None
    reach = at[k1:k2]
    secs = (k2 - k1) / hz
    d = math.hypot((s.lon[k2] - s.lon[j]) * 111_320 * math.cos(math.radians(s.lat[j])), (s.lat[k2] - s.lat[j]) * 111_320)
    ok = (3 <= secs <= 45 and len(reach) > 0 and 60 <= float(np.nanmedian(reach)) <= 115 and 12 <= d <= 180)
    # Where the marks were, as a sailor reads the track (checked against Max's marks on 20 roundings:
    # windward within 2 m, offset within 4 m, median): the windward mark is where the boat came off the
    # beat (wind angle through WINDWARD_TURN_TWA), the offset where it bore away (through OFFSET_TURN_TWA),
    # each a few metres to port of the track
    # bore away: first time the wind angle stays past OFFSET_TURN_TWA (two readings running);
    # off the beat: the last moment before that it was still under WINDWARD_TURN_TWA (up to 40 s back)
    kb = next((i for i in range(k1, min(n - 1, k2 + 1)) if np.isfinite(at[i]) and at[i] >= OFFSET_TURN_TWA
               and at[i + 1] >= OFFSET_TURN_TWA), k2)
    lo = max(0, j - int(40 * hz))
    kw = next((i + 1 for i in range(kb - 1, lo - 1, -1) if np.isfinite(at[i]) and at[i] < WINDWARD_TURN_TWA), k1)
    def port(i):
        la, lo = float(s.lat[i]), float(s.lon[i])
        return _offset(la, lo, float(s.cog[i]) - 90.0, MARK_INSIDE_M) if np.isfinite(s.cog[i]) else (la, lo)
    lat, lon = port(kb)
    return {"lat": lat, "lon": lon, "k": kb, "d": float(d), "ok": bool(ok), "turn": port(kw)}


WINDWARD_TURN_TWA = 65.0
OFFSET_TURN_TWA = 110.0


# A day has windward offsets when the boats typically reach this far from the windward mark before
# bearing away. Without one they bear away within a few boat lengths (CYC frostbites: about 12 m);
# with one, 30 m and more (Southampton, Down Bay)
OFFSET_DAY_MEDIAN_M = 22.0


MARK_INSIDE_M = 7.0


def _offset(lat, lon, bearing_deg, m):
    b = math.radians(bearing_deg)
    return (lat + m * math.cos(b) / 111_320,
            lon + m * math.sin(b) / (111_320 * math.cos(math.radians(lat))))


def _rounding_side(cog, j, hz) -> str | None:
    """Which way a boat turned round the leeward mark. Marks are left to port, so the turn up
    is anticlockwise; a clockwise (starboard) rounding means there was a gate and this boat took
    the other side. Measured as the biggest one-way turn within the rounding, so a gybe on the way
    in or a tack straight after does not count."""
    a, b = max(1, j - int(20 * hz)), min(len(cog), j + int(12 * hz))
    seg = cog[a - 1:b]
    if np.sum(np.isfinite(seg)) < len(seg) * 0.7:
        return None
    d = np.nan_to_num(wrap180(np.diff(seg)))
    d = np.where(np.abs(d) > 45, 0, d)               # GPS glitches, not turns
    c = np.concatenate([[0], np.cumsum(d)])
    cw = float(np.max(c - np.minimum.accumulate(c)))     # biggest clockwise run
    ccw = float(np.max(np.maximum.accumulate(c) - c))    # biggest anticlockwise run
    if max(cw, ccw) < 90:
        return None
    return "starboard" if cw > ccw else "port"


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


def _max_excursion_m(boats, t0, t1) -> float:
    best = 0.0
    for _, log in boats:
        pts = [(r["lat"], r["lon"]) for r in log.track if t0 <= r["t"] <= t1]
        if len(pts) < 2:
            continue
        la0, lo0 = pts[0]
        ml = 111_320 * math.cos(math.radians(la0))
        best = max(best, max(math.hypot((lo - lo0) * ml, (la - la0) * 111_320) for la, lo in pts))
    return best


def _drop_general_recalls(races: list[dict], boats) -> list[dict]:
    """A general recall shows up as a gun followed by another gun a few minutes later, with the
    fleet coming back in between. Drop a timer gun when the next one is within 15 minutes, nobody
    stopped their timer between them, nobody's sailing shows a race (beat then run) starting from
    it, and no boat got further than 300 m from where it was at the gun (a race goes to the
    windward mark and back)."""
    if len(races) < 2:
        return races
    chains = [c["start_ms"] for _, log in boats for c in _course_chains(log)]
    keep = []
    for r, nxt in zip(races, races[1:] + [None]):
        g = r["start_ms"]
        if nxt and nxt["start_ms"] - g < 15 * 60_000 and not r["finish"]:
            raced = any(g - 60_000 <= c <= g + 180_000 for c in chains)
            if not raced and _max_excursion_m(boats, g, nxt["start_ms"]) < 300:
                r["general_recall"] = True
                continue
        keep.append(r)
    return keep


def find_races(boats: list[tuple[str, VkxLog]]) -> list[dict]:
    """All races in a set of logs. Uses the Atlas race timer when anyone used it,
    otherwise spots races from the sailing (beat, run, ... with no long stop)."""
    logs = [l for _, l in boats]
    guns = find_guns(logs)
    races = []
    if guns:
        timer = _timer_races(boats, guns)
        if timer:
            return _with_courses(timer, _course_races(boats, guns), boats)
    return _course_races(boats)


def _timer_races(boats, guns) -> list[dict]:
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
        races = _drop_general_recalls(races, boats)
    return races


def _with_courses(timer: list[dict], course: list[dict], boats) -> list[dict]:
    """Timer races keep the gun, and take the course from the sailing: the marks, the legs and
    the course letter of the race found from the tracks that set off with that gun.

    A race_end from the timer is only used where it makes sense: a stop pressed by mistake at the
    windward mark (before the race's last mark) is ignored, and the finish found from the sailing
    is used instead."""
    for r in timer:
        g = r["start_ms"]
        # the course found from the sailing that set off with this gun; a boat already beating up to
        # the line before the gun can make it look minutes early, so a course still under way at
        # the gun (and finishing well after it) counts too
        c = min((c for c in course if g - 150_000 <= c["start_ms"] <= g + 240_000
                 or (c["start_ms"] <= g and max(c["finish"].values(), default=0) >= g + 300_000)),
                key=lambda c: abs(c["start_ms"] - g), default=None)
        if c is None:
            continue
        pinged = any(any(e["end"] == "pin" and e["t"] <= g for e in log.line) and
                     any(e["end"] == "boat" and e["t"] <= g for e in log.line) for _, log in boats)
        for k in ("legs", "course_letter", "twd", "gate"):
            if k in c:
                r[k] = c[k]
        # the course marks; an estimated start/finish line only when nobody pinged the real one
        r["marks"] = [m for m in c.get("marks", []) if pinged is False or m["kind"] in ("windward", "leeward", "offset")]
        if not pinged:
            for k in ("line_est", "line_basis"):
                if k in c:
                    r[k] = c[k]
        last_mark = max((m.get("t", 0) for m in c.get("marks", []) if m["kind"] in ("windward", "leeward")), default=0)
        fin = dict(c["finish"])
        rend = dict(r["finish"])                       # race_end pressed on each boat's timer
        for name, t in rend.items():
            # the boat stopped its own timer: trust it if it is after the last mark and not long
            # after the finish found from the sailing
            cf = fin.get(name)
            if t > last_mark + 20_000 and (cf is None or t <= cf + 180_000):
                fin[name] = t if cf is None or t < cf else cf
        r["finish"] = fin
        # a last beat the sailing missed (short, or eased into the finish) but the timer saw
        if r.get("course_letter") in COURSES and r.get("legs") and r["legs"][-1] == "down":
            ends = [t for n, t in rend.items() if t > max(c["finish"].values(), default=0) + 45_000]
            if ends and _made_ground_upwind(boats, max(c["finish"].values()), min(ends), r["twd"]) >= 50:
                r["legs"] = r["legs"] + ["up"]
                for n, t in rend.items():                # they finished when they stopped the timer
                    if t > fin.get(n, 0):
                        fin[n] = t
                r["finish"] = fin
                lw = _lowest_point(boats, max(c["finish"].values()) - 60_000, min(ends), r["twd"])
                if lw:                                   # the leeward mark that last beat started from
                    i = max((k for k, m in enumerate(r["marks"]) if m["kind"] in ("windward", "leeward")), default=len(r["marks"]) - 1)
                    r["marks"].insert(i + 1, {"kind": "leeward", "lat": lw[0], "lon": lw[1], "boats": 1, "t": lw[2]})
                seq = tuple(1 if x == "up" else -1 for x in r["legs"])
                r["course_letter"] = _course_letter([(x,) for x in seq], None, None, None)[0] or r["course_letter"]
        if pinged and r.get("legs") and r["legs"][-1] == "up":
            _finish_at_pinged_line(r, boats)
    return timer


def _pinged_line(boats, g):
    pins = [e for _, log in boats for e in log.line if e["end"] == "pin" and e["t"] <= g]
    rcs = [e for _, log in boats for e in log.line if e["end"] == "boat" and e["t"] <= g]
    if not pins or not rcs:
        return None
    return max(pins, key=lambda e: e["t"]), max(rcs, key=lambda e: e["t"])


def _finish_at_pinged_line(r, boats):
    """With the start line pinged, the upwind finish is where each boat crosses that line (and its
    run on past the committee boat) going upwind after its last leeward rounding. That beats a
    timer stopped early or late, and a guess from the speed."""
    pl = _pinged_line(boats, r["start_ms"])
    wm = [m["t"] for m in r.get("marks", []) if m["kind"] == "windward" and "t" in m]
    if not pl or not wm:
        return
    pin, rc = pl
    lat0 = rc["lat"]
    ml = 111_320 * math.cos(math.radians(lat0))
    px, py = (pin["lon"] - rc["lon"]) * ml, (pin["lat"] - rc["lat"]) * 111_320
    L = math.hypot(px, py)
    if L < 5:
        return
    ux, uy = math.sin(math.radians(r["twd"])), math.cos(math.radians(r["twd"]))
    nx, ny = -py / L, px / L                           # normal to the line, pointing upwind
    if nx * ux + ny * uy < 0:
        nx, ny = -nx, -ny
    t_wm = max(wm)
    for name, log in boats:
        pts = [p for p in log.track if t_wm - 120_000 <= p["t"] <= t_wm + 12 * 60_000]
        if len(pts) < 30:
            continue
        d = np.array([((p["lon"] - rc["lon"]) * ml) * nx + ((p["lat"] - rc["lat"]) * 111_320) * ny for p in pts])
        q = np.array([((p["lon"] - rc["lon"]) * ml) * ux + ((p["lat"] - rc["lat"]) * 111_320) * uy for p in pts])
        # this boat's windward rounding, then its leeward rounding (furthest downwind after it)
        ts = np.array([p["t"] for p in pts])
        early = ts <= t_wm + 120_000
        iw = int(np.argmax(np.where(early, q, -np.inf)))
        run = (ts >= ts[iw]) & (ts <= ts[iw] + 6 * 60_000)        # the run that follows, at most 6 min
        il = int(np.argmin(np.where(run, q, np.inf)))
        cross = next((i for i in range(max(il, 1), len(d)) if d[i - 1] < 0 <= d[i] and ts[i] <= ts[il] + 6 * 60_000), None)
        if cross is not None:
            r["finish"][name] = int(pts[cross]["t"])


def _lowest_point(boats, t0, t1, twd):
    """Where a boat got furthest downwind between t0 and t1 (a leeward rounding), a few metres
    inside its turn. (lat, lon, t) or None."""
    ux, uy = math.sin(math.radians(twd)), math.cos(math.radians(twd))
    for _, log in boats:
        pts = [r for r in log.track if t0 <= r["t"] <= t1]
        if len(pts) < 10:
            continue
        ml = 111_320 * math.cos(math.radians(pts[0]["lat"]))
        d = [((p["lon"] - pts[0]["lon"]) * ml) * ux + ((p["lat"] - pts[0]["lat"]) * 111_320) * uy for p in pts]
        j = int(np.argmin(d))
        p = pts[j]
        cog = p.get("cog")                             # degrees
        la, lo = _offset(p["lat"], p["lon"], cog - 90.0, MARK_INSIDE_M)
        return la, lo, p["t"]
    return None


def _made_ground_upwind(boats, t0, t1, twd) -> float:
    ux, uy = math.sin(math.radians(twd)), math.cos(math.radians(twd))
    best = 0.0
    for _, log in boats:
        pts = [r for r in log.track if t0 <= r["t"] <= t1]
        if len(pts) < 2:
            continue
        ml = 111_320 * math.cos(math.radians(pts[0]["lat"]))
        d = [((p["lon"] - pts[0]["lon"]) * ml) * ux + ((p["lat"] - pts[0]["lat"]) * 111_320) * uy for p in pts]
        best = max(best, max(d))
    return best


def _windward_from_offsets(races):
    """On a course with an offset the boats' highest point is a poor guide to the windward mark (they
    can luff above it on the way to the offset). Put it where they came off the beat instead, averaged
    over the boats and roundings that show it."""
    for r in races:
        offs = {m.get("leg"): m for m in r["marks"] if m["kind"] == "offset"}
        for w in (m for m in r["marks"] if m["kind"] == "windward"):
            o = offs.get(w.get("leg"))
            turns = (o or {}).get("turns") or []
            if turns:
                w["lat"], w["lon"] = float(np.mean([q[0] for q in turns])), float(np.mean([q[1] for q in turns]))
                w["from_turn"] = True
        for o in offs.values():
            o.pop("turns", None)


def _fill_offsets(races):
    """On a course with an offset every windward rounding has one, but a boat can bear away early or
    a log can miss it. Put the missing ones where the others sit from their windward mark (the
    race's own when it has any, otherwise the day's)."""
    def vec(r):
        out = []
        ww = {m["leg"]: m for m in r["marks"] if m["kind"] == "windward" and m.get("leg") is not None}
        for o in (m for m in r["marks"] if m["kind"] == "offset"):
            w = ww.get(o.get("leg"))
            if w:
                out.append((o["lat"] - w["lat"], o["lon"] - w["lon"]))
        return out
    day = [v for r in races for v in vec(r)]
    for r in races:
        v = vec(r) or day
        if not v:
            continue
        dlat, dlon = float(np.median([a for a, _ in v])), float(np.median([b for _, b in v]))
        have = {m.get("leg") for m in r["marks"] if m["kind"] == "offset"}
        for w in [m for m in r["marks"] if m["kind"] == "windward" and m.get("leg") not in have]:
            r["marks"].append({"kind": "offset", "lat": w["lat"] + dlat, "lon": w["lon"] + dlon, "boats": 0,
                               "t": w.get("t", 0), "leg": w.get("leg"), "estimated": True})


def _course_races(boats, guns=None) -> list[dict]:
    races = []
    found = []                                       # (start, name, chain)
    for name, log in boats:
        for c in _course_chains(log, guns=guns):
            found.append((c["start_ms"], name, c))
    found.sort(key=lambda f: f[0])
    reach = [d for _, _, c in found for d in c.get("reach_m", [])]
    day_has_offsets = len(reach) >= 3 and float(np.median(reach)) >= OFFSET_DAY_MEDIAN_M
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
        # the course is the one the boat that sailed the most legs sailed (another boat's log can
        # miss a short last beat); otherwise the most common one
        letters = [c.get("course") for c in chains if c.get("course")]
        r["course_letter"] = best.get("course") or (max(set(letters), key=letters.count) if letters else "")
        r["twd"] = circ_mean([c["twd"] for c in chains])
        # marks: average each boat's rounding spot for the same mark
        marks = []
        for k, m in enumerate(best["marks"]):
            pts = [c["marks"][k] for c in chains if len(c["marks"]) > k and c["marks"][k]["kind"] == m["kind"]]
            mk = {"kind": m["kind"], "lat": float(np.mean([q["lat"] for q in pts])),
                  "lon": float(np.mean([q["lon"] for q in pts])), "boats": len(pts),
                  "t": int(max(q["t"] for q in pts)), "leg": m.get("leg")}
            # Leeward gate: anyone rounding to starboard means two marks. Place each by the
            # boats that rounded it; the leeward mark itself stays at the middle of the gate.
            if m["kind"] == "leeward" and any(q.get("rounded") == "starboard" for q in pts):
                mk["gate"] = []
                for side in ("port", "starboard"):
                    g = [q for q in pts if q.get("rounded") == side]
                    if g:
                        mk["gate"].append({"rounded": side, "lat": float(np.mean([q["lat"] for q in g])),
                                           "lon": float(np.mean([q["lon"] for q in g])), "boats": len(g)})
                r["gate"] = True
            marks.append(mk)
        # windward offsets, matched by the beat they follow (not every boat's log shows every one);
        # kept only when the day's sailing says there is an offset and at least half the boats show it
        legs_up = sorted({o["leg"] for c in chains for o in c.get("offsets", [])}) if day_has_offsets else []
        for leg in legs_up:
            pts = [o for c in chains for o in c.get("offsets", []) if o["leg"] == leg]
            if len(pts) * 2 < len(chains):
                continue
            marks.append({"kind": "offset", "lat": float(np.mean([q["lat"] for q in pts])),
                          "lon": float(np.mean([q["lon"] for q in pts])), "boats": len(pts),
                          "t": int(max(q["t"] for q in pts)), "leg": leg,
                          "turns": [q["turn"] for q in pts if q.get("turn")]})
        r["marks"] = marks
        if r["marks"]:
            r["_fin0"] = dict(r["finish"])
            _fit_line(r, boats)
            # nobody finishes before rounding the last mark: if the line fit says so, keep the
            # finish found from the sailing
            last = max((m.get("t", 0) for m in r["marks"] if m["kind"] in ("windward", "leeward")), default=0)
            for n, t in list(r["finish"].items()):
                if t < last + 20_000 and r["_fin0"].get(n, 0) > t:
                    r["finish"][n] = r["_fin0"][n]
    if day_has_offsets:
        _fill_offsets(races)
        _windward_from_offsets(races)
    races.sort(key=lambda r: r["start_ms"])
    _share_committee(races, boats)
    for r in races:
        r.pop("_onset", None)
        r.pop("_fin0", None)
    return races



def _course_axis(race: dict) -> float | None:
    """Bearing from the leeward mark (or the start) to the windward mark."""
    w = next((m for m in race["marks"] if m["kind"] == "windward"), None)
    b = next((m for m in race["marks"] if m["kind"] == "leeward"), None) or \
        next((m for m in race["marks"] if m["kind"] == "start"), None)
    if not w or not b:
        return None
    mlon = 111_320 * math.cos(math.radians(w["lat"]))
    dx, dy = (w["lon"] - b["lon"]) * mlon, (w["lat"] - b["lat"]) * 111_320
    if math.hypot(dx, dy) < 150:
        return None
    return math.degrees(math.atan2(dx, dy)) % 360


def _slope(a, q, max_deg=20.0, min_spread=30.0):
    """Tilt of a line through points (a along the line, q up the course), as dq/da.
    Zero unless there are three or more points spread out enough to say."""
    a, q = np.asarray(a, float), np.asarray(q, float)
    ok = np.isfinite(a) & np.isfinite(q)
    a, q = a[ok], q[ok]
    if len(a) < 3 or np.ptp(a) < min_spread:
        return 0.0
    k = float(np.polyfit(a, q, 1)[0])
    lim = math.tan(math.radians(max_deg))
    return max(-lim, min(lim, k))


def _share_committee(races: list[dict], boats, near_m: float = 40.0) -> None:
    """The committee boat stays anchored all day (it only swings on its anchor), and so does the
    finish mark; only the pin is moved much, to square the line. So when the estimates for
    races in a row put the committee boat within 40 m of each other, it is the same spot: average
    them and refit each race's lines from that one committee boat (and one finish mark).
    The pin stays each race's own."""
    def xy(p, ref):
        ml = 111_320 * math.cos(math.radians(ref[0]))
        return ((p[1] - ref[1]) * ml, (p[0] - ref[0]) * 111_320)
    est = [r for r in races if r.get("line_est") and r.get("_onset")]
    groups = []
    for r in est:
        rc = r["line_est"]["rc"]
        g = groups[-1] if groups else None
        if g:
            mean = (float(np.mean([q["line_est"]["rc"][0] for q in g])), float(np.mean([q["line_est"]["rc"][1] for q in g])))
            if math.hypot(*xy(rc, mean)) <= near_m:
                g.append(r)
                continue
        groups.append([r])
    for g in groups:
        if len(g) < 2:
            continue
        rc = [float(np.mean([r["line_est"]["rc"][k] for r in g])) for k in (0, 1)]
        fe = [r["line_est"]["finish_end"] for r in g]
        fm = [float(np.mean([p[k] for p in fe])) for k in (0, 1)]
        same_fin = all(math.hypot(*xy(p, fm)) <= near_m for p in fe)
        for r in g:
            r["finish"] = dict(r["_fin0"])
            _fit_line(r, boats, fixed_rc=rc, fixed_fin=fm if same_fin else None)
            r["line_est"]["committee_races"] = len(g)


def _fit_line(race: dict, boats: list[tuple[str, VkxLog]], hz: float = 2.0,
              fixed_rc=None, fixed_fin=None) -> None:
    """Start and finish lines off one committee boat in the middle: pin ... RC is the start,
    RC ... finish mark is the finish (upwind finish).

    Both lines are laid roughly square to the course (leeward to windward mark), not to the wind,
    but with one mark boat setting the pin and the finish buoy they are rarely exact. So the start
    line, with three or more boats spread along it at the gun, is tilted to fit where they were.
    The start line is usually longer than the finish line (at least half as long again here).
    Each boat's start and finish are then timed as the moment it crossed its line."""
    axis = _course_axis(race)
    T = math.radians(axis if axis is not None else race["twd"])
    race["line_basis"] = "course" if axis is not None else "wind"
    ux, uy = math.sin(T), math.cos(T)                  # up the course
    rx, ry = math.cos(T), -math.sin(T)                 # to the right, looking up the course
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
        a = x * rx + y * ry
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
        per[name] = dict(grid=grid, x=x, y=y, q=q, a=a, ia=ia, drop=drop)
    if not per:
        return
    V = list(per.values())
    # where everyone was at the start: boats are spread along the line at the gun
    t_gun = int(np.median([race["_onset"][n] for n in per]))
    at_gun = [min(len(v["grid"]) - 1, max(0, int((t_gun - v["grid"][0]) / (1000 / hz)))) for v in V]
    a_on, q_on = [v["a"][i] for v, i in zip(V, at_gun)], [v["q"][i] for v, i in zip(V, at_gun)]
    a_dr, q_dr = [v["a"][v["drop"]] for v in V], [v["q"][v["drop"]] for v in V]
    if sgn > 0:
        # Upwind finish: boats cross, carry on for a few seconds, then ease off. That drop places
        # the line better than the start, where boats are still building speed below it.
        q0 = float(np.nanmedian(q_dr)) - 8.0
    else:
        q0 = float(np.nanmedian(q_on + q_dr))
    a_ref = float(np.nanmedian(a_dr)) if sgn > 0 else float(np.nanmedian(a_on + a_dr))
    # The finish is short and boats ease off at different distances past it, so its tilt cannot
    # be read reliably: keep it square to the course. The start is long and the fleet is spread
    # along it at the gun, so with three or more boats its tilt can be.
    k_s, k_f = _slope(a_on, q_on, min_spread=40.0), 0.0

    def crossing(v, lo, hi, prefer, qline, d=1.0):
        q = d * (v["q"] - qline(v["a"]))
        xs = [i for i in range(max(1, lo), min(len(q) - 1, hi)) if q[i - 1] < 0 <= q[i]]
        return min(xs, key=lambda i: abs(i - prefer)) if xs else None

    def start_crossing(v, qline):
        """The crossing that starts the race: going up through the line and staying above it
        for the next minute (a dip back below before the gun does not count). A boat can set
        off upwind well below the line, so look up to 2½ minutes after it did."""
        q = v["q"] - qline(v["a"])
        lo, hi = max(1, v["ia"] - int(60 * hz)), min(len(q) - 1, v["ia"] + int(150 * hz))
        for i in range(lo, hi):
            if q[i - 1] < 0 <= q[i] and np.nanmin(q[i:i + int(60 * hz)]) > -5:
                return i
        xs = [i for i in range(lo, hi) if q[i - 1] < 0 <= q[i]]
        if xs:
            return min(xs, key=lambda i: abs(i - v["ia"]))
        # never crossed it: closest approach
        seg = np.abs(q[lo:hi])
        return lo + int(np.nanargmin(seg)) if np.isfinite(seg).any() else v["ia"]

    # pass 1: one straight line square to the course, to find which side each line is on
    flat = lambda a: q0 + 0 * a
    def times(qs, qf):
        st, fi, a_s, a_f = [], {}, [], []
        for name, v in per.items():
            i_s = start_crossing(v, qs)
            i_f = crossing(v, v["drop"] - int(90 * hz), v["drop"] + int(5 * hz), v["drop"], qf, sgn) or v["drop"]
            st.append(int(v["grid"][i_s])); fi[name] = int(v["grid"][i_f])
            a_s.append(float(v["a"][i_s])); a_f.append(float(v["a"][i_f]))
        return st, fi, a_s, a_f
    starts, fins, a_s, a_f = times(flat, flat)
    # Looking up the course, the start is always to the left of the committee boat and the finish
    # to the right. Where boats started and finished agree with that, the committee boat goes
    # between them; where they do not, it goes between the middles of the two.
    right = True
    rc = (max(a_s) + min(a_f)) / 2 if max(a_s) < min(a_f) else (np.mean(a_s) + np.mean(a_f)) / 2
    # pass 2: both lines meet at the committee boat; each tilts to fit its own boats
    q_rc = q0 + k_f * (rc - a_ref)
    fin_mark_a = None
    if fixed_rc is not None:                           # the committee boat's spot, shared across races
        X, Y = (fixed_rc[1] - lon0) * mlon, (fixed_rc[0] - lat0) * 111_320
        rc, q_rc = X * rx + Y * ry, X * ux + Y * uy
        if fixed_fin is not None:                      # and the finish mark's: that sets the finish line
            X, Y = (fixed_fin[1] - lon0) * mlon, (fixed_fin[0] - lat0) * 111_320
            fa, fq = X * rx + Y * ry, X * ux + Y * uy
            if fa - rc >= 10:                          # the finish mark is right of the committee boat
                lim = math.tan(math.radians(25))
                k_f = max(-lim, min(lim, (fq - q_rc) / (fa - rc)))
                right, fin_mark_a = fa > rc, fa
    qs = lambda a: q_rc + k_s * (a - rc)
    qf = lambda a: q_rc + k_f * (a - rc)
    starts, fins, a_s, a_f = times(qs, qf)
    d = 1 if right else -1
    fin_len = max(20.0, (max(a_f) - rc if right else rc - min(a_f)) + 15)
    if fin_mark_a is not None:
        fin_len = max(15.0, abs(fin_mark_a - rc))
    st_len = max(1.5 * fin_len, 45.0, (rc - min(a_s) if right else max(a_s) - rc) + 20)
    pin, fmark = rc - d * st_len, rc + d * fin_len

    def ll(a, qline):
        qq = qline(a)
        X, Y = qq * ux + a * rx, qq * uy + a * ry
        return [lat0 + Y / 111_320, lon0 + X / mlon]
    race["start_ms"] = int(np.median(starts))
    race["finish"] = fins
    race["line_est"] = {"pin_end": ll(pin, qs), "rc": ll(rc, qs), "finish_end": ll(fmark, qf),
                        "start_len_m": round(st_len), "finish_len_m": round(fin_len),
                        "basis": race["line_basis"],
                        "start_tilt": round(math.degrees(math.atan(k_s))), "finish_tilt": round(math.degrees(math.atan(k_f)))}
    for m in race["marks"]:
        if m["kind"] == "start":
            m["lat"], m["lon"] = ll(float(np.mean(a_s)), qs)
        elif m["kind"] == "finish":
            m["lat"], m["lon"] = ll(float(np.mean(a_f)), qf)

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
               not_before_ms: int | None = None, area: dict | None = None) -> dict:
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
            notes.append("No race timer was used, so starts and finishes are timed where boats crossed an estimated "
                         "line, roughly square to the course with the committee boat between the start and finish ends.")
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
        if race is not None and race.get("next_start_ms"):
            nxt = sorted(nxt + [race["next_start_ms"]])
        cap = (nxt[0] - int(pre_start_s * 1000)) if nxt else data_end
        ends = []
        for s in series:
            fin = [e["t"] for e in s.log.timer if e["event"] == "race_end" and gun_ms < e["t"] <= cap]
            ends.append(min(fin) + 90_000 if fin else min(cap, s.log.end_ms))
        t1 = min(cap, max(ends))
    else:
        notes.append("No race start found on the timers, so the whole session is shown and start analysis is skipped.")
        t0, t1 = data_start, data_end
    if area and gun_ms is not None:
        out = _left_area(series, area, gun_ms, t1)
        if out is not None and out < t1:
            t1 = out
            notes.append("The replay ends once the boats left the racing area (sailing in).")

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
    marks_xy = []
    for m in (race or {}).get("marks", []):
        if m["kind"] in ("windward", "leeward", "wing", "offset"):
            for g in (m.get("gate") or [m]):
                marks_xy.append(((g["lon"] - lon0) * mlon, (g["lat"] - lat0) * mlat))
    out_boats = []
    for s in series:
        out_boats.append(_analyse_boat(s, grid, hz, gun_i, twd_series, (ux, uy), ref, line, gun_ms, finishes.get(s.name),
                                       marks_xy=marks_xy))
    # most of the fleet going back is a general recall (or a postponed start), not individual recalls
    rec = [b for b in out_boats if any(p["kind"] == "recall" for p in b["penalties"])]
    if (len(out_boats) >= 3 and len(rec) > len(out_boats) / 2) or (len(out_boats) == 2 and len(rec) == 2):
        for b in rec:
            b["penalties"] = [p for p in b["penalties"] if p["kind"] != "recall"]

    _rank(out_boats)

    # fleet wind speed estimate and the race-average wind direction
    tws_all = np.vstack([b.pop("_tws") for b in out_boats])
    racing_each = [b.pop("_racing") for b in out_boats]
    steady_each = [b.pop("_steady") for b in out_boats]
    racing_any = np.any(np.vstack(racing_each), axis=0)
    with np.errstate(all="ignore"):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            tws_fleet = np.nanmedian(tws_all, axis=0)
    tws_smooth = _nan_movmedian(tws_fleet, int(90 * hz))
    ok = np.isfinite(tws_smooth) & racing_any
    if ok.sum() >= 2:                                   # upwind legs in a breeze: between the downwind readings
        idx = np.arange(len(tws_smooth))
        tws_smooth = np.where(np.isfinite(tws_smooth), tws_smooth, np.interp(idx, idx[ok], tws_smooth[ok]))
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
    sides = _pace(out_boats, racing_each, wind_speed.get("tws"), twd_avg, hz, lat0, lon0, gun_i, steady_each)
    _phase(out_boats, racing_each, twd_series, twd_avg, hz)
    if line is not None:
        fin_end = _finish_from_pinged(line, series, grid, finishes, lat0, lon0, mlon, mlat)
        line = {"pin": line["pin"], "boat": line["boat"],
                "length_m": _r(math.dist(line["pin_xy"], line["boat_xy"]), 0)}
        if fin_end:
            line["finish"] = fin_end
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
            "marks": [{"kind": m["kind"], "lat": _r(m["lat"], 6), "lon": _r(m["lon"], 6),
                       **({"gate": [{"rounded": g["rounded"], "lat": _r(g["lat"], 6), "lon": _r(g["lon"], 6)} for g in m["gate"]]}
                          if m.get("gate") else {})} for m in (race or {}).get("marks", [])],
            "gate": bool((race or {}).get("gate")),
            "line_est": (race or {}).get("line_est") if line is None else None,
            "tide": (local.tide_summary(int(gun_ms if gun_ms is not None else t0), int(t0 + n * 1000 / hz))
                     if local.near_station(lat0, lon0) else None),
            "coast": local.coastline_near(lat0, lon0),
        },
        "line": line,
        "wind": {"every": wind_every, "twd": _r(twd_series[::wind_every], 0) if twd_series is not None else None,
                 "twd_avg": _r(twd_avg, 0), "tws": _r(tws_smooth[::wind_every], 1), "speed": wind_speed},
        "sides": sides,
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
    twd = _refine_shifts(series, twd, twd_global, hz)
    return twd, twd_global, source


def _refine_shifts(series, twd, twd_global, hz, smooth_s: float = 45.0):
    """Track shifts second by second from upwind headings, the way the Atlas's own shift display works.
    A boat sailing upwind holds a steady angle to the wind, so on starboard the wind is its course plus
    that angle, on port its course minus it. The angle is calibrated so that boats on opposite tacks at
    the same moment read the same wind; with one boat, from the turn through its tacks. The fleet's
    readings are combined and smoothed. Where nobody is sailing upwind steadily, the tack-based
    estimate is kept."""
    n = len(twd)
    boats = []
    for s in series:
        cog = circ_smooth(s.cog, max(1, int(4 * hz)))
        rate = np.abs(wrap180(np.roll(cog, -int(5 * hz)) - np.roll(cog, int(5 * hz))))
        rel = wrap180(twd - cog)                              # + = wind on the starboard side
        up = (np.abs(rel) > 25) & (np.abs(rel) < 65) & (s.sog > 2.0) & (rate < 12) & np.isfinite(cog)
        if up.sum() >= 120 * hz:
            boats.append((s, cog, up, np.sign(rel)))
    if not boats:
        return twd
    # fleet calibration: at the same moment, port course minus starboard course = 2 x tacking half-angle
    pair = []
    for s1, c1, u1, sd1 in boats:
        for s2, c2, u2, sd2 in boats:
            if s1 is s2:
                continue
            m = u1 & u2 & (sd1 > 0) & (sd2 < 0)
            if m.sum() >= 5 * hz:
                pair.extend((wrap180(c2[m] - c1[m]) / 2).tolist())
    fleet_alpha = float(np.median(pair)) if len(pair) >= 120 * hz else None
    reads = []
    for s, cog, up, side in boats:
        if fleet_alpha is not None:
            alpha = fleet_alpha
        else:
            tacks = [abs(turn) for j, bis, turn in _turns_from_cog(s.cog, s.sog, hz)
                     if abs(wrap180(bis - twd_global)) < 40 and 60 < abs(turn) < 130]
            alpha = float(np.median(tacks)) / 2 if len(tacks) >= 2 else \
                float(np.nanmedian(np.abs(wrap180(twd_global - cog[up]))))
        r = np.full(n, np.nan)
        r[up] = (cog[up] + side[up] * alpha) % 360
        reads.append(r)
    R = np.vstack(reads)
    rad = np.radians(R)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        sn, cs_ = np.nanmean(np.sin(rad), axis=0), np.nanmean(np.cos(rad), axis=0)
    have = np.isfinite(sn)
    k = max(1, int(smooth_s * hz))
    sn_s, cs_s = _nan_movmean(np.where(have, sn, np.nan), k), _nan_movmean(np.where(have, cs_, np.nan), k)
    est = np.degrees(np.arctan2(sn_s, cs_s)) % 360
    ok = np.isfinite(est)
    if ok.sum() < 60 * hz:
        return twd
    # fill the gaps (downwind legs, pre-start) by blending back towards the tack-based estimate
    idx = np.arange(n)
    dev = wrap180(est - twd)
    dev_f = np.interp(idx, idx[ok], dev[ok])
    gap = np.full(n, np.inf)
    last = -np.inf
    for i in range(n):
        if ok[i]:
            last = i
        gap[i] = i - last
    nxt = np.inf
    for i in range(n - 1, -1, -1):
        if ok[i]:
            nxt = i
        gap[i] = min(gap[i], nxt - i)
    weight = np.exp(-gap / (90 * hz))                         # fades out over ~1.5 minutes from a reading
    return (twd + np.clip(dev_f * weight, -25, 25)) % 360

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


def _finish_from_pinged(line, series, grid, finishes, lat0, lon0, mlon, mlat):
    """The finish runs on from the committee boat, away from the pin (the start is to its left,
    the finish to its right). Only the start ends get pinged, so the finish mark is placed just
    beyond where the boats finished: at least 25 m, at most 150 m out."""
    (px, py), (bx, by) = line["pin_xy"], line["boat_xy"]
    L = math.hypot(bx - px, by - py)
    if L < 5:
        return None
    ex, ey = (bx - px) / L, (by - py) / L              # along the line, pin -> committee -> finish
    out = 0.0
    for s in series:
        f = finishes.get(s.name)
        if f is None:
            continue
        # where the boat was on its way across: near the line in the last minute before finishing
        sel = (grid >= f - 60_000) & (grid <= f + 10_000) & np.isfinite(s.x)
        along = (s.x[sel] - bx) * ex + (s.y[sel] - by) * ey
        perp = np.abs(-(s.x[sel] - bx) * ey + (s.y[sel] - by) * ex)
        near = along[perp <= 40]
        if len(near):
            out = max(out, float(np.max(near)))
    d = min(150.0, max(25.0, out + 15.0))
    fx, fy = bx + ex * d, by + ey * d
    return [lat0 + fy / mlat, lon0 + fx / mlon]


def _penalties(s, hz, gun_i, finish_i, vmg, prog, marks_xy, over=None) -> list:
    """Things that look like penalties, for showing a boat in red while it sorts them out.
    These are guesses from GPS: the line and marks are estimates and many penalties are taken
    without a log being able to tell, so each episode is kept short.

    - recall: soon after the gun the boat turns back below the start and then sets off upwind again.
    - turns: a full circle (one way, 330°+ inside 45 s) anywhere in the race; 'mark' if it was
      done next to a mark. Two circles back to back are two turns."""
    n = len(s.sog)
    out = []
    if gun_i is None or gun_i >= n:
        return out
    end = finish_i if finish_i is not None else n
    t = lambda i: _r(i / hz, 1)
    # ---- returned to the start -----------------------------------------------------------
    if prog is not None and vmg is not None:
        w1 = min(end, gun_i + int(100 * hz))
        p = np.where(np.isfinite(prog), prog, np.nan)
        best, i_best = -np.inf, None
        for i in range(gun_i, w1):
            if not np.isfinite(p[i]):
                continue
            if p[i] > best:
                best, i_best = p[i], i
            # gone back 20 m or more, after sailing away from the wind for 12 s or more
            if best - p[i] >= 20 and np.sum(vmg[i_best:i + 1] < -0.8) >= 12 * hz:
                lo = i_best
                j_low = i + int(np.nanargmin(p[i:min(end, i + int(120 * hz))]))
                # it has to have been over and come back: with a pinged line, over it before turning
                # back and behind it after; without one, at least some way up from where it was at the gun
                if over is not None:
                    was_over = np.nanmax(over[gun_i:i + 1]) > 0.5
                    came_back = np.nanmin(over[i:min(end, j_low + int(10 * hz)) + 1]) < 0
                    if not (was_over and came_back):
                        break
                elif not (np.isfinite(p[gun_i]) and best - p[gun_i] >= 5):
                    break
                k_end = None
                for k in range(j_low, min(end, lo + int(150 * hz))):
                    seg = vmg[k:k + int(10 * hz)]
                    if len(seg) and np.nanmin(seg) > 0.8:      # going upwind again, steadily
                        k_end = k
                        break
                if k_end is None:
                    k_end = min(end - 1, lo + int(150 * hz))
                out.append({"kind": "recall", "label": "Over early, going back", "t0": t(lo), "t1": t(k_end)})
                break
    # ---- circles ---------------------------------------------------------------------------
    cog = circ_smooth(s.cog, max(1, int(1 * hz)))
    d = np.nan_to_num(wrap180(np.diff(cog)))
    d = np.where((np.abs(d) > 60) | (s.sog[1:] < 0.6), 0, d)          # GPS jumps, drifting
    c = np.concatenate([[0.0], np.cumsum(d)])
    win = int(45 * hz)
    i = gun_i + int(20 * hz)
    while i < end - int(10 * hz):
        seg = c[i:min(end, i + win)] - c[i]
        hit = np.nonzero(np.abs(seg) >= 330)[0]
        if not len(hit):
            i += max(1, int(hz))
            continue
        j = i + int(hit[0])
        sense = np.sign(seg[hit[0]])
        # tighten the start: the turn begins where the one-way rotation began
        a = i
        while a < j and sense * (c[a + 1] - c[a]) <= 0.5:
            a += 1
        # a second circle straight after makes it two turns
        seg2 = c[j:min(end, j + win)] - c[j]
        two = len(seg2) and np.max(sense * seg2) >= 300
        if two:
            j = j + int(np.nonzero(sense * seg2 >= 300)[0][0])
        # cleared once going again: speed back to 80% of the speed going in, at most 25 s later
        v_in = np.nanmedian(s.sog[max(0, a - int(10 * hz)):a + 1])
        k = j
        while k < min(end - 1, j + int(25 * hz)) and not (s.sog[k] >= 0.8 * v_in):
            k += 1
        near = None
        if marks_xy:
            mid = (a + j) // 2
            dm = [math.hypot(s.x[mid] - mx, s.y[mid] - my) for mx, my in marks_xy]
            if min(dm) <= 45:
                near = True
        if near:
            # rounding a mark is itself a big turn (often with a gybe or tack on the way in or out),
            # so a turn at a mark only counts when it goes a full circle beyond the rounding
            seg3 = sense * (c[a:min(end, a + int(70 * hz))] - c[a])
            if not len(seg3) or np.max(seg3) < 330 + 180:
                i = j + int(5 * hz)
                continue
        label = ("Penalty turn at the mark" if near else ("Two penalty turns" if two else "Penalty turn"))
        out.append({"kind": "mark" if near else "turns", "label": label, "t0": t(a), "t1": t(min(k, a + int(90 * hz)))})
        i = k + int(5 * hz)
    # a turn made while going back to the start is part of the recall
    rec = [p for p in out if p["kind"] == "recall"]
    out = [p for p in out if p["kind"] == "recall" or not any(p["t0"] <= r["t1"] and p["t1"] >= r["t0"] for r in rec)]
    return out


def _analyse_boat(s: BoatSeries, grid, hz, gun_i, twd, u, ref, line, gun_ms, finish_ms=None, marks_xy=None):
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
        prev_c = 0
        for c in changes:
            # find actual crossing near c (where twa crosses 0 or 180), but not further back than the
            # previous change: two tacks inside 15 s would otherwise both find the first one's head to
            # wind, look like a tack undone, and both be dropped
            lo = max(1, c - int(15 * hz), prev_c + 1)
            prev_c = c
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
                # drop manoeuvres that were undone quickly (wobbles). A real tack takes 4-5 s, and boats
                # do tack twice inside 8 s near a mark or a layline, so only closer pairs are wobbles.
                if man and tm - man[-1]["i"] < 5 * hz:
                    man.pop()
                    continue
                man.append(m)
    for m in man:
        m["t"] = _r(m.pop("i") / hz, 1)

    # ---- steady sailing ---------------------------------------------------------------
    # The averages (and the wind speed read from boat speed) describe the boat sailing in a straight
    # line, so tacks, gybes, mark roundings and the acceleration off the start line are left out.
    # On 8 Nov 2026 the fleet's tacks cost speed from head to wind to about +6 s, gybes from -2 s to
    # +10 s; the windows below cover that with a margin, and longer when a boat was slow to recover.
    steady = racing & np.isin(mode, ["up", "down"]) & (sog > 1.5)
    if have_wind:
        for m in man:
            i = int(round(m["t"] * hz))
            pre, post = MANOEUVRE_WINDOW_S[m["kind"]]
            if m.get("recover_s") is not None:
                post = max(post, min(30.0, m["recover_s"] + 4))
            steady[max(0, i - int(pre * hz)):min(n, i + int(post * hz))] = False
        prev = None
        for i in range(n):                                  # mark roundings: the point of sail changes
            x = mode[i]
            if x in ("up", "down", "reach"):
                if prev is not None and x != prev:
                    steady[max(0, i - int(15 * hz)):min(n, i + int(20 * hz))] = False
                prev = x
    if gun_i is not None:
        steady[gun_i:min(n, gun_i + int(20 * hz))] = False
    if finish_i is not None:
        steady[max(0, finish_i - int(5 * hz)):min(n, finish_i + 1)] = False

    def _avg(x, m, nd=2):
        v = np.asarray(x, float)[m]
        v = v[np.isfinite(v)]
        return _r(float(np.mean(v)), nd) if len(v) >= 10 * hz else None

    # ---- wind speed from boat speed (no sensor) ----------------------------------------
    tws_est = np.full(n, np.nan)
    if have_wind:
        sog_s = _nan_movmean(np.where(steady, sog, np.nan), int(20 * hz))
        est = polar.wind_from_speed(sog_s, _nan_movmedian(np.abs(twa), int(9 * hz)))
        tws_est = np.where(steady, est, np.nan)
        dn_est = tws_est[mode == "down"]
        if np.isfinite(dn_est).sum() >= 60 * hz and np.nanmedian(dn_est) >= UPWIND_TWS_TRUSTED_BELOW:
            tws_est = np.where(mode == "down", tws_est, np.nan)

    # ---- heel, smoothed for the sea state -----------------------------------------------
    # Waves roll the boat, so heel read twice a second is noisy in a chop. How much the bow pitches
    # (with slow trends taken out) measures the sea, so the rougher it was the longer heel is
    # averaged; with no pitch, wind speed stands in. Calm days keep a short window, so heel stays
    # sensitive in light air.
    heel_smooth_s, pitch_sd = _heel_window(s.pitch, racing & (sog > 3), hz, tws_est)
    heel_s = _nan_movmean(s.heel, max(1, int(round(heel_smooth_s * hz))))
    if have_wind:
        heel_lw = -heel_s * side

    # ---- legs -------------------------------------------------------------------------
    legs = []
    if have_wind:
        lab = np.where(racing, mode, "-")
        lab = np.where(lab == "slow", "-", lab)
        for k, (l, a, b) in enumerate(_segments(list(lab), int(45 * hz))):
            if l in ("-", "") or b - a < 30 * hz:
                continue
            sl = slice(a, b)
            st_l = np.zeros(n, bool)
            st_l[sl] = steady[sl] if l in ("up", "down") else (racing[sl] & (sog[sl] > 1.5))
            ms = [m for m in man if a / hz <= m["t"] < b / hz and m["kind"] in ("tack", "gybe")]
            legs.append({
                "type": {"up": "Upwind", "down": "Downwind", "reach": "Reach"}.get(l, l),
                "start": _r(a / hz, 1), "end": _r(b / hz, 1),
                "duration_s": _r((b - a) / hz, 0),
                "distance_m": _r(dist[b - 1] - dist[a], 0),
                "avg_sog": _avg(sog, st_l),
                "avg_vmg": _avg(vmg_abs, st_l),
                "avg_twa": _avg(np.abs(twa), st_l, 0),
                "avg_heel": _avg(heel_lw, st_l, 1),
                **_tws_range(tws_est[sl]),
                "manoeuvres": len(ms),
            })

    roundings = []
    for prev, nxt in zip(legs, legs[1:]):
        if prev["type"] != nxt["type"]:
            i = int(nxt["start"] * hz)
            mla, mlo = s.lat[i], s.lon[i]
            if np.isfinite(s.cog[i]) and np.isfinite(mla):     # inside the turn, as for the course marks
                mla, mlo = _offset(mla, mlo, s.cog[i] - 90.0, MARK_INSIDE_M)
            roundings.append({"t": nxt["start"], "kind": "rounding",
                              "label": ("Windward" if prev["type"] == "Upwind" else "Leeward") + " mark",
                              "entry_sog": _r(sog[max(0, i - int(10 * hz))]), "lat": _r(mla, 6), "lon": _r(mlo, 6)})
    man = sorted(man + roundings, key=lambda m: m["t"])

    # ---- start ------------------------------------------------------------------------
    start = None
    over = None                                        # metres over the pinged line (+ = course side)
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
            over = d
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
    up = steady & (mode == "up")
    dn = steady & (mode == "down")
    tacks = [m for m in man if m["kind"] == "tack"]
    gybes = [m for m in man if m["kind"] == "gybe"]
    summary = {
        "finish_s": _r(finish_i / hz, 1) if finish_i is not None else None,
        "elapsed_s": _r((finish_i - gun_i) / hz, 0) if finish_i is not None and gun_i is not None else None,
        "distance_m": _r(dist[-1], 0),
        "max_sog": _r(np.nanmax(np.where(racing, sog, np.nan)) if racing.any() else np.nan),
        "avg_sog": _r(np.nanmean(sog[racing]) if racing.any() else np.nan),
        "up_vmg": _avg(vmg_abs, up),
        "up_sog": _avg(sog, up),
        "up_twa": _avg(np.abs(twa), up, 0),
        "up_heel": _avg(heel_lw, up, 1),
        "heel_smooth_s": heel_smooth_s, "pitch_sd": pitch_sd,
        "dn_vmg": _avg(vmg_abs, dn),
        "dn_sog": _avg(sog, dn),
        "dn_twa": _avg(np.abs(twa), dn, 0),
        "tacks": len(tacks), "gybes": len(gybes),
        "tack_loss_m": _r(np.nanmedian([m["loss_m"] for m in tacks if m["loss_m"] is not None]) if tacks else np.nan, 1),
        "gybe_loss_m": _r(np.nanmedian([m["loss_m"] for m in gybes if m["loss_m"] is not None]) if gybes else np.nan, 1),
        "tack_recover_s": _r(np.nanmedian([m["recover_s"] for m in tacks if m["recover_s"] is not None]) if tacks else np.nan, 1),
        "made_good_m": _r(mg[-1], 0),
        "heading_offset": _r(hdg_offset, 1),
        "up_leeway": _r(leeway, 1),
    }
    if up.any():
        # heel and VMG over 3 s, so a wave or a gust's roll doesn't throw a reading into the wrong bin
        k3 = max(1, int(3 * hz))
        summary["heel_vs_vmg"] = _heel_bins(heel_lw[up], _nan_movmean(vmg_abs, k3)[up], hz)

    res.update({
        "track": {
            "lat": _r(s.lat, 6), "lon": _r(s.lon, 6),
            "sog": _r(sog, 2), "cog": _r(cog_s, 0), "hdg": _r(hdg_fixed, 0),
            "heel": _r(heel_lw if have_wind else heel_s, 1),
            "twa": _r(twa, 0), "vmg": _r(vmg_abs, 2),
            "mg": _r(mg, 0), "mode": [m if m else "" for m in mode.tolist()],
            "steady": "".join("1" if v else "0" for v in steady.tolist()),
        },
        "manoeuvres": man, "legs": legs, "start": start, "summary": summary,
        "penalties": _penalties(s, hz, gun_i, finish_i, vmg if have_wind else None, prog if have_wind else None, marks_xy or [], over),
        "_tws": tws_est, "_racing": racing, "_steady": steady,
    })
    return res


def _pace(boats, racing_each, tws_ref, twd_avg, hz, lat0, lon0, gun_i=None, steady_each=None):
    """Pace: boat speed as a % of Melges 15 target speed for the race's average wind, at the angle
    being sailed. Without a wind sensor the reference has to be one wind speed for the whole race;
    a boat in more breeze, or sailing well, reads over 100%, and one in a lull reads under.
    Adds track["pace"], leg and summary averages, and returns pace by side of the course."""
    if not tws_ref or twd_avg is None:
        return None
    T = math.radians(twd_avg)
    rx, ry = math.cos(T), -math.sin(T)                   # to the right, looking upwind
    mlon = 111_320 * math.cos(math.radians(lat0))
    pool = {"up": ([], []), "down": ([], [])}
    for bi, (b, racing) in enumerate(zip(boats, racing_each)):
        tr = b["track"]
        steady = steady_each[bi] if steady_each is not None else racing
        f = lambda k: np.array([np.nan if v is None else v for v in tr[k]], float)
        sog, twa = _nan_movmean(f("sog"), int(6 * hz)), f("twa")
        # an asymmetric boat doesn't sail deeper than ~150 deg; deeper readings are mid-gybe or wind error
        tgt = polar.target_speeds(tws_ref, np.clip(np.abs(np.nan_to_num(twa, nan=90.0)), 40, 150))
        pace = np.where(racing & (sog > 0.8) & np.isfinite(twa), sog / tgt * 100, np.nan)
        # Before the gun every boat is measured against close-hauled target speed, whatever it is
        # doing, so the trail shows who is building speed into the start.
        up_tgt = polar.speed(tws_ref, 45)
        if gun_i is not None and gun_i > 0:
            raw = f("sog")
            pre = np.arange(len(pace)) < gun_i
            pace = np.where(pre & np.isfinite(raw), _nan_movmean(raw, int(3 * hz)) / up_tgt * 100, pace)
            if b.get("start") is not None and gun_i < len(pace):
                st = b["start"]
                st["pace_at_gun"] = _r(raw[gun_i] / up_tgt * 100, 0) if np.isfinite(raw[gun_i]) else None
                j = min(len(raw) - 1, gun_i + int(10 * hz))
                st["pace_10s"] = _r(raw[j] / up_tgt * 100, 0) if np.isfinite(raw[j]) else None
                k = max(0, gun_i - int(10 * hz))
                st["pace_10s_before"] = _r(raw[k] / up_tgt * 100, 0) if np.isfinite(raw[k]) else None
        tr["pace"] = _r(pace, 0)
        mode = np.array(tr["mode"], dtype=object)
        for leg in b["legs"]:
            sl = slice(int(leg["start"] * hz), int(leg["end"] * hz))
            ps = pace[sl][steady[sl]] if leg["type"] in ("Upwind", "Downwind") else pace[sl]
            leg["pace"] = _r(np.nanmean(ps), 0) if np.isfinite(ps).sum() >= 10 * hz else None
        for key, m in (("up", "up"), ("down", "down")):
            sel = (mode == m) & np.isfinite(pace) & steady
            b["summary"][f"{key}_pace"] = _r(np.nanmean(pace[sel]), 0) if sel.sum() > 20 else None
            a = (f("lon")[sel] - lon0) * mlon * rx + (f("lat")[sel] - lat0) * 111_320 * ry
            pool[key][0].extend(a.tolist())
            pool[key][1].extend(pace[sel].tolist())
    out = {"tws_ref": _r(tws_ref, 1)}
    for key, (a, p) in pool.items():
        a, p = np.array(a), np.array(p)
        if len(a) < 60:
            out[key] = None
            continue
        lo, hi = np.percentile(a, [33.3, 66.7])
        parts = [("Left", a < lo), ("Middle", (a >= lo) & (a <= hi)), ("Right", a > hi)]
        # against the fleet's own average on this point of sail, so the sides compare directly and an
        # error in the target speeds (or the wind estimate) doesn't push every side over or under 100%
        avg = float(np.nanmean(p))
        out[key] = [{"side": n, "pace": _r(np.nanmean(p[m]) / avg * 100, 0), "pace_target": _r(np.nanmean(p[m]), 0),
                     "n": int(m.sum())} for n, m in parts]
    return out


def _phase(boats, racing_each, twd, twd_avg, hz, min_shift: float = 3.0):
    """How often each boat was on the tack or gybe the current shift favours.
    Upwind the lifted tack: starboard when the wind has veered (right) of the race average, port when
    it has backed (left). Downwind the opposite: the gybe that points closer to the leeward mark.
    Moments within min_shift degrees of the average favour neither side and are left out."""
    if twd is None or twd_avg is None:
        return
    # Reference wind: halfway between the fleet's typical starboard and port headings upwind. Time-averaging
    # leans towards whichever tack was sailed longer; the midpoint doesn't, as lifts on both tacks cancel.
    st, pt = [], []
    for b, racing in zip(boats, racing_each):
        tr = b["track"]
        twa = np.array([np.nan if v is None else v for v in tr["twa"]], float)
        cog = np.array([np.nan if v is None else v for v in tr["cog"]], float)
        up = racing & (np.array(tr["mode"], dtype=object) == "up") & np.isfinite(cog) & np.isfinite(twa)
        st.extend(cog[up & (twa > 0)].tolist())
        pt.extend(cog[up & (twa < 0)].tolist())
    if len(st) > 60 and len(pt) > 60:
        ms, mp = circ_mean(st), circ_mean(pt)
        twd_avg = (ms + wrap180(mp - ms) / 2) % 360
    for b in boats:
        b["summary"]["shift_ref"] = _r(twd_avg, 0)
    shift = wrap180(np.asarray(twd, float) - twd_avg)
    big = np.abs(shift) >= min_shift
    for b, racing in zip(boats, racing_each):
        tr = b["track"]
        twa = np.array([np.nan if v is None else v for v in tr["twa"]], float)
        mode = np.array(tr["mode"], dtype=object)
        side = np.sign(twa)                                   # +1 starboard, -1 port
        # each moment counts by the size of the shift: being on the right tack in an 8 deg shift
        # matters more than in a 3 deg wobble (and is less sensitive to the estimate near the threshold)
        w = np.abs(shift)
        for key, m, want in (("up", "up", np.sign(shift)), ("down", "down", -np.sign(shift))):
            sel = racing & big & (mode == m) & np.isfinite(twa)
            n = int(sel.sum())
            right = float(w[sel & (side == want)].sum())
            tot = float(w[sel].sum())
            b["summary"][f"{key}_phase"] = _r(right / tot * 100, 0) if n >= 30 * hz and tot > 0 else None
            b["summary"][f"{key}_phase_s"] = _r(n / hz, 0)
            # per leg as well, for the legs table
        for leg in b["legs"]:
            m = "up" if leg["type"] == "Upwind" else "down" if leg["type"] == "Downwind" else None
            if m is None:
                continue
            sl = slice(int(leg["start"] * hz), int(leg["end"] * hz))
            want = (np.sign(shift) if m == "up" else -np.sign(shift))[sl]
            sel = (big & np.isfinite(twa))[sl]
            n = int(sel.sum())
            ws = w[sl]
            tot = float(ws[sel].sum())
            leg["phase"] = _r(float(ws[sel & (side[sl] == want)].sum()) / tot * 100, 0) if n >= 20 * hz and tot > 0 else None


def _tws_range(est):
    v = np.asarray(est, float)
    v = v[np.isfinite(v)]
    if len(v) < 20:
        return {"tws": None, "tws_lo": None, "tws_hi": None}
    lo, mid, hi = np.percentile(v, [15, 50, 85])
    return {"tws": _r(mid, 1), "tws_lo": _r(lo, 1), "tws_hi": _r(hi, 1)}


# sea state -> heel smoothing: bow pitching (standard deviation, degrees, slow trends removed) and
# the fallback, wind speed (kt), each mapped linearly onto a window in seconds
HEEL_SMOOTH_BY_PITCH = ((0.8, 1.0), (2.3, 4.0))
HEEL_SMOOTH_BY_WIND = ((6.0, 1.0), (16.0, 4.0))


def _heel_window(pitch, mask, hz, tws=None):
    """Seconds to average heel over, and the pitching (deg) it was judged from (None if from wind)."""
    def lerp(x, pts):
        (x0, y0), (x1, y1) = pts
        return float(np.clip(y0 + (x - x0) * (y1 - y0) / (x1 - x0), y0, y1))
    if pitch is not None and np.isfinite(pitch).sum() > 60 * hz:
        hp = pitch - _nan_movmean(pitch, max(1, int(10 * hz)))
        v = hp[mask & np.isfinite(hp)]
        if len(v) > 60 * hz:
            sd = float(np.std(v))
            return round(lerp(sd, HEEL_SMOOTH_BY_PITCH) * 2) / 2, round(sd, 2)
    if tws is not None and np.isfinite(tws).sum() > 30 * hz:
        return round(lerp(float(np.nanmedian(tws)), HEEL_SMOOTH_BY_WIND) * 2) / 2, None
    return 2.0, None


def _heel_bins(heel, vmg, hz=1.0):
    """Upwind VMG by heel to leeward, in 2-degree bins with at least 6 s of steady sailing each."""
    ok = ~np.isnan(heel) & ~np.isnan(vmg)
    heel, vmg = heel[ok], vmg[ok]
    out = []
    for lo in range(-12, 40, 2):
        m = (heel >= lo) & (heel < lo + 2)
        if m.sum() >= 6 * hz:
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
AREA_MARGIN_M = 400.0       # how far past the furthest mark still counts as the racing area


def _busy_area(races, boats) -> dict | None:
    """No marks found (practice laps round a short course): the racing area is where the boats
    spent the time between the first gun and the last race's start, out to most of their sailing."""
    t0, t1 = races[0]["start_ms"], races[-1]["start_ms"] + 5 * 60_000
    pts = [(q["lat"], q["lon"]) for _, log in boats for q in log.track[::4] if t0 <= q["t"] <= t1]
    if len(pts) < 20:
        return None
    lat0, lon0 = float(np.median([p[0] for p in pts])), float(np.median([p[1] for p in pts]))
    mlon = 111_320.0 * math.cos(math.radians(lat0))
    d = [math.hypot((p[1] - lon0) * mlon, (p[0] - lat0) * 111_320.0) for p in pts]
    p90 = float(np.percentile(d, 90))
    return {"lat": lat0, "lon": lon0, "r_m": p90 + max(120.0, p90)}


def racing_area(races: list[dict], boats=None) -> dict | None:
    """The day's racing area: a circle round every mark and start line found, reaching AREA_MARGIN_M
    past the furthest one. A boat that leaves it and doesn't come back is sailing in (or out)."""
    pts = [(m["lat"], m["lon"]) for r in races for m in r.get("marks", []) if m.get("lat") is not None]
    for r in races:
        le = r.get("line_est") or {}
        for k in ("pin", "boat"):
            if isinstance(le.get(k), (list, tuple)) and len(le[k]) == 2:
                pts.append((le[k][0], le[k][1]))
    if len(pts) < 2:
        return _busy_area(races, boats) if boats else None
    lat0 = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
    lon0 = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
    mlon = 111_320.0 * math.cos(math.radians(lat0))
    far = max(math.hypot((p[1] - lon0) * mlon, (p[0] - lat0) * 111_320.0) for p in pts)
    return {"lat": lat0, "lon": lon0, "r_m": far + AREA_MARGIN_M}


def _left_area(series, area, gun_ms, t1) -> int | None:
    """When the last boat left the racing area for good after the gun (plus 30 s), or None if one
    was still in it at t1."""
    mlon = 111_320.0 * math.cos(math.radians(area["lat"]))
    last = None
    for s in series:
        rows = [r for r in s.log.track if gun_ms <= r["t"] <= t1]
        if not rows:
            continue
        t = np.array([r["t"] for r in rows], dtype=np.int64)
        d = np.hypot((np.array([r["lon"] for r in rows]) - area["lon"]) * mlon,
                     (np.array([r["lat"] for r in rows]) - area["lat"]) * 111_320.0)
        inside = np.nonzero(d <= area["r_m"])[0]
        if not len(inside):
            continue                                  # never in it: not this boat's race
        k = inside[-1]
        if k == len(d) - 1:
            return None                               # still racing at the end of the window
        out = int(t[k + 1]) + 30_000
        last = out if last is None else max(last, out)
    return last


def _exit_time(log, area, t_from, t_to) -> int | None:
    """When a boat left the racing area for good between t_from and t_to (None if it stayed in)."""
    mlon = 111_320.0 * math.cos(math.radians(area["lat"]))
    rows = [r for r in log.track if t_from <= r["t"] <= t_to]
    if not rows:
        return None
    inside = [k for k, r in enumerate(rows)
              if math.hypot((r["lon"] - area["lon"]) * mlon, (r["lat"] - area["lat"]) * 111_320.0) <= area["r_m"]]
    if not inside or inside[-1] == len(rows) - 1:
        return None
    return int(rows[inside[-1] + 1]["t"])


def _drop_sail_in_beat(r, logs, area):
    """A last "beat" that is really the sail home: the boats reach off without a single tack and
    leave the racing area. The race then ended at the last leeward mark (a downwind finish)."""
    legs, twd = r.get("legs") or [], r.get("twd")
    lws = [m for m in r.get("marks", []) if m["kind"] == "leeward"]
    if len(legs) < 3 or legs[-1] != "up" or twd is None or not lws:
        return
    last_lw = max(lws, key=lambda m: m.get("t", 0))
    t0 = last_lw.get("t", 0)
    home = 0
    for name in r.get("finish") or {}:
        log = logs.get(name)
        if log is None:
            continue
        out = _exit_time(log, area, t0, t0 + 20 * 60_000)
        if out is None:
            return                                    # someone stayed out there: a real beat
        twa = [wrap180(q["cog"] - twd) for q in log.track if t0 + 20_000 <= q["t"] <= out and q["sog_kt"] > 2]
        up = [a for a in twa if abs(a) < 75]
        if len(up) >= 2 and min(up) < 0 < max(up):
            return                                    # tacked: a real beat
        home += 1
    if not home:
        return
    r["legs"] = legs[:-1]
    r["marks"] = [m for m in r["marks"] if m is not last_lw]
    for name, f in list(r["finish"].items()):
        if f > t0:
            r["finish"][name] = int(t0)
    seq = tuple(1 if x == "up" else -1 for x in r["legs"])
    r["course_letter"] = _course_letter([(x,) for x in seq], None, None, None)[0] or r.get("course_letter", "")
    r["sail_in_trimmed"] = True


def _trim_sail_in(races, boats, area):
    """A finish found after the boat had left the racing area is the sail in, not the finish (the
    run home looks like one more run). Finish it instead where the last run reached the leeward
    mark's depth (the finish line sits by the leeward mark on a downwind finish), or where it left
    the area."""
    if not area:
        return
    logs = dict(boats)
    for r in races:
        _drop_sail_in_beat(r, logs, area)
    for k, r in enumerate(races):
        nxt = races[k + 1]["start_ms"] if k + 1 < len(races) else None
        twd = r.get("twd")
        lw = [m for m in r.get("marks", []) if m["kind"] == "leeward"]
        last_mark = max((m.get("t", 0) for m in r.get("marks", []) if m["kind"] in ("windward", "offset", "leeward")),
                        default=r["start_ms"])
        for name, f in list((r.get("finish") or {}).items()):
            log = logs.get(name)
            if log is None or (nxt and f >= nxt):
                continue
            out = _exit_time(log, area, max(r["start_ms"], last_mark - 120_000), f)
            if out is None:
                continue
            new = out
            if lw and twd is not None and r.get("legs") and r["legs"][-1] == "down":
                ux, uy = math.sin(math.radians(twd)), math.cos(math.radians(twd))
                mlon = 111_320.0 * math.cos(math.radians(area["lat"]))
                depth = lambda la, lo: (lo - area["lon"]) * mlon * ux + (la - area["lat"]) * 111_320.0 * uy
                d_lw = float(np.mean([depth(m["lat"], m["lon"]) for m in lw]))
                hit = next((q["t"] for q in log.track if last_mark <= q["t"] <= out and depth(q["lat"], q["lon"]) <= d_lw), None)
                if hit is not None:
                    new = int(hit)
            if new < f:
                r["finish"][name] = new
                r["sail_in_trimmed"] = True


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
    area = racing_area(races, boats)
    _trim_sail_in(races, boats, area)
    for r, nx in zip(races, races[1:]):
        r["next_start_ms"] = nx["start_ms"]
    for k, r in enumerate(races, 1):
        rr = build_race(boats, race=r, title=f"Race {k}", not_before_ms=(prev_end + 30_000) if prev_end else None,
                        area=area, **kw)
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
            "course_letter": r.get("course_letter", ""), "gate": bool(r.get("gate")), "legs": r.get("legs") or [],
            "offset": any(m["kind"] == "offset" for m in r.get("marks", [])),
            "elapsed_s": s["elapsed_s"], "winner": win["name"], "boats": len(bs),
            "twd": rr["wind"].get("twd_avg") if rr["wind"].get("twd_avg") is not None else rr["meta"]["twd_global"],
            "course": r.get("course") or {},
            "up_vmg": s["up_vmg"], "dn_vmg": s["dn_vmg"], "tacks": s["tacks"], "gybes": s["gybes"],
            "tack_loss_m": s["tack_loss_m"],
            "tws": rr["wind"]["speed"].get("tws"), "tws_lo": rr["wind"]["speed"].get("tws_lo"),
            "tws_hi": rr["wind"]["speed"].get("tws_hi"),
            "tide": ({"state": rr["meta"]["tide"]["state"], "strength": rr["meta"]["tide"]["strength"]}
                     if rr["meta"].get("tide") else None),
            "results": [{"name": b["name"], "pos": b["summary"]["position"], "elapsed_s": b["summary"]["elapsed_s"],
                         "sailed": b["summary"]["elapsed_s"] is not None or (b["summary"]["distance_m"] or 0) >= 300}
                        for b in bs],
        })
    return {"day": {"title": day_title, "races": rows}, "races": out}
