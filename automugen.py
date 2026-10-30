"""Tournament state, read-only MUGEN discovery, and explicit Windows launching."""

from __future__ import annotations

import copy
import csv
import io
import json
import os
from pathlib import Path
import random
import re
import secrets
import subprocess
import time
import uuid
import logging
import shutil

from winner import ObservationError, SUPPORTED_SHA256, WinnerObserver, supported
from broadcast import ensure_presentation, view as broadcast_view
from predictions import Predictions
from twitch import TwitchError
from portraits import PortraitCache
from chat import ChatPredictions, refund_bets, MAX_WAGER


DEFAULT_GAME = r"E:\Games\Jump Force Mugen V13"
SIZES = (2, 4, 8, 16, 32, 64)
LAUNCH_STATES = {"idle", "launching", "running", "awaiting_result", "failed", "recovery_required"}


class UserError(Exception):
    """A visible, actionable operator error."""


def text(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def uncomment(line: str) -> str:
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            return line[:i].strip()
    return line.strip()


def contained(base: Path, relative: str) -> Path:
    relative = relative.replace("\\", os.sep).replace("/", os.sep)
    if not relative or "\x00" in relative or Path(relative).is_absolute() or ":" in relative:
        raise UserError("Invalid game-relative path.")
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base.resolve()):
        raise UserError("Game paths must stay inside their expected folder.")
    return candidate


def fighter_def(root: Path, entry: str) -> Path:
    if entry.startswith("-") or any(ord(c) < 32 for c in entry):
        raise UserError("Unsafe character name.")
    relative = entry.replace("\\", "/")
    if not relative.lower().endswith(".def"):
        relative = f"{relative}/{relative.rsplit('/', 1)[-1]}.def"
    path = contained(root / "chars", relative)
    if not path.is_file() or path.suffix.lower() != ".def":
        raise UserError(f"Missing character definition: {entry}")
    return path


def display_name(path: Path, fallback: str) -> str:
    section = ""
    values = {}
    for raw in text(path).splitlines():
        line = uncomment(raw)
        if line.startswith("[") and "]" in line:
            section = line[1:line.index("]")].strip().lower()
        elif section == "info" and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip().strip('"')
    return values.get("displayname") or values.get("name") or fallback


def discover(game: str) -> dict:
    root = Path(game).resolve()
    select = root / "data" / "select.def"
    if not select.is_file() or not (root / "Mugen.exe").is_file():
        raise UserError("Choose a game folder containing Mugen.exe and data\\select.def.")
    fighters, warnings, seen = [], [], set()
    section = ""
    for number, raw in enumerate(text(select).splitlines(), 1):
        line = uncomment(raw)
        if line.startswith("[") and "]" in line:
            section = line[1:line.index("]")].strip().lower()
            continue
        if section != "characters" or not line:
            continue
        entry = next(csv.reader([line], skipinitialspace=True))[0].strip().strip('"')
        if entry.lower() in {"empty", "randomselect"}:
            continue
        try:
            path = fighter_def(root, entry)
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            fighters.append({"id": entry, "name": display_name(path, entry)})
        except (UserError, OSError) as exc:
            warnings.append(f"select.def:{number}: {exc}")
    stages = []
    stages_root = root / "stages"
    if stages_root.is_dir():
        for path in sorted(stages_root.rglob("*"), key=lambda p: str(p).casefold()):
            if path.is_file() and path.suffix.lower() == ".def":
                try:
                    contained(stages_root, str(path.relative_to(stages_root)))
                    stages.append({"id": str(path.relative_to(root)), "name": path.stem})
                except UserError as exc:
                    warnings.append(f"Stage {path.name}: {exc}")
    if not fighters:
        warnings.append("No usable fighters found in the [Characters] section.")
    if not stages:
        warnings.append("No stage definitions found; launching is disabled.")
    return {"fighters": fighters, "stages": stages, "warnings": warnings}


def new_state() -> dict:
    return {
        "version": 1,
        "game_path": DEFAULT_GAME,
        "tournament": None,
        "launch": {"status": "idle", "message": "No game launched."},
        "automation": {"enabled": False, "message": "Automatic tournament is stopped."},
        "detector_verified": None,
        "prediction_mode": "chat",
    }


def new_tournament(fighters: list[dict], size: int, seed: str, seconds: int) -> dict:
    if type(size) is not int or size not in SIZES or len(fighters) < size:
        raise UserError("Choose a supported bracket size with enough selected fighters.")
    if type(seconds) is not int or not 0 <= seconds <= 600:
        raise UserError("Local prediction window must be between 0 and 600 seconds.")
    if not isinstance(seed, str) or len(seed) > 128:
        raise UserError("Seed must be text of at most 128 characters.")
    seed = seed or secrets.token_hex(8)
    chosen = random.Random(seed).sample(fighters, size)
    rounds = []
    count = size // 2
    while count:
        rounds.append([
            {
                "id": f"r{len(rounds) + 1}m{i + 1}",
                "players": [chosen[2 * i]["id"], chosen[2 * i + 1]["id"]] if not rounds else [None, None],
                "winner": None,
                "window_until": None,
                "confirmed_at": None,
                "best_of": 3 if count <= 2 else 1,
                "wins": [0, 0],
            }
            for i in range(count)
        ])
        count //= 2
    return {
        "id": uuid.uuid4().hex,
        "seed": seed,
        "window_seconds": seconds,
        "fighters": chosen,
        "rounds": rounds,
        "created_at": time.time(),
        "champion": None,
    }


def current_match(tournament: dict | None) -> dict | None:
    if tournament:
        for round_matches in tournament["rounds"]:
            for match in round_matches:
                if match["winner"] is None and all(match["players"]):
                    return match
    return None


def verified_observation(state: dict, observation: dict, pid: int | None) -> bool:
    match = current_match(state["tournament"])
    launch = state["launch"]
    return (
        match is not None
        and launch.get("tournament_id") == state["tournament"]["id"]
        and launch.get("match_id") == match["id"]
        and launch.get("round_number") == sum(match["wins"]) + 1
        and pid is not None and observation.get("pid") == pid
        and observation.get("build") == SUPPORTED_SHA256
        and observation.get("round") == 1
        and observation.get("side") in (1, 2)
        and observation.get("finish") in (1, 3)
        and observation.get("samples", 0) >= 3
        and observation.get("outcome") == "winner"
        and not observation.get("error")
    )


def confirm_winner(tournament: dict, match_id: str, winner: str) -> None:
    match = current_match(tournament)
    if not match or match["id"] != match_id or winner not in match["players"]:
        raise UserError("The result is stale or the winner is not in the current match.")
    for r, matches in enumerate(tournament["rounds"]):
        if match in matches:
            i = matches.index(match)
            match["wins"][match["players"].index(winner)] += 1
            match["window_until"] = None
            if max(match["wins"]) < match["best_of"] // 2 + 1:
                return
            match["winner"] = winner
            match["confirmed_at"] = time.time()
            if r + 1 < len(tournament["rounds"]):
                tournament["rounds"][r + 1][i // 2]["players"][i % 2] = winner
            else:
                tournament["champion"] = winner
            return


def _require_state(condition: bool) -> None:
    if not condition:
        raise AssertionError


def validate_state(state: dict) -> None:
    try:
        _require_state(state["version"] == 1)
        _require_state(isinstance(state["game_path"], str))
        _require_state(state["launch"]["status"] in LAUNCH_STATES)
        _require_state(state.get("detector_verified") in (None, SUPPORTED_SHA256))
        if "automation" in state:
            _require_state(type(state["automation"]["enabled"]) is bool)
            _require_state(isinstance(state["automation"]["message"], str))
        _require_state(state.get("prediction_mode", "twitch") in {"local", "twitch", "chat"})
        _require_state(state.get("twitch_error") is None or isinstance(state["twitch_error"], str))
        report = state.get("portrait_update")
        if report:
            _require_state(report["status"] in {"running", "complete", "interrupted", "failed"})
            _require_state(all(type(report[k]) is int and report[k] >= 0 for k in ("total", "checked", "ready")))
            _require_state(report["ready"] <= report["checked"] <= report["total"])
            _require_state(isinstance(report["failures"], dict) and isinstance(report["message"], str))
            _require_state(len(report["failures"]) + report["ready"] == report["checked"])
            _require_state(all(isinstance(k, str) and isinstance(v, str) for k, v in report["failures"].items()))
        _require_state(isinstance(state.get("portrait_revision", ""), str))
        prediction = state.get("prediction")
        if prediction:
            _require_state(prediction["status"] in {"CREATING", "ACTIVE", "LOCKED", "RESOLVED", "CANCELED", "ABANDONED"})
            _require_state(all(isinstance(prediction[key], str) for key in ("title", "match_id", "tournament_id", "broadcaster_id")))
            kind = prediction.get("kind", "match")
            _require_state(kind in {"match", "champion"})
            options = len(prediction["players"])
            _require_state(options in ({2, 4} if kind == "champion" else {2}))
            _require_state(options == len(set(prediction["players"])))
            _require_state(len(prediction["labels"]) == options and isinstance(prediction["outcome_map"], dict))
            _require_state(all(isinstance(s, str) and 1 <= len(s) <= 25 for s in prediction["labels"]))
            _require_state(isinstance(prediction.get("cancel_requested", False), bool))
            _require_state(prediction["id"] is None or isinstance(prediction["id"], str))
            if prediction["status"] not in {"CREATING", "ABANDONED"}:
                _require_state(prediction["id"])
                _require_state(set(prediction["outcome_map"]) == set(prediction["players"]))
                _require_state(len(set(prediction["outcome_map"].values())) == options)
                _require_state(all(isinstance(value, str) and value for value in prediction["outcome_map"].values()))
                _require_state(type(prediction["deadline"]) in (int, float) and 0 <= prediction["deadline"] < 1e12)
            _require_state(prediction.get("provider", "twitch") in {"twitch", "chat"})
            outcome_stats = prediction.get("outcome_stats")
            if outcome_stats is not None:
                _require_state(prediction.get("provider", "twitch") == "twitch")
                _require_state(isinstance(outcome_stats, list) and len(outcome_stats) == 2)
                _require_state([row["player_id"] for row in outcome_stats] == prediction["players"])
                _require_state(all(
                    type(row["users"]) is int and row["users"] >= 0
                    and type(row["channel_points"]) is int and row["channel_points"] >= 0
                    for row in outcome_stats
                ))
            if prediction.get("provider") == "chat":
                _require_state(prediction["status"] in {"ACTIVE", "LOCKED", "CANCELED", "RESOLVED"})
                _require_state(isinstance(prediction["connection_id"], str))
                _require_state(type(prediction["started_at"]) in (int, float))
                _require_state(0 <= prediction["started_at"] < 1e12)
                duration = 180 if "kind" not in prediction or kind == "champion" else 60
                _require_state(prediction["deadline"] == prediction["started_at"] + duration)
                credit_bets = prediction.get("economy") == "credits"
                entries = prediction["bets"] if credit_bets else prediction["votes"]
                _require_state(isinstance(entries, dict) and len(entries) <= 10000)
                for uid, vote in entries.items():
                    _require_state(isinstance(uid, str) and uid.isdecimal() and len(uid) <= 30)
                    _require_state(vote["user_id"] == uid and vote["choice"] in range(1, options + 1))
                    _require_state(isinstance(vote["login"], str) and len(vote["login"]) <= 25)
                    _require_state(isinstance(vote["id"], str) and len(vote["id"]) <= 100)
                    _require_state(prediction["started_at"] <= vote["sent_at"] < prediction["deadline"])
                    _require_state(prediction["started_at"] <= vote["received_at"] < prediction["deadline"])
                    if credit_bets:
                        _require_state(type(vote["choice"]) is int)
                        _require_state(type(vote["amount"]) is int and 1 <= vote["amount"] <= MAX_WAGER)
                        board = state["betting_balances"][prediction["broadcaster_id"]]
                        _require_state(uid in board)
                if credit_bets and prediction["status"] in {"RESOLVED", "CANCELED"}:
                    _require_state(prediction["settlement"] in {"PAID", "REFUNDED"})
                    _require_state(type(prediction["payout_credits"]) is int)
                    _require_state(prediction["payout_credits"] == sum(bet["amount"] for bet in entries.values()))
        last_result = state.get("last_betting_result")
        if last_result is not None:
            _require_state(isinstance(last_result["tournament_id"], str) and isinstance(last_result["match_id"], str))
            summary = last_result["prediction"]
            _require_state(summary["provider"] == "chat" and summary["status"] in {"RESOLVED", "CANCELED"})
            _require_state(summary["settlement"] in {"PAID", "REFUNDED"})
            _require_state(len(summary["counts"]) == len(summary["pools"]) in (2, 4))
            _require_state(all(type(n) is int and n >= 0 for n in summary["counts"] + summary["pools"]))
            _require_state(type(summary["pool"]) is int and type(summary["payout_credits"]) is int)
            _require_state(summary["pool"] == sum(summary["pools"]) == summary["payout_credits"])
        _require_state(type(state.get("legacy_native_pending", False)) is bool)
        for channel, board in state.get("betting_balances", {}).items():
            _require_state(isinstance(channel, str) and channel.isascii() and channel.isdecimal() and isinstance(board, dict))
            for uid, row in board.items():
                _require_state(isinstance(uid, str) and uid.isascii() and uid.isdecimal() and len(uid) <= 30)
                _require_state(isinstance(row["login"], str) and re.fullmatch(r"[a-z0-9_]{1,25}", row["login"]))
                _require_state(type(row["credits"]) is int and 0 <= row["credits"] <= 2**53 - 1)
        for channel, board in state.get("chat_scores", {}).items():
            _require_state(isinstance(channel, str) and channel.isdecimal() and isinstance(board, dict))
            for uid, row in board.items():
                _require_state(isinstance(uid, str) and uid.isdecimal())
                _require_state(isinstance(row["login"], str) and len(row["login"]) <= 25)
                _require_state(type(row["points"]) is int and row["points"] >= 0)
        t = state["tournament"]
        if t is None:
            _require_state(not prediction or prediction["status"] in {"RESOLVED", "CANCELED", "ABANDONED"})
            return
        fighters = t["fighters"]
        ids = {f["id"] for f in fighters}
        _require_state(len(ids) == len(fighters) and len(ids) in SIZES)
        _require_state(all(isinstance(f["id"], str) and isinstance(f["name"], str) for f in fighters))
        _require_state(isinstance(t["id"], str) and isinstance(t["seed"], str))
        _require_state(type(t["window_seconds"]) is int and 0 <= t["window_seconds"] <= 600)
        _require_state(len(t["rounds"]) == len(ids).bit_length() - 1)
        count = len(ids) // 2
        first_players = []
        unfinished_seen = False
        for r, matches in enumerate(t["rounds"]):
            _require_state(len(matches) == count)
            for i, m in enumerate(matches):
                _require_state(m["id"] == f"r{r + 1}m{i + 1}")
                _require_state(len(m["players"]) == 2)
                if "best_of" in m:
                    _require_state(m["best_of"] in (1, 3))
                    _require_state(len(m["wins"]) == 2)
                    target = m["best_of"] // 2 + 1
                    _require_state(all(type(w) is int and 0 <= w <= target for w in m["wins"]))
                    if m["winner"] is None:
                        _require_state(max(m["wins"]) < target)
                    else:
                        _require_state(m["wins"][m["players"].index(m["winner"])] == target)
                        _require_state(m["wins"][1 - m["players"].index(m["winner"])] < target)
                _require_state(all(p is None or p in ids for p in m["players"]))
                if r == 0:
                    first_players.extend(m["players"])
                else:
                    _require_state(m["players"] == [t["rounds"][r - 1][2 * i + j]["winner"] for j in (0, 1)])
                _require_state(m["window_until"] is None or (
                    type(m["window_until"]) in (int, float) and 0 <= m["window_until"] < 1e12
                ))
                if m["winner"] is not None:
                    _require_state(not unfinished_seen)
                    _require_state(all(m["players"]) and m["winner"] in m["players"])
                else:
                    unfinished_seen = True
            count //= 2
        _require_state(set(first_players) == ids and len(first_players) == len(ids))
        _require_state(t["champion"] == t["rounds"][-1][0]["winner"])
        if prediction and prediction["status"] not in {"RESOLVED", "CANCELED", "ABANDONED"}:
            _require_state(prediction["tournament_id"] == t["id"])
            predicted = next((m for r in t["rounds"] for m in r if m["id"] == prediction["match_id"]), None)
            if prediction.get("kind") == "champion":
                candidates = ([p for semi in t["rounds"][-2] for p in semi["players"]]
                              if len(t["rounds"]) > 1 else t["rounds"][0][0]["players"])
                _require_state(predicted == t["rounds"][-1][0] and candidates == prediction["players"])
                _require_state(prediction["window_match_id"] == (t["rounds"][-2][0]["id"] if len(t["rounds"]) > 1 else predicted["id"]))
            else:
                _require_state(predicted and predicted["players"] == prediction["players"])
        presentation = t.get("presentation")
        if presentation:
            _require_state(presentation["match_id"] in {m["id"] for r in t["rounds"] for m in r})
            _require_state(all(type(presentation[key]) is bool for key in ("mandatory", "prepared")))
            _require_state(presentation["mode"] in {"twitch", "local", "chat"})
            if "betting_schedule" in presentation:
                _require_state(presentation["betting_schedule"] == 2)
                _require_state(presentation["betting_kind"] in {"match", "champion", "none"})
            for key in ("started_at", "full_until", "ready_at"):
                _require_state(presentation[key] is None or (type(presentation[key]) in (int, float) and 0 <= presentation[key] < 1e12))
            if presentation["prepared"]:
                _require_state(presentation["ready_at"] is not None)
                _require_state(presentation["full_until"] is not None)
                _require_state(presentation["ready_at"] >= presentation["full_until"] >= presentation["started_at"])
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        raise UserError("Invalid saved state; refusing to reset it. See README recovery instructions.") from exc


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if not self.path.exists():
            return new_state()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            validate_state(state)
            return state
        except (ValueError, OSError) as exc:
            raise UserError(f"Cannot load {self.path}: {exc}. See README recovery instructions.") from exc

    def save(self, state: dict) -> None:
        validate_state(state)
        payload = json.dumps(state, ensure_ascii=True, indent=2)
        if self.path.exists():
            self._atomic(self.path.with_suffix(".backup.json"), self.path.read_text(encoding="utf-8"))
        self._atomic(self.path, payload)

    @staticmethod
    def _atomic(path: Path, payload: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def build_command(game: str, p1: str, p2: str, stage: str) -> list[str]:
    root = Path(game).resolve()
    exe = root / "Mugen.exe"
    if not exe.is_file():
        raise UserError("Mugen.exe is missing.")
    fighter_def(root, p1)
    fighter_def(root, p2)
    if p1.casefold() == p2.casefold():
        raise UserError("A fighter cannot face itself.")
    stage_path = contained(root / "stages", stage.removeprefix("stages\\").removeprefix("stages/"))
    if stage_path.suffix.lower() != ".def" or not stage_path.is_file():
        raise UserError("Choose an existing stage definition.")
    binary = exe.read_bytes()
    if not all(flag in binary for flag in (b"-p<n> <playername>", b"-p<n>.ai 1", b"-s <stagename>", b"-rounds <num>")):
        raise UserError("This executable's embedded help does not verify the required CLI options.")
    return [
        str(exe), "-p1", p1, "-p2", p2,
        "-p1.ai", "1", "-p2.ai", "1",
        "-s", str(stage_path.relative_to(root / "stages")),
        "-rounds", "1",
    ]


def game_processes() -> list[str]:
    if os.name != "nt":
        raise UserError("Game launching and process checks require Windows.")
    result = subprocess.run(
        ["tasklist.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=15,
    )
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or any(len(row) < 2 for row in rows):
        raise UserError("Could not verify the Windows process list; launch is blocked.")
    return [row[1] for row in rows if row[0].lower() in {"mugen.exe", "3v3.exe", "4v4.exe"}]


class Application:
    def __init__(self, store: Store, broadcast_enabled: bool = True):
        self.store = store
        self.state = store.load()
        self.roster = {"fighters": [], "stages": [], "warnings": []}
        self.discovery_error = None
        self.process = None
        self.observer = None
        self.broadcast_enabled = broadcast_enabled
        self.predictions = Predictions(self)
        self.chat = ChatPredictions(self)
        self.portrait_queue = []
        migrated = copy.deepcopy(self.state)
        pending_chat = migrated.get("prediction")
        legacy_betting = pending_chat and pending_chat.get("economy") != "credits"
        if pending_chat and pending_chat.get("economy") != "credits":
            if pending_chat.get("provider", "twitch") == "twitch":
                migrated["legacy_native_prediction"] = migrated.pop("prediction")
                migrated["legacy_native_pending"] = pending_chat["status"] not in {"RESOLVED", "CANCELED", "ABANDONED"}
                if migrated["legacy_native_pending"]:
                    migrated["twitch_error"] = "Channel Points support removed. Manage the saved prediction in Twitch Creator Dashboard, then acknowledge it here; no remote refund was performed."
            else:
                migrated["legacy_chat_prediction"] = migrated.pop("prediction")
                migrated["twitch_error"] = "Free picks have been replaced by credit betting. Old scores are preserved separately, not converted; reconnect chat."
            pending_chat = None
        if migrated.get("prediction_mode", "twitch") == "twitch":
            migrated["prediction_mode"] = "chat"
        presentation = (migrated.get("tournament") or {}).get("presentation")
        migrating_window = legacy_betting and presentation and (
            self.state["prediction"]["match_id"] == presentation["match_id"]
            and self.state["prediction"]["tournament_id"] == migrated["tournament"]["id"]
        )
        if presentation and (presentation["mode"] == "twitch" or migrating_window):
            presentation["mode"] = migrated.get("prediction_mode", "chat")
            if pending_chat is None:
                migrated["tournament"].pop("presentation")
                ensure_presentation(migrated, current_match(migrated["tournament"]))
        if pending_chat and pending_chat["status"] == "ACTIVE":
            pending_chat["status"] = "CANCELED"
            refund_bets(migrated, pending_chat)
            migrated["twitch_error"] = "Server restarted during betting. All stakes refunded; reconnect and explicitly continue."
        # Preserve existing stakes/deadlines; apply the new schedule only to unstarted windows.
        presentation = (migrated.get("tournament") or {}).get("presentation")
        if presentation and presentation.get("betting_schedule") != 2 and not (
            pending_chat and pending_chat["status"] in {"ACTIVE", "LOCKED"}
        ):
            migrated["tournament"].pop("presentation")
            ensure_presentation(migrated, current_match(migrated["tournament"]))
        if migrated.get("portrait_update", {}).get("status") == "running":
            migrated["portrait_update"]["status"] = "interrupted"
            migrated["portrait_update"]["message"] = "Image update interrupted by server restart. Run Update roster & portraits again; cached images are retained."
        migrated.setdefault("automation", {"enabled": False, "message": "Automatic tournament is stopped."})
        migrated.setdefault("detector_verified", None)
        if migrated["automation"]["enabled"]:
            migrated["automation"] = {"enabled": False, "message": "Server restarted. Review the current match, then explicitly resume."}
        if migrated["tournament"]:
            for matches in migrated["tournament"]["rounds"]:
                for match in matches:
                    if "best_of" not in match:
                        # Preserve completed legacy results as single-win matches.
                        match["best_of"] = 1 if match["winner"] else (3 if len(matches) <= 2 else 1)
                        match["wins"] = [int(player == match["winner"]) if player else 0 for player in match["players"]]
            launch = migrated["launch"]
            pending = launch.get("pending_detected_winner")
            observation = launch.get("observation", {})
            if pending:
                match = current_match(migrated["tournament"])
                if (
                    launch.get("exit_code") == 0 and not launch.get("timed_out")
                    and verified_observation(migrated, observation, launch.get("pid"))
                    and match["players"][observation["side"] - 1] == pending
                ):
                    confirm_winner(migrated["tournament"], match["id"], pending)
                    launch["status"] = "idle"
                    launch["detected_winner"] = pending
                    launch["message"] = f"Saved engine result applied: {pending} won. Start automatic play to continue."
                else:
                    launch["message"] = "Saved detector result could not be verified; replay or review manually."
                del launch["pending_detected_winner"]
        if migrated != self.state:
            self.commit(migrated)
        if self.state["launch"]["status"] in {"launching", "running"}:
            state = copy.deepcopy(self.state)
            state["launch"]["status"] = "recovery_required"
            state["launch"]["message"] = "Server restarted during a match. Close the game, then acknowledge recovery."
            self.commit(state)
        self.refresh()
        self.detection_supported = supported(Path(self.state["game_path"]) / "Mugen.exe") if not self.discovery_error else False
        if self.broadcast_enabled:
            state = copy.deepcopy(self.state)
            if ensure_presentation(state, current_match(state["tournament"])):
                self.commit(state)

    def commit(self, state: dict) -> None:
        if self.broadcast_enabled:
            ensure_presentation(state, current_match(state["tournament"]))
        self.store.save(state)
        self.state = copy.deepcopy(state)

    def refresh(self) -> None:
        self.portraits = PortraitCache(Path(self.state["game_path"]), self.store.path.parent / "portraits",
                                      self.store.path.parent / "portrait-overrides")
        try:
            self.roster = discover(self.state["game_path"])
            self.discovery_error = None
        except (OSError, UserError, csv.Error) as exc:
            self.discovery_error = str(exc)
            self.roster = {"fighters": [], "stages": [], "warnings": []}

    def poll(self) -> None:
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                state = copy.deepcopy(self.state)
                state["launch"]["status"] = "awaiting_result" if code == 0 else "failed"
                state["launch"]["message"] = (
                    "Game exited with code 0. Verify the outcome manually; exit is NOT a winner signal."
                    if code == 0 else
                    f"Game exited with error code {code}. No result can be confirmed. Check the game error and replay the match."
                )
                state["launch"]["exit_code"] = code
                observation = self.observer.finish() if self.observer else {"error": "No observer was attached to this launch."}
                self.observer = None
                state["launch"]["observation"] = observation
                log = state["launch"].get("log")
                if log:
                    self.capture_game_log(state)
                    Store._atomic(Path(log).with_suffix(".result.json"), json.dumps({
                        "command": state["launch"].get("command"),
                        "stage": state["launch"].get("stage"),
                        "game_log": state["launch"].get("game_log"),
                        "game_log_capture": state["launch"].get("game_log_capture"),
                        "tournament_id": state["launch"].get("tournament_id"),
                        "match_id": state["launch"].get("match_id"),
                        "round_number": state["launch"].get("round_number"),
                        "exit_code": code, "timed_out": state["launch"].get("timed_out", False),
                        "observation": observation,
                    }, indent=2))
                match = current_match(state["tournament"])
                valid_context = verified_observation(state, observation, self.process.pid)
                if code == 0 and not state["launch"].get("timed_out") and observation.get("outcome") == "winner" and valid_context:
                    winner = match["players"][observation["side"] - 1]
                    confirm_winner(state["tournament"], match["id"], winner)
                    state["launch"]["status"] = "idle"
                    state["launch"]["message"] = f"Engine detected {winner} won the round. Score {match['wins'][0]}-{match['wins'][1]} saved."
                    state["launch"]["detected_winner"] = winner
                    if state["tournament"]["champion"]:
                        state["automation"] = {"enabled": False, "message": f"Tournament complete. Champion: {state['tournament']['champion']}"}
                else:
                    if state["launch"].get("timed_out"):
                        state["launch"]["status"] = "failed"
                    reason = (
                        "Round exceeded the 180-second watchdog; no result counted."
                        if state["launch"].get("timed_out") else
                        f"Game exited with error code {code}; no result counted." if code else
                        "Draw detected; no result counted. Replay or review manually." if observation.get("outcome") == "draw" else
                        observation.get("error", "Launch/result context did not match; manual review required.")
                    )
                    state["launch"]["message"] += f" Detection: {reason}"
                    if state["automation"]["enabled"]:
                        state["automation"] = {"enabled": False, "message": f"Automatic tournament paused: {reason}"}
                if log:
                    with Path(log).open("ab") as output:
                        output.write((
                            "\n[AutoMugen result]\n"
                            + json.dumps({"exit_code": code, "observation": observation,
                                          "message": state["launch"]["message"],
                                          "game_log": state["launch"].get("game_log"),
                                          "game_log_capture": state["launch"].get("game_log_capture")}, indent=2)
                            + "\n"
                        ).encode("utf-8"))
                self.commit(state)
                self.process = None

    def capture_game_log(self, state: dict) -> None:
        source = Path(state["game_path"]) / "mugen.log"
        target = Path(state["launch"]["log"]).with_suffix(".mugen.log")
        try:
            if not source.is_file():
                state["launch"]["game_log_capture"] = "Game did not create mugen.log."
                return
            stamp = source.stat()
            before = state["launch"].get("game_log_before")
            if before == [stamp.st_mtime_ns, stamp.st_size]:
                state["launch"]["game_log_capture"] = "mugen.log was not updated by this launch; previous log not copied."
                return
            shutil.copyfile(source, target)
            state["launch"]["game_log"] = str(target)
            state["launch"]["game_log_capture"] = "Game mugen.log copied for this launch."
        except OSError as exc:
            logging.getLogger("automugen").exception("Cannot preserve game log")
            state["launch"]["game_log_capture"] = f"Cannot copy mugen.log: {exc}"

    def tick(self) -> None:
        """Progress without relying on an open browser or HTTP polling."""
        self.poll()
        self.chat.tick()
        if self.state.get("portrait_update", {}).get("status") == "running":
            self.update_portrait_batch()
            return
        if not self.state["automation"]["enabled"]:
            return
        if self.process is not None:
            if time.time() - self.state["launch"]["started_at"] > 180:
                state = copy.deepcopy(self.state)
                state["automation"] = {"enabled": False, "message": "Automatic tournament paused: round exceeded 180 seconds. Stopping only the game launched by this server; no result counted."}
                state["launch"]["timed_out"] = True
                self.commit(state)
                self.process.terminate()
            return
        match = current_match(self.state["tournament"])
        if not match:
            return
        data = {
            "tournament_id": self.state["tournament"]["id"], "match_id": match["id"],
            "round_number": sum(match["wins"]) + 1,
        }
        try:
            if self.broadcast_enabled:
                presentation = self.state["tournament"]["presentation"]
                if not presentation["prepared"]:
                    self.predictions.prepare(local=presentation["mode"] == "local")
                    return
                if presentation["ready_at"] is None or time.time() < presentation["ready_at"]:
                    return
                self.predictions.guard_launch(match)
                self.act("launch", {**data, "stage": "random"})
                return
            if match["window_until"] is None:
                self.act("window", data)
            elif match["window_until"] <= time.time():
                self.act("launch", {**data, "stage": "random"})
        except (UserError, TwitchError, OSError, subprocess.SubprocessError) as exc:
            logging.getLogger("automugen").exception("Automatic tournament paused")
            state = copy.deepcopy(self.state)
            state["automation"] = {"enabled": False, "message": f"Automatic tournament paused: {exc}"}
            self.commit(state)

    def close_observer(self):
        if self.observer:
            self.observer.finish()
            self.observer = None

    def update_portrait_batch(self):
        state = copy.deepcopy(self.state)
        report = state["portrait_update"]
        for _ in range(min(8, len(self.portrait_queue))):
            entry = self.portrait_queue.pop(0)
            path = self.portraits.get(entry)
            report["checked"] += 1
            if path:
                report["ready"] += 1
            else:
                report["failures"][entry] = self.portraits.errors[entry]
        if not self.portrait_queue:
            report["status"] = "complete"
            report["message"] = f"Image update complete: {report['ready']} ready, {len(report['failures'])} failed. See Discovery notes for failures."
            state["portrait_revision"] = uuid.uuid4().hex
        self.commit(state)

    def snapshot(self) -> dict:
        self.poll()
        public_state = copy.deepcopy(self.state)
        if public_state.get("prediction"):
            public_state["prediction"].pop("votes", None)
            public_state["prediction"].pop("bets", None)
        public_state.pop("chat_scores", None)
        public_state.pop("betting_balances", None)
        public_state.pop("legacy_chat_prediction", None)
        public_state.pop("legacy_native_prediction", None)
        return {
            **public_state,
            "roster": {**self.roster, "warnings": self.roster["warnings"] + [
                f"Portrait {entry}: {error}" for entry, error in {
                    **self.state.get("portrait_update", {}).get("failures", {}),
                    **self.portraits.errors,
                }.items()
            ]},
            "discovery_error": self.discovery_error,
            "current_match": current_match(self.state["tournament"]),
            "server_time": time.time(),
            "detection": {
                "supported": self.detection_supported,
                "verified": self.state["detector_verified"] == SUPPORTED_SHA256,
                "method": "Read-only engine observer; exact executable build required.",
            },
            "twitch": self.predictions.public(),
            "broadcast": broadcast_view(self.state),
            "chat": self.chat.public(),
        }

    def require_no_game(self) -> None:
        if self.process is not None or game_processes():
            raise UserError("A MUGEN/3v3/4v4 process is still open. Close it before continuing.")

    def act(self, action: str, data: dict) -> None:
        self.poll()
        state = copy.deepcopy(self.state)
        if action == "legacy_ack":
            if not state.get("legacy_native_pending"):
                raise UserError("No old Channel Points prediction is awaiting acknowledgment.")
            if data.get("verified_handled") is not True:
                raise UserError("Confirm you handled the old Channel Points prediction in Twitch Creator Dashboard.")
            if state["automation"]["enabled"]:
                raise UserError("Pause automatic launches first.")
            self.require_no_game()
            state["legacy_native_pending"] = False
            state["twitch_error"] = None
            self.commit(state)
            return
        if action in {"chat_select", "chat_reconnect", "chat_continue", "chat_reopen"}:
            if state["automation"]["enabled"]:
                raise UserError("Pause automatic launches before changing chat controls.")
            self.require_no_game()
            if action == "chat_reconnect":
                if self.predictions.active():
                    raise UserError("Settle or refund open bets before reconnecting chat.")
                self.chat.reader.connect(self.chat.client)
                return
            if action == "chat_continue":
                if not self.chat.reader.connected or self.predictions.active():
                    raise UserError("Connect chat and settle or refund any open bets first.")
                state["twitch_error"] = None
                self.commit(state)
                return
            if self.predictions.active():
                raise UserError("Settle or refund current bets before changing betting controls.")
            if action == "chat_reopen":
                previous = state.get("prediction")
                if not previous or previous.get("provider") != "chat" or previous["status"] != "CANCELED":
                    raise UserError("Only a canceled and refunded betting window can be reopened.")
                if not self.chat.reader.connected:
                    raise UserError("Reconnect chat before reopening betting.")
                match = current_match(state["tournament"])
                if not match or previous.get("window_match_id", previous["match_id"]) != match["id"] or previous["tournament_id"] != state["tournament"]["id"]:
                    raise UserError("The canceled window is not for this match.")
            mode = "chat"
            state["prediction_mode"] = mode
            state["twitch_error"] = None
            if state["tournament"] and current_match(state["tournament"]):
                match = current_match(state["tournament"])
                p = state["tournament"]["presentation"]
                if p["prepared"] and p["mandatory"]:
                    if sum(match["wins"]) or (
                        state["launch"].get("match_id") == match["id"]
                        and state["launch"].get("tournament_id") == state["tournament"]["id"]
                    ):
                        raise UserError("Betting cannot reopen on a match that already began.")
                    p.update(prepared=False, full_until=None, ready_at=None)
                p["mode"] = mode
            self.commit(state)
            if action == "chat_reopen":
                self.chat.prepare()
            return
        if state.get("portrait_update", {}).get("status") == "running" and action in {
            "update_portraits", "refresh", "settings", "create", "auto_start", "launch",
        }:
            raise UserError("Wait until the roster/portrait update finishes before changing assets or launching games.")
        if action == "update_portraits":
            if state["automation"]["enabled"]:
                raise UserError("Pause automatic launches before updating the roster and portraits.")
            self.require_no_game()
            self.refresh()
            if self.discovery_error:
                raise UserError(self.discovery_error)
            self.detection_supported = supported(Path(state["game_path"]) / "Mugen.exe")
            entries = dict.fromkeys(f["id"] for f in self.roster["fighters"])
            if state["tournament"]:
                entries.update(dict.fromkeys(f["id"] for f in state["tournament"]["fighters"]))
            state["portrait_update"] = {
                "status": "running", "total": len(entries), "checked": 0, "ready": 0,
                "failures": {}, "message": "Checking game assets and generating missing/changed portraits...",
            }
            self.commit(state)
            self.portrait_queue = list(entries)
            return
        if action in {"prediction_prepare", "prediction_local", "prediction_sync", "prediction_cancel"}:
            if self.process is not None:
                raise UserError("Wait until the current game exits before changing betting controls.")
            try:
                if action in {"prediction_prepare", "prediction_local"}:
                    self.predictions.prepare(local=action == "prediction_local")
                else:
                    self.predictions.sync(cancel=action == "prediction_cancel")
            except (TwitchError, ValueError) as exc:
                self.predictions.failed(TwitchError(str(exc)))
            return
        if action == "auto_pause":
            state["automation"] = {"enabled": False, "message": "Automatic launches paused. Any current round will finish and be observed."}
            self.commit(state)
            return
        elif action == "auto_start":
            self.require_no_game()
            if not self.detection_supported or not supported(Path(state["game_path"]) / "Mugen.exe"):
                raise UserError("This Mugen.exe build does not support verified automatic winner observation.")
            if not current_match(state["tournament"]):
                raise UserError("Seed an unfinished tournament first.")
            if state["launch"]["status"] == "recovery_required":
                raise UserError("Acknowledge launch recovery before starting automatically.")
            state["automation"] = {"enabled": True, "message": "Automatic tournament running. Random stages; one round per launch; 180-second watchdog."}
            state["launch"]["status"] = "idle"
            state["launch"]["message"] = "Automatic run requested. Any previous unconfirmed round will be replayed."
            if self.broadcast_enabled and state.get("twitch_error"):
                raise UserError("Resolve the visible betting error before resuming.")
            self.commit(state)
            return
        elif action == "settings":
            if state["automation"]["enabled"]:
                raise UserError("Pause automatic launches before changing settings.")
            self.require_no_game()
            if state["tournament"] and not state["tournament"]["champion"]:
                raise UserError("Finish or explicitly replace the active tournament before changing game folders.")
            path = data.get("game_path")
            if not isinstance(path, str) or not path.strip():
                raise UserError("Enter a game folder.")
            discover(path)
            state["game_path"] = str(Path(path).resolve())
            self.commit(state)
            self.refresh()
            self.detection_supported = supported(Path(state["game_path"]) / "Mugen.exe")
            return
        if action == "refresh":
            self.refresh()
            if self.discovery_error:
                raise UserError(self.discovery_error)
            return
        if action == "create":
            if self.predictions.active():
                raise UserError("Settle or refund open bets (or handle the legacy prediction) before replacing the bracket.")
            if state["automation"]["enabled"]:
                raise UserError("Pause automatic launches before replacing the tournament.")
            self.require_no_game()
            if state["launch"]["status"] == "recovery_required":
                raise UserError("Acknowledge launch recovery before replacing a tournament.")
            if state["tournament"] and data.get("replace") is not True:
                raise UserError("Explicitly confirm replacement of the existing bracket.")
            if self.discovery_error:
                raise UserError(self.discovery_error)
            requested = data.get("fighters")
            allowed = {f["id"]: f for f in self.roster["fighters"]}
            if not isinstance(requested, list) or not all(isinstance(x, str) for x in requested):
                raise UserError("Select a fighter pool.")
            if len(set(requested)) != len(requested) or any(x not in allowed for x in requested):
                raise UserError("The selected fighter pool is stale or contains duplicates.")
            state["tournament"] = new_tournament(
                [allowed[x] for x in requested], data.get("size"), data.get("seed", ""), data.get("seconds"),
            )
            state["prediction_mode"] = "chat"
            state["twitch_error"] = None
            state["launch"] = {"status": "idle", "message": "Bracket seeded. Prepare chat betting, or explicitly choose local preview."}
        else:
            match = current_match(state["tournament"])
            if not match or data.get("match_id") != match["id"] or data.get("tournament_id") != state["tournament"]["id"]:
                raise UserError("This action is stale. Refresh the dashboard.")
            status = state["launch"]["status"]
            if action == "window":
                if self.broadcast_enabled:
                    try:
                        self.predictions.prepare(local=state["tournament"]["presentation"]["mode"] == "local")
                    except TwitchError as exc:
                        self.predictions.failed(exc)
                    return
                if status not in {"idle", "failed"}:
                    raise UserError("Cannot reopen the local window after this match has launched.")
                match["window_until"] = time.time() + state["tournament"]["window_seconds"]
            elif action == "recover":
                if status != "recovery_required":
                    raise UserError("No interrupted launch to recover.")
                self.require_no_game()
                state["launch"]["status"] = "awaiting_result"
                state["launch"]["message"] = "Recovery acknowledged. Confirm an observed result or explicitly replay the match."
            elif action == "result":
                if status != "awaiting_result":
                    raise UserError("Launch the match and close the game before confirming an observed result.")
                if data.get("round_number") != sum(match["wins"]) + 1:
                    raise UserError("This round confirmation is stale. Refresh the dashboard.")
                self.require_no_game()
                confirm_winner(state["tournament"], match["id"], data.get("winner"))
                continuing = match["winner"] is None
                state["launch"] = {
                    "status": "idle",
                    "message": (
                        f"Round win saved. Series score {match['wins'][0]}-{match['wins'][1]}. Launch the next round; first to two wins advances."
                        if continuing else "Match winner confirmed and bracket advanced."
                    ),
                }
            elif action == "launch":
                if self.broadcast_enabled:
                    p = state["tournament"]["presentation"]
                    if not p["prepared"] or p["ready_at"] is None or time.time() < p["ready_at"]:
                        raise UserError("Broadcast presentation is not ready. Prepare the window and wait for its full countdown/intermission.")
                    self.predictions.guard_launch(match)
                if status not in {"idle", "failed", "awaiting_result"}:
                    raise UserError("A launch is already active or needs recovery.")
                if match["window_until"] and match["window_until"] > time.time():
                    raise UserError("Wait until the local prediction window closes.")
                self.require_no_game()
                stage = data.get("stage", "random")
                if stage == "random":
                    if not self.roster["stages"]:
                        raise UserError("No stage definitions found; rescan the game folder.")
                    stage = secrets.choice(self.roster["stages"])["id"]
                if not isinstance(stage, str) or stage not in {s["id"] for s in self.roster["stages"]}:
                    raise UserError("Choose a stage from the discovered list.")
                logs = self.store.path.parent / "logs"
                logs.mkdir(parents=True, exist_ok=True)
                prefix = f"{state['tournament']['id']}-{match['id']}-{uuid.uuid4().hex[:8]}"
                log = logs / f"{prefix}.console.log"
                command = build_command(state["game_path"], *match["players"], stage)
                source_log = Path(state["game_path"]) / "mugen.log"
                before = source_log.stat() if source_log.is_file() else None
                state["launch"] = {
                    "status": "launching", "match_id": match["id"], "log": str(log),
                    "command": command, "stage": stage,
                    "tournament_id": state["tournament"]["id"],
                    "round_number": sum(match["wins"]) + 1, "started_at": time.time(),
                    "game_log_before": [before.st_mtime_ns, before.st_size] if before else None,
                    "message": "Launch intent saved. Starting MUGEN.",
                }
                self.commit(state)
                try:
                    with log.open("wb") as output:
                        output.write((
                            "[AutoMugen launcher]\nGame console output may be empty; "
                            "launcher/result records are labeled separately.\n"
                            + json.dumps({"command": command, "stage": stage,
                                          "tournament_id": state["tournament"]["id"],
                                          "match_id": match["id"]}, indent=2) + "\n[Game console]\n"
                        ).encode("utf-8"))
                        output.flush()
                        self.process = subprocess.Popen(
                            command, cwd=state["game_path"], stdin=subprocess.DEVNULL,
                            stdout=output, stderr=subprocess.STDOUT, shell=False,
                        )
                except OSError as exc:
                    state["launch"]["status"] = "failed"
                    state["launch"]["message"] = f"Launch failed: {exc}"
                    self.commit(state)
                    raise UserError(state["launch"]["message"]) from exc
                state["launch"]["status"] = "running"
                state["launch"]["pid"] = self.process.pid
                state["launch"]["message"] = "Game running. Observe the result, then close the game before confirming."
                if self.detection_supported:
                    try:
                        self.observer = WinnerObserver(self.process.pid, Path(state["game_path"]) / "Mugen.exe")
                        state["launch"]["message"] = "Game running. Read-only winner observer attached; result will be recorded after a clean exit."
                    except (ObservationError, OSError) as exc:
                        state["automation"] = {"enabled": False, "message": f"Automatic tournament paused: observer could not start: {exc}"}
                        state["launch"]["message"] = f"Game launched, but winner observation failed: {exc}. Manual review required."
            else:
                raise UserError("Unknown action.")
        self.commit(state)
