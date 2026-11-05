# Frostbite Replays

Race replays and debriefs for the fleet, built from Vakaros Atlas logs.

**The website** is published from this repository with GitHub Pages. Every time a `.vkx` file is added to `logs/`, the site rebuilds itself in about two minutes.

## Add your race

1. After sailing, open **Vakaros Connect**, pick the session, and export it as a `.vkx` file.
2. Keep its name (`Your Name_s_Vakaros_3-22-2026.vkx`) or rename it after your boat (`Mojo.vkx`). That name is what shows on the replay.
3. Open the [`logs`](logs) folder, choose **Add file → Upload files**, drag your file in and press **Commit changes**.

Files from every boat on the same day are put together automatically: one page per day, one tab per race, everyone raced against everyone.

You need a free GitHub account. If you are not a collaborator yet, GitHub offers to make your upload a pull request. It goes live once it is merged.

## Boat names and colours

`boats.json` maps an Atlas serial number to a boat name and an optional colour:

```json
{"0228005931": {"name": "Max Hafen", "color": "#e4572e"}}
```

## Series and results

`site.json` holds the series. Days are grouped under them and scored on the **Preliminary results** page.

```json
"series": [
  {"name": "Frostbite Series · Part 1", "start": "2026-11-07", "end": null, "discards": 0},
  {"name": "Frostbite Series · Part 2", "start": null, "end": null, "discards": 0}
]
```

- A series with no `start` has not begun. One with no `end` runs until the next series starts.
- `discards` is the number of worst scores each boat drops.
- `labels` marks days that are not part of a series, such as `"2026-03-22": "Test upload"`.
- Scoring is low point. Boats that uploaded but did not finish a race score DNF; boats with no upload that day score DNC. Both score entries + 1. The official results in the weekly email take priority.

## Site settings

`site.json` also sets the title, the club line and the time zone used to group races into days.

## Run it locally

```
pip install -e .
sailreplay site logs -o _site      # build the website into _site/
sailreplay download                # pull sessions off an Atlas over Bluetooth
sailreplay day logs/*.vkx          # one day's page on its own
```

See `sailreplay/` for the parser, analysis and replay page. Tests: `python -m pytest tests`.
