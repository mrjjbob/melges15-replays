"""
Build a whole fleet website from a folder of .vkx logs.

  logs/                 anyone's .vkx files, any names, any days
  site.json             optional: title, club, timezone, series (name/start/end/discards), labels
                        e.g. {"series": [{"name": "Frostbite Part 1", "start": "2026-11-07"}],
                              "labels": {"2026-03-22": "Test upload"}}
  boats.json            optional: Atlas serial -> boat name/colour

Output:
  index.html            every racing day by series, newest first, plus how to add your own files
  results.html          series scores: the club scoring sheet where added, otherwise from the uploaded data
  days/YYYY-MM-DD.html  replay + debrief of every race that day, all boats together
"""
from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import analysis, official, replay, vkx

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


def race_points(results: list[dict], dnf_after_s: float = 600) -> tuple[dict, bool]:
    """CYC high-point for one race (Frostbite SI 11.2 and 13.1).
    A finisher scores one point for each boat she beats, plus one. A boat that started but did not
    finish, or finished more than ten minutes after the first boat, is DNF and scores 1.
    Returns ({boat: (points, code)}, ordered_by_made_good)."""
    results = [x for x in results if x.get("sailed", True)]           # boats on the water for this race
    fin = [x for x in results if x.get("elapsed_s") is not None]
    by_mg = not fin
    if fin:
        first = min(x["elapsed_s"] for x in fin)
        ok = sorted((x for x in fin if x["elapsed_s"] - first <= dnf_after_s), key=lambda x: x["elapsed_s"])
    else:   # nobody's finish could be read: fall back to the order of distance made good
        ok = sorted(results, key=lambda x: x["pos"])
    starters = len(results)
    out = {}
    for k, x in enumerate(ok, 1):
        out[x["name"]] = (starters - k + 1, None)
    for x in results:
        out.setdefault(x["name"], (1, "DNF"))
    return out, by_mg


def score_series(name: str, days: list[dict], *, kind: str = "series", discard_from: int = 0,
                 official_days: dict | None = None) -> dict:
    """CYC Frostbite scoring (SI 13) from what was uploaded: high point, DNC scores 0, no throw-outs
    in a series. A regatta drops each boat's worst race when five or more races are sailed (SI 13.3).
    A boat qualifies for a series by sailing on two or more days (SI 13.2).
    A day in `official_days` (from the club's scoring sheet) is scored from the sheet instead."""
    official_days = official_days or {}
    races = []
    for d in sorted(days, key=lambda d: d["date"]):
        o = official_days.get(d["date"])
        if o:
            for i, rn in enumerate(o["races"]):
                races.append({"date": d["date"], "race": rn, "href": d.get("href"), "weather": o.get("weather"),
                              "pts": {b: (v[i], None) for b, v in o["boats"].items()},
                              "by_made_good": False, "official": True})
            continue
        for r in d.get("race_rows", []):
            pts, by_mg = race_points(r["results"])
            races.append({"date": d["date"], "race": r["race"], "href": d.get("href"),
                          "weather": d.get("weather"), "pts": pts, "by_made_good": by_mg, "official": False})
    boats = sorted({b for r in races for b in r["pts"]})
    rows = []
    for b in boats:
        scores = []
        for r in races:
            if b in r["pts"]:
                p, code = r["pts"][b]
                scores.append({"pts": p, "code": code})
            else:
                scores.append({"pts": 0, "code": "DNC"})
        total = sum(x["pts"] for x in scores)
        net = total
        if kind == "regatta" and discard_from and len(scores) >= discard_from:
            worst = min(range(len(scores)), key=lambda i: (scores[i]["pts"], -i))
            scores[worst]["dropped"] = True
            net = total - scores[worst]["pts"]
        days_sailed = len({r["date"] for r, x in zip(races, scores) if x["pts"] > 0 or x["code"] not in (None, "DNC")})
        rows.append({"boat": b, "scores": scores, "total": total, "net": net, "days": days_sailed,
                     "qualified": days_sailed >= 2 if kind == "series" else True})
    rows.sort(key=lambda r: (-r["net"], -r["total"], r["boat"]))
    rank = 0
    for k, r in enumerate(rows, 1):
        if k == 1 or r["net"] != rows[k - 2]["net"]:
            rank = k
        r["rank"] = rank
    return {"name": name, "kind": kind, "entries": len(boats), "discard_from": discard_from if kind == "regatta" else 0,
            "races": [{k: r[k] for k in ("date", "race", "href", "weather", "by_made_good", "official")} for r in races],
            "rows": rows}


def resolve_boat(path: Path, boats_cfg: dict, fleet: list[dict]) -> str:
    """Which boat a log belongs to. Scored by sail number, as on the official sheet (SI 13.6):
    an Atlas serial in the file name, then a sail number, then a skipper's surname or alias,
    then whatever the file is called."""
    stem = path.stem.lower()
    for b in fleet:
        if any(sn.lower() in stem for sn in b.get("serials", [])):
            return fleet_label(b)
    guess = boat_name_from_file(path, boats_cfg)
    words = set(re.findall(r"[a-z0-9]+", guess.lower()))
    for b in fleet:
        if str(b.get("sail", "")).lower() in words:
            return fleet_label(b)
    for b in fleet:
        names = [b.get("name", "")] + list(b.get("aliases", []))
        if any(n and n.lower() == guess.lower() for n in names):
            return fleet_label(b)
        if b.get("name") and b["name"].lower() in words:
            return fleet_label(b)
    return guess


def fleet_label(b: dict) -> str:
    return f"{b['sail']} {b['name']}".strip() if b.get("sail") else b.get("name", "")


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


def _tide_words(rows) -> str:
    """' · flood' / ' · ebb' / ' · flood then ebb' for a day's races."""
    st = []
    for r in rows:
        t = (r.get("tide") or {}).get("state")
        if t and (not st or st[-1] != t):
            st.append(t)
    return (" · " + " then ".join(st)) if st else ""


def _compass(d: float) -> str:
    pts = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return pts[round((d % 360) / 22.5) % 16]


def _local_day(ms: int, tz) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")


def build_site(logs_dir: str | Path, out_dir: str | Path, *, boats_cfg: dict | None = None,
               site_cfg: dict | None = None, repo: str | None = None, branch: str = "main",
               fleet: list[dict] | None = None, results_dir: str | Path | None = None, log=print) -> dict:
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
        name = resolve_boat(p, boats_cfg, fleet or [])
        by_day[_local_day(lg.start_ms, tz)][name].append(lg)

    colors = {v["name"]: v["color"] for v in boats_cfg.values() if v.get("name") and v.get("color")}
    for k, b in enumerate(fleet or []):           # each fleet boat keeps its colour all season
        colors.setdefault(fleet_label(b), b.get("color") or analysis.PALETTE[k % len(analysis.PALETTE)])
    heel = {v["name"]: float(v.get("heel_offset", 0)) for v in boats_cfg.values() if v.get("name")}
    series_cfg = site_cfg.get("series") or []
    labels = site_cfg.get("labels") or {}
    days = []
    event_days: dict[str, list] = {}
    for day in sorted(by_day, reverse=True):
        boats = [(n, merge_logs(ls, n)) for n, ls in sorted(by_day[day].items())]
        nice = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
        label = labels.get(day)
        ser = None if label else series_for(day, series_cfg)
        title = nice + (f" · {label}" if label else f" · {ser['name']}" if ser else "")
        entry = {"date": day, "title": title, "date_title": nice, "boats": [n for n, _ in boats], "races": 0,
                 "series": ser["name"] if ser else None, "label": label}
        ev = next((e for e in site_cfg.get("events") or [] if day in (e.get("dates") or [])), None)
        if ev:
            entry["event"] = ev["name"]
        try:
            d = analysis.build_day(boats, title=title, colors=colors, heel_offsets=heel)
        except ValueError as e:
            entry["note"] = str(e)
            days.append(entry)
            log(f"{day}: {len(boats)} boat(s), no races found")
            continue
        # Course names. The CYC letters (W, W2, L, M, Z) only mean something in the club's own season;
        # an event elsewhere names its courses in its sailing instructions: "course_names" maps the
        # legs sailed ("up-down-up-down") to that name, per event or per date. Otherwise no letter.
        names = (ev or {}).get("course_names") or (site_cfg.get("course_names") or {}).get(day)
        if names or not entry["series"]:
            for k, row in enumerate(d["day"]["races"]):
                letter = (names or {}).get("-".join(row.get("legs") or []), "")
                row["course_letter"] = letter
                d["races"][k]["meta"]["course_letter"] = letter
        # course letters from the committee's board, when given in site.json ("courses": {date: ["W", "M", ...]})
        for k, letter in enumerate((site_cfg.get("courses") or {}).get(day, [])):
            if letter and k < len(d["races"]):
                d["races"][k]["meta"]["course_letter"] = letter
                d["day"]["races"][k]["course_letter"] = letter
        off_season = not entry["series"] or day in set(site_cfg.get("hidden_days") or [])
        d["day"]["home"] = "../other.html" if off_season else "../index.html"
        d["day"]["home_label"] = "← Other races" if off_season else "← All days"
        d["day"]["series"] = entry["series"]
        if ev and len(ev.get("dates") or []) > 1:
            event_days.setdefault(ev["name"], []).append((day, d, entry))   # written as one page below
        else:
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
            "weather": (f"≈{round(min(tws))}–{round(max(tws))} kt " if tws and round(min(tws)) != round(max(tws)) else
                        f"≈{round(tws[0])} kt " if tws else "") + (_compass(analysis.circ_mean(twd)) if twd else "")
                       + _tide_words(rs),
            "tide": _tide_words(rs).lstrip(" ·") or None,
        })
        days.append(entry)
        log(f"{day}: {len(boats)} boat(s), {len(rs)} race(s)")

    # a regatta over several days: one replay page, the race picker grouped by day and the races
    # numbered through the event; each day's debrief table stays that day's, the Advanced tab spans all
    for name, parts in event_days.items():
        parts.sort(key=lambda p: p[0])
        ev = next(e for e in site_cfg.get("events") or [] if e["name"] == name)
        slug = ev.get("slug") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        races, rows, dlist, n = [], [], [], 0
        for i, (day, d, entry) in enumerate(parts):
            label = (entry.get("label") or "").split(" · ")[-1] if " · " in (entry.get("label") or "") else f"Day {i + 1}"
            dlist.append({"date": day, "label": label, "title": d["day"]["title"], "first": n + 1})
            for rr, row in zip(d["races"], d["day"]["races"]):
                n += 1
                rr["meta"]["title"] = f"Race {n}"
                rr["meta"]["race_no"] = n
                rr["meta"]["day_idx"] = i
                races.append(rr)
                rows.append({**row, "race": n, "day": i, "day_race": row["race"]})
            entry["href"] = f"days/{slug}.html#r{dlist[-1]['first']}"
        first = parts[0][1]["day"]
        dd = {"day": {**first, "title": name, "races": rows, "days": dlist}, "races": races}
        replay.write_html(dd, out / "days" / f"{slug}.html")
        log(f"{name}: {len(parts)} days, {n} races on one page")

    # official results from the club's scoring workbook (results/*.xlsx), when one has been added
    by_sail = {str(b["sail"]): fleet_label(b) for b in (fleet or []) if b.get("sail")}
    def _rename(n):
        m = re.match(r"\s*(\d+)\b", n)
        return by_sail.get(m.group(1), n) if m else n
    rdir = Path(results_dir) if results_dir else logs_dir.parent / "results"
    off, oprobs = official.load(rdir, [d["date"] for d in days], site_cfg.get("official_class", "MELGES 15"), _rename)
    problems += oprobs
    for day, o in off.items():
        e = next((d for d in days if d["date"] == day), None)
        if e is None:                               # a scored day nobody uploaded: still in the series
            label = labels.get(day)
            ser = None if label else series_for(day, series_cfg)
            nice = datetime.strptime(day, "%Y-%m-%d").strftime("%a %d %b %Y")
            e = {"date": day, "title": nice + (f" · {ser['name']}" if ser else ""), "date_title": nice,
                 "boats": [], "races": 0, "series": ser["name"] if ser else None, "label": label}
            days.append(e)
        e["official"] = True
        if o["sheet_date"] != day:
            log(f"{day}: official results dated {o['sheet_date']} on the sheet, used for {day}")
        log(f"{day}: official results, {len(o['boats'])} boat(s), {len(o['races'])} race(s)")
    days.sort(key=lambda d: d["date"], reverse=True)

    def _has(d):
        return d["races"] or d["date"] in off
    standings = []
    for x in series_cfg:
        if not x.get("start"):
            continue
        sd = [d for d in days if d.get("series") == x["name"] and _has(d)]
        standings.append(score_series(x["name"], sd, kind="series", official_days=off))
    for rg in site_cfg.get("regattas") or []:
        rd = [d for d in days if d["date"] == rg.get("date") and _has(d)]
        if rd:
            standings.append(score_series(rg["name"], rd, kind="regatta", discard_from=int(rg.get("discard_from", 5)),
                                          official_days=off))
    # the fleet's upload page and email (see uploader/), or else GitHub's own upload page
    upload_url = site_cfg.get("upload_url") or (f"https://github.com/{repo}/upload/{branch}/logs" if repo else None)
    # days left off the home page ("hidden_days" in site.json); their replay pages are still built
    # The home page is the season: days in a series. Everything else (practice, test sessions, racing
    # outside the season, and dates in "hidden_days") goes on other.html, linked only from the footer.
    hidden_days = set(site_cfg.get("hidden_days") or [])
    home_days = [d for d in days if d.get("series") and d["date"] not in hidden_days]
    other_days = [d for d in days if d not in home_days]
    manifest = {
        "title": site_cfg.get("title", "Race Replays"),
        "club": site_cfg.get("club", ""),
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "upload_url": upload_url,
        "upload_page": bool(site_cfg.get("upload_url")),
        "upload_email": site_cfg.get("upload_email"),
        "days": home_days, "problems": problems,
        "boat_count": len({b for d in home_days for b in d["boats"]}),
        "other_count": len(other_days),
        "series": [{"name": x["name"], "start": x.get("start"), "end": x.get("end")} for x in series_cfg],
        "links": site_cfg.get("links") or [],
        # multi-day events ("events" in site.json): their days are boxed together on the list
        "events": [{"name": e["name"], "dates": e.get("dates") or [], "where": e.get("where")} for e in site_cfg.get("events") or []],
        "results_note": site_cfg.get("results_note", "A day not yet on the club's scoring sheet is scored from the uploaded Atlas data, so it may change when the sheet is added."),
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
    other = dict(manifest, archive=True, days=other_days, other_count=0,
                 boat_count=len({b for d in other_days for b in d["boats"]}))
    page = INDEX_TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", html.escape(manifest["title"] + " · other races"))
    page = page.replace("__SITE_JSON__", json.dumps(other, separators=(",", ":")).replace("</", "<\\/"))
    (out / "other.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="robots" content="noindex">\n'
        '<style>[hidden]{display:none!important}body{margin:0}</style>\n</head>\n<body>\n'
        + page + "\n</body>\n</html>\n", encoding="utf-8")
    res = {"title": manifest["title"], "club": manifest["club"], "note": manifest["results_note"], "links": manifest["links"],
           "updated": manifest["updated"], "standings": standings, "upload_url": upload_url}
    page = RESULTS_TEMPLATE.read_text(encoding="utf-8").replace("__TITLE__", html.escape(manifest["title"] + " scores"))
    page = page.replace("__RESULTS_JSON__", json.dumps(res, separators=(",", ":")).replace("</", "<\\/"))
    (out / "results.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<style>[hidden]{display:none!important}body{margin:0}</style>\n</head>\n<body>\n'
        + page + "\n</body>\n</html>\n", encoding="utf-8")
    (out / ".nojekyll").write_text("")
    for f in ("logo.png", "favicon.png"):                 # the fleet logo and site icon
        src = Path(__file__).parent / "assets" / f
        if src.exists():
            (out / f).write_bytes(src.read_bytes())
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest
