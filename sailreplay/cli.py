"""
sailreplay command line.

  sailreplay download                  pull new sessions off every Atlas in Bluetooth range
  sailreplay info logs/*.vkx           what is in each log (time, length, starts found)
  sailreplay csv logs/mojo.vkx         dump raw data to CSV files
  sailreplay race logs/*.vkx -o race.html
                                       build the replay + debrief page for one race
  sailreplay day logs/*.vkx -o day.html
                                       every race in the logs on one page, with a day summary

Boat names: put a boats.json next to where you run the command, e.g.
  {"0228006B82": {"name": "Mojo", "color": "#e4572e", "heel_offset": 0}}
Keys are Atlas serial numbers. Downloaded files are named after the boat.
On the race command you can also name boats inline: "Mojo=logs/a.vkx".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import analysis, replay, vkx


# --------------------------------------------------------------------------- config
def load_boats(path: str | None) -> dict:
    p = Path(path or "boats.json")
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        sys.exit(f"{p} is not valid JSON: {e}")
    return {k.upper(): (v if isinstance(v, dict) else {"name": v}) for k, v in data.items()}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "boat"


# --------------------------------------------------------------------------- download
async def _download(args) -> None:
    try:
        from atlas_ble_client import AtlasClient
    except ImportError:
        sys.exit("Downloading needs atlas-ble-client: pip install atlas-ble-client")
    boats = load_boats(args.boats)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    print("Scanning for Atlas units (10 s). Switch them on and close Vakaros Connect on nearby phones...")
    devices = await AtlasClient.find_devices(target_sn=args.serial)
    if not devices:
        sys.exit("No Atlas found. Check it is on, in range, and not connected to a phone.")
    since = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None
    for dev in devices:
        boat = boats.get(dev.serial_number.upper(), {}).get("name") or dev.serial_number
        print(f"\n{boat} (Atlas {dev.serial_number})")
        try:
            async with AtlasClient(dev) as client:
                sessions = await client.list_sessions()
                if not sessions:
                    print("  no sessions on the unit")
                    continue
                now = datetime.now(timezone.utc)
                newest = max(sessions, key=lambda x: x.index)
                for sess in sessions:
                    started = datetime.fromtimestamp(sess.start_sec, tz=timezone.utc)
                    ended = datetime.fromtimestamp(sess.end_sec, tz=timezone.utc)
                    # the session being recorded right now has no proper end yet: the unit reports
                    # its end as "now", or nonsense dates and sizes. Skip it; switching off closes it.
                    if sess.index == newest.index and (ended > now - timedelta(minutes=10) or started > now
                                                       or sess.total_chunks > 200_000 or sess.duration_sec == 0):
                        print(f"  session {sess.index}: still recording, skipped. Switch the Atlas off to close it, then download again.")
                        continue
                    if started > now + timedelta(days=1) or sess.total_chunks > 200_000 or sess.total_chunks <= 0:
                        print(f"  session {sess.index}: unit reports {started:%Y-%m-%d}, {sess.total_chunks} chunks; looks damaged, skipped")
                        continue
                    if since and ended < since:
                        continue
                    if sess.duration_sec < args.min_minutes * 60:
                        continue
                    name = f"{slug(boat)}_{started:%Y%m%d_%H%M}_s{sess.index}.vkx"
                    path = out / name
                    skip = path.with_suffix(".skip")
                    if (path.exists() or skip.exists()) and not args.force:
                        continue
                    a, b = sess.start_chunk, sess.end_chunk
                    if not args.no_probe:
                        win = await _sailing_window(client, sess, AtlasClient)
                        if win is False:
                            print(f"  {name}  {sess.duration_sec/3600:.1f} h, boat never sailed, skipped")
                            skip.write_text("no sailing found in a sample of this session\n")
                            continue
                        if win:
                            a, b = win
                    total = b - a + 1
                    part = "" if total == sess.total_chunks else f" (sailing part: {total} of {sess.total_chunks})"
                    print(f"  {name}  {total} chunks{part}", end="", flush=True)
                    records, done = [], 0
                    async for chunk_id, data in client.stream_chunks(sess, chunk_ids=list(range(a, b + 1))):
                        records.append(AtlasClient._assemble_chunk(chunk_id, data))
                        done += 1
                        if done % 50 == 0:
                            print(f"\r  {name}  {done}/{total} chunks{part}", end="", flush=True)
                    data = b"".join(records)
                    log = vkx.parse_bytes(data)
                    mins = vkx.sailing_minutes(log)
                    if mins < args.min_sailing:
                        print(f"\r  {name}  only {mins:.0f} min of sailing, not kept" + " " * 20)
                        skip.write_text(f"only {mins:.1f} min of sailing\n")
                        continue
                    w = vkx.sailing_window(log, margin_s=900)
                    if w:
                        data = vkx.crop_bytes(data, *w)
                        log = vkx.parse_bytes(data)
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(data)
                    tmp.replace(path)
                    missing = total - done
                    print(f"\r  {name}  saved: {mins:.0f} min of sailing, {len(log.track)} fixes"
                          + (f", {missing} chunks could not be read" if missing else "") + " " * 10)
        except Exception as e:  # keep going with the next boat
            print(f"  failed: {e}")


async def _chunk_moving(client, sess, AtlasClient, cid: int, min_kt: float = 2.5):
    """True/False for whether the boat was sailing in one chunk, None if unreadable."""
    try:
        async for c, data in client.stream_chunks(sess, chunk_ids=[cid]):
            log = vkx.parse_bytes(AtlasClient._assemble_chunk(c, data))
            if not log.track:
                return None
            return sum(1 for r in log.track if r["sog_kt"] >= min_kt) >= 3
    except Exception:
        return None
    return None


async def _sailing_window(client, sess, AtlasClient, samples: int = 32, margin_s: int = 900):
    """Find the stretch of a session where the boat was sailing without downloading all of it.
    Samples chunks through the session, then narrows the first and last sailing points down.
    Returns (first_chunk, last_chunk), False if the boat never sailed, None if unsure (download all)."""
    lo_c, hi_c, n = sess.start_chunk, sess.end_chunk, sess.total_chunks
    if n <= 60:
        return None
    ids = sorted({lo_c + int(n * (k + 0.5) / samples) for k in range(samples)})
    marks = {}
    for cid in ids:
        marks[cid] = await _chunk_moving(client, sess, AtlasClient, cid)
    if all(v is None for v in marks.values()):
        return None
    moving = [c for c in ids if marks[c]]
    if not moving:
        return False

    async def edge(still, sail):                  # bisect between a still chunk and a sailing one
        while abs(sail - still) > 6:
            mid = (still + sail) // 2
            m = await _chunk_moving(client, sess, AtlasClient, mid)
            if m:
                sail = mid
            else:
                still = mid
        return sail

    before = [c for c in ids if c < moving[0]]
    after = [c for c in ids if c > moving[-1]]
    first = await edge(before[-1], moving[0]) if before else lo_c
    last = await edge(after[0], moving[-1]) if after else hi_c
    per_s = n / max(1, sess.duration_sec)
    m = int(margin_s * per_s) + 2
    return max(lo_c, first - m), min(hi_c, last + m)


def cmd_download(args) -> None:
    asyncio.run(_download(args))


# --------------------------------------------------------------------------- info / csv
def cmd_info(args) -> None:
    for f in args.files:
        log = vkx.parse_file(f)
        print(log.summary())
        races = analysis.find_races([(Path(f).stem, log)])
        analysis.course_changes(races)
        for k, r in enumerate(races, 1):
            st = datetime.fromtimestamp(r["start_ms"] / 1000, tz=timezone.utc)
            fin = max(r["finish"].values()) if r.get("finish") else None
            dur = f"{(fin - r['start_ms']) / 60000:.0f} min" if fin else "?"
            c = r.get("course") or {}
            extra = f", course {c['length_m']} m" if c.get("length_m") else ""
            moved = [f"{k2} mark moved {c[k2 + '_moved_m']} m" for k2 in ("windward", "leeward") if (c.get(k2 + "_moved_m") or 0) >= 30]
            print(f"    race {k}: {'~' if r.get('estimated') else ''}{st:%H:%M:%S} UTC, {dur}{extra}"
                  + (" (" + ", ".join(moved) + ")" if moved else ""))
        if not races:
            print("    no races found")
        if log.unknown_bytes:
            print(f"    note: {log.unknown_bytes} bytes skipped (unknown or damaged rows)")
        if args.verbose:
            print(f"    rows: {log.row_counts}")


def cmd_csv(args) -> None:
    for f in args.files:
        log = vkx.parse_file(f)
        for p in vkx.write_csvs(log, args.out):
            print(p)


# --------------------------------------------------------------------------- race
def _resolve_boats(specs: list[str], boats_cfg: dict):
    """'Name=path' or 'path'. Names come from inline, then boats.json (serial or file stem), then the filename."""
    out = []
    by_slug = {slug(v["name"]): v for v in boats_cfg.values() if v.get("name")}
    for spec in specs:
        name = None
        if "=" in spec and not Path(spec).exists():
            name, spec = spec.split("=", 1)
        p = Path(spec)
        if not p.exists():
            sys.exit(f"File not found: {p}")
        cfg = {}
        stem = p.stem
        for serial, v in boats_cfg.items():
            if serial.lower() in stem.lower():
                cfg = v
        if not cfg:
            for s, v in by_slug.items():
                if stem.lower().startswith(s):
                    cfg = v
        name = name or cfg.get("name") or re.sub(r"_\d{8}_\d{4}_s\d+$", "", stem).replace("_", " ").title()
        out.append((name, p, cfg))
    return out


def _load_boats(resolved) -> list:
    """(name, log) per boat. Several files with the same boat name (a watch's one file per race,
    or an Atlas log plus a watch) are joined into one log for that boat."""
    from .site import merge_logs
    by_name: dict = {}
    for n, p, _ in resolved:
        by_name.setdefault(n, []).append(vkx.parse_file(p))
    return [(n, merge_logs(ls, n)) for n, ls in by_name.items()]


def _parse_gun(s: str, logs) -> int:
    """HH:MM:SS (UTC, on the log's date) or a full ISO time."""
    try:
        if "T" in s or "-" in s:
            dt = datetime.fromisoformat(s)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        h, m, *sec = [int(x) for x in s.split(":")]
        day = datetime.fromtimestamp(min(l.start_ms for l in logs if l.track) / 1000, tz=timezone.utc)
        return int(day.replace(hour=h, minute=m, second=sec[0] if sec else 0, microsecond=0).timestamp() * 1000)
    except ValueError:
        sys.exit(f"Could not read the gun time {s!r}. Use HH:MM:SS in UTC, e.g. 16:05:00.")


def cmd_race(args) -> None:
    cfg = load_boats(args.boats)
    resolved = _resolve_boats(args.files, cfg)
    boats = _load_boats(resolved)
    for n, log in boats:
        print(f"{n:16s} {log.summary()}")
    colors = {n: c["color"] for n, _, c in resolved if c.get("color")}
    heel = {n: float(c.get("heel_offset", 0)) for n, _, c in resolved}
    for item in args.heel_offset or []:
        k, v = item.split("=", 1)
        heel[k] = float(v)
    gun = _parse_gun(args.gun, [l for _, l in boats]) if args.gun else None
    races = [] if gun else analysis.find_races(boats)
    analysis.course_changes(races)
    k = (args.race or 1) - 1
    if races:
        if len(races) > 1:
            print(f"\n{len(races)} races found:")
            for j, r in enumerate(races, 1):
                print(f"  race {j}: {'~' if r.get('estimated') else ''}"
                      f"{datetime.fromtimestamp(r['start_ms']/1000, tz=timezone.utc):%H:%M:%S} UTC")
            if args.race is None:
                print("Building race 1. Use --race N for another, or the day command for all of them.")
        if k >= len(races):
            sys.exit(f"Only {len(races)} race(s) found.")
    first = min(l.start_ms for _, l in boats if l.track)
    title = args.title or f"Race {k + 1} · {datetime.fromtimestamp(first/1000):%a %d %b}"
    prev_end = max(races[k - 1]["finish"].values()) + 30_000 if races and k > 0 and races[k - 1].get("finish") else None
    race = analysis.build_race(boats, twd=args.twd, gun_ms=gun, race=races[k] if races else None,
                               not_before_ms=prev_end, hz=args.hz, heel_offsets=heel, title=title, colors=colors)
    m = race["meta"]
    print(f"\nWind {m['twd_global']}° ({m['twd_source']})")
    for n in m["notes"]:
        print("note:", n)
    for b in sorted(race["boats"], key=lambda b: b["summary"]["position"]):
        s = b["summary"]
        print(f"  {s['position']}. {b['name']:16s} up VMG {s['up_vmg']} kt  down VMG {s['dn_vmg']} kt  "
              f"{s['tacks']} tacks (median loss {s['tack_loss_m']} m)")
    out = replay.write_html(race, args.out)
    print(f"\nReplay page: {out}")
    if args.csv:
        for p in analysis.write_race_csvs(race, args.csv):
            print("  ", p)
    if args.json:
        Path(args.json).write_text(json.dumps(race, separators=(",", ":")))
        print("   race data:", args.json)


def cmd_day(args) -> None:
    cfg = load_boats(args.boats)
    resolved = _resolve_boats(args.files, cfg)
    boats = _load_boats(resolved)
    for n, log in boats:
        print(f"{n:16s} {log.summary()}")
    colors = {n: c["color"] for n, _, c in resolved if c.get("color")}
    heel = {n: float(c.get("heel_offset", 0)) for n, _, c in resolved}
    try:
        day = analysis.build_day(boats, title=args.title, twd=args.twd, hz=args.hz, heel_offsets=heel, colors=colors)
    except ValueError as e:
        sys.exit(str(e))
    print(f"\n{day['day']['title']}: {len(day['races'])} races")
    for r in day["day"]["races"]:
        c = r["course"]
        moved = [f"{k[0].upper()} moved {c[k + '_moved_m']} m" for k in ("windward", "leeward") if (c.get(k + "_moved_m") or 0) >= 30]
        t = datetime.fromtimestamp(r["start_ms"] / 1000, tz=timezone.utc)
        el = f"{int(r['elapsed_s'] // 60)}:{int(r['elapsed_s'] % 60):02d}" if r["elapsed_s"] else "?"
        print(f"  race {r['race']}: {'~' if r['estimated'] else ''}{t:%H:%M} UTC  {el}  wind {r['twd']}°"
              f"  course {c.get('length_m', '?')} m  {'  '.join(moved)}")
    out = replay.write_html(day, args.out)
    print(f"\nDay page: {out}")
    if args.csv:
        for j, rr in enumerate(day["races"], 1):
            for p in analysis.write_race_csvs(rr, Path(args.csv) / f"race{j}"):
                print("  ", p)


def cmd_site(args) -> None:
    from . import site
    cfg = load_boats(args.boats)
    site_cfg = {}
    sp = Path(args.config)
    if sp.exists():
        site_cfg = json.loads(sp.read_text())
    fleet = []
    fp = Path(args.fleet)
    if fp.exists():
        fleet = json.loads(fp.read_text()).get("boats", [])
    m = site.build_site(args.logs, args.out, boats_cfg=cfg, site_cfg=site_cfg, repo=args.repo, branch=args.branch, fleet=fleet,
                        results_dir=args.results)
    print(f"\n{len([d for d in m['days'] if d['races']])} racing days, {m['boat_count']} boats -> {Path(args.out) / 'index.html'}")
    for p in m["problems"]:
        print("  skipped:", p)


# --------------------------------------------------------------------------- main
def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="sailreplay", description="Vakaros Atlas logs to race replays and analysis.")
    ap.add_argument("--boats", help="boats.json with serial -> name/color (default ./boats.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="download sessions from Atlas units over Bluetooth")
    d.add_argument("-o", "--out", default="logs", help="folder for .vkx files (default logs/)")
    d.add_argument("--serial", help="only this Atlas serial number")
    d.add_argument("--days", type=float, default=21, help="only sessions from the last N days (0 = all, default 21)")
    d.add_argument("--min-minutes", type=float, default=3, help="skip sessions shorter than this (default 3)")
    d.add_argument("--force", action="store_true", help="download again even if the file exists")
    d.add_argument("--no-probe", action="store_true", help="download whole sessions, even parked stretches")
    d.add_argument("--min-sailing", type=float, default=10, help="keep sessions with at least this many minutes of sailing (default 10)")
    d.set_defaults(func=cmd_download)

    i = sub.add_parser("info", help="summarise .vkx or .fit files")
    i.add_argument("files", nargs="+")
    i.add_argument("-v", "--verbose", action="store_true")
    i.set_defaults(func=cmd_info)

    c = sub.add_parser("csv", help="export raw rows to CSV")
    c.add_argument("files", nargs="+")
    c.add_argument("-o", "--out", default="csv")
    c.set_defaults(func=cmd_csv)

    r = sub.add_parser("race", help="build the replay page for one race")
    r.add_argument("files", nargs="+", help='.vkx or .fit files, optionally "Boat name=file.vkx"; repeat a name to join its files')
    r.add_argument("-o", "--out", default="race.html")
    r.add_argument("--title")
    r.add_argument("--race", type=int, help="which start in the logs (1 = first)")
    r.add_argument("--gun", help="gun time in UTC (HH:MM:SS) if nobody used the race timer")
    r.add_argument("--twd", type=float, help="true wind direction in degrees, if you know it")
    r.add_argument("--hz", type=float, default=2.0, help="replay sample rate (default 2 per second)")
    r.add_argument("--heel-offset", action="append", metavar="BOAT=DEG", help="subtract a mounting offset from heel")
    r.add_argument("--csv", metavar="DIR", help="also write analysis CSVs here")
    r.add_argument("--json", metavar="FILE", help="also write the race data as JSON")
    r.set_defaults(func=cmd_race)

    dd = sub.add_parser("day", help="every race in the logs on one page")
    dd.add_argument("files", nargs="+", help='.vkx or .fit files, optionally "Boat name=file.vkx"; repeat a name to join its files')
    dd.add_argument("-o", "--out", default="day.html")
    dd.add_argument("--title")
    dd.add_argument("--twd", type=float, help="true wind direction for every race, if you know it")
    dd.add_argument("--hz", type=float, default=2.0)
    dd.add_argument("--csv", metavar="DIR", help="also write analysis CSVs, one folder per race")
    dd.set_defaults(func=cmd_day)

    st = sub.add_parser("site", help="build the fleet website from a folder of logs")
    st.add_argument("logs", nargs="?", default="logs", help="folder of .vkx and .fit files (default logs/)")
    st.add_argument("-o", "--out", default="_site")
    st.add_argument("--config", default="site.json", help="site settings (title, club, timezone)")
    st.add_argument("--fleet", default="fleet.json", help="fleet list: sail numbers, names, Atlas serials")
    st.add_argument("--results", help="folder of official scoring workbooks (.xlsx); default results/ next to the logs")
    st.add_argument("--repo", help="owner/name on GitHub, for the upload button")
    st.add_argument("--branch", default="main")
    st.set_defaults(func=cmd_site)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
