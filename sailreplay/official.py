"""Official results from the club's scoring workbook (results/*.xlsx).

The sheet has one block per class per day:

    Nov. 9, 2026
    MELGES 15     1   2   3   4   5   TOTAL
    270 Glackin   7   5   6   6   5   29
    ...
                  15-18 Mph NW, W          <- weather, optional

Only the blocks for our class are read. Points are taken as written (CYC high point, SI 13).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        m = re.fullmatch(r"\s*([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\s*", v)
        if m and m.group(1).lower() in _MONTHS:
            return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    return None


def read_workbook(path: Path, cls: str = "MELGES 15") -> tuple[list[dict], list[str]]:
    """Every block for `cls` in the workbook: [{date, races, boats: {name: [pts...]}, weather}]."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    blocks, problems = [], []
    want = cls.strip().upper()
    for ws in wb.worksheets:
        rows = [[c for c in r] for r in ws.iter_rows(values_only=True)]
        last_date = None
        k = 0
        while k < len(rows):
            cells = [c for c in rows[k] if c not in (None, "")]
            d = _as_date(cells[0]) if len(cells) == 1 else None
            if d:
                last_date = d
            head = rows[k]
            label = next((i for i, c in enumerate(head) if isinstance(c, str) and c.strip().upper() == want), None)
            if label is None:
                k += 1
                continue
            cols = [i for i in range(label + 1, len(head)) if isinstance(head[i], (int, float))]
            tot = next((i for i in range(label + 1, len(head)) if isinstance(head[i], str) and head[i].strip().upper() == "TOTAL"), None)
            block = {"date": last_date, "races": [int(head[i]) for i in cols], "boats": {}, "weather": None,
                     "sheet": ws.title}
            k += 1
            while k < len(rows):
                r = rows[k]
                name = r[label] if label < len(r) else None
                if not isinstance(name, str) or not name.strip():
                    break
                pts = [int(r[i]) if isinstance(r[i], (int, float)) else 0 for i in cols]
                block["boats"][name.strip()] = pts
                if tot is not None and isinstance(r[tot], (int, float)) and int(r[tot]) != sum(pts):
                    problems.append(f"{path.name} {ws.title}: {name.strip()} total {int(r[tot])} is not the sum of its races ({sum(pts)})")
                k += 1
            j = k                                   # weather line just under the block (blank rows allowed)
            while j < len(rows) and j < k + 3 and all(c in (None, "") for c in rows[j]):
                j += 1
            if j < len(rows):
                w = [c for c in rows[j] if isinstance(c, str) and c.strip()]
                if len(w) == 1 and not _as_date(w[0]) and not any(isinstance(c, (int, float)) for c in rows[j]):
                    block["weather"] = w[0].strip()
            if block["date"] is None:
                problems.append(f"{path.name} {ws.title}: a {cls} table has no date above it, skipped")
            elif block["boats"]:
                blocks.append(block)
        # tables for other classes are skipped by the loop above
    return blocks, problems


def load(results_dir: Path, sailed_days: list[str], cls: str = "MELGES 15", rename=None) -> tuple[dict, list[str]]:
    """Official results by racing day: {"YYYY-MM-DD": block}. A sheet date within three days of a day
    with uploads is moved onto that day (scoring sheets are often dated the day they were typed up).
    `rename` maps a sheet name ("270 Glackin") to the site's name for that boat."""
    out, problems = {}, []
    if not results_dir or not Path(results_dir).is_dir():
        return out, problems
    sailed = [date.fromisoformat(d) for d in sailed_days]
    for p in sorted(Path(results_dir).glob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        try:
            blocks, probs = read_workbook(p, cls)
        except Exception as e:  # a damaged or odd workbook: report it, keep building
            problems.append(f"{p.name}: could not be read ({e})")
            continue
        problems += probs
        for b in blocks:
            d = b["date"]
            near = [s for s in sailed if abs((s - d).days) <= 3]
            if d not in sailed and near:
                d = min(near, key=lambda s: abs((s - d).days))
            if rename:
                b["boats"] = {rename(n): v for n, v in b["boats"].items()}
            b["sheet_date"] = b["date"].isoformat()
            b["date"] = d.isoformat()
            out[b["date"]] = b                      # a later workbook replaces an earlier one
    return out, problems
