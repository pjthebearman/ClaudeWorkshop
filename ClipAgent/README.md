# Clip Agent (in progress)

A background helper that turns play sessions into organised clips with little
or no manual work. It drives OBS (see [OBS_SETUP.md](OBS_SETUP.md)), listens
to game events, and hands recordings to Clip Trimmer's engine.

## Plan

1. **Capture:** OBS with a 5-minute replay buffer and game/mic/voice-chat
   audio on separate tracks. *(set up)*
2. **MTG Arena matches:** read Arena's log (`Player.log`, Detailed Logs on);
   start recording when a match starts and stop when it ends; name the file
   with date, opponent and result; save a timeline of match events beside it;
   make one trimmed video per match. Full matches are kept for now, to see
   how much disk space they use.
3. **WoW Forever moments:** a small addon marks moments (see below), the agent
   saves the replay buffer around each one and files the clip.
4. **Profiles:** trimming rules per game and clip type (MTGA, WoW boss kill,
   WoW PvP...), tuned over time.
5. **Automation:** new captures are processed without opening anything.
6. **Character story:** clips and screenshots assembled into a timeline per
   character, possibly shareable.

Voice chat is recorded on its own track. Whether clips include it is a UX
decision still to make.

## WoW Forever moments

**On by default**

| Moment | Clip | Screenshot |
| --- | --- | --- |
| Level up | yes | yes |
| Dungeon or raid boss killed | yes | yes |
| Epic (purple) or better item looted | yes | yes |
| Your character dies | yes | |
| PvP killing blow | yes | |
| Manual hotkey | yes | |

**Optional**

Rare (blue) loot · quest completed · new zone discovered · rare or elite mob
killed · duel won or lost · group wipe · reputation level reached · profession
skill milestones · mount or pet obtained · first visit to a dungeon · honor
rank up.

## Future options for MTG Arena

- Which matches: all, or only trophy (7-win) runs.
- What to keep: full match, trimmed match, or only the final 1, 3 or 5
  minutes, full or trimmed.
