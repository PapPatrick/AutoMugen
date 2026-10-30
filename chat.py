"""Authenticated chat betting with persistent, non-redeemable virtual credits."""

import copy
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
import uuid
from urllib.parse import urlencode

from twitch import TwitchClient, TwitchError


STARTING_CREDITS = 1000
MINIMUM_CREDITS = 100
MAX_WAGER = 1_000_000_000
MESSAGE_CLOCK_TOLERANCE = 2
LOGIN = re.compile(r"[a-z0-9_]{1,25}")


class ChatClient(TwitchClient):
    """Only a separate bot account may authorize access to the stream's chat."""

    def __init__(self):
        super().__init__()
        self.channel_login = os.environ.get("AUTOMUGEN_TWITCH_CHAT_CHANNEL", "").strip().lower().lstrip("#")

    def require_channel(self):
        if not LOGIN.fullmatch(self.channel_login):
            raise TwitchError("Set AUTOMUGEN_TWITCH_CHAT_CHANNEL to your streaming channel's login, then restart. A separate bot account is required.")

    def authorize_url(self, redirect_uri):
        self.require_channel()
        return super().authorize_url(redirect_uri)

    def validate(self):
        self.require_channel()
        super().validate()
        if self.login == self.channel_login:
            self.validated_until = 0
            raise TwitchError("The streaming account cannot be used as the bot. Sign in with a separate Twitch bot account.")

    def resolve_channel(self):
        """Resolve the required stream channel independently of the bot identity."""
        self.validate()
        target = self.channel_login
        if not LOGIN.fullmatch(self.login or "") or not LOGIN.fullmatch(target or ""):
            raise TwitchError("Invalid Twitch login. AUTOMUGEN_TWITCH_CHAT_CHANNEL must be a channel login like 'mystream'.")
        users = self.request("GET", "https://api.twitch.tv/helix/users?" + urlencode({"login": target}))
        rows = users.get("data")
        if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get("login") != target):
            raise TwitchError(f"Twitch channel '{target}' was not found.")
        channel_id = rows[0].get("id")
        if not isinstance(channel_id, str) or not channel_id.isdecimal():
            raise TwitchError("Twitch returned an invalid channel ID.")
        if channel_id == self.user_id:
            raise TwitchError("The streaming account cannot be used as the bot. Sign in with a separate Twitch bot account.")
        return target, channel_id


def parse_command(line, channel, received_at):
    """Only tagged, recent messages in the authorized channel can place bets."""
    if not line.startswith("@") or " " not in line:
        return None
    raw_tags, message = line[1:].split(" ", 1)
    tags = dict(part.split("=", 1) for part in raw_tags.split(";") if "=" in part)
    match = re.fullmatch(
        r":([a-z0-9_]+)![^ ]+ PRIVMSG #([a-z0-9_]+) :(!balance|!bet (all|[0-9]{1,10}) ([1-4]))\s*",
        message, re.I,
    )
    if not match or match[2].lower() != channel:
        return None
    user_id, message_id, sent = (tags.get(k, "") for k in ("user-id", "id", "tmi-sent-ts"))
    if (not user_id.isascii() or not user_id.isdecimal() or len(user_id) > 30
            or not message_id or len(message_id) > 100 or not sent.isascii()
            or not sent.isdecimal() or len(sent) > 16 or len(match[1]) > 25):
        return None
    sent_at = int(sent) / 1000
    if not -MESSAGE_CLOCK_TOLERANCE <= received_at - sent_at <= 10:
        return None
    sent_at = min(sent_at, received_at)
    amount = match[4]
    if amount and amount.lower() != "all" and not 1 <= int(amount) <= MAX_WAGER:
        return None
    return {
        "user_id": user_id, "login": match[1].lower(),
        "command": "bet" if amount else "balance",
        "amount": "all" if amount and amount.lower() == "all" else int(amount) if amount else None,
        "choice": int(match[5]) if amount else None,
        "id": message_id, "sent_at": sent_at, "received_at": received_at,
    }


def refund_bets(state, prediction):
    board = state.setdefault("betting_balances", {}).setdefault(prediction["broadcaster_id"], {})
    for user_id, bet in prediction["bets"].items():
        board[user_id]["credits"] += bet["amount"]
    prediction["settlement"] = "REFUNDED"
    prediction["payout_credits"] = sum(bet["amount"] for bet in prediction["bets"].values())
    remember_result(state, prediction)


def public_bets(prediction):
    counts, pools = [0] * len(prediction["players"]), [0] * len(prediction["players"])
    for bet in prediction["bets"].values():
        counts[bet["choice"] - 1] += 1
        pools[bet["choice"] - 1] += bet["amount"]
    return {
        "provider": "chat", "status": prediction["status"],
        "counts": counts, "pools": pools, "pool": sum(pools),
        "settlement": prediction.get("settlement"),
        "payout_credits": prediction.get("payout_credits"),
        "players": list(prediction["players"]), "kind": prediction.get("kind", "match"),
    }


def remember_result(state, prediction):
    summary = public_bets(prediction)
    summary["status"] = "CANCELED" if prediction["status"] == "CANCELED" else "RESOLVED"
    state["last_betting_result"] = {
        "tournament_id": prediction["tournament_id"],
        "match_id": prediction["match_id"],
        "prediction": summary,
    }


def settle_bets(state, prediction, winner):
    """Redistribute the exact pool using deterministic largest-remainder rounding."""
    bets = prediction["bets"]
    winning = {uid: bet for uid, bet in bets.items() if bet["choice"] == winner}
    winning_pool = sum(bet["amount"] for bet in winning.values())
    pool = sum(bet["amount"] for bet in bets.values())
    if not winning_pool or winning_pool == pool:
        refund_bets(state, prediction)
        return
    payouts = {uid: bet["amount"] * pool // winning_pool for uid, bet in winning.items()}
    remainder = pool - sum(payouts.values())
    order = sorted(winning, key=lambda uid: (-(winning[uid]["amount"] * pool % winning_pool), uid))
    for uid in order[:remainder]:
        payouts[uid] += 1
    board = state["betting_balances"][prediction["broadcaster_id"]]
    for uid, credits in payouts.items():
        board[uid]["credits"] += credits
    prediction["settlement"] = "PAID"
    prediction["payout_credits"] = pool
    remember_result(state, prediction)


class ChatReader:
    def __init__(self):
        self.events = queue.Queue(maxsize=5000)
        self.outgoing = queue.Queue(maxsize=5000)
        self.stop_event = threading.Event()
        self.thread = None
        self.connected = False
        self.error = "Not connected. Sign in for virtual-credit chat betting."
        self.reply_error = None
        self.channel = ""
        # account_id is the joined channel's (room) ID; bets and balances belong to it.
        self.account_id = ""
        self.bot_login = ""
        self.connection_id = ""
        self.commands_received = 0
        self.commands_rejected = 0
        self.replies_sent = 0

    def connect(self, client, expected_channel_id=None):
        self.close()
        channel, channel_id = client.resolve_channel()
        if expected_channel_id is not None and channel_id != expected_channel_id:
            raise TwitchError("Locked bets belong to a different stream channel. Reconnect to their original channel.")
        self.events = queue.Queue(maxsize=5000)
        self.outgoing = queue.Queue(maxsize=5000)
        self.reply_error = None
        self.commands_received = 0
        self.commands_rejected = 0
        self.replies_sent = 0
        self.stop_event = threading.Event()
        self.channel, self.account_id, self.bot_login = channel, channel_id, client.login
        self.connection_id = uuid.uuid4().hex
        self.error = "Connecting to Twitch chat..."
        self.thread = threading.Thread(target=self.run, args=(client.token,), daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=12)
            if self.thread.is_alive():
                raise TwitchError("Chat connection is still stopping. Retry after a few seconds.")
        self.connected = False

    def say(self, message):
        if not self.connected:
            return
        try:
            self.outgoing.put_nowait(message.replace("\r", " ").replace("\n", " ")[:400])
        except queue.Full:
            self.reply_error = "Chat reply queue is full. Some confirmations were not sent; saved bets and balances are unaffected."
            logging.getLogger("automugen").warning(self.reply_error)

    def run(self, token):
        try:
            with socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=10) as raw:
                with ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv") as stream:
                    stream.settimeout(1)
                    stream.sendall((
                        f"PASS oauth:{token}\r\nNICK {self.bot_login or self.channel}\r\n"
                        "CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership\r\n"
                        f"JOIN #{self.channel}\r\n"
                    ).encode())
                    buffer = b""
                    last_received = time.monotonic()
                    joined_by = last_received + 20
                    next_reply = last_received
                    while not self.stop_event.is_set():
                        if self.connected and time.monotonic() >= next_reply:
                            try:
                                reply = self.outgoing.get_nowait()
                            except queue.Empty:
                                pass
                            else:
                                stream.sendall(f"PRIVMSG #{self.channel} :{reply}\r\n".encode())
                                self.replies_sent += 1
                                next_reply = time.monotonic() + 1.6
                        try:
                            chunk = stream.recv(8192)
                        except socket.timeout:
                            if time.monotonic() - last_received > 360:
                                raise TwitchError("Chat connection timed out. Reconnect before preparing another window.")
                            if not self.connected and time.monotonic() > joined_by:
                                raise TwitchError("Twitch did not confirm joining chat. Check authorization and reconnect.")
                            continue
                        if not chunk:
                            raise TwitchError("Twitch chat disconnected. Reconnect explicitly.")
                        last_received = time.monotonic()
                        buffer += chunk
                        if len(buffer) > 65536:
                            raise TwitchError("Chat message exceeded the safe receive limit.")
                        while b"\r\n" in buffer:
                            line_bytes, buffer = buffer.split(b"\r\n", 1)
                            line = line_bytes.decode("utf-8", errors="replace")
                            if line.startswith("PING "):
                                stream.sendall(("PONG " + line[5:] + "\r\n").encode())
                            elif line == ":tmi.twitch.tv RECONNECT" or re.match(r"^(?:@[^ ]+ )?:tmi\.twitch\.tv NOTICE ", line) and (
                                "authentication failed" in line.lower() or "improperly formatted auth" in line.lower()
                                or "msg_banned" in line or "msg_channel_suspended" in line
                            ):
                                raise TwitchError("Twitch chat requested reconnect or rejected authorization/channel access.")
                            elif re.fullmatch(rf"@([^ ]+) :tmi\.twitch\.tv ROOMSTATE #{re.escape(self.channel)}", line):
                                tags = dict(part.split("=", 1) for part in line[1:].split(" ", 1)[0].split(";") if "=" in part)
                                if tags.get("room-id") != self.account_id:
                                    raise TwitchError("Twitch joined a different channel than the configured stream channel.")
                                self.connected, self.error = True, None
                            elif re.match(r"^@[^ ]+ :tmi\.twitch\.tv NOTICE ", line):
                                tags = dict(part.split("=", 1) for part in line[1:].split(" ", 1)[0].split(";") if "=" in part)
                                notice = tags.get("msg-id", "")
                                if re.fullmatch(r"(?:msg|write|whisper)_[a-z_]{1,60}", notice):
                                    self.reply_error = (
                                        f"Twitch rejected a bot message ({notice[:80]}). "
                                        "Check the bot's channel permissions, verified account status and chat restrictions."
                                    )
                                    logging.getLogger("automugen").warning(self.reply_error)
                            command = parse_command(line, self.channel, time.time())
                            if command:
                                self.commands_received += 1
                                self.events.put_nowait(command)
                            elif re.search(r" PRIVMSG #[a-z0-9_]+ :!(?:bet|balance)(?:\s|$)", line, re.I):
                                self.commands_rejected += 1
        except (OSError, TwitchError, queue.Full):
            # Never include socket requests/credentials in diagnostics.
            self.error = "Twitch chat connection failed or receive queue overflowed. Reconnect; open bets will be refunded."
        finally:
            self.connected = False
            if not self.error:
                self.error = "Chat disconnected. Reconnect before opening another window."


class ChatPredictions:
    def __init__(self, app):
        self.app = app
        self.client = ChatClient()
        self.reader = ChatReader()
        self.pending_events = []

    def public(self):
        p = self.app.state.get("prediction")
        totals = public_bets(p) if p and p.get("economy") == "credits" else {
            "counts": [0, 0], "pools": [0, 0], "pool": 0,
        }
        channel = self.reader.account_id or self.app.state.get("chat_channel_id", "")
        balances = self.app.state.get("betting_balances", {}).get(channel, {})
        leaders = sorted(balances.values(), key=lambda row: (-row["credits"], row["login"]))[:5]
        return {"connected": self.reader.connected, "channel": self.reader.channel,
                "bot": self.reader.bot_login,
                "message": self.reader.error or self.reader.reply_error or "Connected. !bet 100 1 / !bet all 2 / !balance. Virtual credits only.",
                "counts": totals["counts"], "pools": totals["pools"], "pool": totals["pool"],
                "players": totals.get("players", []), "kind": totals.get("kind", "match"),
                "commands_received": self.reader.commands_received,
                "commands_rejected": self.reader.commands_rejected,
                "replies_sent": self.reader.replies_sent,
                "leaderboard": leaders}

    def prepare(self):
        from automugen import current_match
        from broadcast import ensure_presentation, window_seconds
        state = copy.deepcopy(self.app.state)
        m = current_match(state["tournament"])
        if not m:
            raise TwitchError("No current match.")
        ensure_presentation(state, m)
        p = state["tournament"]["presentation"]
        if p["prepared"]:
            return
        if self.app.predictions.active() and p["mandatory"]:
            raise TwitchError("Settle or refund the previous betting window before opening another.")
        if p["mandatory"] and not self.reader.connected:
            raise TwitchError("Connect Twitch chat first. Use Sign in as bot with a separate account; no Affiliate status needed.")
        p["mode"] = "chat"
        p["prepared"] = True
        if p["mandatory"]:
            now = time.time()
            duration = window_seconds(p)
            p["full_until"], p["ready_at"] = now + duration, now + duration
            kind = p.get("betting_kind", "match")
            t = state["tournament"]
            players = ([player for semi in t["rounds"][-2] for player in semi["players"]]
                       if kind == "champion" and len(t["rounds"]) > 1 else list(m["players"]))
            if not all(players):
                raise TwitchError("All semifinalists must be known before champion betting opens.")
            names = {f["id"]: f["name"] for f in state["tournament"]["fighters"]}
            state["prediction"] = {
                "provider": "chat", "economy": "credits", "status": "ACTIVE", "id": uuid.uuid4().hex,
                "kind": kind, "window_match_id": m["id"],
                "title": "Grand final champion" if kind == "champion" else f"Match winner / {m['id']}",
                "match_id": t["rounds"][-1][0]["id"] if kind == "champion" else m["id"],
                "tournament_id": t["id"], "players": players,
                "labels": [f"P{i + 1} {names[player]}"[:25] for i, player in enumerate(players)],
                "outcome_map": {player: str(i + 1) for i, player in enumerate(players)},
                "broadcaster_id": self.reader.account_id, "deadline": now + duration, "started_at": now,
                "connection_id": self.reader.connection_id, "bets": {},
            }
        if self.reader.account_id:
            state["chat_channel_id"] = self.reader.account_id
        state["twitch_error"] = None
        self.app.commit(state)
        if p["mandatory"]:
            label = "CHAMPION BETTING" if kind == "champion" else "MATCH BETTING"
            self.reader.say(f"{label} OPEN for {duration} seconds. !bet 100 <number> / !bet all <number> / !balance. One final bet per window. Virtual credits only.")
            for i, player in enumerate(players):
                self.reader.say(f"Choice {i + 1}: {names[player]}. Type !bet 100 {i + 1}.")

    def tick(self):
        for _ in range(5000 - len(self.pending_events)):
            try:
                self.pending_events.append(self.reader.events.get_nowait())
            except queue.Empty:
                break
        self._tick(self.pending_events)
        self.pending_events = []

    def _tick(self, events):
        state = copy.deepcopy(self.app.state)
        p = state.get("prediction")
        open_bets = p and p.get("economy") == "credits" and p["status"] in {"ACTIVE", "LOCKED"}
        if open_bets and p["status"] == "ACTIVE" and (
            not self.reader.connected or p["connection_id"] != self.reader.connection_id
            or p["broadcaster_id"] != self.reader.account_id
        ):
            p["status"] = "CANCELED"
            refund_bets(state, p)
            state["twitch_error"] = "Chat was interrupted during betting. All stakes refunded. Reconnect, then explicitly continue."
            state["automation"] = {"enabled": False, "message": state["twitch_error"]}
            self.app.commit(state)
            return
        changed = False
        replies = []
        for event in events:
            uid, login = event["user_id"], event["login"]
            if not self.reader.connected:
                continue
            board = state.setdefault("betting_balances", {}).setdefault(self.reader.account_id, {})
            row = board.get(uid, {"login": login, "credits": STARTING_CREDITS})
            if event["command"] == "balance":
                board[uid] = {**row, "login": login}
                changed = True
                replies.append(f"@{login}: {row['credits']:,} credits available.")
                continue
            if not open_bets or p["status"] != "ACTIVE" or not (
                p["started_at"] <= event["sent_at"] < p["deadline"]
                and p["started_at"] <= event["received_at"] < p["deadline"]
            ):
                replies.append(f"@{login}: betting is closed.")
                continue
            if uid in p["bets"]:
                replies.append(f"@{login}: one final bet per window; your existing bet is unchanged.")
                continue
            if not 1 <= event["choice"] <= len(p["players"]):
                replies.append(f"@{login}: choose a number from 1 to {len(p['players'])}; bet not accepted.")
                continue
            if len(p["bets"]) >= 10000:
                replies.append(f"@{login}: participant limit reached; bet not accepted.")
                continue
            available = max(MINIMUM_CREDITS, row["credits"])
            amount = available if event["amount"] == "all" else event["amount"]
            if amount > available or amount > MAX_WAGER:
                replies.append(f"@{login}: bet rejected; available {available:,}, maximum bet {MAX_WAGER:,} credits.")
                continue
            board[uid] = {"login": login, "credits": available - amount}
            p["bets"][uid] = {**event, "amount": amount}
            changed = True
            fighter = p["players"][event["choice"] - 1]
            name = next(f["name"] for f in state["tournament"]["fighters"] if f["id"] == fighter)
            target = "grand final champion" if p.get("kind") == "champion" else f"match {p['match_id']}"
            replies.append(
                f"@{login}: BET CONFIRMED - {amount:,} credits on #{event['choice']} {name[:80]} "
                f"to win {target}; {available - amount:,} credits remaining."
            )
        if open_bets and p["status"] == "ACTIVE":
            if time.time() >= p["deadline"]:
                p["status"] = "LOCKED"
                changed = True
                target = "grand final champion" if p.get("kind") == "champion" else "full match winner"
                replies.append(f"BETTING CLOSED. Stakes are locked until the {target} is known.")
        if not open_bets:
            if changed:
                self.app.commit(state)
            for reply in replies:
                self.reader.say(reply)
            return
        t = state["tournament"]
        if not t or t["id"] != p["tournament_id"]:
            raise TwitchError("Betting tournament mismatch. Refund the window before continuing.")
        match = next(m for r in t["rounds"] for m in r if m["id"] == p["match_id"])
        if match["winner"]:
            if p["status"] != "LOCKED":
                raise TwitchError("Cannot settle bets before the window is locked.")
            winner = p["players"].index(match["winner"]) + 1
            settle_bets(state, p, winner)
            p["status"] = "RESOLVED"
            p["winner"] = match["winner"]
            changed = True
            replies.append(f"P{winner} wins! {p['payout_credits']:,} credits " +
                           ("refunded (no winning or opposing stakes)." if p["settlement"] == "REFUNDED" else "paid to winning bettors, including their stakes."))
        if changed:
            self.app.commit(state)
        for reply in replies:
            self.reader.say(reply)

    def cancel(self):
        p = self.app.state.get("prediction")
        if not p or p.get("provider") != "chat" or p["status"] not in {"ACTIVE", "LOCKED"}:
            raise TwitchError("No open credit-betting window to refund.")
        state = copy.deepcopy(self.app.state)
        state["prediction"]["status"] = "CANCELED"
        refund_bets(state, state["prediction"])
        state["automation"] = {"enabled": False, "message": "Bets canceled and all stakes refunded; automatic launches paused."}
        state["twitch_error"] = None
        self.app.commit(state)
        self.reader.say("BETTING CANCELED. All stakes have been refunded.")
