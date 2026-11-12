from sailreplay import analysis


def test_racing_area_covers_marks_with_margin():
    races = [{"marks": [{"kind": "windward", "lat": 40.0100, "lon": -72.0}, {"kind": "leeward", "lat": 40.0, "lon": -72.0}]}]
    a = analysis.racing_area(races)
    assert abs(a["lat"] - 40.005) < 1e-6
    # half the 1.1 km leg plus the margin
    assert 556 + analysis.AREA_MARGIN_M - 5 < a["r_m"] < 556 + analysis.AREA_MARGIN_M + 5


def test_racing_area_needs_marks_or_boats():
    assert analysis.racing_area([{"marks": []}]) is None
