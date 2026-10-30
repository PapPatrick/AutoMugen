# AutoMugen — local tournament control

Local tournament and broadcast control for Jump Force Mugen AI-versus-AI streams.
Python 3.11+ on Windows, with Flask installed from `requirements.txt`. Local preview needs no
account. **Virtual-credit Twitch chat betting works without Affiliate or Partner status.**
There is no Twitch Channel Points integration, money, purchase, redemption or prize.
The default game folder is `E:\Games\Jump Force Mugen V13`.

## Start

In PowerShell:

```powershell
Set-Location 'C:\Users\pap\Desktop\AutoMugen'
python -m pip install -r requirements.txt
python server.py
```

Operator dashboard: **http://127.0.0.1:8765/**

OBS Browser Source: **http://127.0.0.1:8765/overlay**

Use a 1920 × 1080 Browser Source. Bracket/matchup presentations have an opaque
background; the in-game scoreboard has a transparent background.
Capture the game separately with OBS Game Capture or Window Capture. Keep the
Python server running while using either page; Ctrl+C stops it. Stopping the
server does not kill a game. `python server.py --port 8766` changes both URLs.
The server binds only to this PC's loopback interface; it is not a public website.
The dashboard API and static routes are served by Flask using Werkzeug's threaded
development server. The tournament scheduler runs in a separate server-owned
thread and shares a lock with HTTP requests so state changes remain serialized.

## Run a tournament

1. Verify the game folder, roster discovery notes, and available stages.
2. Select a fighter pool (all valid fighters are initially selected). Choose
   2, 4, 8, 16, 32, or 64 participants. The default is 16. An optional text seed
   makes the draw reproducible for the same ordered fighter pool.
3. Seed the bracket. Fighters are sampled without duplicates and paired in
   a single-elimination tree. Matches run in bracket order.
4. For unattended play, click **Start automatic tournament** and approve its
   confirmation. It launches rounds using random stages, reads engine results,
   updates series scores, and proceeds to the champion without needing an open
   browser. Before **every match before the semifinals**, it opens a 60-second
   next-match betting window. Before the **first semifinal**, it opens one
   180-second window to pick the tournament champion from all four semifinalists.
   Champion bets stay locked through both semifinals and settle after the grand
   final; no further per-match windows open. Connect chat for virtual-credit
   betting, or explicitly select **Local preview (no betting)** first.
   Stable engine results advance immediately, including the first round: no
   one-time manual confirmation is required. Saved results held by the previous
   confirmation gate are verified against their launch context and applied once
   during upgrade; invalid evidence remains unscored.
   Starting replays any earlier unconfirmed round; it never guesses its outcome.
   **Pause automatic launches** prevents the next launch, but allows the current
   round to finish and be scored. Automation never resumes after server restart.
5. For manual launch control, use **Prepare live window** (or explicitly choose
   **Local preview**) and wait until the presentation finishes before launching.
   The 60-second match / 180-second champion windows cannot be shortened. Deadlines persist
   through refreshes. An open chat window is voided on restart because messages
   were missed and all stakes are refunded; locked bets persist for settlement.
   Matches after the champion window have a 24-second rotating intermission.
   Betting matches can launch as soon as the saved deadline is reached and bets
   are locked, without an additional transition delay.
   BO3 game two/three do not reopen betting windows.
6. Leave **Random stage** selected (the default) to draw from the discovered
   stages on every launch or replay, or choose a specific stage. The chosen
   stage is saved with the launch and displayed on the match desk.
   Explicitly click **Launch AI match**. Nothing launches
   just by opening a page or seeding a tournament. Countdown expiry launches
   the next round only while automatic tournament mode is explicitly enabled.
7. Each launch plays **one round** with `-rounds 1`, then MUGEN quits.
   Early matches are best of one; semifinals and the final are best of three
   (first to two engine-detected or manually confirmed round wins). Supported
   builds automatically record stable engine results after a clean process exit.
   Scores persist through restarts and appear on the dashboard and overlay.
   Draws, crashes, and unclear outcomes must be replayed, not counted.
   An exit code never identifies a winner;
   a nonzero error exit blocks result confirmation and allows replay only.
   If detection fails, automation pauses visibly. Confirm only an outcome you
   actually observed, or replay. Draws, crashes, and unclear outcomes never
   receive an automatic winner.
8. The result is saved before the bracket advances. The final result
   produces the champion. Seeding again explicitly confirms replacement.

## Implemented

- Read-only discovery of real `[Characters]` entries from `data\select.def`;
  ignores `empty`, `randomselect`, comments, other sections, and duplicate
  definitions. Supports spaces, quoted entries, explicit `.def` paths, UTF-8
  and legacy Windows text. Display names come from character `[Info]` metadata.
  Missing definitions and invalid paths appear in discovery notes.
- Stage discovery under `stages`, with validated game-relative paths.
- Persistent random brackets, timed broadcast presentations, explicit AI launch/replay,
  read-only engine winner detection, manual fallback, progress, and champion display.
- Explicit start/pause for an unattended server-side scheduler. A 180-second
  watchdog pauses and terminates only the process it launched, without counting
  a result. Unexpected processes, draw results, observation errors, or crashes
  pause automation; no silent defaults or random winners.
- Responsive local operator dashboard and polling OBS overlay. An offline
  warning appears if the overlay loses the server.
- Portraits extracted read-only from installed SFF files, connected bracket cards,
  large character matchup screens, automatic content rotation and champion screen.
- Twitch chat authorization, next-fight virtual-credit wagers, local locking,
  full-match proportional payouts and explicit cancellation/refunds.
  Tokens are never included in state, logs or overlay responses.
- Authenticated Twitch IRC commands: `!bet 100 1`, `!bet all 2`, `!balance`,
  persistent per-channel balances and a top-five leaderboard. New viewers start
  with 1,000 credits; balances below 100 refill to 100 before an accepted bet.
  Credits are free and have no cash value. No Affiliate requirement.
- Windows process checks block launching or confirming results while any
  `Mugen.exe`, `3v3.exe`, or `4v4.exe` instance remains open. Matching executable
  names are conservatively blocked even if from a different game installation.
  A locked data folder prevents duplicate servers using the same state.
- Atomic state replacement, last-state backup, saved launch intent, visible
  errors, and explicit interrupted-launch recovery.
- Loopback-only HTTP, fixed static-file routes, per-run request tokens, and
  browser origin/host checks. Do not expose this service through a tunnel or
  reverse proxy; any local process/user can access the operator API.

## Verified launch evidence and limitations

Read-only inspection of the supplied `Mugen.exe` found embedded help containing:

```text
-log <logfile>      Records match data to <logfile>
-p<n> <playername>  Loads player n, eg. -p3 kfm
-p<n>.ai 1         Enables AI for player ?, eg. -p1.ai 1
-rounds <num>      Plays for <num> rounds, and then quits
-s <stagename>     Loads stage <stagename>
```

The app validates the player, AI, and stage help markers again before launch. It passes
arguments as a list with `shell=False`, using the game folder as the working
directory:

```text
Mugen.exe -p1 "<fighter entry>" -p2 "<fighter entry>" -p1.ai 1 -p2.ai 1 -s "<stage>.def" -rounds 1
```

MUGEN's QuickVS stage loader prepends `stages\` itself. The dashboard keeps
game-relative stage IDs, but the launcher removes that leading directory for
`-s` (for example, `-s "Dimensional Infinity Fortress.def"`). Nested stage
subdirectories and the `.def` extension are preserved. Passing
`-s "stages\<stage>.def"` incorrectly produces `stages\stages\<stage>.def`.

**No game match was launched automatically during development.** Real gameplay,
character-specific AI behavior, asset completeness beyond `.def` existence, and
match-data output must be verified by the operator's first explicit launch.
The `3v3.exe` and `4v4.exe` variants are not launch targets in this iteration.

**Winner detection is build-specific and read-only.** `winner.py` supports only
the inspected executable with SHA-256:

```text
30bdfba28e9d946e0dde859d07a6bab5d5abcf5fef9f48c62e9d13904f52ab2e
```

The adapter derives field layouts from this binary's registered Lua `roundstate`
and `roundno` accessors, plus its KO/time-over outcome branches. It opens only
the process the app launches, with query/read permissions; it never patches,
injects into, or writes to game memory. This pinned executable has PE relocations
stripped. The observer reads its fixed preferred image base and verifies the
mapped PE header against the on-disk executable before reading any engine
fields. This avoids Toolhelp module enumeration during early WOW64 startup,
which produced Windows error 299. Header reads retry briefly during startup;
a different mapped header fails closed.
It requires seeing round 1 actively fighting, then a completed-round state with
three matching outcome snapshots. Side 1 and side 2 map to the launched P1/P2.
Normal KO and time-over wins can score; draws and double KOs pause for review.
The clean exit alone is never used to pick a winner.

The Windows read API/module enumeration and outcome/progression logic are tested.
**Live observation has succeeded:** the supplied build produced 356 stable
snapshots reporting P2 (GokuTerror) with a normal KO and clean exit.
The obsolete first-result confirmation gate is removed. Unknown builds disable
automatic play and retain manual controls. Memory-read failure or a missed round
does not fall back to guessing, OCR, name matching, or life-bar comparison.

The existing `mugen.log` showed gameflow, not an established result contract.
Although `-log` is documented in the executable, this installed build does not
consume its filename correctly: the reported launch failure treated the absolute
log path as a QuickVS character. The launcher therefore does **not** pass `-log`
or a log filename. Its output/schema and winner semantics remain unverified.
Console output is retained for investigation, never used to infer a result.

**Live Twitch operation requires human setup and is not end-to-end verified.**
Chat betting uses authenticated TLS IRC, not simulated viewers or wagers.
Lifecycle tests use mocks only; development did not authorize an account or
place live bets. Until you authorize
the appropriate channel, the UI says not authorized and opening a window fails
visibly. Local preview is explicitly labeled and never silently substitutes
for failed live betting.

## Broadcast behavior

Every pre-semifinal match has a **60-second two-fighter match-winner window**.
Once all four semifinalists are known, a single **180-second champion window**
opens before the first semifinal. Choices 1–4 follow the two semifinal pairings
in bracket order, and keep the same numbers through the grand final.
There are no per-match bets during the semifinals or final. A four-fighter draw
starts directly with champion betting. A two-fighter draw has one 180-second
champion window for its two finalists.

The bracket remains on screen for the full 60 or 180 seconds from successful live
preparation/local preview. Chat bets must be sent and received before the
server's saved deadline. The overlay shows betting open only when the tracked
window is ACTIVE, connected to chat, and its deadline has not passed. The top banner
combines the overlay title and phase, and shows one total pool counter; the
bracket and matchup cards show per-fighter bet counts and credit totals.
Matchup multipliers are estimates of gross return, including stake, and change
as bets arrive; final payouts use the locked pool and whole-credit rounding.

After the window, tree and character matchup screens rotate every eight seconds.
There is no extra launch delay after a betting window. Championship matches
have a 24-second rotating intermission. Rotation is anchored to saved timestamps,
not page-load time. During a launched game the top banner is hidden to keep
MUGEN's health bars, character names and timer unobstructed. Only a compact
720-by-50-pixel match/series badge appears at the bottom center of the
1920-by-1080 canvas, leaving the bottom corners clear. Long names truncate
instead of expanding over gameplay. For completely unobstructed gameplay,
use **http://127.0.0.1:8765/overlay?hud=off** in OBS; this hides the in-fight
badge only, retaining the full bracket/matchup presentations and offline warnings.
No automatic game-HUD detection is claimed. Completed tournaments
show the champion/tree; interrupted launches show operator review. An offline
banner marks stale displays. For 32/64 entrants the early draw is paginated in
16-fighter sections every 12 seconds, with championship rounds always visible.
After a match/BO3 series is fully decided, the overlay replaces the next scene
with a 15-second result card. It identifies the winner, each side's bet count
and credits wagered, and the exact total credits paid out (including returned stakes).
Canceled windows and one-sided pools show refunded credits instead of winnings.
No-betting matches show the winner without
bet totals.

This switches **content inside the one OBS Browser Source**, not OBS scenes;
no OBS WebSocket integration is claimed. In OBS, avoid shutting down the source
when hidden if you want immediate visual updates; a refresh never restarts timers.
Upgrades do not open new betting on a match/BO3 series that already began.

### Real local character portraits

After updating or adding game characters, pause automatic launches, let the current
game close, then click **Update roster & portraits** under Game connection. It
rescans the configured game folder, checks every roster fighter (plus saved
tournament participants), reuses valid cached PNGs, and generates missing,
changed or corrupt images directly from the game assets. A progress bar and
ready/failed counts remain visible while the server processes small batches;
the dashboard/overlay stay responsive. Individual failures appear in Discovery
notes without stopping the remaining images. Completed updates automatically
refresh displayed images, including previously failed requests. New roster
entries are selected in the fighter pool, without changing your saved bracket.
If the game folder changed, save the correct folder first using the existing
tournament safeguards.

The update report survives restarts; an interrupted run is labeled interrupted,
never completed. Click the button again to recheck/reuse completed cache entries.
Games cannot launch during an image update, and automation is not started by
updating images. **Rescan roster** remains a lightweight roster-only operation.

`portraits.py` reads sprite `9000,1`, falling back to `9000,0`, from installed
character SFF files. Supports SFF v1 PCX and v2 raw/RLE8/RLE5/LZ5/embedded PNG,
including five-bit compressed portraits and linked sprites/palettes. Indexed
SFF PNG portraits use their SFF palette, not the PNG's sometimes dummy black
palette; true-color PNG portraits retain their own colors and alpha. The embedded palette is used;
external ACT palettes are not applied. Unsupported or missing assets show
explicit initial-letter placeholders, **not invented character art**.
Reasons appear in dashboard Discovery notes after image requests.
No external image download occurs and no game assets are modified.

Decoded PNGs are cached in `runtime\portraits` and invalidated by source
path/size/mtime and decoder version. For a local override, put a valid PNG in
`runtime\portrait-overrides\<sha256-of-exact-fighter-id>.png`. Generate the name:

```powershell
python -c "import hashlib; print(hashlib.sha256('Goku OP'.encode('utf-8')).hexdigest() + '.png')"
```

Use the exact roster ID including case, spaces and backslashes. Overrides take
precedence; invalid overrides report an error rather than secretly switching
images. PNGs must be noninterlaced, 8-bit, at most 2048 pixels per axis / four
million pixels. The cache and overrides must stay outside the game folder.

## Twitch authorization and virtual-credit betting

This is a local chat-betting economy inspired by pooled stream betting, not an
integration with SaltyTeemo or its accounts/balances. Credits cannot be bought,
transferred, redeemed or exchanged for money, prizes or Twitch Channel Points.

1. Register an application at https://dev.twitch.tv/console/apps with the exact
   redirect **`http://localhost:8765/auth/callback`** (adjust for a custom port).
   Use a public client supporting the implicit user flow. No client secret is needed.
2. Set the client ID in the server's environment, then start:

   ```powershell
   $env:AUTOMUGEN_TWITCH_CLIENT_ID = 'your-client-id'
   # Required: your streaming channel, NOT the bot's login
   $env:AUTOMUGEN_TWITCH_CHAT_CHANNEL = 'your_stream_login'
   .\venv\Scripts\python.exe server.py
   ```

   The Client ID identifies the registered application, not the streamer or bot
   account. Twitch requires it for the bot's OAuth sign-in and channel lookup.
   No separate numeric account-ID setting is needed.

   Python does not automatically load `.env`. The old
   `AUTOMUGEN_TWITCH_TOKEN` environment variable is no longer used.
3. With launches paused, click **Sign in as bot**. Sign in as a
   **separate bot account** (e.g.
   `automugen_bot`; use a private browser window so Twitch doesn't reuse your
   streaming login). With `AUTOMUGEN_TWITCH_CHAT_CHANNEL` set, the bot joins
   that channel, reads wagers there and posts replies as itself; your streaming
   account never signs in. The channel setting is required: there is no own-channel
   fallback, and signing in with the streaming account is rejected.
   The channel's numeric ID is looked up via Twitch and must match the room ID
   Twitch reports on join; balances are stored per stream channel, so switching
   from broadcaster to bot keeps existing balances. Optionally type
   `/mod automugen_bot` in your chat so AutoMod, follower/slow mode and
   rate limits don't block its replies.
   Grant **`chat:read` and `chat:edit`** so the bot can read wagers and post
   confirmations, balances and results. No Affiliate/Partner status or native
   prediction permission is required. Existing read-only authorizations need a
   new sign-in. Wait for CONNECTED before preparing betting.
4. Seed a tournament and use **Prepare chat betting**, or start automatic play.
   Before each pre-semifinal match, viewers have 60 seconds:
   **`!bet 100 1`** wagers 100 credits on P1; **`!bet all 2`** wagers all available
   credits on P2. **`!balance`** reports the viewer's available balance.
   At the semifinals, there is one 180-second window to bet on which of the four
   remaining characters will win the grand final, using **`!bet 100 1`** through
   **`!bet 100 4`**. The bot, dashboard and overlay show the numbered fighters
   with portraits and live pools. Champion bets replace per-match betting for
   both semifinals and the final.
   After saving an accepted bet, the bot confirms it publicly in the channel:
   viewer, amount, numbered fighter/name, match or champion target, and remaining
   balance. Confirmations use the rate-limited chat queue and may arrive late.

Each viewer starts with **1,000 credits**, keyed by stable Twitch user ID and
partitioned by broadcaster. Before an accepted bet, a balance below 100 is
refilled to **100**, not increased by 100. There is **one final bet per viewer per
window**, with no changes or additional stakes, preventing repeated refills during
the same window. Stakes are debited immediately and saved atomically with the bet.
An insufficient-balance wager is rejected without deducting credits or refilling.
Individual wagers are capped at 1,000,000,000 credits. `!balance` never refills.
Balances carry across tournaments; the top five appear on stream and the dashboard.

After a verified/operator-confirmed **match winner**, or the **grand final champion**
for champion bets, winning
bettors split the **entire pool in proportion to their winning-side stakes**.
There is no fee. Gross payout includes the original stake. Integer division uses
largest-remainder rounding, with stable user-ID ordering for ties, so exactly the
pool is returned. If nobody bet on the winner, or everyone bet on the winner,
**all stakes are refunded** instead. An empty losing option does not void a
four-fighter pool if there are winning and losing stakes on the other options.
Semifinal results never settle champion bets; eliminated fighters' stakes stay
in the pool for the grand-final payout. BO3 game one, a draw, timeout, crash or
ambiguous observation never settles a pool.
Settlement and balances commit together; repeated polling/restarts cannot pay twice.

Only recent tagged commands in the configured stream channel's TLS IRC chat
count. Both send and receive timestamps must fall inside the saved betting
window. Up to two seconds of clock skew is tolerated for Twitch timestamps ahead
of the PC clock; these timestamps are clamped to receipt time so they cannot move
a wager past the cutoff. Messages more than ten seconds late, notices, whispers and invalid
commands are ignored. Submit before the final seconds. Replies are rate-limited
to one every 1.6 seconds and may be delayed; a full reply queue is reported to the
operator without changing saved bets. At most 10,000 bettors can join a window;
additional wagers are rejected. Raw bets, user IDs and balance ledgers are not
exposed by the overlay API; local state contains them and should stay private.
The dashboard reports received/rejected command counts and replies sent for the
current connection, and displays Twitch bot-message restrictions such as required
phone verification. A socket send is not proof Twitch published the message.

**Disconnect, receive-queue overflow, account/connection mismatch or server
restart during open betting cancels the window, refunds all accepted stakes and
pauses launches.** Reconnect/sign in, then **Reopen refunded window** for
an unstarted match (60 seconds) or the first semifinal (180-second champion
window), or explicitly **Continue after refunds**. A series that already
began cannot reopen betting. Locked pools survive disconnection/restart and can
settle while chat is offline. **Cancel & refund stakes** also refunds an open or
locked pool and pauses launches; it cannot undo a resolved payout. Pausing launches
alone does not stop the betting clock. Local preview disables betting explicitly.

Credentials remain in memory; sign in again after restart/expiry. The callback
allows signing in while a pool is locked and launches are paused, but only
reconnects to that pool's original stream channel; locked bets are not reopened.
The callback
removes the token fragment from history and validates short-lived single-use
OAuth state, client, channel, scopes and expiry. No auto-refresh or credential
store is implemented. Keep the server running and monitor visible failures.

After updating the Python files, stop the old server with Ctrl+C and start
`python server.py` again before refreshing the dashboard or OBS source.
Static JavaScript updates immediately, but an existing Python process keeps its
old backend code. An incompatible betting API disables controls and displays a
restart instruction rather than substituting fake zero-credit totals.

**Upgrade:** old free-pick scores and the last pick window are preserved separately,
not converted into spendable credits. Old native prediction records are archived
locally. An unresolved Channel Points prediction blocks new betting until you
handle it yourself in Twitch Creator Dashboard and explicitly acknowledge that
in AutoMugen. This app no longer calls the native prediction API and cannot
refund or resolve those old points; acknowledging is not a remote operation.

Official references: https://dev.twitch.tv/docs/irc/ and
https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#implicit-grant-flow

**Series length is enforced by AutoMugen.** Every launch is limited to one
round; the bracket advances at one win for early rounds and two wins for
semifinals/final. `-rounds 3` is not used: a fixed total-round limit would not
implement first-to-two. Replays draw a new random stage when random is selected.
Existing unfinished matches adopt these rules; previously completed legacy
matches remain completed single-win results rather than inventing round scores.
The installed timer and difficulty remain unchanged.
The inspected `mugen.cfg` had `Time = -1` (unlimited time) and `Difficulty = 1`.
Manual rounds may need to be ended and replayed if stalled; automatic rounds
are bounded by the 180-second watchdog and pause on timeout.
The app does not edit original game/config/character files. The game itself
may naturally write its own normal logs or settings when you run it.

## Persistence and recovery

`runtime\state.json` is the authoritative current tournament.
`runtime\state.backup.json` is the previous saved snapshot, not an archive.
**Logs are in `runtime\logs\`, not a top-level `logs\` folder.** Each launch
keeps its console log with labeled AutoMugen launcher/result diagnostics
(the game's own console output can still be empty), an observation
`.result.json` audit including command/exit code, and a `.mugen.log` copy of the
game's actual diagnostic log after exit. The dashboard displays both log paths.
The game's original `mugen.log` stays in the game folder; copying does not alter it.
If the game did not create or update that log, the audit records that explicitly
instead of attributing a previous match's output to the new launch.
Older saved launch records may still reference the unsupported match-log path;
replaying creates a corrected command and a new console log. There are no
secrets in these files, but logs may contain local paths. Copy state files
elsewhere with the server stopped if you want to archive a tournament.

After an interrupted active launch, the dashboard enters **recovery required**.
Close all game instances and click **Acknowledge recovery**. Confirm only an
outcome you actually witnessed; otherwise replay. The app deliberately does not
reattach to a saved PID, infer a winner from a process exit, or terminate
unrelated processes. A launch that fails to create a process is marked failed
and may be retried.

If state is malformed, startup fails visibly rather than resetting it.
Stop any server using this folder, preserve your state files, then explicitly
restore the prior valid snapshot with:

```powershell
python server.py --restore-backup
```

Restoration validates the backup, preserves the displaced current file as
`runtime\state.displaced-<timestamp>.json`, and starts the server. Inspect the
restored bracket: it may be one action behind. If the backup is also invalid,
restore an independently archived copy rather than guessing results.
Disk errors surface in the dashboard/console. If an error happens just after a
game opens, do not launch another instance: close the game and restart the
server to enter recovery. Interrupted rounds are never awarded automatically.

## Checks

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
node --check public\app.js
node --check public\overlay.js
node --check public\broadcast.js
node tests\test_overlay.js
node --check public\auth.js
```

Tests use temporary game fixtures and mocked process launching; they never run
the installed game. Coverage includes deterministic draws, every supported
bracket size through the champion, invalid/stale results, roster parsing,
path/CLI validation, countdown and concurrent-game guards, explicit manual
advancement after exit, interrupted-launch recovery, persistence failures,
duplicate-server locking, and HTTP surface checks. Automatic-play tests run a
full simulated tournament without browser polling and cover countdowns, series
scores, pause/resume, restart recovery, watchdogs, crashes, draws, missing results,
stale launch context, and read-only engine outcome validation.
Broadcast/Predictions tests cover exact 60/180-second boundaries, persistent
deadlines, four-candidate champion settlement after the final, BO3 progression,
screen rotation, missing authorization,
single-use OAuth state, exact pooled payouts/rounding, stake deductions, refills,
restart/disconnect refunds, persistence failures and safe portrait HTTP routing.
Removed native controls and legacy-state migration are also covered. Portrait tests include bounded
decoding and read-only samples from installed assets when available; the symlink
escape test skips on Windows accounts without symlink privileges.
