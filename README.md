# Frostbite Replays

Race replays and debriefs for the fleet, built from Vakaros Atlas logs.

**The website** is published from this repository with GitHub Pages. Every time a `.vkx` file is added to `logs/`, the site rebuilds itself in about two minutes.

## Add your race

1. After sailing, open **Vakaros Connect**, pick the session, and export it as a `.vkx` file.
2. Keep its name (`Your Name_s_Vakaros_11-8-2026.vkx`). It is matched to your boat in `fleet.json` by surname, or put your sail number in the name, like `302.vkx`.
3. Open the [`logs`](logs) folder, choose **Add file → Upload files**, drag your file in and press **Commit changes**.

Files from every boat on the same day are put together automatically: one page per day, one tab per race, everyone raced against everyone.

You need a free GitHub account. If you are not a collaborator yet, GitHub offers to make your upload a pull request. It goes live once it is merged.

**No GitHub account?** The fleet uploader in [`uploader/`](uploader) lets sailors email their file straight from the Vakaros Connect share button, or use a simple upload page. See its README to set it up.

## The fleet

`fleet.json` lists the Melges 15 fleet by sail number, as the club scores it (Frostbite SI 13.6). It starts with the boats from the 2025–26 results. Uploads are matched to a boat by, in order:

1. an Atlas serial number in `serials` (the downloader names files by serial)
2. a sail number in the file name, like `302.vkx`
3. a surname or alias, so Vakaros Connect's own file names (`Max_Hafen_s_Vakaros_...`) work as they are

Anything else shows under its file name. Add each boat's Atlas serial once and its files always land in the right place.

## Heel smoothing, wind speed and pace

- **Heel** is averaged over a window set by the sea state: how much the bow pitches (slow trends removed) on each boat in each race, from 1 s at ±0.8° (flat water) to 4 s at ±2.3° (a chop). With no pitch data, wind speed sets it (1 s at 6 kt to 4 s at 16 kt). The logs are 2 Hz, too slow to measure the wave period itself. Settings: `HEEL_SMOOTH_BY_PITCH` and `HEEL_SMOOTH_BY_WIND` in `sailreplay/analysis.py`.
- **Wind speed** from boat speed: upwind speed barely changes once a Melges 15 is powered up, so when a boat's downwind legs (at least a minute of them) say 10 kt or more, its upwind readings are left out. Downwind speeds past the target table count as at least that wind, up to 18 kt, rather than being dropped.
- **Which side paid** is now against the fleet's own average on that point of sail (100%), so the sides compare directly whatever the target speeds get wrong. Per-boat and per-leg pace are still against target speed.

## Steady sailing and the Advanced tab

Averages (upwind and downwind speed, VMG, wind angle and heel, per race and per leg), the heel-vs-VMG chart, pace and the wind speed read from boat speed all use **steady sailing** only. Each tack is left out from 5 s before head to wind to 12 s after, each gybe from 6 s before to 15 s after (longer when a boat was slow to get back up to speed), plus mark roundings, the first 20 s after the gun and the last 5 s before the finish. The windows are `MANOEUVRE_WINDOW_S` in `sailreplay/analysis.py`. Distance sailed, elapsed time and made good still use every second.

Each day page has an **Advanced** tab that lays every race over each other, with race and boat filters:

- Tack and gybe windows: VMG, speed, wind angle and heel from 10 s before to 30 s after, averaged per boat
- Every start, centred on the middle of each line and turned so each race's wind at the gun is up (the default) or with a level line, course up; with play, a time slider and looping; colour by finish place to look for a winning pattern
- Every tack and every gybe overlaid at head to wind or dead downwind. Each is turned so the wind is halfway between two straight lines, the track coming in and the track going out, each followed from close to the turn for as long as the boat held that course (to its previous or next manoeuvre, a 25° wander, or 60 s), so back-to-back tacks stay square; if one side is too short to read, the other side and the fleet's median turn angle stand in. Mirroring to one side is optional, a button steps the time shown after the turn through 30, 20 and 10 s, and the tack (or gybe) that made the most distance upwind (downwind) by the end is drawn in gold, in its boat's colour
- Every windward or leeward rounding overlaid on the mark, with the three-length zone, the one furthest towards the next mark 30 s after in gold, speed and wind angle through it and a table of entry and exit speeds
- Upwind and downwind VMG by heel (1° bands; bands with under 6 s of data are drawn dashed) across all the races picked, with a gold line at the ideal heel: where the boats' pooled VMG peaks

## Season, scoring and courses (`site.json`)

```json
"series": [
  {"name": "Fall ’26 Series", "start": "2026-11-08"},
  {"name": "Spring ’27 Series", "start": null}
],
"regattas": [{"name": "New Year’s Regatta", "date": "2027-01-03"}],
"courses": {"2026-11-08": ["W", "W", "M", "W"]},
"labels": {"2026-03-22": "Test race"},
"hidden_days": ["2026-03-22"]
```

- **Series:** a series with no `start` has not begun. One with no `end` runs until the next one starts. Set the Spring start once the Sailing Instructions are out.
- **Regattas:** scored on their own and also inside their series. Each boat drops its worst race when five or more are sailed (SI 13.3).
- **Scoring:** CYC high point (SI 13.1). One point for each boat beaten plus one. DNF scores 1, as does finishing more than ten minutes after the first boat (SI 11.2). DNC scores 0. No throw-outs in a series. Qualifying takes two days sailed (SI 13.2). Only boats that uploaded are counted, so these are **preliminary**: the weekly results email is official.
- **Courses:** each race is labelled from its legs: W (windward–leeward–finish), W2 (twice around), L (windward–finish) or Z (finish downwind). A modified gold cup (M) reads as W2, because its wing-mark reach looks like a run sailed with one gybe. Set it from the committee's board under `courses` if it's used.
- **Labels** name a day that isn't part of a series. It shows by that name instead of its date and isn't scored.
- **Official results:** put the club's scoring workbook (`.xlsx`) in `results/`. Each Melges 15 table in it (a date, a `MELGES 15` row with race numbers and TOTAL, one row per boat, then an optional weather line) replaces the preliminary scores for that day, and the results page marks the day Official. Boats are matched by sail number. A sheet dated up to three days after a racing day is used for that day. Add the updated workbook each week, under the same name to replace it.
- **Hidden days:** dates in `hidden_days` are left off the home page. Their replay pages are still built at `days/<date>.html`, so a direct link keeps working.
- `title`, `club`, `timezone` and the `links` at the top of the home page are set here too.

### Recalls and penalty turns

A boat shows red (its card and its name on the map) while it looks to be sorting out a penalty:

- **OCS, going back:** soon after the start it turns back below the line (20 m or more, downwind for 12 s or more) and restarts; red until it is sailing upwind again. Being over the line at the gun without turning back is not marked. If most of the fleet goes back it is a general recall and nobody is marked. With race timers, a gun followed by another within 15 minutes, with the fleet staying within 300 m of the line in between, is treated as a general recall and the race starts at the second gun.
- **Penalty turn:** a full circle one way inside 45 seconds (two in a row count as two turns), until the boat is back up to speed. Next to a mark it shows as a mark turn.

These are guesses from GPS against estimated lines and marks, and many penalties are taken without a log showing them, so each one only stays red for a short time (at most about a minute and a half) and they never change the results.

### Leeward gate

Leeward marks are left to port, so boats round up anticlockwise. If anyone rounds the leeward mark the other way (a starboard rounding), the race is treated as having a gate: the replay shows two gate marks, placed where each group of boats rounded, and the day table marks the course "gate".

### Tide and shoreline

Each race shows whether the tide is flooding (coming in) or ebbing (going out), with bars for how strong it is, from NOAA predictions for Northport, Northport Bay (station 8515586). Predictions for March 2026 and September 2026 to May 2027 are stored in `sailreplay/tides_8515586.json`, so nothing needs to be online. For any other date the build fetches them from NOAA automatically. The faint shoreline on the map is OpenStreetMap coastline, stored in `sailreplay/coast_northport.json`. Boats turn grey once they finish.

## Run it locally

```
pip install -e .
sailreplay site logs -o _site      # build the website into _site/
sailreplay download                # pull sessions off an Atlas over Bluetooth
sailreplay day logs/*.vkx          # one day's page on its own
```

See `sailreplay/` for the parser, analysis and replay page. Tests: `python -m pytest tests`.
