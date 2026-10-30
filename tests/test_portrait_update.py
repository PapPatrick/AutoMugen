import copy
from unittest.mock import patch

from automugen import Application, UserError, validate_state
from portraits import _png
from test_automugen import Fixture
import hashlib


class PortraitUpdateTests(Fixture):
    def override(self, app, entry):
        folder = app.portraits.overrides_dir
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(entry.encode()).hexdigest() + ".png")
        path.write_bytes(_png(1, 1, b"\xff\0\0\xff"))
        return path

    def start(self, app):
        with patch("automugen.game_processes", return_value=[]):
            app.act("update_portraits", {})

    def finish(self, app):
        for _ in range(10):
            if app.state["portrait_update"]["status"] != "running":
                return
            app.tick()
        self.fail("Update did not finish")

    def test_batch_update_progress_failures_persistence_and_retry(self):
        app = self.app_with_bracket()
        tournament = copy.deepcopy(app.state["tournament"])
        first = self.fighters[0]["id"]
        self.override(app, first)
        self.start(app)
        self.assertEqual(app.state["portrait_update"]["total"], 16)
        app.tick()
        self.assertEqual(app.state["portrait_update"]["checked"], 8)
        self.finish(app)
        report = app.state["portrait_update"]
        self.assertEqual((report["ready"], len(report["failures"])), (1, 15))
        self.assertEqual(report["status"], "complete")
        self.assertEqual(app.state["tournament"], tournament)
        revision = app.state["portrait_revision"]
        for fighter in self.fighters:
            self.override(app, fighter["id"])
        self.start(app)
        self.finish(app)
        self.assertEqual(app.state["portrait_update"]["ready"], 16)
        self.assertFalse(app.state["portrait_update"]["failures"])
        self.assertNotEqual(app.state["portrait_revision"], revision)
        self.assertEqual(app.state, self.store.load())

    def test_update_repairs_corrupt_cache_and_rebuilds_changed_images(self):
        app = self.app_with_bracket()
        entry = self.fighters[0]["id"]
        override = self.override(app, entry)
        cached = app.portraits.get(entry)
        cached.write_bytes(b"broken cache")
        self.start(app)
        self.finish(app)
        self.assertEqual(app.state["portrait_update"]["ready"], 1)
        self.assertTrue(cached.read_bytes().startswith(b"\x89PNG"))
        override.write_bytes(_png(2, 1, b"\0\xff\0\xff" * 2))
        self.start(app)
        self.finish(app)
        updated = app.portraits.get(entry)
        self.assertNotEqual(updated, cached)
        self.assertNotEqual(updated.read_bytes(), cached.read_bytes())

    def test_added_roster_entries_rescanned_and_old_tournament_preserved(self):
        app = self.app_with_bracket()
        tournament = copy.deepcopy(app.state["tournament"])
        folder = self.game / "chars" / "New fighter"
        folder.mkdir()
        (folder / "New fighter.def").write_text('[Info]\ndisplayname="New fighter"\n')
        (self.game / "data" / "select.def").write_text("[Characters]\nNew fighter\n")
        self.override(app, "New fighter")
        self.start(app)
        self.assertEqual(app.roster["fighters"], [{"id": "New fighter", "name": "New fighter"}])
        self.assertEqual(app.state["portrait_update"]["total"], 9)
        self.finish(app)
        self.assertEqual(app.state["portrait_update"]["ready"], 1)
        self.assertEqual(app.state["tournament"], tournament)

    def test_restart_marks_interruption_and_reuses_generated_cache(self):
        app = self.app_with_bracket()
        for fighter in self.fighters:
            self.override(app, fighter["id"])
        self.start(app)
        app.tick()
        first = app.portraits.get(self.fighters[0]["id"])
        mtime = first.stat().st_mtime_ns
        restarted = Application(self.store, broadcast_enabled=False)
        self.assertEqual(restarted.state["portrait_update"]["status"], "interrupted")
        self.assertEqual(restarted.state["portrait_update"]["checked"], 8)
        self.start(restarted)
        self.finish(restarted)
        self.assertEqual(restarted.state["portrait_update"]["ready"], 16)
        self.assertEqual(first.stat().st_mtime_ns, mtime)

    def test_duplicate_updates_and_launches_blocked_without_mutation(self):
        app = self.app_with_bracket()
        self.start(app)
        before = copy.deepcopy(app.state)
        for action in ("update_portraits", "refresh", "settings", "create", "auto_start", "launch"):
            with self.subTest(action=action), self.assertRaisesRegex(UserError, "update finishes"):
                app.act(action, {})
        self.assertEqual(app.state, before)

    def test_update_requires_paused_automation_and_closed_game(self):
        app = self.app_with_bracket()
        app.state["automation"]["enabled"] = True
        with self.assertRaisesRegex(UserError, "Pause"):
            self.start(app)
        app.state["automation"]["enabled"] = False
        with patch("automugen.game_processes", return_value=["123"]), self.assertRaisesRegex(UserError, "still open"):
            app.act("update_portraits", {})
        self.assertNotIn("portrait_update", app.state)

    def test_missing_game_fails_visibly_no_empty_success(self):
        app = self.app_with_bracket()
        (self.game / "data" / "select.def").unlink()
        with self.assertRaises(UserError):
            self.start(app)
        self.assertNotIn("portrait_update", app.state)

    def test_invalid_update_report_rejected(self):
        app = self.app_with_bracket()
        self.start(app)
        state = copy.deepcopy(app.state)
        state["portrait_update"]["checked"] = 100
        with self.assertRaises(UserError):
            validate_state(state)
