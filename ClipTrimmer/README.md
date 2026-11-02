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
3. **Play selection** to check it, then export.

Clips are saved next to the original, e.g. `Game 2026.11.02 - 20.15.33.01_3m12s-3m42s.mp4`.

| Export | What you get |
| --- | --- |
| Original quality | Same resolution, frame rate and audio tracks, cut to the exact frame. Quick, but files are large. |
| For Discord | Two-pass H.264 sized to fit 10 MB (free), 50 MB or 500 MB (Nitro). Drops to 720p/480p or 30 fps only when the budget is tight. If you record your mic on a separate track, it's mixed in, because Discord only plays the first audio track. |

A 30-second 1080p60 clip fits in 10 MB at 720p60.

## Notes

- Preview uses your browser's video player. NVIDIA's H.264 recordings work
  everywhere. HEVC/AV1 recordings preview in Chrome or Edge on most recent
  PCs. If the preview won't play, typed times still export fine.
- The tool only listens on your own computer (`127.0.0.1`) and needs the
  one-time link it opens, so other sites and machines can't use it.
