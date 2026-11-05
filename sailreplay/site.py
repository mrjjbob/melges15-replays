"""
Build a whole fleet website from a folder of .vkx logs.

  logs/                 anyone's .vkx files, any names, any days
  site.json             optional: {"title": ..., "club": ..., "timezone": "America/New_York"}
  boats.json            optional: Atlas serial -> boat name/colour

Output:
  index.html            every racing day, newest first, plus how to add your own files
  days/YYYY-MM-DD.html  replay + debrief of every race that day, all boats together
"""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import analysis, replay, vkx

INDEX_TEMPLATE = Path(__file__).with_name("index_template.html")


def boat_name_from_file(path: Path, boats_cfg: dict) -> str:
    stem = path.stem
    for serial, v in boats_cfg.items():
        if serial.lower() in stem.lower() and v.get("name"):
            return v["name"]
    # Vakaros Connect export: "Max_Hafen_s_Vakaros_3-22-2026"
    m = re.match(r"(.+?)_s_Vakaros", stem, re.I)
    if m:
        stem = m.group(1)
    # sailreplay download: "mojo_20260322_1552_s3"
    stem = re.sub(r"_\d{8}_\d{4}_s\d+$", "", stem)
    stem = re.sub(r"[-_ ]?\d{1,2}-\d{1,2}-\d{2,4}$", "", stem)      # trailing date
    name = re.sub(r"[_]+", " ", stem).strip()
    if name.islower() or name.isupper():
        name = name.title()
    return name or path.stem


def merge_logs(logs: list[vkx.VkxLog], source: str) -> vkx.VkxLog:
    """One boat, several files the same day (two sessions, or a re-download): join them."""
    if len(logs) == 1:
        return logs[0]
    out = vkx.VkxLog(source=source, version=logs[0].version, log_rate_hz=logs[0].log_rate_hz,
                     fixed_to_body=logs[0].fixed_to_body)
    for name in ("track", "timer", "line", "shifts", "wind", "water", "depth", "load", "temperature", "declination"):
        rows = {}
        for l in logs:
            for r in getattr(l, name):
                rows[(r["t"], r.get("event"), r.get("end"))] = r
        setattr(out, name, sorted(rows.values(), key=lambda r: r["t"]))
    return out


def _local_day(ms: int, tz) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")


def build_site(logs_dir: str | Path, out_dir: str | Path, *, boats_cfg: dict | None = None,
               site_cfg: dict | None = None, repo: str | None = None, branch: str = "main",
               log=print) -> dict:
    from zoneinfo import ZoneInfo
    boats_cfg = boats_cfg or {}
    site_cfg = site_cfg or {}
    tz = ZoneInfo(site_cfg.get("timezone", "America/New_York"))
    logs_dir, out = Path(logs_dir), Path(out_dir)
    (out / "days").mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in logs_dir.rglob("*") if p.suffix.lower() == ".vkx" and p.is_file())
    by_day: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    problems = []
    for p in files:
        try:
            lg = vkx.parse_file(p)
        except Exception as e:  # damaged file: report it, keep going
            problems.append(f"{p.name}: could not be read ({e})")
            continue
        if len(lg.track) < 120:
            problems.append(f"{p.name}: too short to use")
            continue
        name = boat_name_from_file(p, boats_cfg)
        by_day[_local_day(lg.start_ms, tz)][name].append(lg)

    colors = {v["name"]: v["color"] for v in boats_cfg.values() if v.get("name") and v.get("color")}
    heel = {v["name"]: float(v.get("heel_offset", 0)) for v in boats_cfg.values() if v.get("name")}
    days = []
    for day in sorted(by_day, reverse=True):
        boats = [(n, merge_logs(ls, n)) for n, ls in sorted(by_day[day].items())]
        nice = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
        entry = {"date": day, "title": nice, "boats": [n for n, _ in boats], "races": 0}
        try:
            d = analysis.build_day(boats, title=nice, colors=colors, heel_offsets=heel)
        except ValueError as e:
            entry["note"] = str(e)
            days.append(entry)
            log(f"{day}: {len(boats)} boat(s), no races found")
            continue
        d["day"]["home"] = "../index.html"
        replay.write_html(d, out / "days" / f"{day}.html")
        rs = d["day"]["races"]
        tws = [r["tws"] for r in rs if r.get("tws")]
        twd = [r["twd"] for r in rs if r.get("twd") is not None]
        entry.update({
            "races": len(rs), "href": f"days/{day}.html",
            "first_start_ms": rs[0]["start_ms"],
            "wind_kt": [round(min(tws)), round(max(tws))] if tws else None,
            "wind_dir": round(analysis.circ_mean(twd)) if twd else None,
            "estimated": any(r["estimated"] for r in rs),
            "course_changes": sum(1 for r in rs if any((r["course"].get(k) or 0) >= 30 for k in ("windward_moved_m", "leeward_moved_m"))),
            "winners": [r["winner"] for r in rs] if len(boats) > 1 else None,
        })
        days.append(entry)
        log(f"{day}: {len(boats)} boat(s), {len(rs)} race(s)")

    upload_url = f"https://github.com/{repo}/upload/{branch}/logs" if repo else None
    manifest = {
        "title": site_cfg.get("title", "Race Replays"),
        "club": site_cfg.get("club", ""),
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "upload_url": upload_url,
        "repo_url": f"https://github.com/{repo}" if repo else None,
        "days": days, "problems": problems,
        "boat_count": len({b for d in days for b in d["boats"]}),
    }
    page = INDEX_TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("__TITLE__", html.escape(manifest["title"]))
    page = page.replace("__SITE_JSON__", json.dumps(manifest, separators=(",", ":")).replace("</", "<\\/"))
    (out / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<style>[hidden]{display:none!important}body{margin:0}</style>\n</head>\n<body>\n'
        + page + "\n</body>\n</html>\n", encoding="utf-8")
    (out / ".nojekyll").write_text("")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest
