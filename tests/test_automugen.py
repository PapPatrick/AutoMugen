import copy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from automugen import (
    Application, Store, UserError, build_command, confirm_winner,
    current_match, discover, new_state, new_tournament, validate_state,
)
from server import Server, acquire_lock
from winner import SUPPORTED_SHA256


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.game = self.root / "Game With Spaces"
        (self.game / "data").mkdir(parents=True)
        (self.game / "stages").mkdir()
        (self.game / "stages" / "Test Stage.def").write_text('[Info]\nname="Test Stage"\n')
        (self.game / "Mugen.exe").write_bytes(
            b"Fake executable, NEVER run in tests.\0-p<n> <playername>\0-p<n>.ai 1\0-s <stagename>\0-rounds <num>"
        )
        self.fighters = [{"id": f"Fighter {i}", "name": f"Fighter {i}"} for i in range(16)]
        for fighter in self.fighters:
            folder = self.game / "chars" / fighter["id"]
            folder.mkdir(parents=True)
            (folder / f'{fighter["id"]}.def').write_text(f'[Info]\ndisplayname = "{fighter["name"]}"\n')
        (self.game / "data" / "select.def").write_text(
            "[Characters]\nempty\nrandomselect\n" + "\n".join(f["id"] for f in self.fighters)
            + "\n[ExtraStages]\nstages\\Test Stage.def\n"
        )
        self.store = Store(self.root / "runtime" / "state.json")
        initial = new_state()
        initial["game_path"] = str(self.game)
        self.store.save(initial)

    def app_with_bracket(self):
        app = Application(self.store, broadcast_enabled=False)
        with patch("automugen.game_processes", return_value=[]):
            app.act("create", {"fighters": [f["id"] for f in self.fighters], "size": 8, "seconds": 0, "seed": "test"})
        return app

    @staticmethod
    def context(app):
        match = current_match(app.state["tournament"])
        return {"tournament_id": app.state["tournament"]["id"], "match_id": match["id"],
                "round_number": sum(match["wins"]) + 1}


class BracketTests(unittest.TestCase):
    def test_best_of_three_requires_two_wins_and_can_go_to_third_round(self):
        t = new_tournament([{"id": str(i), "name": str(i)} for i in range(8)], 8, "series", 0)
        for _ in range(4):
            m = current_match(t)
            self.assertEqual(m["best_of"], 1)
            confirm_winner(t, m["id"], m["players"][0])
        m = current_match(t)
        self.assertEqual(m["best_of"], 3)
        confirm_winner(t, m["id"], m["players"][0])
        self.assertEqual(m["wins"], [1, 0])
        self.assertIsNone(t["rounds"][-1][0]["players"][0])
        confirm_winner(t, m["id"], m["players"][1])
        self.assertEqual(m["wins"], [1, 1])
        state = new_state()
        state["tournament"] = json.loads(json.dumps(t))
        validate_state(state)
        t = state["tournament"]
        m = current_match(t)
        confirm_winner(t, m["id"], m["players"][1])
        self.assertEqual(m["wins"], [1, 2])
        self.assertEqual(t["rounds"][-1][0]["players"][0], m["players"][1])

    def test_all_supported_sizes_progress_and_reload(self):
        fighters = [{"id": str(i), "name": str(i)} for i in range(64)]
        for size in (2, 4, 8, 16, 32, 64):
            with self.subTest(size=size):
                t = new_tournament(fighters, size, "same seed", 60)
                self.assertEqual(len({f["id"] for f in t["fighters"]}), size)
                self.assertEqual(t["fighters"], new_tournament(fighters, size, "same seed", 60)["fighters"])
                count = 0
                while match := current_match(t):
                    confirm_winner(t, match["id"], match["players"][0])
                    state = new_state()
                    state["tournament"] = json.loads(json.dumps(t))
                    validate_state(state)
                    count += 1
                self.assertEqual(count, size - 1 + min(3, size - 1))
                self.assertEqual(t["champion"], t["rounds"][-1][0]["winner"])

    def test_bad_sizes_and_windows_rejected(self):
        fighters = [{"id": str(i), "name": str(i)} for i in range(16)]
        for size, seconds in [(3, 60), (32, 60), (True, 60), (4, -1), (4, 601), (4, True)]:
            with self.subTest(size=size, seconds=seconds), self.assertRaises(UserError):
                new_tournament(fighters, size, "", seconds)

    def test_stale_result_and_unknown_winner(self):
        t = new_tournament([{"id": str(i), "name": str(i)} for i in range(4)], 4, "seed", 0)
        match = current_match(t)
        with self.assertRaises(UserError):
            confirm_winner(t, "wrong", match["players"][0])
        with self.assertRaises(UserError):
            confirm_winner(t, match["id"], "intruder")
        confirm_winner(t, match["id"], match["players"][0])
        self.assertIsNone(match["winner"])
        confirm_winner(t, match["id"], match["players"][0])
        with self.assertRaises(UserError):
            confirm_winner(t, match["id"], match["players"][0])


class RosterTests(Fixture):
    def test_real_entries_only_deduplicated_and_metadata(self):
        (self.game / "data" / "select.def").write_bytes(
            b'[Characters]\nEMPTY\nrandomselect\n"Fighter 0", stages\\Test Stage.def, order=1 ; note\n'
            b'Fighter 0\nFighter 1 ; note\nMissing\n..\\escape\n[ExtraStages]\nFighter 2\n'
        )
        result = discover(str(self.game))
        self.assertEqual([f["id"] for f in result["fighters"]], ["Fighter 0", "Fighter 1"])
        self.assertEqual(result["fighters"][0]["name"], "Fighter 0")
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(len(result["stages"]), 1)

    def test_explicit_definition_legacy_encoding_and_quoted_comments(self):
        folder = self.game / "chars" / "Nested"
        folder.mkdir()
        (folder / "alt.def").write_bytes(b'[Info]\ndisplayname="Caf\xe9; Fighter"\n')
        (self.game / "data" / "select.def").write_text('[Characters]\n"Nested\\alt.def", random\n')
        result = discover(str(self.game))
        self.assertEqual(result["fighters"], [{"id": "Nested\\alt.def", "name": "Caf\u00e9; Fighter"}])

    def test_empty_game_fails_visibly(self):
        with self.assertRaises(UserError):
            discover(str(self.root))


class LauncherTests(Fixture):
    def command(self, **overrides):
        args = dict(game=str(self.game), p1="Fighter 0", p2="Fighter 1", stage=r"stages\Test Stage.def")
        args.update(overrides)
        return build_command(**args)

    def test_exact_verified_arguments_and_single_round_limit(self):
        args = self.command()
        self.assertEqual(args[1:9], ["-p1", "Fighter 0", "-p2", "Fighter 1", "-p1.ai", "1", "-p2.ai", "1"])
        self.assertEqual(args[9], "-s")
        self.assertEqual(args[10], "Test Stage.def")
        self.assertEqual(len(args), 13)
        self.assertNotIn("-log", args)
        self.assertFalse(any("runtime" in arg or ".log" in arg for arg in args))
        self.assertEqual(args[11:], ["-rounds", "1"])

    def test_stage_argument_resolves_under_engine_stages_directory(self):
        nested = self.game / "stages" / "Nested Arena"
        nested.mkdir()
        (nested / "Final Stage.def").write_text("[Info]\nname=Final\n")
        for stage in (
            r"stages\Test Stage.def",
            "stages/Test Stage.def",
            r"stages\Nested Arena\Final Stage.def",
        ):
            with self.subTest(stage=stage):
                args = self.command(stage=stage)
                engine_path = self.game / "stages" / args[args.index("-s") + 1]
                self.assertTrue(engine_path.is_file())
                self.assertNotEqual(Path(args[10]).parts[0], "stages")
        self.assertEqual(self.command(stage=r"stages\Nested Arena\Final Stage.def")[10],
                         str(Path("Nested Arena") / "Final Stage.def"))

    def test_validation_blocks_traversal_missing_and_unverified_binary(self):
        for overrides in [{"p1": r"..\outside"}, {"p1": "-p2"}, {"p2": "Fighter 0"}, {"stage": r"stages\..\data\select.def"}, {"stage": r"C:\evil.def"}, {"p2": "missing"}]:
            with self.subTest(overrides=overrides), self.assertRaises(UserError):
                self.command(**overrides)
        (self.game / "Mugen.exe").write_bytes(b"No verified help")
        with self.assertRaises(UserError):
            self.command()

    @patch("automugen.game_processes", return_value=[])
    def test_explicit_launch_exit_and_manual_advancement(self, processes):
        app = self.app_with_bracket()
        data = {**self.context(app), "stage": r"stages\Test Stage.def"}
        with patch("automugen.subprocess.Popen") as popen:
            process = popen.return_value
            process.pid = 123
            process.poll.return_value = None
            app.act("launch", data)
            popen.assert_called_once()
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(popen.call_args.kwargs["cwd"], str(self.game))
            self.assertNotIn("-log", popen.call_args.args[0])
            self.assertFalse(any(arg.endswith(".log") for arg in popen.call_args.args[0]))
            self.assertTrue(app.state["launch"]["log"].endswith(".console.log"))
            self.assertTrue(Path(app.state["launch"]["log"]).exists())
            self.assertEqual(app.state["launch"]["status"], "running")
            with self.assertRaises(UserError):
                app.act("launch", data)
            with self.assertRaises(UserError):
                app.act("result", {**data, "winner": current_match(app.state["tournament"])["players"][0]})
            process.poll.return_value = 0
            app.poll()
        self.assertEqual(app.state["launch"]["status"], "awaiting_result")
        match = current_match(app.state["tournament"])
        self.assertIsNone(match["winner"])
        app.act("result", {**data, "winner": match["players"][0]})
        self.assertNotEqual(current_match(app.state["tournament"])["id"], data["match_id"])
        self.assertEqual(self.store.load(), app.state)

    @patch("automugen.game_processes", return_value=[])
    def test_error_exit_blocks_results_and_allows_replay(self, processes):
        app = self.app_with_bracket()
        data = {**self.context(app), "stage": r"stages\Test Stage.def"}
        tournament = copy.deepcopy(app.state["tournament"])
        with patch("automugen.subprocess.Popen") as popen:
            process = popen.return_value
            process.pid = 123
            process.poll.return_value = None
            app.act("launch", data)
            process.poll.return_value = 1
            app.poll()
            self.assertEqual(app.state["launch"]["status"], "failed")
            self.assertIn("error code 1", app.state["launch"]["message"])
            self.assertEqual(self.store.load(), app.state)
            with self.assertRaises(UserError):
                app.act("result", {**data, "winner": current_match(app.state["tournament"])["players"][0]})
            self.assertEqual(app.state["tournament"], tournament)
            process.poll.return_value = None
            app.act("launch", data)
            self.assertEqual(popen.call_count, 2)
            self.assertEqual(app.state["launch"]["status"], "running")

    @patch("automugen.game_processes", return_value=[])
    def test_random_stage_is_default_saved_and_redrawn_for_replay(self, processes):
        app = self.app_with_bracket()
        other = self.game / "stages" / "Other Stage.def"
        other.write_text("[Info]\nname=Other\n")
        app.refresh()
        stages = app.roster["stages"]
        data = self.context(app)
        with patch("automugen.secrets.choice", side_effect=[stages[0], stages[1]]) as choose, patch("automugen.subprocess.Popen") as popen:
            process = popen.return_value
            process.pid = 123
            process.poll.return_value = None
            app.act("launch", data)
            self.assertEqual(app.state["launch"]["stage"], stages[0]["id"])
            self.assertEqual(self.store.load()["launch"]["stage"], stages[0]["id"])
            self.assertEqual(popen.call_args.args[0][10], Path(stages[0]["id"]).name)
            process.poll.return_value = 0
            app.poll()
            process.poll.return_value = None
            app.act("launch", {**data, "stage": "random"})
            self.assertEqual(app.state["launch"]["stage"], stages[1]["id"])
            self.assertEqual(popen.call_args.args[0][10], Path(stages[1]["id"]).name)
            self.assertEqual(choose.call_count, 2)

    @patch("automugen.game_processes", return_value=[])
    def test_random_stage_without_stages_fails_before_launch(self, processes):
        app = self.app_with_bracket()
        app.roster["stages"] = []
        with patch("automugen.subprocess.Popen") as popen, self.assertRaisesRegex(UserError, "No stage"):
            app.act("launch", {**self.context(app), "stage": "random"})
        popen.assert_not_called()
        self.assertEqual(app.state["launch"]["status"], "idle")

    @patch("automugen.game_processes", return_value=[])
    def test_launch_failure_persists_and_can_retry(self, processes):
        app = self.app_with_bracket()
        with patch("automugen.subprocess.Popen", side_effect=OSError("test denied")), self.assertRaisesRegex(UserError, "test denied"):
            app.act("launch", {**self.context(app), "stage": r"stages\Test Stage.def"})
        self.assertEqual(app.state["launch"]["status"], "failed")
        self.assertEqual(self.store.load()["launch"]["status"], "failed")

    def test_external_process_and_countdown_block_launch(self):
        app = self.app_with_bracket()
        data = {**self.context(app), "stage": r"stages\Test Stage.def"}
        with patch("automugen.game_processes", return_value=["456"]), patch("automugen.subprocess.Popen") as popen:
            with self.assertRaisesRegex(UserError, "still open"):
                app.act("launch", data)
            popen.assert_not_called()
        updated = copy.deepcopy(app.state)
        current_match(updated["tournament"])["window_until"] = time.time() + 60
        app.commit(updated)
        with self.assertRaisesRegex(UserError, "window closes"):
            app.act("launch", data)

    def test_windows_process_list_parsing_and_failure(self):
        with patch("automugen.os.name", "nt"), patch("automugen.subprocess.CREATE_NO_WINDOW", 0, create=True), patch("automugen.subprocess.run") as run:
            from automugen import game_processes
            run.return_value.stdout = '"Mugen.exe","123","Console","1","12 K"\n"unrelated.exe","456","Console","1","10 K"\n"4v4.exe","789","Console","1","12 K"\n'
            self.assertEqual(game_processes(), ["123", "789"])
            run.return_value.stdout = "Not a process list"
            with self.assertRaises(UserError):
                game_processes()
            run.side_effect = subprocess.TimeoutExpired("tasklist", 15)
            with self.assertRaises(subprocess.TimeoutExpired):
                game_processes()


class PersistenceTests(Fixture):
    def test_legacy_bracket_migration_preserves_completed_results(self):
        app = self.app_with_bracket()
        state = copy.deepcopy(app.state)
        first = current_match(state["tournament"])
        confirm_winner(state["tournament"], first["id"], first["players"][0])
        for matches in state["tournament"]["rounds"]:
            for match in matches:
                del match["best_of"]
                del match["wins"]
        app.commit(state)
        restarted = Application(self.store, broadcast_enabled=False)
        t = restarted.state["tournament"]
        self.assertEqual(t["id"], state["tournament"]["id"])
        self.assertEqual(t["rounds"][0][0]["winner"], first["winner"])
        self.assertEqual(t["rounds"][0][0]["wins"], [1, 0])
        self.assertEqual([r[0]["best_of"] for r in t["rounds"]], [1, 3, 3])
        self.assertEqual(restarted.state, self.store.load())

    def test_committed_state_is_not_aliased_to_mutable_launch_work(self):
        app = self.app_with_bracket()
        updated = copy.deepcopy(app.state)
        app.commit(updated)
        updated["launch"]["status"] = "running"
        self.assertEqual(app.state["launch"]["status"], "idle")
        self.assertEqual(app.state, self.store.load())

    def test_countdown_survives_reload_and_duplicate_pool_is_rejected(self):
        app = self.app_with_bracket()
        updated = copy.deepcopy(app.state)
        updated["tournament"]["window_seconds"] = 60
        app.commit(updated)
        app.act("window", self.context(app))
        deadline = current_match(app.state["tournament"])["window_until"]
        restarted = Application(self.store, broadcast_enabled=False)
        self.assertEqual(current_match(restarted.state["tournament"])["window_until"], deadline)
        with patch("automugen.game_processes", return_value=[]), self.assertRaisesRegex(UserError, "duplicates"):
            restarted.act("create", {"fighters": ["Fighter 0", "Fighter 0"], "size": 2, "seconds": 0, "replace": True})

    def test_corrupt_state_never_silently_resets(self):
        self.store.path.write_text("broken")
        with self.assertRaises(UserError):
            Application(self.store, broadcast_enabled=False)
        self.assertEqual(self.store.path.read_text(), "broken")

    def test_atomic_save_keeps_backup_and_save_failure_does_not_change_memory(self):
        app = self.app_with_bracket()
        before = copy.deepcopy(app.state)
        updated = copy.deepcopy(before)
        updated["tournament"]["window_seconds"] = 42
        with patch("automugen.os.replace", side_effect=OSError("disk failure")), self.assertRaises(OSError):
            app.commit(updated)
        self.assertEqual(app.state, before)
        self.assertEqual(self.store.load(), before)
        self.assertFalse(self.store.path.with_suffix(".json.tmp").exists())
        app.commit(updated)
        backup = json.loads(self.store.path.with_suffix(".backup.json").read_text())
        self.assertEqual(backup, before)

    def test_restart_recovers_without_inferred_winner(self):
        app = self.app_with_bracket()
        updated = copy.deepcopy(app.state)
        updated["launch"] = {"status": "running", "message": "Test interrupted"}
        app.commit(updated)
        restarted = Application(self.store, broadcast_enabled=False)
        self.assertEqual(restarted.state["launch"]["status"], "recovery_required")
        with patch("automugen.game_processes", return_value=["123"]), self.assertRaises(UserError):
            restarted.act("recover", self.context(restarted))
        with patch("automugen.game_processes", return_value=[]):
            restarted.act("recover", self.context(restarted))
        self.assertEqual(restarted.state["launch"]["status"], "awaiting_result")
        self.assertIsNone(current_match(restarted.state["tournament"])["winner"])

    def test_invalid_tree_rejected(self):
        app = self.app_with_bracket()
        bad = copy.deepcopy(app.state)
        bad["tournament"]["rounds"][1][0]["players"][0] = bad["tournament"]["fighters"][0]["id"]
        with self.assertRaises(UserError):
            validate_state(bad)

    def test_state_validation_is_not_disabled_by_optimized_python(self):
        script = (
            "from automugen import UserError, new_state, validate_state\n"
            "state = new_state()\n"
            "state['version'] = 2\n"
            "try:\n"
            "    validate_state(state)\n"
            "except UserError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('invalid state was accepted')\n"
        )
        result = subprocess.run(
            [sys.executable, "-O", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_duplicate_server_data_lock(self):
        first = acquire_lock(self.root / "lock")
        self.addCleanup(first.close)
        with self.assertRaises(UserError):
            acquire_lock(self.root / "lock")

    def test_stale_tournament_id_cannot_act_on_replacement(self):
        app = self.app_with_bracket()
        data = self.context(app)
        data["tournament_id"] = "previous tournament"
        with self.assertRaisesRegex(UserError, "stale"):
            app.act("window", data)


class AutomationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.app = self.app_with_bracket()
        self.app.detection_supported = True
        state = copy.deepcopy(self.app.state)
        state["detector_verified"] = SUPPORTED_SHA256
        self.app.commit(state)
        self.processes = self.enterContext(patch("automugen.game_processes", return_value=[]))
        self.enterContext(patch("automugen.supported", return_value=True))
        self.popen = self.enterContext(patch("automugen.subprocess.Popen"))
        self.observers = self.enterContext(patch("automugen.WinnerObserver"))
        self.popen.return_value.pid = 123
        self.popen.return_value.poll.return_value = None
        self.observers.return_value.finish.return_value = {
            "outcome": "winner", "side": 1, "round": 1, "finish": 1, "samples": 3, "pid": 123,
            "build": SUPPORTED_SHA256,
        }

    def launch_next(self):
        self.app.tick()  # Open local countdown.
        self.app.tick()  # Launch after zero-second countdown.
        self.assertEqual(self.app.state["launch"]["status"], "running")

    def exit_round(self, code=0):
        self.popen.return_value.poll.return_value = code
        self.app.tick()
        self.popen.return_value.poll.return_value = None

    def test_entire_tournament_runs_without_browser_requests(self):
        self.app.act("auto_start", {})
        rounds = 0
        while current_match(self.app.state["tournament"]):
            self.launch_next()
            self.exit_round()
            rounds += 1
            self.assertLess(rounds, 20)
            self.assertEqual(self.store.load(), self.app.state)
        self.assertEqual(rounds, 10)  # Four BO1 matches and three 2-0 BO3 matches.
        self.assertFalse(self.app.state["automation"]["enabled"])
        self.assertIsNotNone(self.app.state["tournament"]["champion"])
        self.assertEqual(self.popen.call_count, 10)

    def test_first_live_result_advances_without_confirmation_gate(self):
        state = copy.deepcopy(self.app.state)
        state["detector_verified"] = None
        self.app.commit(state)
        self.app.act("auto_start", {})
        self.launch_next()
        self.exit_round()
        self.assertEqual(self.app.state["tournament"]["rounds"][0][0]["wins"], [1, 0])
        self.assertEqual(current_match(self.app.state["tournament"])["id"], "r1m2")
        self.assertTrue(self.app.state["automation"]["enabled"])
        self.assertNotIn("pending_detected_winner", self.app.state["launch"])

    def test_saved_legacy_pending_result_applied_once_without_new_game(self):
        state = copy.deepcopy(self.app.state)
        match = current_match(state["tournament"])
        state["launch"] = {
            "status": "awaiting_result", "message": "legacy gate", "exit_code": 0,
            "pid": 123, "tournament_id": state["tournament"]["id"],
            "match_id": match["id"], "round_number": 1,
            "pending_detected_winner": match["players"][0],
            "observation": self.observers.return_value.finish.return_value,
        }
        self.app.commit(state)
        restarted = Application(self.store, broadcast_enabled=False)
        self.assertEqual(restarted.state["tournament"]["rounds"][0][0]["wins"], [1, 0])
        self.assertEqual(current_match(restarted.state["tournament"])["id"], "r1m2")
        self.assertNotIn("pending_detected_winner", restarted.state["launch"])
        again = Application(self.store, broadcast_enabled=False)
        self.assertEqual(again.state, restarted.state)
        self.popen.assert_not_called()

    def test_console_contains_labeled_launcher_and_result_even_without_game_output(self):
        self.app.act("auto_start", {})
        self.launch_next()
        log = Path(self.app.state["launch"]["log"])
        self.assertIn("[AutoMugen launcher]", log.read_text())
        self.exit_round()
        contents = log.read_text()
        self.assertIn("[AutoMugen result]", contents)
        self.assertIn('"exit_code": 0', contents)
        self.assertIn("Engine detected", contents)

    def test_server_scheduler_finishes_without_any_http_requests(self):
        self.app.act("auto_start", {})
        self.popen.return_value.poll.return_value = 0
        server = Server(0, self.app)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with server.lock:
                    complete = self.app.state["tournament"]["champion"] is not None
                if complete:
                    break
                time.sleep(0.01)
            self.assertTrue(complete)
            self.assertEqual(self.popen.call_count, 10)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_missing_result_crash_and_draw_pause_without_advancing(self):
        for observation, code in (
            ({"error": "No stable result"}, 0),
            ({"outcome": "draw", "side": 0}, 0),
            ({"outcome": "winner", "side": 1}, 1),
        ):
            with self.subTest(observation=observation, code=code):
                self.observers.return_value.finish.return_value = observation
                self.app.act("auto_start", {})
                self.launch_next()
                before = copy.deepcopy(self.app.state["tournament"])
                self.exit_round(code)
                self.assertFalse(self.app.state["automation"]["enabled"])
                self.assertEqual(self.app.state["tournament"], before)
                count = self.popen.call_count
                self.app.tick()
                self.assertEqual(self.popen.call_count, count)

    def test_pause_finishes_current_round_but_stops_further_launches(self):
        self.app.act("auto_start", {})
        self.launch_next()
        self.app.act("auto_pause", {})
        self.exit_round()
        self.assertEqual(self.app.state["tournament"]["rounds"][0][0]["wins"], [1, 0])
        for _ in range(3):
            self.app.tick()
        self.assertEqual(self.popen.call_count, 1)

    def test_watchdog_stops_only_owned_process_and_never_awards_win(self):
        self.app.act("auto_start", {})
        self.launch_next()
        state = copy.deepcopy(self.app.state)
        state["launch"]["started_at"] = time.time() - 181
        self.app.commit(state)
        self.app.tick()
        self.popen.return_value.terminate.assert_called_once()
        self.assertFalse(self.app.state["automation"]["enabled"])
        self.assertTrue(self.app.state["launch"]["timed_out"])
        self.exit_round(0)
        self.assertEqual(current_match(self.app.state["tournament"])["wins"], [0, 0])

    def test_countdown_is_respected_and_does_not_depend_on_browser(self):
        state = copy.deepcopy(self.app.state)
        state["tournament"]["window_seconds"] = 60
        self.app.commit(state)
        self.app.act("auto_start", {})
        self.app.tick()
        self.app.tick()
        self.popen.assert_not_called()
        state = copy.deepcopy(self.app.state)
        current_match(state["tournament"])["window_until"] = time.time() - 1
        self.app.commit(state)
        self.app.tick()
        self.popen.assert_called_once()

    def test_restart_never_resumes_automatically(self):
        self.app.act("auto_start", {})
        self.launch_next()
        restarted = Application(self.store, broadcast_enabled=False)
        self.assertFalse(restarted.state["automation"]["enabled"])
        self.assertEqual(restarted.state["launch"]["status"], "recovery_required")
        restarted.tick()
        self.assertEqual(self.popen.call_count, 1)

    def test_launch_error_and_external_process_pause(self):
        self.app.act("auto_start", {})
        self.app.tick()
        self.processes.return_value = ["999"]
        with self.assertLogs("automugen", level="ERROR"):
            self.app.tick()
        self.assertFalse(self.app.state["automation"]["enabled"])
        self.popen.assert_not_called()

    def test_unsupported_detection_blocks_automatic_start(self):
        self.app.detection_supported = False
        with self.assertRaisesRegex(UserError, "does not support"):
            self.app.act("auto_start", {})
        self.assertFalse(self.app.state["automation"]["enabled"])

    def test_result_context_cannot_award_stale_round(self):
        self.app.act("auto_start", {})
        self.launch_next()
        state = copy.deepcopy(self.app.state)
        state["launch"]["round_number"] = 999
        self.app.commit(state)
        self.exit_round()
        self.assertFalse(self.app.state["automation"]["enabled"])
        self.assertEqual(current_match(self.app.state["tournament"])["wins"], [0, 0])

    def test_observer_failure_pauses_and_keeps_game_under_manual_control(self):
        from winner import ObservationError
        self.observers.side_effect = ObservationError("test access denied")
        self.app.act("auto_start", {})
        self.launch_next()
        self.assertFalse(self.app.state["automation"]["enabled"])
        self.assertIn("test access denied", self.app.state["automation"]["message"])
        self.exit_round()
        self.assertEqual(current_match(self.app.state["tournament"])["wins"], [0, 0])

    def test_mismatched_pid_or_build_never_awards_a_result(self):
        for field, value in (("pid", 999), ("build", "different"), ("round", 2)):
            with self.subTest(field=field):
                observation = {
                    "outcome": "winner", "side": 1, "round": 1, "finish": 1,
                    "samples": 3, "pid": 123, "build": SUPPORTED_SHA256, field: value,
                }
                self.observers.return_value.finish.return_value = observation
                self.app.act("auto_start", {})
                self.launch_next()
                self.exit_round()
                self.assertFalse(self.app.state["automation"]["enabled"])
                self.assertEqual(current_match(self.app.state["tournament"])["wins"], [0, 0])

    def test_result_audit_is_saved_before_next_launch(self):
        self.app.act("auto_start", {})
        self.launch_next()
        log = Path(self.app.state["launch"]["log"])
        self.exit_round()
        audit = json.loads(log.with_suffix(".result.json").read_text())
        self.assertEqual(audit["observation"]["side"], 1)
        self.assertEqual(audit["observation"]["build"], SUPPORTED_SHA256)
        self.assertEqual(audit["exit_code"], 0)
        self.assertEqual(audit["match_id"], "r1m1")

    def test_game_log_is_copied_on_exit_and_referenced_in_audit(self):
        source = self.game / "mugen.log"
        source.write_text("Old game log")
        self.app.act("auto_start", {})
        self.launch_next()
        source.write_text("Loading stage...OK\nSuccessful program termination.\n")
        self.exit_round()
        log = Path(self.app.state["launch"]["game_log"])
        self.assertEqual(log.read_bytes(), source.read_bytes())
        audit = json.loads(Path(self.app.state["launch"]["log"]).with_suffix(".result.json").read_text())
        self.assertEqual(audit["game_log"], str(log))
        self.assertIn("command", audit)

    def test_unchanged_game_log_is_not_misattributed_to_new_launch(self):
        source = self.game / "mugen.log"
        source.write_text("Previous match")
        self.app.act("auto_start", {})
        self.launch_next()
        self.exit_round()
        self.assertNotIn("game_log", self.app.state["launch"])
        self.assertIn("not updated", self.app.state["launch"]["game_log_capture"])


class HttpTests(Fixture):
    def setUp(self):
        super().setUp()
        self.app = Application(self.store, broadcast_enabled=False)
        self.server = Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_static_pages_and_read_only_overlay(self):
        for path in ("/", "/overlay", "/style.css", "/app.js", "/overlay.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 200)
        response = self.server.flask_app.test_client().get(
            "/",
            headers={"Host": f"127.0.0.1:{self.server.server_port}"},
        )
        self.assertEqual(response.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        response.close()
        code, raw = self.request("GET", "/api/overlay")
        self.assertEqual(code, 200)
        overlay = json.loads(raw)
        self.assertFalse(overlay["twitch"]["connected"])
        self.assertNotIn("token", overlay)
        self.assertNotIn("game_path", overlay)
        self.assertIsNone(overlay["result"])
        self.assertEqual(self.request("GET", "/../server.py")[0], 404)

    def test_origin_token_and_host_guards(self):
        self.assertEqual(self.request("POST", "/api/action/refresh", "{}")[0], 403)
        headers = {"Content-Type": "application/json", "X-AutoMugen-Token": self.server.token}
        self.assertEqual(self.request("POST", "/api/action/refresh", "{}", headers)[0], 200)
        headers["Origin"] = "https://evil.example"
        self.assertEqual(self.request("POST", "/api/action/refresh", "{}", headers)[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)

    def test_bad_inputs_are_visible_and_do_not_mutate(self):
        headers = {"Content-Type": "application/json", "X-AutoMugen-Token": self.server.token}
        before = copy.deepcopy(self.app.state)
        for body in ("[]", "{broken", '{"size":NaN}'):
            code, raw = self.request("POST", "/api/action/create", body, headers)
            self.assertEqual(code, 400)
            self.assertIn("error", json.loads(raw))
        invalid_type = {**headers, "Content-Type": "text/plain"}
        self.assertEqual(self.request("POST", "/api/action/refresh", "{}", invalid_type)[0], 400)
        oversized = " " * (256 * 1024 + 1)
        self.assertEqual(self.request("POST", "/api/action/refresh", oversized, headers)[0], 400)
        self.assertEqual(self.app.state, before)


if __name__ == "__main__":
    unittest.main()
