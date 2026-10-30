"""Persistent broadcast scheduling; rendering derives from server timestamps."""

import time

from chat import public_bets

WINDOW = 180
MATCH_WINDOW = 60
INTERMISSION = 24
ROTATE = 8
RESULT_DURATION = 15
TERMINAL = {"RESOLVED", "CANCELED", "ABANDONED"}


def special_window(tournament, match):
    return betting_kind(tournament, match) != "none"


def betting_kind(tournament, match):
    if not match:
        return "none"
    rounds = tournament["rounds"]
    if len(rounds) == 1:
        return "champion"
    if any(match["id"] == m["id"] for r in rounds[:-2] for m in r):
        return "match"
    return "champion" if match["id"] == rounds[-2][0]["id"] else "none"


def window_seconds(presentation):
    return MATCH_WINDOW if presentation.get("betting_kind") == "match" else WINDOW


def ensure_presentation(state, match, now=None):
    """Return whether a new match presentation was created (never per BO3 round)."""
    t = state["tournament"]
    if not t or not match:
        return False
    now = time.time() if now is None else now
    old = t.get("presentation")
    if old and old["match_id"] == match["id"]:
        return False
    special = special_window(t, match)
    if sum(match.get("wins", [0, 0])) or (
        state["launch"].get("match_id") == match["id"]
        and state["launch"].get("tournament_id") == t["id"]
    ):
        # Migration must not accept bets on a fight/series that already began.
        special = False
    t["presentation"] = {
        "match_id": match["id"], "started_at": now,
        "mandatory": special, "full_until": None if special else now,
        "ready_at": None if special else now + INTERMISSION,
        "mode": state.get("prediction_mode", "chat"), "prepared": False,
        "betting_kind": betting_kind(t, match) if special else "none",
        "betting_schedule": 2,
    }
    return True


def view(state, now=None):
    now = time.time() if now is None else now
    t = state["tournament"]
    p = t.get("presentation") if t else None
    prediction = state.get("prediction")
    if not t:
        return {"screen": "bracket", "phase": "standby", "remaining": 0}
    if t["champion"]:
        return {"screen": "bracket", "phase": "complete", "remaining": 0}
    if state["launch"]["status"] in {"running", "launching"}:
        return {"screen": "game", "phase": "fighting", "remaining": 0}
    interrupted = state["launch"]["status"] in {"failed", "recovery_required", "awaiting_result"}
    if not p:
        return {"screen": "bracket", "phase": "waiting", "remaining": 0}
    until = p["full_until"]
    remaining = max(0, (until or now) - now)
    if p["mandatory"] and (until is None or remaining > 0):
        return {
            "screen": "bracket", "phase": "prediction" if until else "setup",
            "remaining": remaining, "deadline": until, "mode": p["mode"],
        }
    anchor = until if p["mandatory"] else p["started_at"]
    screen = "bracket" if int(max(0, now - anchor) // ROTATE) % 2 == 0 else "matchup"
    return {
        "screen": screen, "phase": "review" if interrupted else "intermission",
        "remaining": max(0, (p["ready_at"] or now) - now), "deadline": p["ready_at"],
        "mode": p["mode"],
        "prediction_status": prediction.get("status") if prediction else None,
    }


def result_view(state, now=None):
    """Return a short-lived, aggregate-only result card for the latest match."""
    tournament = state.get("tournament")
    if not tournament:
        return None

    completed = [
        (match["confirmed_at"], round_number, match_number, match)
        for round_number, matches in enumerate(tournament["rounds"])
        for match_number, match in enumerate(matches)
        if match.get("winner") and type(match.get("confirmed_at")) in (int, float)
    ]
    if not completed:
        return None

    confirmed_at, round_number, _, match = max(completed, key=lambda result: result[:3])
    now = time.time() if now is None else now
    expires_at = confirmed_at + RESULT_DURATION
    if not confirmed_at <= now < expires_at:
        return None

    result = {
        "match_id": match["id"],
        "round": round_number,
        "confirmed_at": confirmed_at,
        "expires_at": expires_at,
        "winner": match["winner"],
        "players": list(match["players"]),
        "prediction": None,
    }
    prediction = state.get("prediction")
    if (
        not prediction
        or prediction.get("tournament_id") != tournament["id"]
        or prediction.get("match_id") != match["id"]
        or prediction.get("status") not in {"ACTIVE", "LOCKED", "RESOLVED", "CANCELED"}
    ):
        saved = state.get("last_betting_result")
        if saved and saved["tournament_id"] == tournament["id"] and saved["match_id"] == match["id"]:
            result["prediction"] = saved["prediction"]
        return result

    if prediction.get("status") == "CANCELED":
        result["prediction"] = {
            "provider": "chat",
            "status": "CANCELED",
            "payout_credits": prediction.get("payout_credits", 0),
            "settlement": "REFUNDED",
        }
        return result

    if prediction.get("economy") == "credits":
        result["prediction"] = public_bets(prediction)
    return result
