# Clip Trimmer

A tiny desktop tool for cutting a short clip out of a long recording, like the
5-minute NVIDIA instant replays. Pick a start and end, then export at original
quality or squeezed under Discord's upload limit.

## Setup (once)

1. Install [Python 3](https://www.python.org/downloads/). On Windows, tick
   "Add python.exe to PATH" in the installer.
2. Install ffmpeg:
   - Windows: `winget install Gyan.FFmpeg`, then open a new terminal
   - macOS: `brew install ffmpeg`

No other packages are needed.

## Use

- **Windows:** drag a video onto `Clip Trimmer.bat`, or double-click it to pick a file.
- **Anywhere:** `python clip_trimmer.py "path/to/video.mp4"`

The editor opens in your browser. Keep the small terminal window open while
you work; closing it (or pressing **Quit**) stops the tool.

1. Scrub to the moment. The selection starts as the last 30 seconds, since
   that's usually where the replay moment is.
2. Press **I** / **O** (or the buttons) to set the start and end, or type times
   such as `3:12.5`. **30s before here** selects the 30 seconds before the playhead.
3. **Play selection** to check it.
4. Optionally type a **clip name**, like `Triple kill`, then export.

Clips go into a `Clips` folder inside your Videos folder (created if needed),
with a subfolder per game taken from NVIDIA's filename:

```
Videos/Clips/Apex Legends/Triple kill.mp4
Videos/Clips/Apex Legends/Triple kill - Discord.mp4
```

With no name, the clip is named after the recording and the times, e.g.
`Apex Legends 2026.11.02 - 20.15.33.01_3m12s-3m42s.mp4`. Existing files are
never overwritten; a `(2)` is added instead. Files that don't follow NVIDIA's
naming go into `Clips/Other`. To save somewhere else, set the
`CLIP_TRIMMER_FOLDER` environment variable to the folder you want.

## Cutting idle time

The drop-down next to the export buttons can remove pauses automatically. It's
made for card games like MTG Arena, where a lot of a recording is thinking and
reading. It works with either export button.

| Option | What it cuts |
| --- | --- |
| Keep everything | Nothing. |
| Cut idle time | Any stretch over a second where the screen is still, such as thinking or reading, leaving a short beat so cuts don't feel abrupt. |
| Cut idle time - Aggressive | Also cuts mouse movement and hovering (heading for End Turn, reading a card), and shrinks pop-up windows such as Graveyard or Choose One to a quick flash as they open and close. A dark screen at the very end, such as Victory or Defeat, is kept. |

It works by measuring how much of the screen changes ten times a second, so
background shimmer and sparkle are ignored. Sudden game sounds count as action
too: an attack's sound keeps the attacker moving forward just before it. Only
the first audio track is used, so a separate mic track doesn't count. On a
2:18 Arena game, *Cut idle time* gave 1:42 and *Aggressive* gave 1:09. The finished message says how much
was cut. Background music jumps at each cut; a tiny fade stops clicks.

| Export | What you get |
| --- | --- |
| Original quality | Same resolution, frame rate and audio tracks, cut to the exact frame. Quick, but files are large. |
| For Discord | Two-pass H.264 sized to fit 20 MB (free), 50 MB or 500 MB (Nitro). Drops to 720p/480p or 30 fps only when the budget is tight. If you record your mic on a separate track, it's mixed in, because Discord only plays the first audio track. |

A 30-second 1080p60 clip fits in 20 MB at full 1080p60.

## Notes

- Preview uses your browser's video player. NVIDIA's H.264 recordings work
  everywhere. HEVC/AV1 recordings preview in Chrome or Edge on most recent
  PCs. If the preview won't play, typed times still export fine.
- The tool only listens on your own computer (`127.0.0.1`) and needs the
  one-time link it opens, so other sites and machines can't use it.
