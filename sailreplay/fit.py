"""
Garmin .fit activities (a watch's "Sail Race" activity) as logs the rest of sailreplay can use.

A watch records a position only every 1 to 13 seconds ("smart recording", about every 4 s
on a Fenix 7) and has no compass for the boat's heading and no heel or pitch. So:

  - positions are joined up in straight lines to the 2 Hz grid the Atlas uses, but only across
    gaps up to MAX_GAP_S; longer gaps stay empty
  - speed is the watch's own speed, joined up the same way; course over ground comes from the
    positions
  - heading, heel and pitch are left empty (NaN): those charts simply don't show the boat
  - the Sail Race countdown is a lap of its own, so the gun is where the next lap starts;
    it becomes a race_start timer event, like pressing start on the Atlas

Heart rate, when the watch has it, is kept on each row as "hr".
"""
from __future__ import annotations

import math
from pathlib import Path

from .vkx import VkxLog

HZ = 2
MAX_GAP_S = 15.0
SESSION_GAP_S = 30 * 60      # records after a gap this long (watch left running overnight) are dropped
COUNTDOWN_S = (60, 600)      # a first lap this long, in a sail race, is the start countdown


def parse_file(path: str | Path) -> VkxLog:
    import fitdecode
    recs, laps, sport = [], [], {}
    with fitdecode.FitReader(str(path)) as fr:
        for m in fr:
            if not isinstance(m, fitdecode.FitDataMessage):
                continue
            if m.name == "record":
                d = {f.name: f.value for f in m.fields}
                if d.get("timestamp") is None or d.get("position_lat") is None or d.get("position_long") is None:
                    continue
                recs.append(d)
            elif m.name == "lap":
                d = {f.name: f.value for f in m.fields}
                if d.get("start_time") is not None:
                    laps.append(d)
            elif m.name == "sport":
                sport = {f.name: f.value for f in m.fields}
    log = VkxLog(source=Path(path).name, log_rate_hz=HZ)
    log.device = "watch"
    if not recs:
        return log
    recs.sort(key=lambda d: d["timestamp"])
    # keep the first session only
    keep = [recs[0]]
    for d in recs[1:]:
        if (d["timestamp"] - keep[-1]["timestamp"]).total_seconds() > SESSION_GAP_S:
            break
        keep.append(d)
    deg = 180.0 / 2 ** 31
    pts = [(d["timestamp"].timestamp(), d["position_lat"] * deg, d["position_long"] * deg,
            d.get("enhanced_speed", d.get("speed")), d.get("heart_rate")) for d in keep]

    t0, t1 = pts[0][0], pts[-1][0]
    n = int((t1 - t0) * HZ) + 1
    j = 0
    rows = []
    nan = float("nan")
    for k in range(n):
        t = t0 + k / HZ
        while j + 1 < len(pts) - 1 and pts[j + 1][0] <= t:
            j += 1
        a, b = pts[j], pts[min(j + 1, len(pts) - 1)]
        if b[0] - a[0] > MAX_GAP_S or t > b[0]:
            continue
        f = 0.0 if b[0] == a[0] else (t - a[0]) / (b[0] - a[0])
        lat, lon = a[1] + f * (b[1] - a[1]), a[2] + f * (b[2] - a[2])
        sa, sb = a[3], b[3]
        sog = (sa + f * (sb - sa)) if sa is not None and sb is not None else (sa if sa is not None else sb)
        ml = 111_320 * math.cos(math.radians(lat))
        dx, dy = (b[2] - a[2]) * ml, (b[1] - a[1]) * 111_320
        cog = math.degrees(math.atan2(dx, dy)) % 360 if math.hypot(dx, dy) > 0.5 else nan
        hr = a[4] if f < 0.5 else b[4]
        rows.append({"t": int(round(t * 1000)), "lat": lat, "lon": lon,
                     "sog_kt": (sog / 0.514444) if sog is not None else nan, "cog": cog,
                     "alt": nan, "heading": nan, "pitch": nan, "heel": nan, "hr": hr})
    log.track = rows

    # the gun: a sail race starts with a countdown lap; the next lap starts at the gun
    laps.sort(key=lambda d: d["start_time"])
    is_race = (sport.get("sub_sport") == "sail_race") or str(sport.get("name", "")).lower().startswith("sail race")
    if is_race and len(laps) >= 2:
        first = laps[0].get("total_elapsed_time") or 0
        if COUNTDOWN_S[0] <= first <= COUNTDOWN_S[1]:
            g = int(round(laps[1]["start_time"].timestamp() * 1000))
            if rows and rows[0]["t"] <= g <= rows[-1]["t"]:
                log.timer = [{"t": g - int(first * 1000), "event": "start", "value_s": int(round(first))},
                             {"t": g, "event": "race_start"}]
    log.row_counts = {"records": len(keep)}
    return log
