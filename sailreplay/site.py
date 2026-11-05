"""
Build a whole fleet website from a folder of .vkx logs.

  logs/                 anyone's .vkx files, any names, any days
  site.json             optional: title, club, timezone, series (name/start/end/discards), labels
                        e.g. {"series": [{"name": "Frostbite Part 1", "start": "2026-11-07"}],
                              "labels": {"2026-03-22": "Test upload"}}
  boats.json            optional: Atlas serial -> boat name/colour

Output:
  index.html            every racing day by series, newest first, plus how to add your own files
  results.html          preliminary series standings from the uploaded data
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
RESULTS_TEMPLATE = Path(__file__).with_name("results_template.html")


def series_for(day: str, series: list[dict]) -> dict | None:
    """Which series a date belongs to. A series with no start has not begun. A series with no end
    runs until the next series starts."""
    active = sorted([x for x in series if x.get("start")], key=lambda x: x["start"])
    for k, x in enumerate(active):
        end = x.get("end")
        if not end and k + 1 < len(active):
            end = active[k + 1]["start"]
            if day >= end:
                continue
        if day >= x["start"] and (not end or day <= end):
            return x
    return None


def score_series(name: str, days: list[dict], discards: int = 0) -> dict:
    """Low-point scoring (RRS Appendix A) from what was uploaded. Boats that finished get their place.
    A boat that has a log that day but no finish in a race scores DNF; a boat with no log that day
    scores DNC. Both score entries + 1."""
    races = []
    for d in sorted(days, key=lambda d: d["date"]):
        for r in d.get("race_rows", []):
            fin = [x for x in r["results"] if x.get("elapsed_s") is not None]
            by_mg = not fin
            order = sorted(fin, key=lambda x: x["elapsed_s"]) if fin else sorted(r["results"], key=lambda x: x["pos"])
            races.append({"date": d["date"], "day_title": d["title"], "race": r["race"], "href": d.get("href"),
                          "boats_that_day": set(d["boats"]), "order": [x["name"] for x in order],
                          "by_made_good": by_mg})
    boats = sorted({b for r in races for b in r["order"]})
    n = len(boats)
    rows = []
    for b in boats:
        scores = []
        for r in races:
            if b in r["order"]:
                scores.append({"pts": r["order"].index(b) + 1, "code": None})
            elif b in r["boats_that_day"]:
                scores.append({"pts": n + 1, "code": "DNF"})
            else:
                scores.append({"pts": n + 1, "code": "DNC"})
        total = sum(x["pts"] for x in scores)
        dropped = []
        if discards and len(scores) > discards:
            worst = sorted(range(len(scores)), key=lambda i: (-scores[i]["pts"], -i))[:discards]
            for i in worst:
                scores[i]["dropped"] = True
            dropped = worst
        net = total - sum(scores[i]["pts"] for i in dropped)
        rows.append({"boat": b, "scores": scores, "total": total, "net": net,
                     "sailed": sum(1 for x in scores if x["code"] is None)})
    # ties: better net, then more best results (RRS A8.1)
    rows.sort(key=lambda r: (r["net"], sorted(x["pts"] for x in r["scores"] if not x.get("dropped"))))
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    return {"name": name, "discards": discards, "entries": n,
            "races": [{"date": r["date"], "day_title": r["day_title"], "race": r["race"], "href": r["href"],
                       "by_made_good": r["by_made_good"]} for r in races],
            "rows": rows}


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
    stem = re.sub(r"[-_ ]?(\d{6,}|\(\d+\))$", "", stem)            # trailing timestamps, "(1)" copies
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
    series_cfg = site_cfg.get("series") or []
    labels = site_cfg.get("labels") or {}
    days = []
    for day in sorted(by_day, reverse=True):
        boats = [(n, merge_logs(ls, n)) for n, ls in sorted(by_day[day].items())]
        nice = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
        label = labels.get(day)
        ser = None if label else series_for(day, series_cfg)
        title = nice + (f" · {label}" if label else f" · {ser['name']}" if ser else "")
        entry = {"date": day, "title": title, "date_title": nice, "boats": [n for n, _ in boats], "races": 0,
                 "series": ser["name"] if ser else None, "label": label}
        try:
            d = analysis.build_day(boats, title=title, colors=colors, heel_offsets=heel)
        except ValueError as e:
            entry["note"] = str(e)
            days.append(entry)
            log(f"{day}: {len(boats)} boat(s), no races found")
            continue
        d["day"]["home"] = "../index.html"
        d["day"]["series"] = entry["series"]
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
            "race_rows": [{"race": r["race"], "results": r["results"]} for r in rs],
        })
        days.append(entry)
        log(f"{day}: {len(boats)} boat(s), {len(rs)} race(s)")

    standings = []
    for x in series_cfg:
        if not x.get("start"):
            continue
        sd = [d for d in days if d.get("series") == x["name"] and d["races"]]
        standings.append(score_series(x["name"], sd, int(x.get("discards", 0))))
    upload_url = f"https://github.com/{repo}/upload/{branch}/logs" if repo else None
    manifest = {
        "title": site_cfg.get("title", "Race Replays"),
        "club": site_cfg.get("club", ""),
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "upload_url": upload_url,
        "repo_url": f"https://github.com/{repo}" if repo else None,
        "days": days, "problems": problems,
        "boat_count": len({b for d in days for b in d["boats"]}),
        "series": [{"name": x["name"], "start": x.get("start"), "end": x.get("end")} for x in series_cfg],
        "results_note": site_cfg.get("results_note", "Preliminary results from the uploaded Atlas data. Subject to change after the weekly results email."),
    }
    for d in days:
        d.pop("race_rows", None)
    page = INDEX_TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("__TITLE__", html.escape(manifest["title"]))
    page = page.replace("__SITE_JSON__", json.dumps(manifest, separators=(",", ":")).replace("</", "<\\/"))
    (out / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<style>[hidden]{display:none!important}body{margin:0}</style>\n</head>\n<body>\n'
        + page + "\n</body>\n</html>\n", encoding="utf-8")
    res = {"title": manifest["title"], "club": manifest["club"], "note": manifest["results_note"],
           "updated": manifest["updated"], "standings": standings, "upload_url": upload_url}
    page = RESULTS_TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", html.escape(manifest["title"] + " results"))
    page = page.replace("__RESULTS_JSON__", json.dumps(res, separators=(",", ":")).replace("</", "<\\/"))
    (out / "results.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<style>[hidden]{display:none!important}body{margin:0}</style>\n</head>\n<body>\n'
        + page + "\n</body>\n</html>\n", encoding="utf-8")
    (out / ".nojekyll").write_text("")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest
