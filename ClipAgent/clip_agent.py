#!/usr/bin/env python3
"""Clip Agent: runs in the background next to OBS and files your captures automatically.

    python clip_agent.py            run (keep this window open)
    python clip_agent.py --forget   forget the saved OBS password

Keeps OBS's replay buffer running and files each saved replay under Videos/Captures/<Game>/.
Records every MTG Arena match (start to finish, from Arena's log), names it with the opponent
and result, saves the match's event timeline beside it, and makes a trimmed copy in Clips.
Needs Python 3.8+, ffmpeg on PATH, and OBS's WebSocket server enabled (see OBS_SETUP.md).
"""
import base64
import getpass
import hashlib
import json
import os
import queue
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HOST, PORT = "127.0.0.1", 4455
CREDENTIAL_NAME = "ClipAgent/OBS"
FFMPEG = shutil.which("ffmpeg")

# Game executables -> the folder/file name used for captures. Names match the NVIDIA style
# ("<Game> <date> <time>") so Clip Trimmer sorts exported clips into the same game folders.
GAMES = {
    "mtga": "Magic The Gathering Arena",
    "wowclassic": "World of Warcraft Classic",
    "wow": "World of Warcraft",
}


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


# ---------------------------------------------------------------- password storage

def _windows_credentials():
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    return ctypes, CREDENTIAL, advapi


def load_password():
    if os.environ.get("CLIP_AGENT_OBS_PASSWORD") is not None:
        return os.environ["CLIP_AGENT_OBS_PASSWORD"]
    if sys.platform != "win32":
        return None
    ctypes, CREDENTIAL, advapi = _windows_credentials()
    found = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(CREDENTIAL_NAME, 1, 0, ctypes.byref(found)):  # 1 = generic credential
        return None
    try:
        blob = ctypes.string_at(found.contents.CredentialBlob, found.contents.CredentialBlobSize)
        return blob.decode("utf-16-le")
    finally:
        advapi.CredFree(found)


def save_password(password):
    if sys.platform != "win32":
        return False
    ctypes, CREDENTIAL, advapi = _windows_credentials()
    blob = password.encode("utf-16-le")
    cred = CREDENTIAL()
    cred.Type = 1                       # generic
    cred.TargetName = CREDENTIAL_NAME
    cred.UserName = "obs-websocket"
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char))
    cred.Persist = 2                    # keep across sign-ins, this PC only
    return bool(advapi.CredWriteW(ctypes.byref(cred), 0))


def forget_password():
    if sys.platform == "win32":
        ctypes, _, advapi = _windows_credentials()
        advapi.CredDeleteW(CREDENTIAL_NAME, 1, 0)


# ---------------------------------------------------------------- OBS WebSocket (protocol v5)

class OBSError(Exception):
    pass


class AuthFailed(OBSError):
    pass


class OBS:
    """Minimal obs-websocket v5 client: requests plus Outputs events, standard library only."""

    def __init__(self, password, on_event):
        self.password, self.on_event = password, on_event
        self.sock, self.pending, self.lock = None, {}, threading.Lock()
        self.buffer = b""  # bytes read past the end of the handshake
        self.closed = threading.Event()

    # --- WebSocket framing
    def _send_frame(self, text):
        data = text.encode()
        header = bytes([0x81])  # final frame, text
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = secrets.token_bytes(4)  # clients must mask
        with self.lock:
            self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _recv_exact(self, n):
        buf, self.buffer = self.buffer[:n], self.buffer[n:]
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("OBS closed the connection")
            buf += chunk
        return buf

    def _recv_message(self):
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 8:
                raise ConnectionError("OBS closed the connection")
            if opcode == 9:  # ping
                with self.lock:
                    self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + b"\0\0\0\0" + payload)
                continue
            if opcode in (0, 1, 2):
                parts.append(payload)
                if b1 & 0x80:
                    return json.loads(b"".join(parts))

    # --- connection
    def connect(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=5)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("OBS didn't answer")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("OBS refused the connection")
        self.buffer = response.split(b"\r\n\r\n", 1)[1]  # OBS's first message may already be here

        hello = self._recv_message()["d"]
        identify = {"rpcVersion": 1, "eventSubscriptions": 1 << 6}  # Outputs: recording + replay buffer
        if "authentication" in hello:
            if not self.password:
                raise AuthFailed("OBS wants a password")
            auth = hello["authentication"]
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest()).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self._send_frame(json.dumps({"op": 1, "d": identify}))
        try:
            reply = self._recv_message()
        except ConnectionError:
            raise AuthFailed("OBS rejected the password")  # it closes the socket with code 4009
        if reply.get("op") != 2:
            raise OBSError("unexpected reply from OBS")
        self.sock.settimeout(None)
        self.version = hello.get("obsWebSocketVersion", "?")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = self._recv_message()
                if msg.get("op") == 7:  # request response
                    waiter = self.pending.pop(msg["d"]["requestId"], None)
                    if waiter:
                        waiter.put(msg["d"])
                elif msg.get("op") == 5:  # event
                    try:
                        self.on_event(msg["d"]["eventType"], msg["d"].get("eventData", {}))
                    except Exception as e:
                        log(f"Problem handling {msg['d'].get('eventType')}: {e}")
        except (OSError, ValueError, ConnectionError):
            pass
        finally:
            self.closed.set()
            for waiter in list(self.pending.values()):
                waiter.put(None)

    def request(self, request_type, data=None, timeout=10):
        request_id = secrets.token_hex(8)
        waiter = queue.Queue()
        self.pending[request_id] = waiter
        self._send_frame(json.dumps({"op": 6, "d": {"requestType": request_type, "requestId": request_id,
                                                     "requestData": data or {}}}))
        try:
            reply = waiter.get(timeout=timeout)
        except queue.Empty:
            raise OBSError(f"OBS didn't answer {request_type}")
        if reply is None:
            raise ConnectionError("OBS disconnected")
        status = reply["requestStatus"]
        if not status["result"]:
            raise OBSError(f"{request_type} failed: {status.get('comment') or status.get('code')}")
        return reply.get("responseData", {})

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------- filing captures

def videos_folder():
    if sys.platform == "win32":
        try:  # the real Videos folder, even if it was moved into OneDrive
            import ctypes
            import uuid
            guid = ctypes.create_string_buffer(uuid.UUID("18989B1D-99B5-455B-841C-AB7C74E4DDFC").bytes_le, 16)
            found = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(found)) == 0:
                path = found.value
                ctypes.windll.ole32.CoTaskMemFree(found)
                return Path(path)
        except Exception:
            pass
    folder = Path.home() / ("Movies" if sys.platform == "darwin" else "Videos")
    return folder if folder.is_dir() else Path.home()


def captures_folder():
    root = os.environ.get("CLIP_AGENT_CAPTURES")
    return Path(root).expanduser() if root else videos_folder() / "Captures"


def foreground_program():
    """File name of the program in front, e.g. 'MTGA.exe' (Windows only)."""
    if sys.platform != "win32":
        return os.environ.get("CLIP_AGENT_FAKE_FOREGROUND", "")
    try:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def game_for(program):
    stem = Path(program).stem.lower()
    for key, name in GAMES.items():  # first match wins, so more specific keys come first
        if stem.startswith(key):
            return name
    return Path(program).stem or "Other"


def file_capture(source, game, when, label=""):
    """Convert an OBS capture to MP4 (no re-encode, all tracks) and file it under Captures/<game>/."""
    source = Path(source)
    folder = captures_folder() / game
    folder.mkdir(parents=True, exist_ok=True)
    base = f"{game} {when:%Y-%m-%d %H-%M-%S}" + (f" {safe_name(label)}" if label else "")
    target, n = folder / f"{base}.mp4", 2
    while target.exists():
        target, n = folder / f"{base} ({n}).mp4", n + 1
    for _ in range(20):  # OBS may still be finishing the file
        try:
            with open(source, "rb"):
                break
        except OSError:
            time.sleep(0.5)
    result = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(source), "-map", "0", "-c", "copy",
                             "-movflags", "+faststart", str(target)], capture_output=True, text=True)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        if target.exists():
            target.unlink()
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "ffmpeg failed")
    source.unlink()  # the MP4 has exactly the same contents
    return target


# ---------------------------------------------------------------- main loop

# ---------------------------------------------------------------- MTG Arena matches

ARENA = "Magic The Gathering Arena"

# MTGA profile: moments the game log marks as worth seeing, as (seconds before, seconds after).
# Idle-time cutting (aggressive) never removes these; it decides everything else.
MTGA_KEEP = {
    "castspell": (0.6, 2.0), "attack": (0.8, 2.5), "damage": (0.5, 1.5), "life": (0.3, 1.2),
    "destroy": (0.3, 1.5), "sacrifice": (0.3, 1.5), "exile": (0.3, 1.5), "countered": (0.3, 1.5),
    "turn": (0.0, 1.5), "game_over": (1.0, 6.0),
}
STOP_AFTER_MATCH = 5  # seconds of the Victory/Defeat screen to keep


def safe_name(text):
    return "".join(c for c in str(text) if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")


def trimmer():
    """Clip Trimmer's engine, from the ClipTrimmer folder next to this one."""
    folder = Path(__file__).resolve().parent.parent / "ClipTrimmer"
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    import clip_trimmer
    return clip_trimmer


class Agent:
    def __init__(self):
        self.work = queue.Queue()  # filing happens off the connection thread
        self.obs = None
        self.recording = None      # the match being recorded: {"match":…, "started": epoch seconds}
        self.record_started = threading.Event()
        self.record_stopped = queue.Queue()
        self.stop_now = threading.Event()   # cuts the post-match wait short when a new match starts
        self.record_lock = threading.Lock()

    # --- OBS events
    def on_event(self, kind, data):
        if kind == "ReplayBufferSaved":
            program = foreground_program()
            self.work.put(("replay", data["savedReplayPath"], game_for(program), datetime.now()))
        elif kind == "ReplayBufferStateChanged" and data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED":
            log("Replay buffer was stopped in OBS; it will be restarted.")
            self.restart_buffer = True
        elif kind == "RecordStateChanged":
            if data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STARTED":
                if self.recording and not self.recording.get("started"):
                    self.recording["started"] = time.time()
                self.record_started.set()
            elif data.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED":
                self.record_stopped.put(data.get("outputPath"))

    # --- Arena log events
    def match_started(self, match):
        if self.recording:  # previous match still open (its end screen, or its end never logged)
            self.stop_now.set()
            self.finish_recording(wait=False)
        obs = self.obs
        if not obs or obs.closed.is_set():
            log(f"Arena match vs {match['opponent']} started, but OBS isn't connected, so it won't be recorded.")
            return
        try:
            if obs.request("GetRecordStatus")["outputActive"]:
                log(f"Arena match vs {match['opponent']} started; OBS is already recording, so leaving it alone.")
                return
            self.record_started.clear()
            self.recording = {"match": match, "started": None}
            obs.request("StartRecord")
            if not self.record_started.wait(10):
                self.recording["started"] = time.time()
            log(f"Recording Arena match vs {match['opponent']}…")
        except (OBSError, ConnectionError) as e:
            self.recording = None
            log(f"Couldn't start recording the match: {e}")

    def match_ended(self, match):
        recording = self.recording
        if not recording or recording["match"] is not match:
            return
        log(f"Match over: {match['result'] or 'no result'} vs {match['opponent']}. Stopping in {STOP_AFTER_MATCH}s…")
        self.stop_now.clear()
        self.finish_recording(wait=True)

    def finish_recording(self, wait):
        recording = self.recording
        if wait:
            self.stop_now.wait(STOP_AFTER_MATCH)
        with self.record_lock:
            if not recording or self.recording is not recording:
                return  # already finished
            self.recording = None
            try:
                while not self.record_stopped.empty():
                    self.record_stopped.get_nowait()
                path = self.obs.request("StopRecord").get("outputPath")
                try:
                    path = self.record_stopped.get(timeout=30) or path  # wait until OBS has closed the file
                except queue.Empty:
                    pass
                self.work.put(("match", path, recording))
            except (OBSError, ConnectionError, AttributeError) as e:
                log(f"Couldn't stop the recording: {e}")

    def process_match(self, path, recording):
        match, started = recording["match"], recording["started"]
        when = datetime.fromtimestamp(match["start"])
        label = f"vs {match['opponent'] or 'unknown'}" + (f" ({match['result']})" if match["result"] else "")
        full = file_capture(path, ARENA, when, label)
        events = [dict(e, video_time=round(e["t"] - started, 2)) for e in match["events"]]
        info = {k: v for k, v in match.items() if k != "events"}
        info.update(recording_started=started, events=events)
        full.with_suffix(".events.json").write_text(json.dumps(info, indent=1))
        log(f"Saved match -> {full.parent.name}/{full.name} ({full.stat().st_size / 1e6:.0f} MB)")

        keep = [(e["video_time"] - MTGA_KEEP[e["type"]][0], e["video_time"] + MTGA_KEEP[e["type"]][1])
                for e in events if e["type"] in MTGA_KEEP]
        log("Trimming idle time…")
        trimmed, message = trimmer().export_clip(full, "original", "aggressive", full.stem + " - trimmed",
                                                 must_keep=keep)
        log(message.replace("Saved ", "Saved trimmed -> "))

    def worker(self):
        while True:
            item = self.work.get()
            try:
                if item[0] == "replay":
                    target = file_capture(*item[1:])
                    log(f"Saved replay -> {target.parent.name}/{target.name}")
                else:
                    self.process_match(*item[1:])
            except Exception as e:
                log(f"Couldn't file {item[1]}: {e}")

    def run(self):
        threading.Thread(target=self.worker, daemon=True).start()
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import arena_log
        tracker = arena_log.MatchTracker(
            on_start=self.match_started,
            on_end=lambda m: threading.Thread(target=self.match_ended, args=(m,), daemon=True).start())
        arena_log.LogWatcher(tracker, os.environ.get("CLIP_AGENT_ARENA_LOG")).start()
        password, asked = load_password(), False
        waiting_note = False
        while True:
            obs = OBS(password, self.on_event)
            try:
                obs.connect()
            except AuthFailed:
                obs.close()
                if asked or password:
                    log("OBS didn't accept that password.")
                password = getpass.getpass("OBS WebSocket password (Tools > WebSocket Server Settings > "
                                           "Show Connect Info): ")
                asked = True
                continue
            except (OSError, ConnectionError, OBSError, ValueError):
                obs.close()
                if not waiting_note:
                    log("Waiting for OBS (is it running, with the WebSocket server enabled?)…")
                    waiting_note = True
                time.sleep(5)
                continue
            if asked and save_password(password):
                log("Password saved in Windows Credential Manager as " + CREDENTIAL_NAME)
            asked, waiting_note = False, False
            log(f"Connected to OBS (WebSocket {obs.version}). Watching for Arena matches.")
            self.obs = obs
            self.restart_buffer = True
            try:
                while not obs.closed.wait(2):
                    if self.restart_buffer:
                        self.restart_buffer = False
                        if not obs.request("GetReplayBufferStatus")["outputActive"]:
                            obs.request("StartReplayBuffer")
                            log("Replay buffer started. Press your Save Replay hotkey to keep a moment.")
            except OBSError as e:
                if "replay buffer" in str(e).lower() or "ReplayBuffer" in str(e):
                    log(f"{e}. Is the replay buffer enabled in Settings > Output > Replay Buffer?")
                    obs.closed.wait()
                else:
                    log(str(e))
            except ConnectionError:
                pass
            self.obs = None
            obs.close()
            log("Lost connection to OBS.")


def main():
    if "--forget" in sys.argv:
        forget_password()
        print("Forgot the saved OBS password.")
        return 0
    if not FFMPEG:
        print("ffmpeg needs to be installed and on your PATH (winget install Gyan.FFmpeg).")
        input("Press Enter to close.")
        return 1
    log(f"Clip Agent running. Captures go to {captures_folder()}. Close this window to stop.")
    try:
        Agent().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
