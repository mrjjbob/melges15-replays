"""
Reader for Garmin .fit activity files (GPS watches), turned into the same VkxLog the Atlas gives.

A watch has GPS but no compass or tilt sensor that knows which way the boat points, so a watch
log has position and speed only: heading is taken from the course over the ground, and heel and
pitch are left empty (NaN), which keeps watch boats out of the heel charts.

No third-party dependencies. Only the messages that matter are decoded:
  record (20)  timestamp, position, speed
  lap (19)     a "Sail Race" activity splits into a countdown lap and a race lap; where the
               second lap starts is the watch's gun
  file_id (0)  maker and product, for the log's description

FIT format: a 12/14-byte header, then records. A record header byte either introduces a
definition message (which fields, sizes and byte order a local message type has from now on) or
a data message laid out by the last definition for its local type. "Compressed timestamp"
headers carry a 5-bit time offset instead of a timestamp field.
"""
from __future__ import annotations

import io
import math
import struct
import zipfile
from pathlib import Path

from .vkx import MS_TO_KT, VkxLog

FIT_EPOCH_S = 631065600          # 1989-12-31 00:00:00 UTC in Unix seconds
SEMICIRCLE = 180.0 / 2 ** 31

# Garmin watches put their GPS fixes (position and speed alike) about 1.75 s behind the time
# stamped on them: their speed lags the Atlas's by as much as their position does, so it is
# the watch's filtering, not where the sailor sits. Measured on a quatix 7 against a
# mast-mounted Atlas 2, five races, 8 Nov 2026: 1.5-2.25 s.
WATCH_LAG_MS = 1750

# base type number (low 5 bits) -> (struct code, size, invalid value)
_BASE = {
    0x00: ("B", 1, 0xFF), 0x01: ("b", 1, 0x7F), 0x02: ("B", 1, 0xFF),
    0x03: ("h", 2, 0x7FFF), 0x04: ("H", 2, 0xFFFF), 0x05: ("i", 4, 0x7FFFFFFF),
    0x06: ("I", 4, 0xFFFFFFFF), 0x08: ("f", 4, None), 0x09: ("d", 8, None),
    0x0A: ("B", 1, 0x00), 0x0B: ("H", 2, 0x0000), 0x0C: ("I", 4, 0x00000000),
    0x0D: ("B", 1, 0xFF), 0x0E: ("q", 8, 0x7FFFFFFFFFFFFFFF),
    0x0F: ("Q", 8, 0xFFFFFFFFFFFFFFFF), 0x10: ("Q", 8, 0x0000000000000000),
}

# global message -> {field number: name}
_WANT = {
    0: {1: "manufacturer", 2: "product"},
    19: {253: "timestamp", 2: "start_time", 7: "total_elapsed_time", 24: "lap_trigger"},
    20: {253: "timestamp", 0: "lat", 1: "lon", 6: "speed", 73: "enhanced_speed"},
}
_MAKERS = {1: "Garmin", 23: "Suunto", 294: "Coros", 32: "Wahoo", 255: "development"}


class FitError(ValueError):
    pass


def read_fit_messages(data: bytes) -> dict[int, list[dict]]:
    """Decode the wanted messages: {global message number: [ {field name: value}, ... ]}."""
    if len(data) < 12:
        raise FitError("file too short to be a .fit file")
    hsize = data[0]
    if data[8:12] != b".FIT":
        raise FitError("not a .fit file")
    data_size = struct.unpack_from("<I", data, 4)[0]
    end = min(len(data), hsize + data_size)
    i = hsize
    defs: dict[int, dict] = {}
    out: dict[int, list[dict]] = {k: [] for k in _WANT}
    last_ts = None
    while i < end:
        h = data[i]
        i += 1
        if h & 0x80:                                   # compressed timestamp data message
            local = (h >> 5) & 0x03
            off = h & 0x1F
            if last_ts is not None:
                ts = (last_ts & ~0x1F) + off
                if off < (last_ts & 0x1F):
                    ts += 0x20
                last_ts = ts
            d = defs.get(local)
            if d is None:
                raise FitError("data before its definition")
            row = _read_data(data, i, d)
            i += d["size"]
            if d["global"] in out:
                if last_ts is not None:
                    row.setdefault("timestamp", last_ts)
                out[d["global"]].append(row)
            continue
        local = h & 0x0F
        if h & 0x40:                                   # definition message
            dev = bool(h & 0x20)
            arch = data[i + 1]
            endian = ">" if arch == 1 else "<"
            glob = struct.unpack_from(endian + "H", data, i + 2)[0]
            nf = data[i + 4]
            i += 5
            fields = []
            for _ in range(nf):
                num, size, base = data[i], data[i + 1], data[i + 2]
                fields.append((num, size, base & 0x1F))
                i += 3
            size = sum(f[1] for f in fields)
            if dev:
                nd = data[i]
                i += 1
                for _ in range(nd):
                    size += data[i + 1]
                    i += 3
            defs[local] = {"global": glob, "endian": endian, "fields": fields, "size": size}
        else:                                          # normal data message
            d = defs.get(local)
            if d is None:
                raise FitError("data before its definition")
            row = _read_data(data, i, d)
            i += d["size"]
            if "timestamp" in row:
                last_ts = row["timestamp"]
            if d["global"] in out:
                out[d["global"]].append(row)
    return out


def _read_data(data: bytes, i: int, d: dict) -> dict:
    want = _WANT.get(d["global"])
    row: dict = {}
    for num, size, base in d["fields"]:
        if want is not None and (num in want or num == 253):
            code, bsize, invalid = _BASE.get(base, ("B", 1, None))
            if size == bsize:
                v = struct.unpack_from(d["endian"] + code, data, i)[0]
                if invalid is None or v != invalid:
                    if not (code in "fd" and isinstance(v, float) and math.isnan(v)):
                        row[(want or {}).get(num, "timestamp" if num == 253 else str(num))] = v
        i += size
    # the timestamp is needed for compressed headers even in messages we otherwise skip
    if want is None:
        j = i - d["size"]
        for num, size, base in d["fields"]:
            if num == 253 and size == 4:
                row["timestamp"] = struct.unpack_from(d["endian"] + "I", data, j)[0]
            j += size
    return row


def _fit_bytes(path: Path) -> bytes:
    """A .fit file, or the .zip Garmin Connect's "Export File" gives (first .fit inside)."""
    data = path.read_bytes()
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".fit")]
            if not names:
                raise FitError("the .zip has no .fit file in it")
            return z.read(sorted(names)[0])
    return data


def parse_fit_bytes(data: bytes, source: str = "", lag_ms: int = WATCH_LAG_MS) -> VkxLog:
    msgs = read_fit_messages(data)
    log = VkxLog(source=source, log_rate_hz=1, fixed_to_body=False)
    log.device = "watch"
    fid = msgs[0][0] if msgs[0] else {}
    log.device_name = _MAKERS.get(fid.get("manufacturer"), "GPS") + " watch"

    pts = []
    for r in msgs[20]:
        if "lat" not in r or "lon" not in r or "timestamp" not in r:
            continue
        lat, lon = r["lat"] * SEMICIRCLE, r["lon"] * SEMICIRCLE
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            continue
        sp = r.get("enhanced_speed", r.get("speed"))
        sp = sp / 1000.0 if sp is not None else float("nan")
        t = (r["timestamp"] + FIT_EPOCH_S) * 1000 - lag_ms
        pts.append((t, lat, lon, sp))
    pts.sort()
    # drop repeated timestamps (some watches write a fix twice around a lap)
    dedup = []
    for p in pts:
        if dedup and p[0] == dedup[-1][0]:
            continue
        dedup.append(p)
    pts = dedup

    def bearing(a, b):
        dy = b[1] - a[1]
        dx = (b[2] - a[2]) * math.cos(math.radians(a[1]))
        return math.degrees(math.atan2(dx, dy)) % 360 if (dx or dy) else None

    nan = float("nan")
    last_cog = 0.0
    for k, p in enumerate(pts):
        a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
        c = bearing(a, b)
        if c is None:
            c = last_cog
        last_cog = c
        sp = p[3] if not math.isnan(p[3]) else 0.0
        if math.isnan(p[3]) and 0 < k < len(pts) - 1:     # no speed field: from the positions
            dt = (b[0] - a[0]) / 1000
            if dt > 0:
                dy = (b[1] - a[1]) * 110540
                dx = (b[2] - a[2]) * 111320 * math.cos(math.radians(p[1]))
                sp = math.hypot(dx, dy) / dt
        log.track.append({"t": int(p[0]), "lat": p[1], "lon": p[2], "sog_kt": sp * MS_TO_KT, "cog": c,
                          "alt": 0.0, "heading": c, "pitch": nan, "heel": nan})

    # A "Sail Race" activity: the countdown is the first lap, and the race lap starts at the gun.
    laps = sorted((l for l in msgs[19] if "start_time" in l), key=lambda l: l["start_time"])
    if len(laps) >= 2 and "total_elapsed_time" in laps[0]:
        dur = laps[0]["total_elapsed_time"] / 1000.0
        gap = laps[1]["start_time"] - laps[0]["start_time"]
        if 30 <= dur <= 600 and abs(gap - dur) <= 2 and abs(dur - round(dur / 30) * 30) <= 1.5:
            t0 = (laps[0]["start_time"] + FIT_EPOCH_S) * 1000
            gun = (laps[1]["start_time"] + FIT_EPOCH_S) * 1000
            log.timer.append({"t": t0, "event": "start", "value_s": int(round(dur))})
            log.timer.append({"t": gun, "event": "race_start", "value_s": 0})
    log.row_counts = {"record": len(msgs[20]), "lap": len(msgs[19])}
    return log


def parse_fit(path: str | Path) -> VkxLog:
    p = Path(path)
    return parse_fit_bytes(_fit_bytes(p), source=p.name)
