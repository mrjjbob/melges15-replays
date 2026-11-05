"""Render a race (or a whole day of races) into a self-contained replay page."""
from __future__ import annotations

import html
import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("replay_template.html")


def render_html(data: dict, *, standalone: bool = True) -> str:
    """data is one race from build_race or a day from build_day.
    standalone=True gives a full HTML document to open from disk or put on a website;
    standalone=False gives the bare page used when publishing as a claude.ai artifact."""
    title = data["day"]["title"] if "day" in data else data["meta"]["title"]
    blob = json.dumps(data, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    page = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__TITLE__", html.escape(title))
            .replace("__RACE_JSON__", blob))
    if not standalone:
        return page
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '<style>[hidden]{display:none!important}body{margin:0}</style>\n</head>\n<body>\n'
            + page + "\n</body>\n</html>\n")


def write_html(data: dict, path: str | Path, *, standalone: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(data, standalone=standalone), encoding="utf-8")
    return path
