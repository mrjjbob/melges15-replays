import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import pytest  # noqa: E402

from sailreplay import analysis, replay, vkx  # noqa: E402
from sailreplay.vkx_write import VkxWriter, euler_to_quat  # noqa: E402


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory):
    import make_demo_race
    d = tmp_path_factory.mktemp("demo")
    make_demo_race.simulate(d)
    return d


@pytest.fixture(scope="session")
def race(demo_dir):
    names = {"mojo": "Mojo", "wet_leopard": "Wet Leopard", "sea_biscuit": "Sea Biscuit", "knot_fast": "Knot Fast"}
    boats = [(names[p.stem], vkx.parse_file(p)) for p in sorted(demo_dir.glob("*.vkx"))]
    return analysis.build_race(boats)


def test_quaternion_roundtrip():
    for h, p, r in [(0, 0, 0), (225, 2, -8), (359, -3, 15), (90, 0, 30)]:
        hh, pp, rr = vkx.quat_to_euler(*euler_to_quat(h, p, r))
        assert abs(analysis.wrap180(hh - h)) < 1e-6 and abs(pp - p) < 1e-6 and abs(rr - r) < 1e-6


def test_parse_rows_and_units():
    w = VkxWriter()
    w.config(10)
    w.pvo(1_700_000_000_000, 40.5, -73.5, 2.572, 270.0, 268.0, 1.0, -6.0)
    w.timer(1_700_000_000_500, 3, 0)
    w.line(1_700_000_000_600, 0, 40.5001, -73.5002)
    log = vkx.parse_bytes(w.bytes())
    assert log.log_rate_hz == 10 and log.version == 5
    t = log.track[0]
    assert abs(t["lat"] - 40.5) < 1e-6 and abs(t["sog_kt"] - 5.0) < 0.01 and abs(t["cog"] - 270) < 1e-3
    assert abs(t["heel"] + 6) < 1e-4
    assert log.timer[0]["event"] == "race_start" and log.line[0]["end"] == "pin"


def test_unknown_rows_are_skipped():
    w = VkxWriter()
    w.pvo(1_000, 1.0, 2.0, 1.0, 10, 10, 0, 0)
    data = bytearray(w.bytes())
    data += bytes([0x77]) + b"\x00" * 9          # junk with an undocumented key
    w2 = VkxWriter()
    w2.pvo(2_000, 1.0, 2.0, 1.0, 10, 10, 0, 0)
    data += w2.bytes()
    log = vkx.parse_bytes(bytes(data))
    assert len(log.track) == 2 and log.unknown_bytes == 10


def test_ble_chunk_assembly_parses():
    """Records built the way atlas-ble-client builds them must parse."""
    w = VkxWriter()
    w.pvo(5_000, 10.0, 20.0, 3.0, 45, 45, 0, 0)
    page = w.bytes()
    body = page[8:-3]                             # rows between header and terminator
    fake_ble = b"\x00" * 8 + body + b"\x00" * 5   # shape _assemble_chunk expects
    from atlas_ble_client import AtlasClient
    rec = AtlasClient._assemble_chunk(7, fake_ble)
    log = vkx.parse_bytes(rec)
    assert len(log.track) == 1 and abs(log.track[0]["lon"] - 20.0) < 1e-6


def test_wind_estimate_close_to_truth(race):
    # simulated breeze oscillates around 225-231 deg
    assert abs(analysis.wrap180(race["meta"]["twd_global"] - 228)) < 6
    assert race["meta"]["twd_source"] == "tacks"


def test_start_and_ocs(race):
    by = {b["name"]: b for b in race["boats"]}
    assert by["Sea Biscuit"]["start"]["ocs"] is True
    assert by["Mojo"]["start"]["ocs"] is False
    assert race["line"]["length_m"] == pytest.approx(120, abs=3)


def test_results_and_manoeuvres(race):
    by = {b["name"]: b for b in race["boats"]}
    assert by["Mojo"]["summary"]["position"] == 1
    for b in race["boats"]:
        kinds = [m["kind"] for m in b["manoeuvres"]]
        assert kinds.count("rounding") == 3      # W, L, W
        assert b["summary"]["tacks"] >= 3
        assert [l["type"] for l in b["legs"]] == ["Upwind", "Downwind", "Upwind", "Downwind"]
        assert 34 <= b["summary"]["up_twa"] <= 46


def test_html_is_self_contained(race):
    page = replay.render_html(race)
    assert page.startswith("<!doctype html>") and "__RACE_JSON__" not in page
    assert "</script>" not in page.split('id="race-data">')[1].split("</script>")[0]


def test_cli_race(demo_dir, tmp_path):
    from sailreplay.cli import main
    out = tmp_path / "r.html"
    main(["race", *map(str, sorted(demo_dir.glob("*.vkx"))), "-o", str(out), "--csv", str(tmp_path / "csv"), "--twd", "228"])
    assert out.exists() and (tmp_path / "csv" / "summary.csv").exists()


def test_races_found_without_timer(tmp_path):
    """No race timer: the race is spotted from the sailing, and the start lands near the real gun."""
    import make_demo_race
    make_demo_race.simulate(tmp_path, timers=False)
    boats = [(p.stem, vkx.parse_file(p)) for p in sorted(tmp_path.glob("*.vkx"))]
    races = analysis.find_races(boats)
    assert len(races) == 1 and races[0]["estimated"]
    assert abs(races[0]["start_ms"] - make_demo_race.GUN_MS) < 40_000
    kinds = [m["kind"] for m in races[0]["marks"]]
    assert kinds[0] == "start" and kinds[-1] == "finish" and "windward" in kinds and "leeward" in kinds
    day = analysis.build_day(boats)
    assert len(day["races"]) == 1 and day["races"][0]["meta"]["start_estimated"]
    assert "day" in day and replay.render_html(day).count('id="race-data"') == 1


def test_course_changes():
    races = [{"marks": [{"kind": "windward", "lat": 40.0, "lon": -73.0}, {"kind": "leeward", "lat": 40.006, "lon": -73.0}]},
             {"marks": [{"kind": "windward", "lat": 40.001, "lon": -73.0}, {"kind": "leeward", "lat": 40.006, "lon": -73.0}]}]
    analysis.course_changes(races)
    assert races[0]["course"]["length_m"] == pytest.approx(668, abs=2)
    assert races[1]["course"]["windward_moved_m"] == pytest.approx(111, abs=2)
    assert races[1]["course"]["leeward_moved_m"] == 0


def test_polar_inverts():
    from sailreplay import polar
    import numpy as np
    for tws, twa in [(6, 45), (8, 135), (11, 150), (5, 120)]:
        v = polar.speed(tws, twa)
        est = polar.wind_from_speed(np.array([v]), np.array([twa]))[0]
        assert abs(est - tws) < 0.3
    assert np.isnan(polar.wind_from_speed(np.array([6.0]), np.array([90]))[0])   # reaching is not read


def test_line_is_square_to_wind(tmp_path):
    import make_demo_race
    make_demo_race.simulate(tmp_path, timers=False)
    boats = [(p.stem, vkx.parse_file(p)) for p in sorted(tmp_path.glob("*.vkx"))]
    r = analysis.find_races(boats)[0]
    le = r["line_est"]
    mlon = 111_320 * math.cos(math.radians(le["rc"][0]))
    dx = (le["finish_end"][1] - le["pin_end"][1]) * mlon
    dy = (le["finish_end"][0] - le["pin_end"][0]) * 111_320
    bearing = math.degrees(math.atan2(dx, dy)) % 180
    assert abs(((bearing - (r["twd"] + 90) % 180) + 90) % 180 - 90) < 1
    assert abs(r["start_ms"] - make_demo_race.GUN_MS) < 30_000
