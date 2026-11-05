"""Minimal VKX writer (used for tests and the demo race). Mirrors the spec in vkx.py."""
from __future__ import annotations

import math
import struct


def euler_to_quat(heading: float, pitch: float, roll: float) -> tuple[float, float, float, float]:
    """(heading, pitch, roll) in degrees -> quaternion (w, x, y, z), body -> true NED, ZYX order."""
    y, p, r = (math.radians(a) / 2 for a in (heading, pitch, roll))
    cy, sy, cp, sp, cr, sr = math.cos(y), math.sin(y), math.cos(p), math.sin(p), math.cos(r), math.sin(r)
    return (cr * cp * cy + sr * sp * sy,
            sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy,
            cr * cp * sy - sr * sp * cy)


class VkxWriter:
    PAGE = 2048

    def __init__(self, version: int = 5):
        self.version = version
        self.out = bytearray()
        self.page = bytearray()
        self.chunk = 0
        self._new_page()

    def _new_page(self):
        self.page = bytearray([0xFF, self.version, 0, 0]) + struct.pack("<I", self.chunk)
        self.chunk += 1

    def _row(self, key: int, payload: bytes):
        if len(self.page) + 1 + len(payload) + 3 > self.PAGE:
            self._close_page()
            self._new_page()
        self.page += bytes([key]) + payload

    def _close_page(self):
        n = len(self.page) + 3
        self.out += self.page + bytes([0xFE]) + struct.pack("<H", n)

    def config(self, rate_hz: int, fixed_to_body: bool = True):
        self._row(0x08, struct.pack("<QIB", 0, 1 if fixed_to_body else 0, rate_hz))

    def pvo(self, t_ms, lat, lon, sog_ms, cog_deg, heading, pitch, roll, alt=0.0):
        q = euler_to_quat(heading, pitch, roll)
        self._row(0x02, struct.pack("<Qii7f", int(t_ms), round(lat * 1e7), round(lon * 1e7),
                                    sog_ms, math.radians(cog_deg), alt, *q))

    def timer(self, t_ms, event: int, value_s: int):
        self._row(0x04, struct.pack("<QBi", int(t_ms), event, value_s))

    def line(self, t_ms, end: int, lat, lon):
        self._row(0x05, struct.pack("<QBff", int(t_ms), end, lat, lon))

    def shift(self, t_ms, tack: int, heading, sog_kt, manual: int = 1):
        self._row(0x06, struct.pack("<QBBff", int(t_ms), tack, manual, heading, sog_kt))

    def internal(self, key: int, size: int):
        self._row(key, bytes(size))

    def bytes(self) -> bytes:
        self._close_page()
        data = bytes(self.out)
        self._new_page()
        return data
