"""
Local knowledge for the sailing area: a faint coastline for the map and the tide.

Tides come from NOAA CO-OPS high/low predictions for station 8515586 (Northport, Northport Bay).
A season of predictions ships with the package (tides_8515586.json). For a date it does not
cover, the build asks NOAA directly, which works anywhere with internet (GitHub Actions does);
if that fails too, the race simply has no tide note.

The coastline is OpenStreetMap natural=coastline around Northport Harbor and Bay, simplified
to about 4 m, and is only attached to races sailed inside its box.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).parent
STATION = "8515586"
STATION_NAME = "Northport, Northport Bay"
NOAA_URL = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=predictions"
            "&application=sailreplay&begin_date={b}&end_date={e}&datum=MLLW&station={s}"
            "&time_zone=gmt&units=english&interval=hilo&format=json")


def _ms(iso: str) -> int:
    return int(datetime.strptime(iso.rstrip("Z"), "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


@lru_cache(maxsize=1)
def _cached_events() -> list:
    p = HERE / f"tides_{STATION}.json"
    if not p.exists():
        return []
    return sorted((_ms(t), typ, v) for t, typ, v in json.loads(p.read_text())["events"])


_fetched: dict = {}


def _fetch(day: datetime, log=None) -> list:
    key = day.strftime("%Y%m%d")
    if key in _fetched:
        return _fetched[key]
    from urllib.request import Request, urlopen
    b, e = day - timedelta(days=1), day + timedelta(days=1)
    url = NOAA_URL.format(b=b.strftime("%Y%m%d"), e=e.strftime("%Y%m%d"), s=STATION)
    out = []
    try:
        with urlopen(Request(url, headers={"User-Agent": "sailreplay"}), timeout=15) as r:
            for p in json.load(r).get("predictions", []):
                out.append((_ms(p["t"].replace(" ", "T")), p["type"], round(float(p["v"]), 2)))
    except Exception as ex:  # no network, NOAA down: no tide note
        if log:
            log(f"  tide: could not reach NOAA ({ex.__class__.__name__}); no tide note for {key}")
    _fetched[key] = sorted(out)
    return _fetched[key]


def tide_events(t0_ms: int, t1_ms: int, log=None) -> list:
    """High and low waters bracketing [t0, t1]: one before, all inside, one after."""
    ev = _cached_events()
    def window(ev):
        before = [x for x in ev if x[0] <= t0_ms]
        after = [x for x in ev if x[0] >= t1_ms]
        inside = [x for x in ev if t0_ms < x[0] < t1_ms]
        if not before or not after:
            return None
        return [before[-1]] + inside + [after[0]]
    w = window(ev)
    if w is None:
        w = window(_fetch(datetime.fromtimestamp(t0_ms / 1000, tz=timezone.utc), log))
    return [{"t_ms": t, "type": typ, "ft": v} for t, typ, v in (w or [])]


def tide_summary(t0_ms: int, t1_ms: int, log=None) -> dict | None:
    """Tide during a race, for the race model: the bracketing events and the state at mid-race."""
    ev = tide_events(t0_ms, t1_ms, log)
    if len(ev) < 2:
        return None
    mid = (t0_ms + t1_ms) / 2
    prev = [e for e in ev if e["t_ms"] <= mid][-1]
    nxt = [e for e in ev if e["t_ms"] > mid][0]
    frac = (mid - prev["t_ms"]) / max(1, nxt["t_ms"] - prev["t_ms"])
    return {
        "station": STATION, "station_name": STATION_NAME,
        "events": ev,
        "state": "ebb" if prev["type"] == "H" else "flood",
        "strength": _strength(frac),
    }


def _strength(frac: float) -> str:
    """Rule of twelfths: slack near the turn, strongest in the middle third."""
    if frac < 1 / 6 or frac > 5 / 6:
        return "near slack"
    if 1 / 3 <= frac <= 2 / 3:
        return "strongest"
    return "building" if frac < 0.5 else "easing"


@lru_cache(maxsize=1)
def _coast() -> dict:
    p = HERE / "coast_northport.json"
    return json.loads(p.read_text()) if p.exists() else {"lines": [], "bbox": [0, 0, 0, 0]}


def coastline_near(lat: float, lon: float, radius_m: float = 4000) -> dict | None:
    """Coastline polylines within radius of a point, or None if the race is outside the bundled area."""
    c = _coast()
    s, w, n, e = c["bbox"]
    if not (s <= lat <= n and w <= lon <= e):
        return None
    mlon = 111_320 * math.cos(math.radians(lat))
    lines, cur = [], []
    for ln in c["lines"]:
        for la, lo in ln:
            if math.hypot((lo - lon) * mlon, (la - lat) * 111_320) <= radius_m:
                cur.append([la, lo])
            elif cur:
                # keep the first point outside so the line runs off the edge instead of stopping short
                cur.append([la, lo]); lines.append(cur); cur = []
        if cur:
            lines.append(cur); cur = []
    lines = [l for l in lines if len(l) >= 2]
    return {"lines": lines, "credit": "© OpenStreetMap contributors"} if lines else None
