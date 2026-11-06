"""
Simulate a 4-boat Melges 15 windward-leeward race and write one .vkx per boat.

Used to test the toolkit end to end before real logs are available.
No wind sensor data is written, matching a base Atlas 2 setup.

    python tools/make_demo_race.py demo_data/
"""
from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sailreplay.vkx_write import VkxWriter  # noqa: E402

LAT0, LON0 = 40.8420, -73.7660          # Long Island Sound, off City Island
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))
GUN_MS = 1_793_548_800_000               # 2026-11-01 16:00:00 UTC (11am in New York)
DT = 0.25                                # 4 Hz
KT = 0.514444
TWD0, TWS = 225.0, 12.0


def wrap(a):
    return (a + 180) % 360 - 180


def vec(bearing):
    r = math.radians(bearing)
    return math.sin(r), math.cos(r)


def to_ll(x, y):
    return LAT0 + y / M_PER_DEG_LAT, LON0 + x / M_PER_DEG_LON


def twd_at(t):
    """Oscillating breeze: +/-7 deg over ~7 minutes plus a slow right veer."""
    return TWD0 + 7 * math.sin(2 * math.pi * t / 420) + 0.004 * max(0, t)


def gust_at(t, seed):
    return 1 + 0.08 * math.sin(2 * math.pi * t / 95 + seed) + 0.04 * math.sin(2 * math.pi * t / 23 + 2 * seed)


UX, UY = vec(TWD0)
LX, LY = vec(TWD0 - 90)                  # looking upwind, left = pin end
PIN = (60 * LX, 60 * LY)
RC = (-60 * LX, -60 * LY)
WMARK = (900 * UX, 900 * UY)
LMARK = (-180 * UX, -180 * UY)
FINISH = (0.0, 0.0)
WING = (450 * UX + 380 * LX, 450 * UY + 380 * LY)
FINISH_UP = (RC[0] - 35 * LX + 4 * UX, RC[1] - 35 * LY + 4 * UY)   # finish mark to starboard of the committee boat
# CYC Frostbite SI 7. W = beat, L = run, R = reach, F = finish downwind, U = finish upwind
COURSES = {
    "Z": [("W", WMARK), ("L", LMARK), ("W", WMARK), ("F", FINISH)],
    "W": [("W", WMARK), ("L", LMARK), ("U", FINISH_UP)],
    "L": [("W", WMARK), ("F", FINISH)],
    "T": [("W", WMARK), ("R", WING), ("R", LMARK), ("U", FINISH_UP)],
    "W2": [("W", WMARK), ("L", LMARK), ("W", WMARK), ("L", LMARK), ("U", FINISH_UP)],
}
COURSE = COURSES["Z"]

BOATS = [
    # name,        speed, tack-angle, recovery-tau, shift-sense, line-frac, start-lead
    ("Mojo",        1.02, 41.0,  5.0, 0.90, 0.25, 31.0),
    ("Wet Leopard", 1.00, 42.0,  6.5, 0.70, 0.55, 34.0),
    ("Sea Biscuit", 0.985, 43.5, 8.0, 0.45, 0.80, 40.0),
    ("Knot Fast",   0.995, 42.5, 7.0, 0.55, 0.10, 27.5),
]


class Boat:
    def __init__(self, name, k, alpha, tau, sense, frac, lead, seed, course=None):
        self.course = course or COURSE
        self.name, self.k, self.alpha, self.tau, self.sense = name, k, alpha, tau, sense
        self.frac, self.lead = frac, lead
        self.rng = random.Random(seed)
        self.seed = seed
        # holding spot 45 m below the line at its chosen fraction from the pin
        px = PIN[0] + (RC[0] - PIN[0]) * frac - 45 * UX
        py = PIN[1] + (RC[1] - PIN[1]) * frac - 45 * UY
        self.hold = (px, py)
        self.x, self.y = px + self.rng.uniform(-30, 30), py + self.rng.uniform(-30, 30)
        self.cog = TWD0 + 90
        self.speed = 2.0 * KT
        self.leg = -1                    # -1 pre-start
        self.side = 1                    # +1 starboard, -1 port (wind side)
        self.last_turn = -999.0
        self.finished_at = None
        self.roll_kick = 0.0

    # ---- decisions -------------------------------------------------------
    def target(self, t):
        twd = twd_at(t)
        if self.finished_at is not None:
            return twd - 100, 2.5
        if self.leg == -1:
            if t < -self.lead:
                # reach back and forth around the holding spot
                dx, dy = self.hold[0] - self.x, self.hold[1] - self.y
                along = dx * LX + dy * LY
                if abs(along) > 35 and self.rng.random() < 0.05:
                    self.side = 1 if along > 0 else -1
                depth_err = self._signed_dist_to_line() + 45      # + = too close to the line
                return twd - self.side * (90 + max(-40, min(40, depth_err * 1.5))), 3.0
            self.side = 1
            if self._crossed_line_upwind() and t >= 0:
                self.leg = 0
            tx = PIN[0] + (RC[0] - PIN[0]) * self.frac + 5 * UX - self.x
            ty = PIN[1] + (RC[1] - PIN[1]) * self.frac + 5 * UY - self.y
            b = math.degrees(math.atan2(tx, ty)) % 360
            if abs(wrap(b - twd)) >= self.alpha:
                return b, 5.0
            return twd - self.alpha, 5.0
        kind, mark = self.course[self.leg]
        dx, dy = mark[0] - self.x, mark[1] - self.y
        done = (kind == "F" and self._crossed_line_downwind()) or (kind == "U" and self._signed_dist_to_line() > 0)
        if done or (kind not in "FU" and math.hypot(dx, dy) < 18):
            self.leg += 1
            if self.leg >= len(self.course):
                self.finished_at = t
                return twd - 100, 2.5
            kind, mark = self.course[self.leg]
            dx, dy = mark[0] - self.x, mark[1] - self.y
            self._turn_to(1 if kind in "WU" else self.side, t, force=True)
        b = math.degrees(math.atan2(dx, dy)) % 360
        mean = TWD0 + 0.004 * max(0, t)
        dist = math.hypot(dx, dy)
        if kind == "R":                                        # reach: straight at the mark
            self._turn_to(1 if wrap(twd - b) > 0 else -1, t)
            return b, 7.0
        if kind in "WU":
            rel = wrap(b - twd)
            if abs(rel) >= self.alpha and dist < 250:          # fetching the mark: steer straight at it
                self._turn_to(1 if rel < 0 else -1, t)
                return b, 5.1
            if self.side > 0 and rel >= self.alpha - 2:
                self._turn_to(-1, t)
            elif self.side < 0 and rel <= -(self.alpha - 2):
                self._turn_to(1, t)
            elif t - self.last_turn > 70:
                headed = (self.side > 0 and twd < mean - 3) or (self.side < 0 and twd > mean + 3)
                if headed and self.rng.random() < self.sense * 0.03:
                    self._turn_to(-self.side, t)
            return twd - self.side * self.alpha, 5.1
        d = twd + 180
        rel = wrap(b - d)
        if dist < 120 and abs(wrap(b - twd)) > 100:
            self._turn_to(1 if wrap(twd - b) > 0 else -1, t)
            return b, 8.6
        if self.side > 0 and rel <= -28:
            self._turn_to(-1, t)
        elif self.side < 0 and rel >= 28:
            self._turn_to(1, t)
        return twd - self.side * 150, 8.6

    def _turn_to(self, side, t, force=False):
        if side != self.side and (force or t - self.last_turn > 15):
            self.side = side
            self.last_turn = t
            self.roll_kick = 1.0

    def _signed_dist_to_line(self):
        return (self.x - PIN[0]) * UX + (self.y - PIN[1]) * UY   # + = course side

    def _crossed_line_upwind(self):
        return self._signed_dist_to_line() > 0

    def _crossed_line_downwind(self):
        return self._signed_dist_to_line() < 0 and self.leg == len(self.course) - 1

    # ---- physics ---------------------------------------------------------
    def step(self, t):
        cog_t, spd_kt = self.target(t)
        err = wrap(cog_t - self.cog)
        rate = 20.0 if abs(err) > 30 else 6.0
        self.cog = (self.cog + max(-rate * DT, min(rate * DT, err)) + self.rng.gauss(0, 0.25)) % 360
        g = gust_at(t, self.seed)
        twa = abs(wrap(twd_at(t) - self.cog))
        if twa < 30:
            want = 1.2                                    # head to wind, stalling
        elif twa < 60:
            want = spd_kt * self.k * g * (0.75 + 0.25 * (twa - 30) / 30) if spd_kt < 6 else 5.5
        elif twa < 120:
            want = 7.0 * self.k * g
        else:
            want = spd_kt * self.k * g
        if self.leg == -1 and t < -self.lead:
            want = 3.0
        if self.finished_at is not None:
            want = 2.5
        want *= KT
        tau = self.tau if want > self.speed else 2.5
        self.speed += (want - self.speed) * DT / tau
        vx, vy = vec(self.cog)
        self.x += vx * self.speed * DT
        self.y += vy * self.speed * DT
        self.roll_kick = max(0.0, self.roll_kick - DT / 4)
        upwind = twa < 70
        base_heel = (6 + 5 * (g - 1) / 0.12) if upwind else 3.0
        lee = -self.side                                   # stbd tack heels to port (negative roll)
        heel = lee * base_heel - lee * 14 * math.sin(math.pi * self.roll_kick) + self.rng.gauss(0, 1.2)
        pitch = (2.0 if upwind else -1.0) + self.rng.gauss(0, 0.8)
        hdg = (self.cog + (-self.side * 3 if upwind else 0) + self.rng.gauss(0, 0.8)) % 360
        return heel, pitch, hdg


def simulate(out_dir: Path, timers: bool = True, course: str = "Z"):
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, spec in enumerate(BOATS):
        b = Boat(*spec, seed=100 + i, course=COURSES[course])
        w = VkxWriter()
        w.config(4)
        w.internal(0x01, 32)
        t = -480.0
        if i == 0:   # this boat pinged the line and set tack references
            plat, plon = to_ll(*PIN)
            rlat, rlon = to_ll(*RC)
            w.line(GUN_MS - 400_000, 0, plat, plon)
            w.line(GUN_MS - 390_000, 1, rlat, rlon)
            w.shift(GUN_MS - 240_000, 0, (TWD0 - 41) % 360, 5.1)
            w.shift(GUN_MS - 230_000, 1, (TWD0 + 41) % 360, 5.0)
        if timers:
            w.timer(GUN_MS - 300_000, 1, 300)
            w.timer(GUN_MS, 3, 0)
        noise = random.Random(900 + i)
        ended = None
        while True:
            heel, pitch, hdg = b.step(t)
            lat, lon = to_ll(b.x + noise.gauss(0, 0.3), b.y + noise.gauss(0, 0.3))
            w.pvo(GUN_MS + t * 1000, lat, lon, b.speed + noise.gauss(0, 0.03), b.cog, hdg, pitch, heel)
            if int(t * 4) % 40 == 0:
                w.internal(0x07, 12)
            if b.finished_at is not None and ended is None:
                ended = t
                if timers:
                    w.timer(GUN_MS + t * 1000, 4, int(t))
            if ended is not None and t > ended + 120:
                break
            if t > 3600:
                break
            t += DT
        path = out_dir / f"{b.name.lower().replace(' ', '_')}.vkx"
        path.write_bytes(w.bytes())
        fin = f"finished {b.finished_at/60:.1f} min" if b.finished_at else "DNF"
        print(f"{b.name:12s} -> {path.name} ({path.stat().st_size/1024:.0f} kB, {fin})")


if __name__ == "__main__":
    simulate(Path(sys.argv[1] if len(sys.argv) > 1 else "demo_data"))
