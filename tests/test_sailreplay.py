import json
import math
import numpy as np
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


def test_line_is_square_to_course(tmp_path):
    import make_demo_race
    make_demo_race.simulate(tmp_path, timers=False)
    boats = [(p.stem, vkx.parse_file(p)) for p in sorted(tmp_path.glob("*.vkx"))]
    r = analysis.find_races(boats)[0]
    le = r["line_est"]
    mlon = 111_320 * math.cos(math.radians(le["rc"][0]))
    dx = (le["finish_end"][1] - le["pin_end"][1]) * mlon
    dy = (le["finish_end"][0] - le["pin_end"][0]) * 111_320
    bearing = math.degrees(math.atan2(dx, dy)) % 180
    # the simulated course runs straight up the starting wind; the line is square to the course
    assert abs(((bearing - (make_demo_race.TWD0 + 90) % 180) + 90) % 180 - 90) < 4
    assert le["basis"] == "course"
    assert le["start_len_m"] >= 1.5 * le["finish_len_m"] - 1
    assert abs(r["start_ms"] - make_demo_race.GUN_MS) < 30_000


def test_series_assignment():
    from sailreplay.site import series_for
    ser = [{"name": "P1", "start": "2026-11-07"}, {"name": "P2", "start": None}]
    assert series_for("2026-11-08", ser)["name"] == "P1"
    assert series_for("2026-03-22", ser) is None
    ser[1]["start"] = "2027-03-01"
    assert series_for("2027-02-28", ser)["name"] == "P1"
    assert series_for("2027-03-07", ser)["name"] == "P2"


def test_cyc_high_point_scoring():
    from sailreplay.site import score_series, race_points
    # SI 13.1: a point per boat beaten plus one; DNF scores 1. SI 11.2: over 10 min behind the first is DNF.
    pts, _ = race_points([{"name": "A", "pos": 1, "elapsed_s": 600}, {"name": "B", "pos": 2, "elapsed_s": 650},
                          {"name": "C", "pos": 3, "elapsed_s": 1300}, {"name": "D", "pos": 4, "elapsed_s": None},
                          {"name": "E", "pos": 5, "elapsed_s": None, "sailed": False}])
    assert pts == {"A": (4, None), "B": (3, None), "C": (1, "DNF"), "D": (1, "DNF")}
    day1 = {"date": "2026-11-08", "title": "d1", "boats": ["A", "B", "C"], "race_rows": [
        {"race": 1, "results": [{"name": "A", "pos": 2, "elapsed_s": 700}, {"name": "B", "pos": 1, "elapsed_s": 650},
                                {"name": "C", "pos": 3, "elapsed_s": None}]}]}
    day2 = {"date": "2026-11-15", "title": "d2", "boats": ["A", "C"], "race_rows": [
        {"race": 1, "results": [{"name": "A", "pos": 1, "elapsed_s": 600}, {"name": "C", "pos": 2, "elapsed_s": 640}]}]}
    s = score_series("Fall", [day1, day2])
    by = {r["boat"]: r for r in s["rows"]}
    assert [x["pts"] for x in by["A"]["scores"]] == [2, 2] and by["A"]["total"] == 4
    assert by["B"]["scores"][1] == {"pts": 0, "code": "DNC"} and by["B"]["total"] == 3
    assert by["C"]["scores"][0]["code"] == "DNF" and by["C"]["total"] == 2
    assert s["rows"][0]["boat"] == "A" and by["A"]["qualified"] and not by["B"]["qualified"]
    # regatta: worst race dropped when five or more are sailed (SI 13.3)
    rows = [{"race": k, "results": [{"name": "A", "pos": 1, "elapsed_s": 600 + (k == 3) * 100},
                                    {"name": "B", "pos": 2, "elapsed_s": 650}]} for k in range(1, 6)]
    rg = score_series("New Year's Regatta", [{"date": "2027-01-03", "title": "ny", "boats": ["A", "B"], "race_rows": rows}],
                      kind="regatta", discard_from=5)
    a = {r["boat"]: r for r in rg["rows"]}["A"]
    assert a["total"] == 9 and a["net"] == 8 and sum(1 for x in a["scores"] if x.get("dropped")) == 1


def test_site_builds(tmp_path):
    import shutil
    import make_demo_race
    from sailreplay import site
    logs = tmp_path / "logs"
    make_demo_race.simulate(logs, timers=False)
    m = site.build_site(logs, tmp_path / "_site", site_cfg={"series": [{"name": "P1", "start": "2026-10-01"}]}, repo="a/b", log=lambda *a: None)
    assert (tmp_path / "_site" / "results.html").exists() and (tmp_path / "_site" / "index.html").exists()
    assert m["days"][0]["series"] == "P1" and m["days"][0]["races"] == 1
    day = m["days"][0]["date"]
    hid = site.build_site(logs, tmp_path / "_hid", site_cfg={"hidden_days": [day]}, repo="a/b", log=lambda *a: None)
    assert hid["days"] == [] and hid["boat_count"] == 0
    assert (tmp_path / "_hid" / "days" / f"{day}.html").exists()
    assert hid["other_count"] == 1 and day in (tmp_path / "_hid" / "other.html").read_text()   # listed on Other races


def test_averages_leave_out_manoeuvres():
    """Upwind and downwind averages are taken over steady sailing: no seconds next to a tack or gybe."""
    import make_demo_race
    from sailreplay import analysis
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        make_demo_race.simulate(Path(d), timers=False)
        boats = [(p.stem, vkx.parse_file(p)) for p in sorted(Path(d).glob("*.vkx"))]
        r = analysis.build_day(boats)["races"][0]
    hz = r["meta"]["hz"]
    for b in r["boats"]:
        st = b["track"]["steady"]
        assert len(st) == len(b["track"]["sog"]) and "1" in st
        for m in b["manoeuvres"]:
            if m["kind"] in ("tack", "gybe"):
                i = int(round(m["t"] * hz))
                assert "1" not in st[max(0, i - int(3 * hz)):i + int(5 * hz)]
        assert b["summary"]["heel_vs_vmg"] is None or all(len(x) == 3 for x in b["summary"]["heel_vs_vmg"])


def test_official_results_replace_preliminary(tmp_path):
    import openpyxl
    from datetime import date, timedelta
    import make_demo_race
    from sailreplay import site
    logs = tmp_path / "logs"
    make_demo_race.simulate(logs, timers=False)
    m = site.build_site(logs, tmp_path / "_a", site_cfg={"series": [{"name": "P1", "start": "2026-01-01"}]}, log=lambda *a: None)
    day = date.fromisoformat(m["days"][0]["date"])
    wb = openpyxl.Workbook(); ws = wb.active
    rows = [[None, None, (day + timedelta(days=1)).strftime("%b. %-d, %Y")],      # typed up the next day
            [None, "MELGES 15", 1, "TOTAL"], [None, "1 Zed", 3, 3], [None, "2 Ann", 2, 2], [None, "3 Bob", 0, 0],
            [], [None, None, "15-18 Mph NW"], [],
            [None, None, day.strftime("%b. %-d, %Y")], [None, "JY 15", 1, "TOTAL"], [None, "9 Jay", 1, 1]]
    for r in rows:
        ws.append(r)
    (tmp_path / "results").mkdir(); wb.save(tmp_path / "results" / "scores.xlsx")
    site.build_site(logs, tmp_path / "_b", site_cfg={"series": [{"name": "P1", "start": "2026-01-01"}]},
                    fleet=[{"sail": "2", "name": "Annie"}], log=lambda *a: None)
    s = (tmp_path / "_b" / "results.html").read_text()
    res, _ = json.JSONDecoder().raw_decode(s[s.index('{"title"'):])
    st = res["standings"][0]
    assert [r["official"] for r in st["races"]] == [True] and st["races"][0]["date"] == day.isoformat()
    assert st["races"][0]["weather"] == "15-18 Mph NW"
    assert [(r["boat"], r["total"]) for r in st["rows"]] == [("1 Zed", 3), ("2 Annie", 2), ("3 Bob", 0)]


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


@pytest.mark.parametrize("course,letter,legs", [
    ("W", "W", ["up", "down", "up"]),
    ("L", "L", ["up", "down"]),
    ("W2", "W2", ["up", "down", "up", "down", "up"]),
    ("Z", "Z", ["up", "down", "up", "down"]),
])
def test_cyc_courses(tmp_path, course, letter, legs, capsys):
    import make_demo_race
    make_demo_race.simulate(tmp_path, timers=False, course=course)
    sim = {l.split("->")[0].strip(): float(l.split("finished ")[1].split(" ")[0])
           for l in capsys.readouterr().out.splitlines() if "finished" in l}
    boats = [(p.stem.replace("_", " ").title(), vkx.parse_file(p)) for p in sorted(tmp_path.glob("*.vkx"))]
    races = analysis.find_races(boats)
    assert len(races) == 1
    r = races[0]
    assert r["course_letter"] == letter and r["legs"] == legs
    assert abs(r["start_ms"] - make_demo_race.GUN_MS) < 15_000
    assert sorted(r["finish"], key=r["finish"].get) == sorted(sim, key=sim.get)
    for name, ms in r["finish"].items():
        assert abs((ms - make_demo_race.GUN_MS) / 1000 - sim[name] * 60) < 40


def test_fleet_matching():
    from pathlib import Path as P
    from sailreplay.site import resolve_boat
    fleet = [{"sail": "900", "name": "Hafen", "serials": ["0228005931"], "aliases": ["Max Hafen"]},
             {"sail": "302", "name": "Rubel"}, {"sail": "270", "name": "Simkins", "aliases": ["Glackin"]}]
    assert resolve_boat(P("Max_Hafen_s_Vakaros_3-22-2026.vkx"), {}, fleet) == "900 Hafen"
    assert resolve_boat(P("hafen_20261108_1552_s3.vkx"), {}, fleet) == "900 Hafen"
    assert resolve_boat(P("0228005931_20261108_1552_s3.vkx"), {}, fleet) == "900 Hafen"
    assert resolve_boat(P("302.vkx"), {}, fleet) == "302 Rubel"
    assert resolve_boat(P("Glackin.vkx"), {}, fleet) == "270 Simkins"
    assert resolve_boat(P("New_Sailor_s_Vakaros_11-8-2026.vkx"), {}, fleet) == "New Sailor"


def test_pace(race):
    from sailreplay import polar
    import numpy as np
    assert abs(polar.target_speeds(6, [45])[0] - polar.speed(6, 45)) < 1e-9
    assert race["sides"]["tws_ref"] and race["sides"]["up"] and len(race["sides"]["up"]) == 3
    for b in race["boats"]:
        p = np.array([x for x in b["track"]["pace"] if x is not None], float)
        assert 70 < np.median(p) < 130                       # the fleet sails near its own average
        assert b["summary"]["up_pace"] and b["summary"]["down_pace"]
        assert all("pace" in leg for leg in b["legs"])


def test_tide_and_coast_for_northport():
    from datetime import datetime, timezone
    from sailreplay import local
    t = int(datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc).timestamp() * 1000)
    s = local.tide_summary(t, t + 20 * 60_000)
    assert s["state"] == "ebb"                      # high 15:26Z, low 21:53Z: going out
    assert s["events"][0]["type"] == "H" and s["events"][-1]["type"] == "L"
    c = local.coastline_near(40.914, -73.363)
    assert c and len(c["lines"]) >= 1
    assert local.coastline_near(41.5, -71.3) is None   # outside the bundled area


def test_leeward_rounding_side_and_gate(tmp_path, monkeypatch):
    hz = 2
    def turn(a, b, steps):                       # heading a -> b over steps, the short way given by sign
        return list(np.linspace(a, b, steps) % 360)
    run, beat = [180.0] * 40, [45.0] * 30
    port = np.array(run + turn(180, 45, 12) + beat)            # anticlockwise up: normal port rounding
    stbd = np.array(run + turn(180, 315, 12) + beat[:0] + [315.0] * 30)
    gybe_then_port = np.array([215.0] * 25 + turn(215, 145, 6) + [145.0] * 9 + turn(145, 30, 12) + turn(30, 110, 8) + [110.0] * 22)
    assert analysis._rounding_side(port, 46, hz) == "port"
    assert analysis._rounding_side(stbd, 46, hz) == "starboard"
    assert analysis._rounding_side(gybe_then_port, 40, hz) == "port"   # gybe in, tack out: still a port rounding
    assert analysis._rounding_side(np.array(run * 3), 60, hz) is None

    import make_demo_race
    make_demo_race.simulate(tmp_path, timers=False, course="W")
    boats = [(p.stem, vkx.parse_file(p)) for p in sorted(tmp_path.glob("*.vkx"))]
    assert not analysis.find_races(boats)[0].get("gate")
    # one boat takes the other side of the gate
    real, calls = analysis._rounding_side, []
    def fake(cog, j, hz):
        calls.append(1)
        return "starboard" if len(calls) == 1 else real(cog, j, hz)
    monkeypatch.setattr(analysis, "_rounding_side", fake)
    r = analysis.find_races(boats)[0]
    lw = next(m for m in r["marks"] if m["kind"] == "leeward")
    assert r["gate"] and {g["rounded"] for g in lw["gate"]} == {"port", "starboard"}


def _track(headings, speeds, hz=2):
    """x, y from a list of headings (deg) and speeds (kt), one per sample."""
    from types import SimpleNamespace
    h = np.radians(np.asarray(headings, float)); v = np.asarray(speeds, float) * 0.5144 / hz
    x = np.cumsum(v * np.sin(h)); y = np.cumsum(v * np.cos(h))
    return SimpleNamespace(cog=np.degrees(h) % 360, sog=np.asarray(speeds, float), x=x, y=y)


def test_penalties_recall_and_turns():
    hz = 2
    # wind from the north; beat on starboard at 315, then later a 360 to port while beating
    beat = [315.0] * 200
    circle = list((315 - np.arange(0, 361, 12)) % 360)        # 31 samples, anticlockwise
    s = _track(beat + circle + beat, [5.0] * (400 + len(circle)))
    pen = analysis._penalties(s, hz, 0, None, None, None, [])
    assert [p["kind"] for p in pen] == ["turns"] and 95 < pen[0]["t0"] < 105
    # next to a mark a single circle is just the rounding; a circle on top of the rounding counts
    mid = 200 + len(circle) // 2
    assert analysis._penalties(s, hz, 0, None, None, None, [(s.x[mid] + 10, s.y[mid])]) == []
    big = list((315 - np.arange(0, 541, 12)) % 360)
    s2 = _track(beat + big + beat, [5.0] * (400 + len(big)))
    mid = 200 + len(big) // 2
    pen = analysis._penalties(s2, hz, 0, None, None, None, [(s2.x[mid] + 10, s2.y[mid])])
    assert pen and pen[0]["kind"] == "mark"

    # recall: up for 15 s, back down for 30 s, then upwind again
    hd = [0.0] * 30 + [180.0] * 60 + [0.0] * 200
    s = _track(hd, [4.0] * len(hd))
    s.cog = np.array(hd)
    prog = s.y.copy()
    vmg = 4.0 * np.cos(np.radians(s.cog))
    pen = analysis._penalties(s, hz, 0, None, vmg, prog, [])
    rec = [p for p in pen if p["kind"] == "recall"]
    assert rec and rec[0]["t0"] <= 16 and 44 <= rec[0]["t1"] <= 60
    # a clean start has nothing
    s = _track([0.0] * 300, [4.0] * 300)
    assert analysis._penalties(s, hz, 0, None, np.full(300, 4.0), s.y.copy(), []) == []


def test_general_recall_gun_is_dropped(tmp_path):
    import make_demo_race
    make_demo_race.simulate(tmp_path, timers=True, course="W")
    boats = [(p.stem, vkx.parse_file(p)) for p in sorted(tmp_path.glob("*.vkx"))]
    G = make_demo_race.GUN_MS
    # a gun 7 minutes earlier, while the fleet was milling about near the line: a general recall
    boats[0][1].timer.append({"t": G - 420_000, "event": "race_start", "value_s": 0})
    races = analysis.find_races(boats)
    assert [r["start_ms"] for r in races] == [G]
    # a gun 14 minutes after a real race is a new race, and the real race stays
    boats[0][1].timer[:] = [e for e in boats[0][1].timer if e["t"] != G - 420_000]
    boats[0][1].timer.append({"t": G + 14 * 60_000, "event": "race_start", "value_s": 0})
    races = analysis.find_races(boats)
    assert races and races[0]["start_ms"] == G


def test_committee_boat_shared_between_races(monkeypatch):
    calls = []
    monkeypatch.setattr(analysis, "_fit_line", lambda r, b, fixed_rc=None, fixed_fin=None: calls.append((r["id"], fixed_rc)) or r.setdefault("line_est", {}))
    d = 1 / 111_320                                        # one metre of latitude
    mk = lambda i, y: {"id": i, "_onset": {"a": 0}, "_fin0": {}, "finish": {},
                       "line_est": {"rc": [40.9 + y * d, -73.36], "finish_end": [40.9 + y * d, -73.3605]}}
    races = [mk(1, 0), mk(2, 20), mk(3, 10), mk(4, 120), mk(5, 300), mk(6, 310)]
    analysis._share_committee(races, [1, 2, 3])                 # a fleet: estimates within 40 m are one spot
    got = {i: rc for i, rc in calls}
    assert set(got) == {1, 2, 3, 5, 6}                     # race 4 stands alone, so is not refitted
    assert abs(got[1][0] - (40.9 + 10 * d)) < 1e-9 and got[1] == got[2] == got[3]
    assert got[5] == got[6] != got[1]


def test_real_atlas_log_from_denmark():
    """A real log from someone else's boat: resynced countdown, pinged line, far from Northport."""
    p = Path(__file__).parent / "data" / "atlas2_denmark_20250917.vkx"
    log = vkx.parse_file(p)
    assert len(log.line) == 2
    assert analysis.find_guns([log]) == [1758125101459]          # the sync replaces the first countdown
    d = analysis.build_day([("dk", log)])
    assert len(d["races"]) == 1
    r = d["races"][0]
    assert r["meta"]["tide"] is None and r["meta"]["coast"] is None
    assert r["boats"][0]["penalties"] == []                      # behind the line at the gun, not OCS
    assert any(m["kind"] == "rounding" for m in r["boats"][0]["manoeuvres"])


def test_late_unsynced_timer_does_not_make_a_second_race():
    from types import SimpleNamespace
    G = 1_800_000_000_000
    synced = SimpleNamespace(timer=[{"t": G - 300_000, "event": "start", "value_s": 300},
                                    {"t": G - 240_000, "event": "sync", "value_s": 240},
                                    {"t": G, "event": "race_start", "value_s": 0}])
    late = SimpleNamespace(timer=[{"t": G - 260_000, "event": "start", "value_s": 300},   # started 40 s late
                                  {"t": G + 40_000, "event": "race_start", "value_s": 0}])
    assert analysis.find_guns([synced, late]) == [G]
    assert analysis.find_guns([late]) == [G + 40_000]
