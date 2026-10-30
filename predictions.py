"""Local betting lifecycle and launch guards; no Twitch prediction API."""

import copy
import time

from broadcast import TERMINAL, ensure_presentation, window_seconds
from twitch import TwitchError


class Predictions:
    def __init__(self, app):
        self.app = app

    def public(self):
        prediction = self.app.state.get("prediction")
        legacy = self.app.state.get("legacy_native_prediction")
        return {
            "connected": self.app.chat.reader.connected,
            "channel": self.app.chat.reader.channel,
            "message": self.app.state.get("twitch_error") or self.app.chat.public()["message"],
            "prediction": {k: v for k, v in prediction.items() if k not in {"bets", "votes"}} if prediction else None,
            "error": self.app.state.get("twitch_error"),
            "legacy_pending": bool(self.app.state.get("legacy_native_pending")),
            "legacy_prediction": {key: legacy.get(key) for key in ("id", "title", "status")} if legacy else None,
        }

    def active(self):
        prediction = self.app.state.get("prediction")
        return self.app.state.get("legacy_native_pending") or (
            prediction and prediction["status"] not in TERMINAL
        )

    def failed(self, exc):
        state = copy.deepcopy(self.app.state)
        state["twitch_error"] = str(exc)
        state["automation"] = {"enabled": False, "message": f"Betting requires attention: {exc}"}
        self.app.commit(state)
        raise exc

    def prepare(self, local=False):
        from automugen import current_match
        if self.app.state.get("legacy_native_pending"):
            raise TwitchError("Handle the old Channel Points prediction in Twitch Creator Dashboard, then acknowledge it here.")
        if not local:
            self.app.chat.prepare()
            return
        if self.active():
            raise TwitchError("Settle or refund the open credit bets before switching to local preview.")
        state = copy.deepcopy(self.app.state)
        match = current_match(state["tournament"])
        if not match:
            raise TwitchError("No current match to prepare.")
        ensure_presentation(state, match)
        presentation = state["tournament"]["presentation"]
        state["prediction_mode"] = presentation["mode"] = "local"
        state["twitch_error"] = None
        if not presentation["prepared"]:
            now = time.time()
            presentation["prepared"] = True
            if presentation["mandatory"]:
                duration = window_seconds(presentation)
                presentation.update(full_until=now + duration, ready_at=now + duration)
        self.app.commit(state)

    def sync(self, cancel=False):
        if self.app.state.get("legacy_native_pending"):
            raise TwitchError("Manage the old Channel Points prediction on Twitch; this app no longer calls its API.")
        if cancel:
            self.app.chat.cancel()
        else:
            self.app.chat.tick()

    def guard_launch(self, match):
        if self.app.state.get("twitch_error") or self.app.state.get("legacy_native_pending"):
            raise TwitchError("Resolve the visible betting error before launching.")
        prediction = self.app.state.get("prediction")
        if prediction and prediction["status"] not in TERMINAL:
            tournament = self.app.state["tournament"]
            champion = prediction.get("kind") == "champion"
            eligible = (any(match["id"] == m["id"] for r in tournament["rounds"][-2:] for m in r)
                        and all(player in prediction["players"] for player in match["players"])) if champion else prediction["match_id"] == match["id"]
            if not eligible or prediction["tournament_id"] != tournament["id"]:
                raise TwitchError("Settle or refund the previous match's bets before launching.")
            if prediction["status"] != "LOCKED":
                raise TwitchError("Credit betting is not confirmed locked. Wait for the betting window to close.")
