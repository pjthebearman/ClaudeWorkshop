"""Reads MTG Arena's Player.log (Detailed Logs on) and turns it into match events.

Arena writes one JSON message per line, each with a millisecond "timestamp" from this PC's
clock, so events can be lined up with a recording made on the same PC.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path


def default_log_path():
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE", Path.home())) / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
    return Path.home() / "Library/Logs/Wizards Of The Coast/MTGA/Player.log"  # macOS


ME = re.compile(r"Match to ([0-9A-Za-z]+):")   # "...: Match to <your user id>: ..." names you
INTERESTING = ("matchGameRoomStateChangedEvent", "greToClientEvent")


class MatchTracker:
    """Feed it log lines; it calls on_start(match) and on_end(match) and collects events.

    An event is {"t": seconds since epoch, "type": ..., "by": "me"|"opponent"|None, ...}.
    """

    def __init__(self, on_start=None, on_end=None):
        self.on_start, self.on_end = on_start, on_end
        self.my_user_id, self.match = None, None
        self.controllers, self.zone_owners, self.turn = {}, {}, {}  # game state remembered across updates

    def feed(self, line):
        if line.startswith("[UnityCrossThreadLogger]"):
            m = ME.search(line)
            if m:
                self.my_user_id = m.group(1)
            return
        if not line.startswith("{") or not any(k in line for k in INTERESTING):
            return
        try:
            message = json.loads(line)
        except ValueError:
            return
        t = int(message.get("timestamp", 0)) / 1000 if str(message.get("timestamp", "")).isdigit() else time.time()
        if t > 4e9:  # not milliseconds since 1970 (shouldn't happen); fall back to now
            t = time.time()
        if "matchGameRoomStateChangedEvent" in message:
            self._room(message["matchGameRoomStateChangedEvent"].get("gameRoomInfo", {}), t)
        elif "greToClientEvent" in message and self.match:
            for gre in message["greToClientEvent"].get("greToClientMessages", []):
                self._gre(gre, t)

    # --- match start/end
    def _room(self, info, t):
        config = info.get("gameRoomConfig", {})
        state = info.get("stateType", "")
        players = config.get("reservedPlayers", [])
        me = next((p for p in players if p.get("userId") == self.my_user_id), None)
        if state.endswith("_Playing") and (not self.match or self.match["id"] != config.get("matchId")):
            opponent = next((p for p in players if p is not me), {})
            self.controllers, self.zone_owners, self.turn = {}, {}, {}
            self.match = {
                "id": config.get("matchId"), "start": t, "end": None,
                "me": (me or {}).get("playerName"), "opponent": opponent.get("playerName"),
                "my_team": (me or {}).get("teamId"), "my_seat": (me or {}).get("systemSeatId"),
                "event": (me or opponent).get("eventId"), "result": None, "reason": None, "events": [],
            }
            if self.on_start:
                self.on_start(self.match)
        elif state.endswith("_MatchCompleted") and self.match and self.match["id"] == config.get("matchId"):
            final = info.get("finalMatchResult", {})
            overall = next((r for r in final.get("resultList", []) if r.get("scope") == "MatchScope_Match"), {})
            if overall.get("winningTeamId") is not None and self.match["my_team"] is not None:
                self.match["result"] = "Win" if overall["winningTeamId"] == self.match["my_team"] else "Loss"
            else:
                self.match["result"] = "Draw" if overall.get("result") == "ResultType_Draw" else None
            self.match["reason"] = (overall.get("reason") or "").replace("ResultReason_", "") or None
            self.match["end"] = t
            match, self.match = self.match, None
            if self.on_end:
                self.on_end(match)

    # --- in-game events
    def _who(self, seat):
        if seat is None or not self.match or self.match["my_seat"] is None:
            return None
        return "me" if seat == self.match["my_seat"] else "opponent"

    def _add(self, t, kind, **extra):
        last = self.match["events"][-1] if self.match["events"] else None
        if kind == "choose_attackers" and last and last["type"] == kind and t - last["t"] < 3:
            return  # Arena asks again as you pick each attacker
        self.match["events"].append({"t": round(t, 3), "type": kind, **extra})

    def _gre(self, gre, t):
        kind = gre.get("type", "")
        if kind == "GREMessageType_DeclareAttackersReq":
            self._add(t, "choose_attackers", by="me")
        if kind != "GREMessageType_GameStateMessage":
            return
        state = gre.get("gameStateMessage", {})
        info = state.get("gameInfo", {})
        # Updates only contain what changed, so keep track of who controls each card and zone.
        for obj in state.get("gameObjects", []):
            if "instanceId" in obj and "controllerSeatId" in obj:
                self.controllers[obj["instanceId"]] = obj["controllerSeatId"]
        for zone in state.get("zones", []):
            if "zoneId" in zone and "ownerSeatId" in zone:
                self.zone_owners[zone["zoneId"]] = zone["ownerSeatId"]
        self.turn.update(state.get("turnInfo", {}))
        if info.get("stage") == "GameStage_GameOver":
            game = next((r for r in info.get("results", []) if r.get("scope") == "MatchScope_Game"), {})
            if game and not any(e["type"] == "game_over" and e["game"] == info.get("gameNumber")
                                for e in self.match["events"]):
                won = game.get("winningTeamId") == self.match["my_team"]
                self._add(t, "game_over", game=info.get("gameNumber"), by="me" if won else "opponent",
                          reason=(game.get("reason") or "").replace("ResultReason_", ""))
        for note in state.get("annotations", []):
            types = note.get("type", [])
            details = {d.get("key"): (d.get("valueInt32") or d.get("valueString") or [None])[0]
                       for d in note.get("details", [])}
            affected = note.get("affectedIds", [])
            if "AnnotationType_NewTurnStarted" in types:
                number = self.turn.get("turnNumber") or 1 + sum(e["type"] == "turn" for e in self.match["events"])
                self._add(t, "turn", turn=number, by=self._who(self.turn.get("activePlayer")))
            elif "AnnotationType_ZoneTransfer" in types:
                category = details.get("category")
                if category in ("CastSpell", "PlayLand", "Resolve", "Destroy", "Sacrifice", "Exile", "Return",
                                "Countered", "Discard", "Mill"):
                    card = affected[0] if affected else None
                    seat = self.controllers.get(card) or self.zone_owners.get(details.get("zone_src"))
                    if seat is None and note.get("affectorId") in (1, 2):
                        seat = note["affectorId"]
                    self._add(t, category.lower(), by=self._who(seat))
            elif "AnnotationType_DamageDealt" in types:
                to_player = [a for a in affected if a in (1, 2)]
                self._add(t, "damage", amount=details.get("damage"),
                          to=self._who(to_player[0]) if to_player else "creature")
            elif "AnnotationType_ModifiedLife" in types:
                self._add(t, "life", change=details.get("life"), to=self._who(affected[0] if affected else None))
        turn = self.turn
        if turn.get("step") == "Step_DeclareAttack" and state.get("type") == "GameStateType_Diff":
            attackers = [o for o in state.get("gameObjects", []) if o.get("attackState") == "AttackState_Attacking"]
            if attackers and not (self.match["events"] and self.match["events"][-1]["type"] == "attack"
                                  and t - self.match["events"][-1]["t"] < 5):
                self._add(t, "attack", by=self._who(turn.get("activePlayer")), attackers=len(attackers))


class LogWatcher(threading.Thread):
    """Follows Player.log as Arena writes it, including when Arena restarts and replaces it."""

    def __init__(self, tracker, path=None, poll=0.25):
        super().__init__(daemon=True)
        self.tracker, self.path, self.poll = tracker, Path(path or default_log_path()), poll

    def run(self):
        handle, ident, partial = None, None, ""
        while True:
            try:
                st = self.path.stat()
                # A relaunched Arena renames the old log and starts a new file: new file ID, or smaller.
                if handle is None or (st.st_dev, st.st_ino) != ident or st.st_size < handle.tell():
                    if handle:
                        handle.close()
                    first_open = handle is None
                    handle = open(self.path, "r", encoding="utf-8", errors="replace")
                    ident, partial = (st.st_dev, st.st_ino), ""
                    if first_open:
                        handle.seek(0, os.SEEK_END)  # only follow what happens from now on
                chunk = handle.read()
                if chunk:
                    lines = (partial + chunk).split("\n")
                    partial = lines.pop()
                    for line in lines:
                        self.tracker.feed(line.rstrip("\r"))
                    continue
            except OSError:
                handle = None
            time.sleep(self.poll)


if __name__ == "__main__":  # python arena_log.py [Player.log]  -> prints each match it finds
    def show(match):
        start = match["start"]
        print(f"{match['me']} vs {match['opponent']} ({match['event']}): {match['result']} by {match['reason']}, "
              f"{(match['end'] - start) / 60:.1f} min, {len(match['events'])} events")
        for e in match["events"]:
            extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("t", "type"))
            print(f"  {int(e['t'] - start) // 60}:{int(e['t'] - start) % 60:02d}  {e['type']:<16} {extra}")
    tracker = MatchTracker(on_end=show)
    with open(sys.argv[1] if len(sys.argv) > 1 else default_log_path(), encoding="utf-8", errors="replace") as f:
        for line in f:
            tracker.feed(line.rstrip("\r\n"))
