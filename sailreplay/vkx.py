"""
Parser for Vakaros .vkx telemetry logs.

Follows the official VKX spec published by Vakaros (github.com/vakaros/vkx, v1.4).
A VKX file is a flat stream of rows. Each row is a 1-byte key followed by a
fixed-size payload whose length depends only on the key. All integers are
little-endian. Pages start with 0xFF (header) and end with 0xFE (terminator).

No third-party dependencies: returns plain Python lists of dicts so it can be
used anywhere; `sailreplay.analysis` turns them into numpy arrays.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

# key -> payload size in bytes (excluding the key byte)
ROW_SIZES = {
    0xFF: 7,   # page header
    0xFE: 2,   # page terminator
    0x01: 32,  # internal
    0x02: 44,  # position, velocity, orientation
    0x03: 20,  # declination
    0x04: 13,  # race timer event
    0x05: 17,  # line position
    0x06: 18,  # shift angle
    0x07: 12,  # internal
    0x08: 13,  # device configuration
    0x0A: 16,  # wind
    0x0B: 16,  # speed through water
    0x0C: 12,  # depth
    0x0E: 16,  # internal
    0x0F: 16,  # load
    0x10: 12,  # temperature
    0x20: 13,  # internal
    0x21: 52,  # internal
}

TIMER_EVENTS = {0: "reset", 1: "start", 2: "sync", 3: "race_start", 4: "race_end"}
MS_TO_KT = 1.0 / 0.514444


def quat_to_euler(w: float, x: float, y: float, z: float) -> tuple[float, float, float]:
    """Quaternion (body -> true NED) to (heading, pitch, roll) in degrees.

    heading: 0..360 true. pitch: + bow up. roll: + starboard side down.
    """
    yaw = math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z))
    s = max(-1.0, min(1.0, 2 * (w * y - z * x)))
    pitch = math.asin(s)
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    return (math.degrees(yaw) % 360, math.degrees(pitch), math.degrees(roll))


@dataclass
class VkxLog:
    source: str = ""
    version: int | None = None
    log_rate_hz: int | None = None
    fixed_to_body: bool | None = None
    track: list[dict] = field(default_factory=list)       # 0x02
    timer: list[dict] = field(default_factory=list)       # 0x04
    line: list[dict] = field(default_factory=list)        # 0x05
    shifts: list[dict] = field(default_factory=list)      # 0x06
    wind: list[dict] = field(default_factory=list)        # 0x0A
    water: list[dict] = field(default_factory=list)       # 0x0B
    depth: list[dict] = field(default_factory=list)       # 0x0C
    load: list[dict] = field(default_factory=list)        # 0x0F
    temperature: list[dict] = field(default_factory=list) # 0x10
    declination: list[dict] = field(default_factory=list) # 0x03
    unknown_bytes: int = 0
    row_counts: dict = field(default_factory=dict)
    device: str = "atlas"          # "atlas", or "watch" for a GPS watch (.fit): no heel, no compass
    device_name: str = "Atlas"

    @property
    def start_ms(self) -> int | None:
        return self.track[0]["t"] if self.track else None

    @property
    def end_ms(self) -> int | None:
        return self.track[-1]["t"] if self.track else None

    def summary(self) -> str:
        from datetime import datetime, timezone
        if not self.track:
            return f"{self.source}: no position data"
        s = datetime.fromtimestamp(self.start_ms / 1000, tz=timezone.utc)
        dur = (self.end_ms - self.start_ms) / 1000
        return (f"{self.source}: {len(self.track)} fixes, {dur/60:.1f} min from "
                f"{s:%Y-%m-%d %H:%M:%S} UTC, rate {self.log_rate_hz or '?'} Hz, "
                f"{len(self.timer)} timer events, {len(self.line)} line pings")


def parse_bytes(data: bytes, source: str = "") -> VkxLog:
    log = VkxLog(source=source)
    i, n = 0, len(data)
    counts: dict[int, int] = {}
    while i < n:
        key = data[i]
        size = ROW_SIZES.get(key)
        if size is None or i + 1 + size > n:
            # Unknown key or truncated row: resync on the next page header.
            nxt = data.find(b"\xff", i + 1)
            skip = (nxt if nxt != -1 else n) - i
            log.unknown_bytes += skip
            i += skip
            continue
        p = data[i + 1:i + 1 + size]
        counts[key] = counts.get(key, 0) + 1
        try:
            _decode_row(log, key, p)
        except struct.error:
            log.unknown_bytes += 1 + size
        i += 1 + size
    log.row_counts = {f"0x{k:02X}": v for k, v in sorted(counts.items())}
    for lst in (log.track, log.timer, log.line, log.shifts, log.wind, log.water,
                log.depth, log.load, log.temperature):
        lst.sort(key=lambda r: r["t"])
    _dedupe_track(log)
    return log


def _decode_row(log: VkxLog, key: int, p: bytes) -> None:
    if key == 0xFF:
        if log.version is None:
            log.version = p[0]
    elif key == 0x02:
        t, lat, lon, sog, cog, alt, qw, qx, qy, qz = struct.unpack("<Qii7f", p)
        lat, lon = lat * 1e-7, lon * 1e-7
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
            return  # no GPS fix
        hdg, pitch, roll = quat_to_euler(qw, qx, qy, qz)
        log.track.append({
            "t": t, "lat": lat, "lon": lon,
            "sog_kt": sog * MS_TO_KT, "cog": math.degrees(cog) % 360,
            "alt": alt, "heading": hdg, "pitch": pitch, "heel": roll,
        })
    elif key == 0x03:
        t, dec, lat, lon = struct.unpack("<Qfii", p)
        log.declination.append({"t": t, "declination": math.degrees(dec)})
    elif key == 0x04:
        t, ev, val = struct.unpack("<QBi", p)
        log.timer.append({"t": t, "event": TIMER_EVENTS.get(ev, f"type{ev}"), "value_s": val})
    elif key == 0x05:
        t, end, lat, lon = struct.unpack("<QBff", p)
        log.line.append({"t": t, "end": "pin" if end == 0 else "boat", "lat": lat, "lon": lon})
    elif key == 0x06:
        t, tack, mode, hdg, sog = struct.unpack("<QBBff", p)
        log.shifts.append({"t": t, "tack": "starboard" if tack == 0 else "port",
                           "manual": mode, "heading": hdg % 360, "sog_kt": sog})
    elif key == 0x08:
        _, bits, rate = struct.unpack("<QIB", p)
        log.fixed_to_body = bool(bits & 1)
        log.log_rate_hz = rate
    elif key == 0x0A:
        t, d, s = struct.unpack("<Qff", p)
        log.wind.append({"t": t, "awa": d, "aws_kt": s * MS_TO_KT})
    elif key == 0x0B:
        t, fwd, hor = struct.unpack("<Qff", p)
        log.water.append({"t": t, "stw_kt": fwd * MS_TO_KT, "leeway_kt": hor * MS_TO_KT})
    elif key == 0x0C:
        t, d = struct.unpack("<Qf", p)
        log.depth.append({"t": t, "depth_m": d})
    elif key == 0x0F:
        t, name, v = struct.unpack("<Q4sf", p)
        log.load.append({"t": t, "sensor": name.rstrip(b"\x00").decode("ascii", "replace"), "load": v})
    elif key == 0x10:
        t, c = struct.unpack("<Qf", p)
        log.temperature.append({"t": t, "temp_c": c})


def _dedupe_track(log: VkxLog) -> None:
    out, last = [], None
    for r in log.track:
        if r["t"] != last:
            out.append(r)
            last = r["t"]
    log.track = out


# Files the site reads: Atlas logs, and GPS-watch activities (.fit, or the .zip Garmin exports)
LOG_SUFFIXES = (".vkx", ".fit", ".zip")


def parse_file(path: str | Path) -> VkxLog:
    """Read an Atlas .vkx, or a GPS watch's .fit (or Garmin's .zip export) into the same shape."""
    path = Path(path)
    if path.suffix.lower() in (".fit", ".zip"):
        from .fit import parse_fit
        return parse_fit(path)
    return parse_bytes(path.read_bytes(), source=path.name)


def write_csvs(log: VkxLog, out_dir: str | Path, stem: str | None = None) -> list[Path]:
    """Write one CSV per data type that has rows. Returns paths written."""
    import csv
    from datetime import datetime, timezone
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or Path(log.source).stem or "session"
    written = []
    for name in ("track", "timer", "line", "shifts", "wind", "water", "depth", "load", "temperature"):
        rows = getattr(log, name)
        if not rows:
            continue
        p = out_dir / f"{stem}_{name}.csv"
        cols = ["time_utc"] + list(rows[0].keys())
        with p.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                ts = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")
                w.writerow([ts] + [round(v, 7) if isinstance(v, float) else v for v in r.values()])
        written.append(p)
    return written


def sailing_minutes(log: VkxLog, min_kt: float = 2.5) -> float:
    """Minutes spent moving faster than min_kt (gaps over 5 s are not counted)."""
    t = log.track
    total = 0.0
    for a, b in zip(t, t[1:]):
        dt = (b["t"] - a["t"]) / 1000
        if dt <= 5 and b["sog_kt"] >= min_kt:
            total += dt
    return total / 60


def sailing_window(log: VkxLog, min_kt: float = 2.5, margin_s: float = 900) -> tuple[int, int] | None:
    """(start_ms, end_ms) from the first to the last time the boat was sailing, plus a margin.
    Single GPS spikes are ignored: it takes 10 s above min_kt within half a minute to count."""
    t = log.track
    hits = [r["t"] for r in t if r["sog_kt"] >= min_kt]
    if len(hits) < 20:
        return None
    def steady(i):
        return sum(1 for h in hits[i:i + 40] if h - hits[i] <= 30_000) >= 20
    first = next((hits[i] for i in range(len(hits)) if steady(i)), None)
    last = next((hits[i] for i in range(len(hits) - 1, -1, -1) if steady(max(0, i - 39))), None)
    if first is None or last is None:
        return None
    return int(first - margin_s * 1000), int(last + margin_s * 1000)


def crop_bytes(data: bytes, t0_ms: int, t1_ms: int) -> bytes:
    """Keep only rows between two times (plus the device configuration row), re-paged."""
    from .vkx_write import VkxWriter
    w = VkxWriter()
    i, n = 0, len(data)
    while i < n:
        key = data[i]
        size = ROW_SIZES.get(key)
        if size is None or i + 1 + size > n:
            nxt = data.find(b"\xff", i + 1)
            i = nxt if nxt != -1 else n
            continue
        p = data[i + 1:i + 1 + size]
        if key not in (0xFF, 0xFE):
            t = struct.unpack_from("<Q", p, 0)[0]
            if key == 0x08 or t0_ms <= t <= t1_ms:
                w._row(key, p)
        i += 1 + size
    return w.bytes()
