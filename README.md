# Frostbite Replays

Race replays and debriefs for the fleet, built from Vakaros Atlas logs.

**The website** is published from this repository with GitHub Pages. Every time a `.vkx` file is added to `logs/`, the site rebuilds itself in about two minutes.

## Add your race

1. After sailing, open **Vakaros Connect**, pick the session, and export it as a `.vkx` file.
2. Keep its name (`Your Name_s_Vakaros_11-8-2026.vkx`). It is matched to your boat in `fleet.json` by surname, or put your sail number in the name, like `302.vkx`.
3. Open the [`logs`](logs) folder, choose **Add file → Upload files**, drag your file in and press **Commit changes**.

Files from every boat on the same day are put together automatically: one page per day, one tab per race, everyone raced against everyone.

You need a free GitHub account. If you are not a collaborator yet, GitHub offers to make your upload a pull request. It goes live once it is merged.

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
"labels": {"2026-03-22": "Test race"}
```

- **Series:** a series with no `start` has not begun. One with no `end` runs until the next one starts. Set the Spring start once the Sailing Instructions are out.
- **Regattas:** scored on their own and also inside their series. Each boat drops its worst race when five or more are sailed (SI 13.3).
- **Scoring:** CYC high point (SI 13.1). One point for each boat beaten plus one. DNF scores 1, as does finishing more than ten minutes after the first boat (SI 11.2). DNC scores 0. No throw-outs in a series. Qualifying takes two days sailed (SI 13.2). Only boats that uploaded are counted, so these are **preliminary**: the weekly results email is official.
- **Courses:** each race is labelled from its legs: W (windward–leeward–finish), W2 (twice around), L (windward–finish) or Z (finish downwind). A modified gold cup (M) reads as W2, because its wing-mark reach looks like a run sailed with one gybe. Set it from the committee's board under `courses` if it's used.
- **Labels** name a day that isn't part of a series. It shows by that name instead of its date and isn't scored.
- `title`, `club`, `timezone` and the `links` at the top of the home page are set here too.

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
