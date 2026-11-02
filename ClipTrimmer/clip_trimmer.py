#!/usr/bin/env python3
"""Clip Trimmer: cut a short clip out of a long recording (e.g. NVIDIA instant replays).

Usage:
    python clip_trimmer.py [video file]

With no file, a file picker opens. The editor opens in your web browser; this
window must stay open while you use it. Needs Python 3.8+ and ffmpeg/ffprobe
on PATH. No other packages.
"""
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
TOKEN = secrets.token_urlsafe(16)
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4",
               ".mkv": "video/webm", ".webm": "video/webm"}
DISCORD_LIMITS = {10: "10 MB (free)", 50: "50 MB (Nitro Basic)", 500: "500 MB (Nitro)"}

state = {"path": None, "info": None, "version": 0}
job = {"state": "idle", "progress": 0.0, "message": "", "output": None}
job_lock = threading.Lock()
dialog_requests = queue.Queue()  # file dialogs must run on the main thread


# ---------------------------------------------------------------- ffmpeg helpers

def probe(path):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("No video stream found in that file.")
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": round(fps, 3),
        "codec": video.get("codec_name"),
        "audio_tracks": sum(1 for s in streams if s.get("codec_type") == "audio"),
    }


def load_file(path):
    path = Path(path).expanduser().resolve()
    info = probe(path)
    state.update(path=path, info=info, version=state["version"] + 1)


def unique_output(src, start, end, suffix):
    def stamp(t):
        return f"{int(t // 60)}m{int(t % 60):02d}s"
    # Build names as plain strings: NVIDIA names like "Game 2026.11.02 - 20.15.33.01" are full of dots.
    base = f"{src.stem}_{stamp(start)}-{stamp(end)}{suffix}"
    out, n = src.with_name(base + ".mp4"), 2
    while out.exists():
        out = src.with_name(f"{base} ({n}).mp4")
        n += 1
    return out


def run_ffmpeg(args, duration, lo, hi):
    """Run ffmpeg, mapping its progress onto job['progress'] between lo and hi."""
    proc = subprocess.Popen([FFMPEG, "-hide_banner", "-v", "error", "-y", "-nostats",
                             "-progress", "pipe:1", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
        if m and duration > 0:
            job["progress"] = lo + (hi - lo) * min(1.0, int(m.group(1)) / 1e6 / duration)
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else "ffmpeg failed")


def export(mode, start, end, limit_mb):
    src, info = state["path"], state["info"]
    duration = end - start
    seek = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}"]
    tracks = info["audio_tracks"]

    if mode == "original":
        out = unique_output(src, start, end, "")
        job["message"] = "Exporting at original quality…"
        run_ffmpeg([*seek, "-map", "0:v:0", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
                   duration, 0, 1)
        return out

    # Discord: two-pass encode aimed just under the upload limit.
    limit_bytes = limit_mb * 1000 * 1000
    audio_k = 128 if limit_mb > 10 or duration < 40 else 96
    if not tracks:
        audio_k = 0
    video_k = limit_bytes * 8 * 0.94 / duration / 1000 - audio_k
    if video_k < 150:
        raise ValueError(f"That selection is too long to fit in {limit_mb} MB. Shorten it or pick a larger limit.")
    out = unique_output(src, start, end, "_discord")

    with tempfile.TemporaryDirectory() as tmp:
        for attempt in range(3):
            height = 1080 if video_k >= 3000 else 720 if video_k >= 1200 else 480
            vf = f"scale=-2:'min(ih,{height})'"
            if (info["fps"] or 0) > 31 and video_k < 2000:
                vf += ",fps=30"
            # Discord only plays the first audio track, so NVIDIA's separate mic track gets mixed in.
            vgraph = f"[0:v:0]{vf}[v]"
            if tracks > 1:
                mix = "".join(f"[0:a:{i}]" for i in range(tracks)) + f"amix=inputs={tracks}:normalize=0[a]"
                audio = ["-filter_complex", f"{vgraph};{mix}", "-map", "[v]", "-map", "[a]"]
            elif tracks == 1:
                audio = ["-filter_complex", vgraph, "-map", "[v]", "-map", "0:a:0"]
            else:
                audio = ["-filter_complex", vgraph, "-map", "[v]", "-an"]
            if tracks:
                audio += ["-c:a", "aac", "-b:a", f"{audio_k}k"]
            video = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{int(video_k)}k",
                     "-pix_fmt", "yuv420p", "-passlogfile", os.path.join(tmp, "pass")]
            label = f"Compressing for Discord ({height}p, {int(video_k)} kbps)"
            job["message"] = label + ": pass 1 of 2…"
            run_ffmpeg([*seek, "-filter_complex", vgraph, "-map", "[v]", *video, "-pass", "1", "-an",
                        "-f", "mp4", os.devnull], duration, 0, 0.5)
            job["message"] = label + ": pass 2 of 2…"
            run_ffmpeg([*seek, *audio, *video, "-pass", "2", "-movflags", "+faststart", str(out)],
                       duration, 0.5, 1)
            size = out.stat().st_size
            if size <= limit_bytes:
                return out
            video_k *= limit_bytes / size * 0.93
            job["message"] = "Slightly over the limit, trying again smaller…"
    raise RuntimeError(f"Could not get the clip under {limit_mb} MB. Try a shorter selection.")


def export_thread(mode, start, end, limit_mb):
    try:
        out = export(mode, start, end, limit_mb)
        size = out.stat().st_size / 1e6
        job.update(state="done", progress=1.0, output=str(out),
                   message=f"Saved {out.name} ({size:.1f} MB)")
    except Exception as e:  # report anything back to the page
        job.update(state="error", message=str(e))


def reveal(path):
    path = Path(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def pick_file():
    start_dir = Path.home() / "Videos"
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askopenfilename(
            title="Choose a recording", initialdir=start_dir if start_dir.is_dir() else Path.home(),
            filetypes=[("Videos", "*.mp4 *.mkv *.mov *.m4v *.webm"), ("All files", "*.*")])
        root.destroy()
        return chosen or None
    except Exception:
        chosen = input("Path to the video file: ").strip().strip('"')
        return chosen or None


# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self, query):
        return secrets.compare_digest(self.headers.get("X-Token") or query.get("t", [""])[0], TOKEN)

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self.authorized(query):
            return self.send_error(403, "Open the link printed in the Clip Trimmer window.")
        if url.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/video":
            self.send_video()
        elif url.path == "/api/state":
            path = state["path"]
            self.send_json({"name": path.name if path else None, "folder": str(path.parent) if path else None,
                            "version": state["version"], "info": state["info"], "job": job,
                            "limits": DISCORD_LIMITS})
        else:
            self.send_error(404)

    def send_video(self):
        path = state["path"]
        if not path:
            return self.send_error(404)
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                return self.end_headers()
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", VIDEO_TYPES.get(path.suffix.lower(), "video/mp4"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser cancels requests all the time while seeking

    def do_POST(self):
        url = urlparse(self.path)
        if not self.authorized({}) or self.headers.get("Content-Type") != "application/json":
            return self.send_error(403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send_json({"error": "Bad request"}, 400)

        if url.path == "/api/export":
            if not state["path"]:
                return self.send_json({"error": "No video loaded."}, 400)
            try:
                start, end = float(body["start"]), float(body["end"])
                mode = body["mode"]
                limit = int(body.get("limit", 10))
            except (KeyError, TypeError, ValueError):
                return self.send_json({"error": "Bad request"}, 400)
            duration = state["info"]["duration"]
            start, end = max(0.0, start), min(end, duration) if duration else end
            if mode not in ("original", "discord") or limit not in DISCORD_LIMITS:
                return self.send_json({"error": "Unknown export option."}, 400)
            if end - start < 0.1:
                return self.send_json({"error": "The end needs to be after the start."}, 400)
            with job_lock:
                if job["state"] == "running":
                    return self.send_json({"error": "An export is already running."}, 409)
                job.update(state="running", progress=0.0, message="Starting…", output=None)
            threading.Thread(target=export_thread, args=(mode, start, end, limit), daemon=True).start()
            self.send_json({"ok": True})
        elif url.path == "/api/open":
            if job["state"] == "running":
                return self.send_json({"error": "Wait for the export to finish."}, 409)
            reply = queue.Queue()
            dialog_requests.put(reply)
            result = reply.get()
            if isinstance(result, Exception):
                return self.send_json({"error": str(result)}, 400)
            self.send_json({"ok": True, "changed": bool(result)})
        elif url.path == "/api/reveal":
            if job.get("output"):
                reveal(job["output"])
            self.send_json({"ok": True})
        elif url.path == "/api/quit":
            self.send_json({"ok": True})
            dialog_requests.put(None)
        else:
            self.send_error(404)


def main():
    if not FFMPEG or not FFPROBE:
        print("ffmpeg and ffprobe need to be installed and on your PATH.")
        print("Windows:  winget install Gyan.FFmpeg   (then open a new terminal)")
        print("macOS:    brew install ffmpeg")
        input("Press Enter to close.")
        return 1

    path = sys.argv[1] if len(sys.argv) > 1 else pick_file()
    if not path:
        print("No file chosen.")
        return 0
    try:
        load_file(path)
    except Exception as e:
        print(f"Couldn't open {path}: {e}")
        input("Press Enter to close.")
        return 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/?t={TOKEN}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Clip Trimmer is open in your browser. If it isn't, visit:\n  {url}")
    print("Keep this window open while you work. Close it (or press Ctrl+C) to quit.")
    if not os.environ.get("CLIP_TRIMMER_NO_BROWSER"):
        webbrowser.open(url)

    try:
        while True:
            try:
                reply = dialog_requests.get(timeout=0.5)
            except queue.Empty:
                continue
            if reply is None:  # quit
                break
            try:
                chosen = pick_file()
                if chosen:
                    load_file(chosen)
                    job.update(state="idle", progress=0.0, message="", output=None)
                reply.put(chosen)
            except Exception as e:
                reply.put(e)
    except KeyboardInterrupt:
        pass
    server.shutdown()
    return 0


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clip Trimmer</title>
<style>
  :root { color-scheme: light dark; --bg: #f6f4ef; --fg: #222; --muted: #666; --line: #ccc; --accent: #2f6f4f; --sel: #2f6f4f55; }
  @media (prefers-color-scheme: dark) { :root { --bg: #1d1d1b; --fg: #eee; --muted: #aaa; --line: #444; --accent: #6cc196; --sel: #6cc19655; } }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.4 system-ui, sans-serif; }
  main { max-width: 1000px; margin: 0 auto; padding: 16px; }
  header { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
  h1 { font-size: 1.2rem; margin: 0; }
  #file { color: var(--muted); overflow-wrap: anywhere; flex: 1; }
  video { width: 100%; max-height: 60vh; background: #000; margin-top: 12px; display: block; }
  #bar { position: relative; height: 22px; background: var(--line); margin: 8px 0 12px; cursor: pointer; border-radius: 3px; }
  #range { position: absolute; top: 0; bottom: 0; background: var(--sel); border-left: 2px solid var(--accent); border-right: 2px solid var(--accent); }
  #head { position: absolute; top: -3px; bottom: -3px; width: 2px; background: var(--fg); }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 8px 0; }
  label { display: flex; gap: 6px; align-items: center; }
  input, select, button { font: inherit; color: inherit; background: transparent; border: 1px solid var(--line); border-radius: 4px; padding: 5px 9px; }
  input { width: 7.5em; }
  button { cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.main { background: var(--accent); color: var(--bg); border-color: var(--accent); }
  button:disabled { opacity: .5; cursor: default; }
  .muted { color: var(--muted); }
  progress { width: 220px; }
  #error { color: #c0392b; }
  kbd { border: 1px solid var(--line); border-radius: 3px; padding: 0 4px; font-size: .85em; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Clip Trimmer</h1>
    <span id="file"></span>
    <button id="open">Open another…</button>
    <button id="quit">Quit</button>
  </header>
  <video id="video" controls preload="auto"></video>
  <p id="codec" class="muted" hidden>Your browser can't preview this video's format, but you can still type times below and export.</p>
  <div id="bar" title="Click to seek"><div id="range"></div><div id="head"></div></div>

  <div class="row">
    <label>Start <input id="start" value="0:00.00"></label>
    <button id="setStart">Set start <kbd>I</kbd></button>
    <label>End <input id="end"></label>
    <button id="setEnd">Set end <kbd>O</kbd></button>
    <button id="last30" title="Use the 30 seconds before the playhead">30s before here</button>
    <button id="preview">Play selection</button>
    <span id="length" class="muted"></span>
  </div>

  <div class="row">
    <button class="main" id="exportOriginal">Export original quality</button>
    <button class="main" id="exportDiscord">Export for Discord</button>
    <select id="limit"></select>
  </div>

  <div class="row" id="status" hidden>
    <progress id="progress" max="1" value="0"></progress>
    <span id="message"></span>
    <button id="reveal" hidden>Show in folder</button>
  </div>
  <p id="error"></p>
  <p class="muted">Keys: <kbd>Space</kbd> play/pause · <kbd>←</kbd>/<kbd>→</kbd> 1s (<kbd>Shift</kbd> 5s) · <kbd>,</kbd>/<kbd>.</kbd> one frame · <kbd>I</kbd>/<kbd>O</kbd> set start/end.
  Clips are saved next to the original.</p>
</main>
<script>
const T = new URLSearchParams(location.search).get('t');
const $ = id => document.getElementById(id);
const video = $('video');
let info = null, version = -1, start = 0, end = 0, previewing = false, polling = null;

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'X-Token': T, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function fmt(t) {
  const m = Math.floor(t / 60), s = t - m * 60;
  return m + ':' + s.toFixed(2).padStart(5, '0');
}
function parse(text) {
  const parts = String(text).trim().split(':').map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((acc, n) => acc * 60 + n, 0);
}
function duration() { return (info && info.duration) || video.duration || 0; }

function render() {
  $('start').value = fmt(start);
  $('end').value = fmt(end);
  const d = duration(), len = end - start;
  $('length').textContent = len > 0 ? 'Length ' + fmt(len) : 'End must be after start';
  if (d) {
    $('range').style.left = (start / d * 100) + '%';
    $('range').style.width = (Math.max(0, len) / d * 100) + '%';
  }
}
function setStart(t) { start = Math.max(0, Math.min(t, duration())); if (end <= start) end = Math.min(duration(), start + 30); render(); }
function setEnd(t) { end = Math.max(0, Math.min(t, duration())); if (end <= start) start = Math.max(0, end - 30); render(); }

async function load() {
  const s = await api('/api/state');
  if (s.version !== version) {
    version = s.version; info = s.info;
    $('file').textContent = s.name + ' · ' + info.width + '×' + info.height + ' · ' + Math.round(info.fps) + ' fps' +
      (info.audio_tracks > 1 ? ' · ' + info.audio_tracks + ' audio tracks' : '');
    $('limit').innerHTML = Object.entries(s.limits).map(([mb, label]) => `<option value="${mb}">${label}</option>`).join('');
    $('codec').hidden = true;
    video.src = '/video?t=' + encodeURIComponent(T) + '&v=' + version;
    start = Math.max(0, info.duration - 30); end = info.duration;
    render();
  }
  showJob(s.job);
}

function showJob(job) {
  const running = job.state === 'running';
  $('exportOriginal').disabled = $('exportDiscord').disabled = $('open').disabled = running;
  $('status').hidden = job.state === 'idle' || job.state === 'error';
  $('progress').value = job.progress;
  $('progress').hidden = !running;
  $('message').textContent = job.message;
  $('reveal').hidden = job.state !== 'done';
  $('error').textContent = job.state === 'error' ? job.message : '';
  if (running && !polling) polling = setInterval(poll, 500);
  if (!running && polling) { clearInterval(polling); polling = null; }
}
async function poll() { try { showJob((await api('/api/state')).job); } catch (e) { $('error').textContent = 'Lost connection to Clip Trimmer. Is its window still open?'; } }

async function exportClip(mode) {
  $('error').textContent = '';
  if (end - start < 0.1) { $('error').textContent = 'The end needs to be after the start.'; return; }
  video.pause();
  try {
    await api('/api/export', { mode, start, end, limit: Number($('limit').value) });
    showJob({ state: 'running', progress: 0, message: 'Starting…' });
  } catch (e) { $('error').textContent = e.message; }
}

video.addEventListener('timeupdate', () => {
  const d = duration();
  if (d) $('head').style.left = (video.currentTime / d * 100) + '%';
  if (previewing && video.currentTime >= end) { video.pause(); previewing = false; }
});
video.addEventListener('pause', () => { previewing = false; });
video.addEventListener('error', () => { $('codec').hidden = false; });

$('bar').addEventListener('click', e => {
  const r = $('bar').getBoundingClientRect();
  video.currentTime = (e.clientX - r.left) / r.width * duration();
});
$('setStart').onclick = () => setStart(video.currentTime);
$('setEnd').onclick = () => setEnd(video.currentTime);
$('last30').onclick = () => { end = Math.min(duration(), video.currentTime || duration()); start = Math.max(0, end - 30); render(); };
$('preview').onclick = () => { video.currentTime = start; previewing = true; video.play(); };
for (const id of ['start', 'end']) {
  $(id).addEventListener('change', () => {
    const t = parse($(id).value);
    if (t === null) return render();
    (id === 'start' ? setStart : setEnd)(t);
    video.currentTime = id === 'start' ? start : end;
  });
}
$('exportOriginal').onclick = () => exportClip('original');
$('exportDiscord').onclick = () => exportClip('discord');
$('reveal').onclick = () => api('/api/reveal', {});
$('open').onclick = async () => {
  $('error').textContent = '';
  try { await api('/api/open', {}); await load(); } catch (e) { $('error').textContent = e.message; }
};
$('quit').onclick = async () => {
  await api('/api/quit', {}).catch(() => {});
  document.body.innerHTML = '<main><h1>Clip Trimmer closed</h1><p>You can close this tab.</p></main>';
};

document.addEventListener('keydown', e => {
  if (e.target.matches('input, select') || e.ctrlKey || e.metaKey || e.altKey) return;
  const frame = 1 / ((info && info.fps) || 60);
  const actions = {
    ' ': () => video.paused ? video.play() : video.pause(),
    'ArrowLeft': () => video.currentTime -= e.shiftKey ? 5 : 1,
    'ArrowRight': () => video.currentTime += e.shiftKey ? 5 : 1,
    ',': () => { video.pause(); video.currentTime -= frame; },
    '.': () => { video.pause(); video.currentTime += frame; },
    'i': () => setStart(video.currentTime),
    'o': () => setEnd(video.currentTime),
  };
  const action = actions[e.key.length === 1 ? e.key.toLowerCase() : e.key];
  if (action) { e.preventDefault(); action(); }
});

load().catch(e => { $('error').textContent = e.message; });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
