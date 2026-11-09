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
