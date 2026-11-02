# OBS setup for automatic clips

This sets OBS up as the capture engine for the clip agent: a rolling replay
buffer for moments, full recordings that the agent starts and stops for
MTG Arena matches, and separate audio tracks for game, mic and voice chat.
Menu names are from OBS 30/31 on Windows and may differ slightly.

## 1. Output (Settings → Output, set *Output Mode* to **Advanced**)

**Recording tab**

| Setting | Value | Why |
| --- | --- | --- |
| Type | Standard | |
| Recording Path | A folder on a drive with plenty of space, e.g. `D:\Recordings` | The agent picks files up from here |
| Recording Format | **Matroska Video (.mkv)** | Survives crashes; the agent converts to MP4 when it renames files |
| Audio Track | Tick **1, 2 and 3** | Game, mic, voice chat |
| Video Encoder | NVIDIA NVENC H.264 | Uses the graphics card, like the NVIDIA overlay did |
| Rate Control | CQP, **CQ Level 20** | Constant quality: still card screens get small files, busy fights get bits |
| Keyframe Interval | 2 s | Precise, fast cutting later |
| Preset | P5: Slow (Good Quality) | Drop to P4 if games stutter |

**Audio tab:** name Track 1 `Game`, Track 2 `Mic`, Track 3 `Voice chat`, all 192 kbps.

**Replay Buffer tab:** tick *Enable Replay Buffer*, Maximum Replay Time **300 s**
(5 minutes), Maximum Memory **3000 MB**.

## 2. Video (Settings → Video)

Base and Output resolution: your monitor's resolution. FPS: **60**.

## 3. Sources (one scene, e.g. "Gaming")

| Source | Settings | Audio track |
| --- | --- | --- |
| Game Capture "Arena" | Mode: capture specific window → `MTGA.exe` | |
| Game Capture "WoW" | Mode: capture specific window → the WoW Forever window | |
| Application Audio Capture "Arena audio" | Window: MTGA | 1 only |
| Application Audio Capture "WoW audio" | Window: WoW | 1 only |
| Application Audio Capture "Discord" | Window: Discord | 3 only |
| Mic/Aux (already there) | your microphone | 2 only |
| Desktop Audio (already there) | **disable it** (or untick all tracks) | none |

Set the tracks in **Edit → Advanced Audio Properties**: each source has
checkboxes 1–6 under *Tracks*. Desktop Audio would mix Discord into the game
track, which is why it's turned off. Only the game that's running shows up, so
both Game Captures can stay in the one scene.

## 4. Remote control (Tools → WebSocket Server Settings)

Tick **Enable WebSocket server**, keep port **4455**, keep authentication on,
then **Show Connect Info** and copy the password. The clip agent uses it to
save the replay buffer and start/stop recordings.

## 5. Hotkeys (Settings → Hotkeys)

- **Replay Buffer → Save Replay:** pick a key that doesn't clash with the
  NVIDIA overlay (Alt+F10), e.g. **Ctrl+Shift+S**.
- The agent starts the replay buffer itself, so no hotkey is needed for that.

## 6. MTG Arena

In Arena: **Settings → Account → Detailed Logs (Plugin Support)**, then
restart Arena. Without this, the log the agent reads is nearly empty.

## 7. Check it

1. Start the replay buffer (Controls panel), play for a few minutes, press the
   Save Replay hotkey.
2. Open the saved file in Clip Trimmer: the top line should say
   **3 audio tracks**.
3. Once OBS is working, turn off the NVIDIA App's Instant Replay so you're not
   recording everything twice.
