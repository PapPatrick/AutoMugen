import copy
import json
import queue
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from automugen import Application, UserError, confirm_winner, current_match
from broadcast import result_view
from chat import ChatClient, ChatReader, parse_command, settle_bets
from server import Server
from test_automugen import Fixture
from twitch import TwitchError
from winner import SUPPORTED_SHA256


def line(command="!bet 100 1", *, channel="caster", user="42", sent=1001, message="msg1", login="viewer"):
    return (f"@id={message};tmi-sent-ts={int(sent * 1000)};user-id={user} "
            f":{login}!{login}@{login}.tmi.twitch.tv PRIVMSG #{channel} :{command}")


class ChatParserTests(unittest.TestCase):
    def test_locked_pool_reconnect_rejects_different_channel(self):
        reader, client = ChatReader(), Mock()
        client.resolve_channel.return_value = ("other", "456")
        with patch("chat.threading.Thread") as thread, self.assertRaisesRegex(TwitchError, "original channel"):
            reader.connect(client, expected_channel_id="123")
        thread.assert_not_called()
        self.assertFalse(reader.connected)

    def test_small_clock_skew_does_not_silently_drop_balance_or_bets(self):
        for command in ("!balance", "!bet 100 2"):
            for ahead in (0.001, 0.5, 2):
                with self.subTest(command=command, ahead=ahead):
                    event = parse_command(line(command, sent=1001 + ahead), "caster", 1001)
                    self.assertIsNotNone(event)
                    self.assertEqual(event["sent_at"], 1001)
            self.assertIsNone(parse_command(line(command, sent=1004), "caster", 1001))
            self.assertIsNone(parse_command(line(command, sent=990), "caster", 1001))

    def test_integer_payouts_conserve_every_credit_for_small_pools(self):
        for first in range(1, 8):
            for second in range(1, 8):
                for losing in range(1, 8):
                    state = {"betting_balances": {"123": {
                        "1": {"credits": 0}, "2": {"credits": 0}, "3": {"credits": 0},
                    }}}
                    prediction = {
                        "players": ["a", "b"],
                        "status": "LOCKED", "broadcaster_id": "123", "tournament_id": "t", "match_id": "m",
                        "bets": {"1": {"choice": 1, "amount": first},
                                 "2": {"choice": 1, "amount": second},
                                 "3": {"choice": 2, "amount": losing}},
                    }
                    settle_bets(state, prediction, 1)
                    board = state["betting_balances"]["123"]
                    self.assertEqual(sum(row["credits"] for row in board.values()), first + second + losing)
                    self.assertGreaterEqual(board["1"]["credits"], first)
                    self.assertGreaterEqual(board["2"]["credits"], second)
                    self.assertEqual(board["3"]["credits"], 0)

    def test_strict_commands_channel_tags_and_time(self):
        self.assertEqual(parse_command(line(), "caster", 1001.1)["amount"], 100)
        self.assertEqual(parse_command(line("!bet all 2"), "caster", 1001.1)["choice"], 2)
        self.assertEqual(parse_command(line("!balance"), "caster", 1001.1)["command"], "balance")
        self.assertEqual(parse_command(line("!bet all 4"), "caster", 1001.1)["choice"], 4)
        for invalid in (
            line(channel="someone_else"), line("!bet 100 5"), line("!pick 1"),
            line("hello !bet 100 1"), line("!bet -1 1"), line("!bet 0 1"),
            line("!bet 1.5 1"), line("!bet 1000000001 1"), line("!bet 100 1 extra"),
            line().replace("PRIVMSG", "WHISPER"), line(user=""),
            line(message=""), line(sent=990), line(sent=1010),
            line().split(" ", 1)[1], line(user="4" * 31),
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(parse_command(invalid, "caster", 1001.1))

    def test_chat_authorization_needs_no_affiliate_or_prediction_scope(self):
        client = ChatClient()
        client.client_id, client.token = "client", "token"
        client.channel_login = "streamer"
        with patch.object(client, "request", side_effect=[
            {"client_id": "client", "user_id": "123", "login": "caster",
             "scopes": ["chat:read", "chat:edit"], "expires_in": 300},
        ]):
            client.validate()
        self.assertEqual(client.user_id, "123")
        params = parse_qs(urlsplit(client.authorize_url("http://localhost:8765/auth/callback")).query)
        self.assertEqual(params["scope"], ["chat:read chat:edit"])
        self.assertFalse(hasattr(client, "predictions"))
        self.assertFalse(hasattr(client, "create"))
        self.assertFalse(hasattr(client, "change"))

    def test_bot_channel_is_required_before_login_or_connect(self):
        for target in ("", "Bad Name!"):
            client = ChatClient()
            client.channel_login = target
            with self.subTest(target=target), patch.object(client, "request") as request:
                with self.assertRaisesRegex(TwitchError, "AUTOMUGEN_TWITCH_CHAT_CHANNEL"):
                    client.authorize_url("http://localhost:8765/auth/callback")
                with self.assertRaises(TwitchError):
                    client.resolve_channel()
                request.assert_not_called()

    def test_streaming_account_authorization_is_rejected(self):
        client = ChatClient()
        client.client_id, client.channel_login = "client", "caster"
        url = client.authorize_url("http://localhost:8765/auth/callback")
        state = parse_qs(urlsplit(url).query)["state"][0]
        with patch.object(client, "request", return_value={
            "client_id": "client", "user_id": "123", "login": "caster",
            "scopes": ["chat:read", "chat:edit"], "expires_in": 300,
        }):
            with self.assertRaisesRegex(TwitchError, "separate Twitch bot account"):
                client.authorize("token", state)
        self.assertEqual(client.token, "")
        self.assertEqual(client.validated_until, 0)

    def test_resolved_channel_cannot_have_bot_id(self):
        client = ChatClient()
        client.client_id, client.token, client.channel_login = "client", "token", "caster"
        with patch.object(client, "request", side_effect=[
            {"client_id": "client", "user_id": "999", "login": "bot",
             "scopes": ["chat:read", "chat:edit"], "expires_in": 300},
            {"data": [{"id": "999", "login": "caster"}]},
        ]), self.assertRaisesRegex(TwitchError, "separate Twitch bot account"):
            client.resolve_channel()

    def test_bot_account_joins_configured_stream_channel(self):
        client = ChatClient()
        client.client_id, client.token, client.channel_login = "client", "token", "caster"
        with patch.object(client, "request", side_effect=[
            {"client_id": "client", "user_id": "999", "login": "mugen_bot",
             "scopes": ["chat:read", "chat:edit"], "expires_in": 300},
            {"data": [{"id": "123", "login": "caster"}]},
        ]) as request:
            reader = ChatReader()
            with patch.object(reader, "run"):
                reader.connect(client)
        self.assertIn("login=caster", request.call_args_list[-1].args[1])
        self.assertEqual((reader.channel, reader.account_id, reader.bot_login), ("caster", "123", "mugen_bot"))

        stream, raw = Mock(), Mock()
        for obj in (stream, raw):
            obj.__enter__ = Mock(return_value=obj)
            obj.__exit__ = Mock(return_value=False)
        stream.recv.side_effect = [b"@room-id=999 :tmi.twitch.tv ROOMSTATE #caster\r\n", b""]
        with patch("chat.socket.create_connection", return_value=raw), \
                patch("chat.ssl.create_default_context") as context:
            context.return_value.wrap_socket.return_value = stream
            reader.run("secret")
        sent = b"".join(call.args[0] for call in stream.sendall.call_args_list)
        self.assertIn(b"NICK mugen_bot\r\n", sent)
        self.assertIn(b"JOIN #caster\r\n", sent)
        self.assertFalse(reader.connected)
        self.assertNotIn("secret", reader.error)

    def test_bot_channel_rejects_unknown_or_mismatched_channel(self):
        for rows, target in (([], "caster"), ([{"id": "5", "login": "wrong"}], "caster"),
                             ([{"id": "5", "login": "caster"}], "Bad Name!")):
            client = ChatClient()
            client.client_id, client.token, client.channel_login = "client", "token", target
            with self.subTest(rows=rows, target=target), patch.object(client, "request", side_effect=[
                {"client_id": "client", "user_id": "999", "login": "mugen_bot",
                 "scopes": ["chat:read", "chat:edit"], "expires_in": 300},
                {"data": rows},
            ]):
                with self.assertRaises(TwitchError):
                    client.resolve_channel()

    def test_tls_irc_join_ping_bet_and_rate_limited_replies(self):
        reader = ChatReader()
        reader.channel, reader.account_id = "caster", "123"
        stream, raw = Mock(), Mock()
        for obj in (stream, raw):
            obj.__enter__ = Mock(return_value=obj)
            obj.__exit__ = Mock(return_value=False)
        stream.recv.side_effect = [
            b"PING :tmi.twitch.tv\r\n@room-id=123 :tmi.twitch.tv ROOMSTATE #caster\r\n",
            (line() + "\r\n").encode(), b"",
        ]
        reader.outgoing.put_nowait("first reply")
        reader.outgoing.put_nowait("second reply")
        with patch("chat.socket.create_connection", return_value=raw) as connect, \
                patch("chat.ssl.create_default_context") as context, \
                patch("chat.time.time", return_value=1001.1), patch("chat.time.monotonic", return_value=10):
            context.return_value.wrap_socket.return_value = stream
            reader.run("secret")
        connect.assert_called_with(("irc.chat.twitch.tv", 6697), timeout=10)
        context.return_value.wrap_socket.assert_called_with(raw, server_hostname="irc.chat.twitch.tv")
        sent = b"".join(call.args[0] for call in stream.sendall.call_args_list)
        self.assertIn(b"PASS oauth:secret", sent)
        self.assertIn(b"PONG :tmi.twitch.tv", sent)
        self.assertIn(b"PRIVMSG #caster :first reply", sent)
        self.assertNotIn(b"second reply", sent)
        self.assertEqual(reader.events.get_nowait()["choice"], 1)
        self.assertEqual(reader.commands_received, 1)
        self.assertEqual(reader.replies_sent, 1)
        self.assertFalse(reader.connected)
        self.assertNotIn("secret", reader.error)

    def test_rejected_commands_and_twitch_send_restrictions_are_visible(self):
        reader = ChatReader()
        reader.channel, reader.account_id = "caster", "123"
        stream, raw = Mock(), Mock()
        for obj in (stream, raw):
            obj.__enter__ = Mock(return_value=obj)
            obj.__exit__ = Mock(return_value=False)
        stream.recv.side_effect = [
            b"@room-id=123 :tmi.twitch.tv ROOMSTATE #caster\r\n",
            (line("!balance", sent=1010) + "\r\n" +
             "@msg-id=msg_requires_verified_phone_number :tmi.twitch.tv NOTICE #caster :Private notice body\r\n").encode(),
            b"",
        ]
        with patch("chat.socket.create_connection", return_value=raw), \
                patch("chat.ssl.create_default_context") as context, \
                patch("chat.time.time", return_value=1001.1), \
                self.assertLogs("automugen", level="WARNING") as logs:
            context.return_value.wrap_socket.return_value = stream
            reader.run("secret")
        self.assertEqual(reader.commands_received, 0)
        self.assertEqual(reader.commands_rejected, 1)
        self.assertIn("msg_requires_verified_phone_number", reader.reply_error)
        self.assertNotIn("Private notice body", str(logs.output))
        self.assertNotIn("secret", str(logs.output))

    def test_queue_overflow_fails_visibly_without_credential_leak(self):
        reader = ChatReader()
        reader.channel, reader.account_id = "caster", "123"
        stream, raw = Mock(), Mock()
        for obj in (stream, raw):
            obj.__enter__ = Mock(return_value=obj)
            obj.__exit__ = Mock(return_value=False)
        stream.recv.return_value = (line() + "\r\n").encode()
        reader.events.put_nowait = Mock(side_effect=queue.Full)
        with patch("chat.socket.create_connection", return_value=raw), \
                patch("chat.ssl.create_default_context") as context, patch("chat.time.time", return_value=1001.1):
            context.return_value.wrap_socket.return_value = stream
            reader.run("secret")
        self.assertFalse(reader.connected)
        self.assertIn("overflowed", reader.error)
        self.assertNotIn("secret", reader.error)


class ChatBettingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.clock = self.enterContext(patch("time.time", return_value=1000))
        self.app = Application(self.store)
        with patch("automugen.game_processes", return_value=[]):
            self.app.act("create", {"fighters": [f["id"] for f in self.fighters], "size": 8, "seconds": 0, "seed": "chat"})
        self.reader = self.app.chat.reader
        self.reader.connected = True
        self.reader.account_id = "123"
        self.reader.channel = "caster"
        self.reader.connection_id = "conn"
        self.reader.error = None

    def prepare(self):
        self.app.predictions.prepare()
        return current_match(self.app.state["tournament"])

    def command(self, command="!bet 100 1", **kwargs):
        event = parse_command(line(command, **kwargs), "caster", kwargs.get("sent", 1001) + 0.1)
        self.assertIsNotNone(event)
        self.reader.events.put_nowait(event)

    def finish_match(self, side=0):
        state = copy.deepcopy(self.app.state)
        match = current_match(state["tournament"])
        confirm_winner(state["tournament"], match["id"], match["players"][side])
        self.app.commit(state)

    def balance(self, user="42"):
        return self.app.state["betting_balances"]["123"][user]["credits"]

    def lock(self):
        self.clock.return_value = self.app.state["prediction"]["deadline"]
        self.app.tick()
        self.assertEqual(self.app.state["prediction"]["status"], "LOCKED")

    def test_one_final_bet_debits_once_and_persists(self):
        self.prepare()
        self.command()
        self.command("!bet 400 2", sent=1002, message="msg2")
        self.command()
        self.app.tick()
        self.assertEqual(self.balance(), 900)
        self.assertEqual(self.app.chat.public()["counts"], [1, 0])
        self.assertEqual(self.app.chat.public()["pools"], [100, 0])
        self.assertEqual(len(self.store.load()["prediction"]["bets"]), 1)

    def test_channel_confirmation_names_fighter_amount_target_and_balance_after_save(self):
        self.prepare()
        self.command("!bet 250 2")
        messages = []

        def capture(message):
            saved = self.store.load()
            self.assertEqual(saved["prediction"]["bets"]["42"]["amount"], 250)
            messages.append(message)

        with patch.object(self.reader, "say", side_effect=capture):
            self.app.chat.tick()
        name = next(f["name"] for f in self.app.state["tournament"]["fighters"]
                    if f["id"] == self.app.state["prediction"]["players"][1])
        self.assertIn(f"@viewer: BET CONFIRMED - 250 credits on #2 {name}", messages[0])
        self.assertIn("match r1m1", messages[0])
        self.assertIn("750 credits remaining", messages[0])

    def test_proportional_payouts_conserve_pool_and_round_deterministically(self):
        self.prepare()
        self.command("!bet 1 1", user="42")
        self.command("!bet 2 1", user="43", login="second")
        self.command("!bet 7 2", user="44", login="loser")
        self.lock()
        self.finish_match()
        self.app.tick()
        self.app.tick()
        self.assertEqual(self.balance("42"), 1002)
        self.assertEqual(self.balance("43"), 1005)
        self.assertEqual(self.balance("44"), 993)
        self.assertEqual(sum(row["credits"] for row in self.app.state["betting_balances"]["123"].values()), 3000)
        result = result_view(self.app.state, now=1060)["prediction"]
        self.assertEqual(result["counts"], [2, 1])
        self.assertEqual(result["pools"], [3, 7])
        self.assertEqual(result["payout_credits"], 10)
        self.assertEqual(result["settlement"], "PAID")
        self.assertNotIn("bets", json.dumps(result))

    def test_result_payout_survives_preparation_of_next_betting_window(self):
        self.prepare()
        self.command()
        self.command("!bet 300 2", user="43", login="loser")
        self.lock()
        self.finish_match()
        self.app.tick()
        self.app.predictions.prepare()
        self.assertEqual(self.app.state["prediction"]["match_id"], "r1m2")
        result = result_view(self.app.state, now=1060)
        self.assertEqual(result["match_id"], "r1m1")
        self.assertEqual(result["prediction"]["payout_credits"], 400)
        self.assertEqual(result["prediction"]["counts"], [1, 1])
        self.assertIsNone(result_view(self.app.state, now=1075))

    def test_missing_opposing_or_winning_bets_refunds_every_stake(self):
        for side in (0, 1):
            with self.subTest(side=side):
                self.prepare()
                self.command("!bet 1000 1", sent=self.clock.return_value + 1)
                self.lock()
                self.finish_match(side)
                if current_match(self.app.state["tournament"])["id"] == self.app.state["prediction"]["match_id"]:
                    self.finish_match(side)
                self.app.tick()
                self.assertEqual(self.balance(), 1000)
                self.assertEqual(self.app.state["prediction"]["settlement"], "REFUNDED")

    def test_all_in_and_low_balance_refill_only_before_accepted_bet(self):
        self.prepare()
        state = copy.deepcopy(self.app.state)
        state["betting_balances"] = {"123": {"42": {"login": "viewer", "credits": 15}}}
        self.app.commit(state)
        self.command("!bet 101 1")
        self.app.tick()
        self.assertEqual(self.balance(), 15)
        self.command("!bet all 1", sent=1002, message="all")
        self.app.tick()
        self.assertEqual(self.balance(), 0)
        self.assertEqual(self.app.state["prediction"]["bets"]["42"]["amount"], 100)
        self.command("!bet all 2", sent=1003, message="again")
        self.app.tick()
        self.assertEqual(self.balance(), 0)
        self.app.chat.cancel()
        self.assertEqual(self.balance(), 100)

    def test_balance_registers_once_without_repeated_refills(self):
        self.command("!balance")
        self.app.tick()
        self.assertEqual(self.balance(), 1000)
        state = copy.deepcopy(self.app.state)
        state["betting_balances"]["123"]["42"]["credits"] = 20
        self.app.commit(state)
        self.command("!balance", message="again")
        self.app.tick()
        self.assertEqual(self.balance(), 20)
        self.assertIn("20 credits", self.reader.outgoing.get_nowait() + self.reader.outgoing.get_nowait())

    def test_no_bets_before_open_at_deadline_or_after_lock(self):
        self.prepare()
        for stamp in (999, 1060):
            self.command(sent=stamp)
        self.lock()
        self.assertEqual(self.app.chat.public()["pool"], 0)
        self.command(sent=1061)
        self.app.tick()
        self.assertEqual(self.app.chat.public()["pool"], 0)

    def test_disconnect_refunds_once_and_pauses(self):
        self.prepare()
        self.command()
        self.app.tick()
        self.reader.connected = False
        self.app.tick()
        self.app.tick()
        self.assertEqual(self.balance(), 1000)
        self.assertEqual(self.app.state["prediction"]["status"], "CANCELED")
        self.assertFalse(self.app.state["automation"]["enabled"])
        with self.assertRaises(TwitchError):
            self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))

    def test_connection_or_account_change_refunds_open_bets(self):
        self.prepare()
        self.command()
        self.app.tick()
        self.reader.connection_id = "different"
        self.app.tick()
        self.assertEqual(self.balance(), 1000)

    def test_open_restart_refunds_once_locked_restart_settles_once(self):
        self.prepare()
        self.command()
        self.app.tick()
        restarted = Application(self.store)
        self.assertEqual(restarted.state["prediction"]["status"], "CANCELED")
        self.assertEqual(restarted.state["betting_balances"]["123"]["42"]["credits"], 1000)
        again = Application(self.store)
        self.assertEqual(again.state["betting_balances"]["123"]["42"]["credits"], 1000)

    def test_locked_bets_survive_restart_and_cannot_double_pay(self):
        self.prepare()
        self.command("!bet 100 1")
        self.command("!bet 300 2", user="43", login="loser")
        self.lock()
        self.finish_match()
        restarted = Application(self.store)
        restarted.tick()
        again = Application(self.store)
        again.tick()
        self.assertEqual(again.state["betting_balances"]["123"]["42"]["credits"], 1300)
        self.assertEqual(again.state["prediction"]["status"], "RESOLVED")

    def test_champion_bets_settle_only_after_grand_final(self):
        for _ in range(4):
            self.finish_match()
        self.prepare()
        self.command()
        self.lock()
        self.finish_match()
        self.app.tick()
        self.assertEqual(self.app.state["prediction"]["status"], "LOCKED")
        self.assertEqual(self.balance(), 900)
        self.finish_match()
        self.app.tick()
        self.assertEqual(self.balance(), 900)
        self.assertEqual(self.app.state["prediction"]["status"], "LOCKED")
        original = self.app.state["prediction"]["id"]
        self.app.predictions.prepare()
        self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))
        for _ in range(2):
            self.finish_match()
            self.app.tick()
        self.app.predictions.prepare()
        self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))
        self.assertEqual(self.app.state["prediction"]["id"], original)
        self.finish_match()
        self.app.tick()
        self.assertEqual(self.balance(), 900)
        self.finish_match()
        self.app.tick()
        self.assertEqual(self.balance(), 1000)
        self.assertEqual(self.app.state["prediction"]["status"], "RESOLVED")

    def test_fourth_candidate_payout_survives_restart_and_semifinal_elimination(self):
        for _ in range(4):
            self.finish_match()
        self.prepare()
        prediction = self.app.state["prediction"]
        self.assertEqual(prediction["kind"], "champion")
        self.assertEqual(prediction["deadline"], 1180)
        self.assertEqual(len(prediction["players"]), 4)
        self.assertEqual(self.app.chat.public()["counts"], [0, 0, 0, 0])
        fourth = prediction["players"][3]
        self.command("!bet 100 1")
        self.command("!bet 300 4", user="43", login="winner")
        self.lock()
        self.app = Application(self.store)
        self.finish_match()
        self.finish_match()
        self.app.tick()
        self.assertEqual(self.balance(), 900)
        self.app.predictions.prepare()
        self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))
        self.finish_match(1)
        self.finish_match(1)
        self.app.tick()
        self.app.predictions.prepare()
        self.app.predictions.guard_launch(current_match(self.app.state["tournament"]))
        self.finish_match(1)
        self.finish_match(1)
        self.app.tick()
        self.assertEqual(self.app.state["tournament"]["champion"], fourth)
        self.assertEqual(self.balance("43"), 1100)
        self.assertEqual(self.balance(), 900)
        result = result_view(self.app.state, now=1180)["prediction"]
        self.assertEqual(result["players"][3], fourth)
        self.assertEqual(result["pools"], [100, 0, 0, 300])
        again = Application(self.store)
        again.tick()
        self.assertEqual(again.state["betting_balances"]["123"]["43"]["credits"], 1100)

    def test_champion_open_restart_refunds_and_reopens_three_minutes(self):
        for _ in range(4):
            self.finish_match()
        self.prepare()
        self.command("!bet all 3")
        self.app.tick()
        self.app = Application(self.store)
        self.assertEqual(self.balance(), 1000)
        self.reader = self.app.chat.reader
        self.reader.connected, self.reader.account_id = True, "123"
        self.reader.connection_id = "fresh"
        self.clock.return_value = 1100
        with patch("automugen.game_processes", return_value=[]):
            self.app.act("chat_reopen", {})
        self.assertEqual(self.app.state["prediction"]["deadline"], 1280)
        self.assertEqual(self.app.state["prediction"]["kind"], "champion")

    def test_match_betting_rejects_choices_three_and_four_without_debit(self):
        self.prepare()
        self.command("!bet 100 3")
        self.command("!bet all 4", message="four")
        self.app.tick()
        self.assertEqual(self.app.chat.public()["pool"], 0)
        self.assertNotIn("42", self.app.state.get("betting_balances", {}).get("123", {}))

    def test_two_fighter_tournament_has_one_three_minute_champion_window(self):
        with patch("automugen.game_processes", return_value=[]):
            self.app.act("create", {"fighters": [f["id"] for f in self.fighters], "size": 2,
                                    "seconds": 0, "replace": True})
        self.prepare()
        self.assertEqual(self.app.state["prediction"]["kind"], "champion")
        self.assertEqual(self.app.state["prediction"]["deadline"], 1180)
        self.assertEqual(len(self.app.state["prediction"]["players"]), 2)

    def test_existing_locked_legacy_pool_keeps_original_deadline_and_settlement(self):
        self.prepare()
        state = copy.deepcopy(self.app.state)
        p = state["prediction"]
        p.pop("kind")
        p.pop("window_match_id")
        p["deadline"] = p["started_at"] + 180
        presentation = state["tournament"]["presentation"]
        presentation.pop("betting_kind")
        presentation.pop("betting_schedule")
        presentation.update(full_until=1180, ready_at=1196)
        self.app.commit(state)
        self.command()
        self.lock()
        self.app = Application(self.store)
        self.assertEqual(self.app.state["prediction"]["deadline"], 1180)
        self.finish_match()
        self.app.tick()
        self.assertEqual(self.balance(), 1000)
        self.app.predictions.prepare(local=True)
        self.assertEqual(self.app.state["tournament"]["presentation"]["full_until"], 1240)

    def test_champion_candidates_and_stakes_are_validated(self):
        for _ in range(4):
            self.finish_match()
        self.prepare()
        self.command("!bet 100 4")
        self.app.tick()
        for change in ("candidates", "deadline", "choice"):
            state = copy.deepcopy(self.app.state)
            p = state["prediction"]
            if change == "candidates":
                p["players"].reverse()
            elif change == "deadline":
                p["deadline"] += 1
            else:
                p["bets"]["42"]["choice"] = 5
            with self.subTest(change=change), self.assertRaises(UserError):
                self.app.commit(state)

    def test_atomic_acceptance_and_settlement_failures_are_retryable(self):
        self.prepare()
        self.command()
        with patch.object(self.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.app.chat.tick()
        self.assertNotIn("betting_balances", self.app.state)
        self.app.chat.tick()
        self.assertEqual(self.balance(), 900)
        self.lock()
        self.finish_match()
        with patch.object(self.store, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.app.chat.tick()
        self.assertEqual(self.balance(), 900)
        self.app.chat.tick()
        self.app.chat.tick()
        self.assertEqual(self.balance(), 1000)

    def test_reopening_refunded_window_gets_new_deadline_without_funds_loss(self):
        self.prepare()
        self.command()
        self.app.tick()
        self.app.chat.cancel()
        self.clock.return_value = 1100
        with patch("automugen.game_processes", return_value=[]):
            self.app.act("chat_reopen", {})
        self.assertEqual(self.app.state["prediction"]["deadline"], 1160)
        self.assertEqual(self.app.chat.public()["pool"], 0)
        self.assertEqual(self.balance(), 1000)

    def test_fresh_window_forbidden_after_series_started(self):
        for _ in range(4):
            self.finish_match()
        self.prepare()
        self.lock()
        self.finish_match()
        self.app.chat.cancel()
        with patch("automugen.game_processes", return_value=[]), self.assertRaisesRegex(UserError, "already began"):
            self.app.act("chat_reopen", {})

    def test_invalid_balance_or_stake_is_rejected(self):
        self.prepare()
        self.command()
        self.app.tick()
        for value in (-1, True, 2**53):
            state = copy.deepcopy(self.app.state)
            state["betting_balances"]["123"]["42"]["credits"] = value
            with self.subTest(value=value), self.assertRaises(UserError):
                self.app.commit(state)
        state = copy.deepcopy(self.app.state)
        state["prediction"]["bets"]["42"]["amount"] = -1
        with self.assertRaises(UserError):
            self.app.commit(state)

    def test_public_privacy_channel_partition_and_removed_native_routes(self):
        self.prepare()
        self.command()
        self.app.tick()
        state = copy.deepcopy(self.app.state)
        state["betting_balances"]["999"] = {"70": {"login": "unrelated", "credits": 999999}}
        self.app.commit(state)
        self.assertNotIn("unrelated", json.dumps(self.app.chat.public()))
        snapshot = self.app.snapshot()
        self.assertNotIn("betting_balances", snapshot)
        self.assertNotIn("bets", snapshot["prediction"])
        server = Server(0, self.app)
        try:
            client = server.flask_app.test_client()
            response = client.get("/api/overlay", base_url=f"http://127.0.0.1:{server.server_port}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["chat"]["pool"], 100)
            self.assertNotIn('"user_id"', response.get_data(as_text=True))
            for action in ("twitch_select", "twitch_login", "twitch_check", "prediction_abandon"):
                response = client.post(f"/api/action/{action}", json={},
                                       headers={"X-AutoMugen-Token": server.token},
                                       base_url=f"http://127.0.0.1:{server.server_port}")
                self.assertEqual(response.status_code, 400)
        finally:
            server.server_close()

    def test_chat_oauth_routing_and_credentials_not_returned(self):
        self.app.chat.client.client_id = "client"
        self.app.chat.client.channel_login = "caster"
        server = Server(0, self.app)
        try:
            client = server.flask_app.test_client()
            kwargs = {
                "headers": {"X-AutoMugen-Token": server.token},
                "base_url": f"http://127.0.0.1:{server.server_port}",
            }
            response = client.post("/api/action/chat_login", json={}, **kwargs)
            self.assertEqual(response.status_code, 200)
            params = parse_qs(urlsplit(response.json["authorize_url"]).query)
            self.assertEqual(params["scope"], ["chat:read chat:edit"])
            with patch.object(self.app.chat.client, "authorize") as authorize, \
                    patch.object(self.reader, "connect") as connect:
                response = client.post("/api/action/twitch_callback",
                                       json={"state": params["state"][0], "access_token": "fake-secret"}, **kwargs)
                self.assertEqual(response.status_code, 200)
                authorize.assert_called_once_with("fake-secret", params["state"][0])
                connect.assert_called_once_with(self.app.chat.client)
                self.assertNotIn("fake-secret", response.get_data(as_text=True))
        finally:
            server.server_close()

    def test_locked_pool_allows_reauthorization_only_to_original_channel(self):
        self.prepare()
        self.lock()
        self.app.chat.client.client_id = "client"
        self.app.chat.client.channel_login = "caster"
        server = Server(0, self.app)
        try:
            client = server.flask_app.test_client()
            kwargs = {
                "headers": {"X-AutoMugen-Token": server.token},
                "base_url": f"http://127.0.0.1:{server.server_port}",
            }
            response = client.post("/api/action/chat_login", json={}, **kwargs)
            self.assertEqual(response.status_code, 200)
            params = parse_qs(urlsplit(response.json["authorize_url"]).query)
            with patch.object(self.app.chat.client, "authorize"), patch.object(self.reader, "connect") as connect:
                response = client.post("/api/action/twitch_callback",
                                       json={"state": params["state"][0], "access_token": "fake-secret"}, **kwargs)
                self.assertEqual(response.status_code, 200)
                connect.assert_called_once_with(self.app.chat.client, expected_channel_id="123")
            self.assertEqual(self.app.state["prediction"]["status"], "LOCKED")
        finally:
            server.server_close()

    def test_entire_automatic_tournament_without_prediction_api(self):
        self.app.detection_supported = True
        with patch("automugen.game_processes", return_value=[]), patch("automugen.supported", return_value=True), \
                patch("automugen.subprocess.Popen") as popen, patch("automugen.WinnerObserver") as observers:
            process = popen.return_value
            process.pid = 123
            process.poll.return_value = None
            observers.return_value.finish.return_value = {
                "outcome": "winner", "side": 1, "round": 1, "finish": 1, "samples": 3,
                "pid": 123, "build": SUPPORTED_SHA256,
            }
            self.app.act("auto_start", {})
            windows, games = set(), 0
            while current_match(self.app.state["tournament"]):
                games += 1
                self.assertLess(games, 20)
                self.app.tick()
                prediction = self.app.state.get("prediction")
                if prediction and prediction["status"] == "ACTIVE":
                    windows.add(prediction["id"])
                    self.command(sent=self.clock.return_value + 1, message=f"game{games}")
                presentation = self.app.state["tournament"]["presentation"]
                if self.clock.return_value < presentation["ready_at"]:
                    self.clock.return_value = presentation["ready_at"] - 0.001
                    self.app.tick()
                    self.assertIsNone(self.app.process)
                    self.clock.return_value = presentation["ready_at"]
                self.app.tick()
                self.assertEqual(self.app.state["launch"]["status"], "running")
                process.poll.return_value = 0
                self.app.tick()
                process.poll.return_value = None
            self.assertEqual(games, 10)
            self.assertEqual(len(windows), 5)
            self.assertEqual(self.balance(), 1000)
            self.assertFalse(self.app.state["automation"]["enabled"])
