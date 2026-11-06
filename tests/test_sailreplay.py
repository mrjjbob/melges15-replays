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


def test_series_assignment():
    from sailreplay.site import series_for
    ser = [{"name": "P1", "start": "2026-11-07"}, {"name": "P2", "start": None}]
    assert series_for("2026-11-08", ser)["name"] == "P1"
    assert series_for("2026-03-22", ser) is None
    ser[1]["start"] = "2027-03-01"
    assert series_for("2027-02-28", ser)["name"] == "P1"
    assert series_for("2027-03-07", ser)["name"] == "P2"


def test_low_point_scoring():
    from sailreplay.site import score_series
    day1 = {"date": "2026-11-08", "title": "d1", "boats": ["A", "B", "C"], "race_rows": [
        {"race": 1, "results": [{"name": "A", "pos": 2, "elapsed_s": 700}, {"name": "B", "pos": 1, "elapsed_s": 650},
                                {"name": "C", "pos": 3, "elapsed_s": None}]}]}
    day2 = {"date": "2026-11-15", "title": "d2", "boats": ["A", "C"], "race_rows": [
        {"race": 1, "results": [{"name": "A", "pos": 1, "elapsed_s": 600}, {"name": "C", "pos": 2, "elapsed_s": 640}]}]}
    s = score_series("P1", [day1, day2])
    by = {r["boat"]: r for r in s["rows"]}
    assert s["entries"] == 3
    assert [x["pts"] for x in by["A"]["scores"]] == [2, 1]
    assert by["B"]["scores"][1]["code"] == "DNC" and by["B"]["scores"][1]["pts"] == 4
    assert by["C"]["scores"][0]["code"] == "DNF"
    assert s["rows"][0]["boat"] == "A"
    s2 = score_series("P1", [day1, day2], discards=1)
    assert {r["boat"]: r["net"] for r in s2["rows"]}["B"] == 1


def test_site_builds(tmp_path):
    import shutil
    import make_demo_race
    from sailreplay import site
    logs = tmp_path / "logs"
    make_demo_race.simulate(logs, timers=False)
    m = site.build_site(logs, tmp_path / "_site", site_cfg={"series": [{"name": "P1", "start": "2026-10-01"}]}, repo="a/b", log=lambda *a: None)
    assert (tmp_path / "_site" / "results.html").exists() and (tmp_path / "_site" / "index.html").exists()
    assert m["days"][0]["series"] == "P1" and m["days"][0]["races"] == 1


class _FakeAtlas:
    """Stands in for atlas_ble_client.AtlasClient: sessions made of 1-page chunks."""
    chunk_s = 60

    def __init__(self, sessions):
        self.sessions = sessions          # list of (SessionInfo, speed_fn(t_s) -> m/s)
        self.fetched = 0

    from atlas_ble_client import AtlasClient as _Real
    _assemble_chunk = staticmethod(_Real._assemble_chunk)

    def _chunk(self, sess, fn, cid):
        w = VkxWriter()
        t0 = sess.start_sec + (cid - sess.start_chunk) * self.chunk_s
        for k in range(0, self.chunk_s * 2):               # 2 Hz
            ts = t0 + k / 2
            w.pvo(ts * 1000, 40.9 + k * 1e-6, -73.36, fn(ts - sess.start_sec), 10, 10, 0, 0)
        page = w.bytes()
        return b"\x00" * 8 + page[8:-3] + b"\x00" * 5

    async def list_sessions(self):
        return [s for s, _ in self.sessions]

    async def stream_chunks(self, sess, chunk_ids=None):
        fn = next(f for s, f in self.sessions if s.index == sess.index)
        ids = chunk_ids if chunk_ids is not None else range(sess.start_chunk, sess.end_chunk + 1)
        for c in ids:
            if sess.end_chunk > 10_000_000:
                raise AssertionError("tried to stream the live session")
            self.fetched += 1
            yield c, self._chunk(sess, fn, c)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_downloader_skips_live_and_parked(tmp_path, monkeypatch):
    import time
    import atlas_ble_client
    from atlas_ble_client.client import SessionInfo
    from sailreplay import cli
    now = int(time.time())
    day = 86400
    # 1: two hours sailing, then 40 h parked.  2: parked all along.  3: live, reported nonsense.
    s1 = SessionInfo(1, now - 5 * day, now - 5 * day + 42 * 3600, 0, 42 * 60 - 1)
    s2 = SessionInfo(2, now - 3 * day, now - 3 * day + 3 * 3600, 5000, 5000 + 180 - 1)
    s3 = SessionInfo(3, now + 4 * 365 * day, now + 4 * 365 * day, 9000, 9000 + 26_000_000)
    fake = _FakeAtlas([(s1, lambda t: 2.6 if t < 2 * 3600 else 0.05), (s2, lambda t: 0.05), (s3, lambda t: 0.0)])

    class Dev:
        serial_number = "0228005931"

    class Client:
        @staticmethod
        async def find_devices(target_sn=None):
            return [Dev()]

        _assemble_chunk = staticmethod(_FakeAtlas._assemble_chunk)

        def __new__(cls, dev):
            return fake

    monkeypatch.setattr(atlas_ble_client, "AtlasClient", Client)
    cli.main(["--boats", str(tmp_path / "none.json"), "download", "-o", str(tmp_path / "logs"), "--days", "21"])
    files = sorted(p.name for p in (tmp_path / "logs").iterdir())
    vk = [f for f in files if f.endswith(".vkx")]
    assert len(vk) == 1 and "_s1." in vk[0]
    assert any(f.endswith("_s2.skip") for f in files)
    log = vkx.parse_file(tmp_path / "logs" / vk[0])
    span_h = (log.end_ms - log.start_ms) / 3.6e6
    assert 2.0 <= span_h <= 3.0                                # 2 h sailing + 15 min either side, not 42 h
    assert fake.fetched < 42 * 60 * 0.25                       # fetched well under a quarter of the session
