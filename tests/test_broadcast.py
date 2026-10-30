import copy
import hashlib
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from automugen import Application, UserError, confirm_winner, current_match, validate_state
from broadcast import ensure_presentation, result_view, special_window, view
from portraits import _png
from server import Server
from test_automugen import Fixture
from twitch import TwitchClient, TwitchError


class BroadcastTests(Fixture):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch("time.time", return_value=1000))
        self.app = Application(self.store)
        with patch("automugen.game_processes", return_value=[]):
            self.app.act("create", {"fighters": [f["id"] for f in self.fighters], "size": 8, "seconds": 0, "seed": "broadcast"})
        reader = self.app.chat.reader
        reader.connected, reader.account_id = True, "123"
        reader.channel, reader.connection_id, reader.error = "caster", "connection", None

    def advance_one(self):
        state = copy.deepcopy(self.app.state)
        match = current_match(state["tournament"])
        confirm_winner(state["tournament"], match["id"], match["players"][0])
        self.app.commit(state)

    def test_exact_60_seconds_no_early_launch_or_refresh_reset(self):
        self.app.predictions.prepare()
        presentation = self.app.state["tournament"]["presentation"]
        self.assertEqual(presentation["full_until"], 1060)
        self.assertEqual(presentation["ready_at"], 1060)
        for now in (1000, 1030, 1059.999):
            self.clock.return_value = now
            self.app.snapshot()
            self.assertEqual(view(self.app.state)["screen"], "bracket")
            with self.assertRaisesRegex(UserError, "not ready"):
                self.app.act("launch", self.context(self.app))
        self.app.predictions.prepare()
        self.assertEqual(self.app.state["prediction"]["deadline"], 1060)

    def test_rotation_and_confirmed_lock_before_launch(self):
        self.app.predictions.prepare()
        with self.assertRaisesRegex(TwitchError, "not confirmed locked"):
            self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))
        self.clock.return_value = 1060
        self.app.tick()
        self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))
        self.clock.return_value = 1068
        self.assertEqual(view(self.app.state)["screen"], "matchup")
        state = copy.deepcopy(self.app.state)
        state["automation"]["enabled"] = True
        self.app.commit(state)
        with patch.object(self.app, "act") as action:
            self.clock.return_value = 1059.999
            self.app.tick()
            action.assert_not_called()
            self.clock.return_value = 1060
            self.app.tick()
            self.assertEqual(action.call_args.args[0], "launch")

    def test_each_early_match_then_one_champion_window_not_bo3_rounds(self):
        self.app.predictions.prepare(local=True)
        presentations = []
        while current_match(self.app.state["tournament"]):
            match = current_match(self.app.state["tournament"])
            presentation = copy.deepcopy(self.app.state["tournament"]["presentation"])
            presentations.append((match["id"], presentation["mandatory"]))
            self.app.predictions.prepare(local=True)
            self.advance_one()
            if current_match(self.app.state["tournament"]) and current_match(self.app.state["tournament"])["id"] == match["id"]:
                self.assertTrue(self.app.state["tournament"]["presentation"]["prepared"])
        self.assertEqual({mid for mid, mandatory in presentations if mandatory}, {"r1m1", "r1m2", "r1m3", "r1m4", "r2m1"})
        self.assertEqual(view(self.app.state)["phase"], "complete")

    def test_new_tournament_restores_chat_default_after_local_preview(self):
        self.app.predictions.prepare(local=True)
        with patch("automugen.game_processes", return_value=[]):
            self.app.act("create", {"fighters": [f["id"] for f in self.fighters], "size": 8, "seconds": 180, "replace": True})
        self.assertEqual(self.app.state["tournament"]["presentation"]["mode"], "chat")

    def test_local_preview_between_early_matches_applies_to_future_semifinals(self):
        self.advance_one()
        self.app.act("prediction_local", {})
        for _ in range(3):
            self.advance_one()
        self.assertEqual(self.app.state["tournament"]["presentation"]["mode"], "local")

    def test_upgrade_does_not_open_betting_partway_through_a_series(self):
        state = copy.deepcopy(self.app.state)
        for _ in range(4):
            match = current_match(state["tournament"])
            confirm_winner(state["tournament"], match["id"], match["players"][0])
        match = current_match(state["tournament"])
        self.assertTrue(special_window(state["tournament"], match))
        confirm_winner(state["tournament"], match["id"], match["players"][0])
        state["tournament"].pop("presentation")
        ensure_presentation(state, match)
        self.assertFalse(state["tournament"]["presentation"]["mandatory"])

    def test_corrupt_presentation_rejected(self):
        state = copy.deepcopy(self.app.state)
        state["tournament"]["presentation"]["ready_at"] = float("nan")
        with self.assertRaises(UserError):
            validate_state(state)

    def test_match_result_overlay_visible_for_exactly_fifteen_seconds(self):
        self.advance_one()
        result = result_view(self.app.state, now=1000)
        self.assertEqual(result["match_id"], "r1m1")
        self.assertEqual(result["expires_at"], 1015)
        self.assertIsNone(result["prediction"])
        self.assertIsNone(result_view(self.app.state, now=1015))

    def test_latest_match_wins_result_overlay_when_timestamps_tie(self):
        self.advance_one()
        self.advance_one()
        self.assertEqual(result_view(self.app.state, now=1000)["match_id"], "r1m2")

    def test_old_native_prediction_requires_manual_ack_without_remote_calls(self):
        self.app.predictions.prepare()
        state = copy.deepcopy(self.app.state)
        prediction = state["prediction"]
        prediction.pop("bets")
        prediction.pop("economy")
        prediction.pop("provider")
        state["prediction_mode"] = state["tournament"]["presentation"]["mode"] = "twitch"
        self.store.save(state)
        restarted = Application(self.store)
        self.assertTrue(restarted.state["legacy_native_pending"])
        self.assertEqual(restarted.state["legacy_native_prediction"]["id"], prediction["id"])
        self.assertEqual(restarted.predictions.public()["legacy_prediction"]["id"], prediction["id"])
        self.assertNotIn("prediction", restarted.state)
        with self.assertRaises(TwitchError):
            restarted.predictions.prepare()
        with patch("automugen.game_processes", return_value=[]):
            with self.assertRaises(UserError):
                restarted.act("legacy_ack", {})
            restarted.act("legacy_ack", {"verified_handled": True})
        self.assertFalse(restarted.state["legacy_native_pending"])
        self.assertNotIn("legacy_native_prediction", restarted.snapshot())

    def test_old_free_scores_preserved_but_not_converted_to_credits(self):
        self.app.predictions.prepare()
        state = copy.deepcopy(self.app.state)
        state["prediction"].pop("bets")
        state["prediction"].pop("economy")
        state["prediction"]["votes"] = {}
        state["chat_scores"] = {"123": {"42": {"login": "viewer", "points": 17}}}
        self.store.save(state)
        restarted = Application(self.store)
        self.assertIn("legacy_chat_prediction", restarted.state)
        self.assertEqual(restarted.state["chat_scores"]["123"]["42"]["points"], 17)
        self.assertNotIn("betting_balances", restarted.state)
        self.assertFalse(restarted.state["tournament"]["presentation"]["prepared"])

    def test_portrait_http_allowlist_and_diagnostics(self):
        entry = self.fighters[0]["id"]
        folder = self.app.portraits.overrides_dir
        folder.mkdir(parents=True)
        (folder / (hashlib.sha256(entry.encode()).hexdigest() + ".png")).write_bytes(_png(1, 1, b"\xff\0\0\xff"))
        server = Server(0, self.app)
        try:
            from urllib.parse import quote
            client = server.flask_app.test_client()
            base_url = f"http://127.0.0.1:{server.server_port}"
            response = client.get("/api/portrait?id=" + quote(entry), base_url=base_url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data.startswith(b"\x89PNG"))
            self.assertEqual(client.get("/api/portrait?id=..%5Csecret", base_url=base_url).status_code, 404)
            self.assertEqual(client.get("/api/portrait?id=" + quote(self.fighters[1]["id"]), base_url=base_url).status_code, 404)
            self.assertTrue(any("Portrait Fighter 1:" in s for s in self.app.snapshot()["roster"]["warnings"]))
        finally:
            server.server_close()


class TwitchClientTests(unittest.TestCase):
    def client(self):
        with patch.dict("os.environ", {}, clear=True):
            client = TwitchClient()
        client.client_id, client.token, client.user_id = "client", "secret", "123"
        return client

    def authorized(self):
        return [
            {"client_id": "client", "user_id": "123", "scopes": ["chat:read", "chat:edit"], "expires_in": 3600, "login": "test"},
        ]

    def test_authorization_and_each_chat_scope_checked_without_affiliate(self):
        client = self.client()
        with patch.object(client, "request", side_effect=self.authorized()) as request:
            client.validate()
            client.validate()
            self.assertEqual(request.call_count, 1)
        for field, value in (("client_id", "wrong"), ("user_id", "wrong"),
                             ("scopes", ["chat:read"]), ("scopes", ["chat:edit"]), ("expires_in", 0)):
            client = self.client()
            response = self.authorized()
            response[0][field] = value
            with self.subTest(field=field, value=value), patch.object(client, "request", side_effect=response), self.assertRaises(TwitchError):
                client.validate()

    def test_bot_id_derived_from_validated_user(self):
        client = self.client()
        client.user_id = ""
        with patch.object(client, "request", side_effect=self.authorized()):
            client.validate()
        self.assertEqual(client.user_id, "123")

    def test_http_errors_do_not_expose_tokens(self):
        from urllib.error import HTTPError, URLError
        client = self.client()
        for error in (HTTPError("https://api.twitch.tv", 401, "secret", {}, None), URLError("secret")):
            with patch("twitch.urlopen", side_effect=error), self.assertRaises(TwitchError) as raised:
                client.request("GET", "https://id.twitch.tv/oauth2/validate")
            self.assertNotIn("secret", str(raised.exception))

    def test_oauth_state_single_use_token_memory_only(self):
        client = self.client()
        params = parse_qs(urlsplit(client.authorize_url("http://localhost:8765/auth/callback")).query)
        self.assertEqual(params["scope"], ["chat:read chat:edit"])
        with patch.object(client, "request", side_effect=self.authorized()):
            client.authorize("newtoken", params["state"][0])
        self.assertEqual(client.token, "newtoken")
        with self.assertRaisesRegex(TwitchError, "state"):
            client.authorize("injected", params["state"][0])
        client.authorize_url("http://localhost:8765/auth/callback")
        with self.assertRaisesRegex(TwitchError, "state"):
            client.authorize("injected", "wrong")
