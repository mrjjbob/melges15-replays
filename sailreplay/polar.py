"""
Boat speed model used to estimate wind speed from how fast the boat sailed.

There is no published Melges 15 polar. This table is a provisional estimate built from:
  - the Quantum / US Sailing tuning guide wind bands (light 0-6, 6-10, 10-14, 15+;
    downwind light-air mode to 8 kt, deeper angles 9-12, planing from about 8 kt),
  - its Portsmouth rating (D-PN 86 provisional; RYA PN 994), about 6% quicker than an RS200,
  - typical speeds of 15 ft planing asymmetric dinghies of similar weight and sail area.

Speeds are knots through the water for a well-sailed boat. Replace POLAR with your fleet's
own numbers when you have them; everything else picks them up.
"""
from __future__ import annotations

import numpy as np

NAME = "Melges 15 (provisional)"

TWS = np.array([3, 4, 6, 8, 10, 12, 14, 16, 20], dtype=float)
# true wind angle columns
TWA = np.array([40, 45, 52, 60, 75, 90, 110, 120, 135, 150, 165, 180], dtype=float)
# rows follow TWS, columns follow TWA
POLAR = np.array([
    # 40   45   52   60   75   90   110  120  135  150  165  180
    [1.6, 2.2, 2.6, 2.8, 3.0, 3.1, 3.1, 3.0, 2.7, 2.2, 1.9, 1.8],   # 3 kt
    [2.4, 3.0, 3.4, 3.6, 3.9, 4.1, 4.1, 4.0, 3.5, 2.9, 2.4, 2.3],   # 4 kt
    [3.4, 4.0, 4.4, 4.7, 5.1, 5.5, 5.6, 5.4, 4.9, 4.1, 3.5, 3.3],   # 6 kt
    [4.2, 4.8, 5.2, 5.6, 6.2, 6.9, 7.4, 7.2, 6.6, 5.6, 4.8, 4.5],   # 8 kt  planing starts
    [4.8, 5.3, 5.7, 6.1, 7.1, 8.2, 9.0, 8.9, 8.3, 7.4, 6.4, 6.0],   # 10 kt
    [5.2, 5.6, 6.0, 6.5, 7.8, 9.2, 10.2, 10.2, 9.8, 9.0, 8.0, 7.4],  # 12 kt
    [5.4, 5.8, 6.2, 6.8, 8.3, 9.9, 11.0, 11.2, 10.9, 10.4, 9.6, 8.8],  # 14 kt
    [5.5, 5.9, 6.3, 7.0, 8.7, 10.4, 11.6, 11.9, 11.8, 11.6, 11.0, 10.2],  # 16 kt
    [5.6, 6.0, 6.4, 7.2, 9.2, 11.0, 12.4, 12.8, 13.0, 13.2, 13.0, 12.4],  # 20 kt
])

_FINE = np.arange(2.0, 22.01, 0.25)


def speed(tws: float, twa: float) -> float:
    """Target boat speed (kt) at a true wind speed and angle."""
    twa = min(180.0, max(TWA[0], abs(twa)))
    col = np.array([np.interp(twa, TWA, row) for row in POLAR])
    return float(np.interp(tws, TWS, col))


def _table_for_angle(twa: float) -> np.ndarray:
    twa = min(180.0, max(TWA[0], abs(twa)))
    col = np.array([np.interp(twa, TWA, row) for row in POLAR])
    return np.interp(_FINE, TWS, col)


def wind_from_speed(sog: np.ndarray, twa: np.ndarray) -> np.ndarray:
    """Invert the polar: the true wind speed at which a boat on its numbers
    would sail at `sog` at angle `twa`. NaN where it cannot be read (reaching, slow, off the table)."""
    sog = np.asarray(sog, float)
    twa = np.abs(np.asarray(twa, float))
    out = np.full(sog.shape, np.nan)
    ok = np.isfinite(sog) & np.isfinite(twa) & (sog > 0.8) & ((twa < 65) | (twa > 115))
    # bucket angles to 2 deg so each table is built once
    keys = np.round(twa[ok] / 2) * 2
    idx = np.nonzero(ok)[0]
    for k in np.unique(keys):
        sel = idx[keys == k]
        tab = _table_for_angle(k)
        good = np.maximum.accumulate(tab)                  # keep it monotonic in wind speed
        v = sog[sel]
        r = np.interp(v, good, _FINE, left=np.nan, right=np.nan)
        if k < 65:
            r = np.where(r > 12, np.nan, r)                # upwind speed barely changes above 12 kt
        out[sel] = r
    return out


def describe(lo: float, mid: float, hi: float) -> str:
    """Plain words for a wind range, using the tuning-guide bands."""
    if not np.isfinite(mid):
        return ""
    if mid < 6:
        return "light air"
    if mid < 8.5:
        return "light to medium, just under planing"
    if mid < 12:
        return "medium, planing downwind"
    if mid < 15:
        return "fresh"
    return "heavy"
