"""Daily updates from main and native user schedule registration."""
import fcntl
import os
from pathlib import Path
import plistlib
import unittest
from unittest.mock import patch

from sandbox.controller_update import daily, engine, usage
from sandbox.controller_update.models import NoAction, RolledBack, SkipReason, Updated
from tests.controller_update_support import Repos


class DailyUpdateTests(unittest.TestCase):
    def setUp(self):
        repos = self.repos = Repos()
        self.addCleanup(repos.cleanup)
        self.enterContext(patch.dict(os.environ, repos.process_env()))
        self.locks = repos.base / "locks"

    def follow(self, failure=None):
        return engine.follow_main(self.repos.ctl, lock_dir=self.locks,
                                  refresher=lambda *_args: failure)

    def test_published_main_moves_without_server_hint_and_checks_again(self):
        revision = self.repos.publish()
        result = self.follow()
        self.assertIsInstance(result, Updated)
        self.assertEqual(self.repos.head(), revision)
        self.assertEqual(self.follow(), NoAction(SkipReason.UP_TO_DATE))

    def test_dirty_and_detached_checkouts_stay_at_their_revision(self):
        self.repos.publish()
        untracked = self.repos.ctl / "local.txt"
        untracked.touch()
        self.assertEqual(self.follow().reason, SkipReason.DIRTY)
        self.assertTrue(untracked.exists())
        untracked.unlink()
        self.repos.git(self.repos.ctl, "checkout", "--detach")
        self.assertEqual(self.follow().reason, SkipReason.NO_UPSTREAM)
        self.assertEqual(self.repos.head(), self.repos.c1)

    def test_divergent_main_refuses_to_move(self):
        self.repos.publish()
        local = self.repos.commit(self.repos.ctl, "local.txt", "one", "local change")
        self.assertEqual(self.follow().reason, SkipReason.NOT_DESCENDANT)
        self.assertEqual(self.repos.head(), local)

    def test_setup_failure_restores_prior_checkout(self):
        self.repos.publish()
        self.assertIsInstance(self.follow("synthetic failure"), RolledBack)
        self.assertEqual(self.repos.head(), self.repos.c1)
        self.assertEqual(self.repos.git(self.repos.ctl, "status", "--porcelain"), "")

    def test_running_command_keeps_main_at_original_revision(self):
        revision = self.repos.publish()
        with usage.open_lock(self.repos.ctl) as checkout_lock:
            fcntl.flock(checkout_lock, fcntl.LOCK_SH)
            self.assertEqual(self.follow().reason, SkipReason.LOCKED)
            self.assertEqual(self.repos.head(), self.repos.c1)
        self.assertIsInstance(self.follow(), Updated)
        self.assertEqual(self.repos.head(), revision)

    def test_macos_setup_registers_calendar_job_without_immediate_launch(self):
        home = self.repos.base
        with (patch.object(Path, "home", return_value=home),
              patch.object(daily.sys, "platform", "darwin"),
              patch.object(daily, "_enabled", return_value=True),
              patch.object(engine, "default_lock_dir", return_value=self.locks),
              patch.object(daily, "_run", side_effect=lambda *args: args[1] != "print") as call):
            result = daily.install_schedule(self.repos.ctl)
            label, path = daily._mac_unit(self.repos.ctl)
            unit = plistlib.loads(path.read_bytes())
            self.assertEqual(unit["StartCalendarInterval"], {"Hour": 9, "Minute": 0})
            self.assertEqual(unit["ProgramArguments"][0], "/bin/sh")
            self.assertEqual(unit["ProgramArguments"][1:],
                             [str(self.repos.ctl / "sb"), "client-update", "now"])
            self.assertNotIn("RunAtLoad", unit)
            self.assertTrue(unit["StandardErrorPath"].endswith(".log"))
            self.assertTrue(result[0])
            self.assertIn(label, path.name)
            self.assertIn("bootstrap", call.call_args.args)

    def test_changed_macos_job_bootstraps_even_if_print_would_report_loaded(self):
        with (patch.object(Path, "home", return_value=self.repos.base),
              patch.object(daily.sys, "platform", "darwin"),
              patch.object(daily, "_enabled", return_value=True),
              patch.object(engine, "default_lock_dir", return_value=self.locks),
              patch.object(daily, "_run", side_effect=lambda *args: args[1] != "bootstrap") as call):
            _, path = daily._mac_unit(self.repos.ctl)
            path.parent.mkdir(parents=True)
            path.write_bytes(plistlib.dumps({"StartCalendarInterval": {"Hour": 8, "Minute": 0}}))
            self.assertFalse(daily.install_schedule(self.repos.ctl)[0])
            self.assertEqual([entry.args[1] for entry in call.call_args_list],
                             ["bootout", "bootstrap"])

    def test_linux_setup_registers_persistent_calendar_timer(self):
        config = self.repos.base / "config"
        with (patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}),
              patch.object(daily.sys, "platform", "linux"),
              patch.object(daily, "_enabled", return_value=True),
              patch.object(daily.shutil, "which", return_value="/bin/systemctl"),
              patch.object(daily, "_run", return_value=True) as call):
            self.assertTrue(daily.install_schedule(self.repos.ctl)[0])
            name = daily._name(self.repos.ctl)
            directory = config / "systemd/user"
            timer = (directory / f"{name}.timer").read_text()
            self.assertIn("OnCalendar=*-*-* 09:00:00", timer)
            self.assertIn("Persistent=true", timer)
            call.assert_called_with("systemctl", "--user", "enable", "--now", f"{name}.timer")

    def test_explicit_release_pin_skips_fetch_even_when_enabled(self):
        with (patch.dict(os.environ, {"SB_RELEASE_REVISION": self.repos.c1,
                                      "SANDBOX_AUTO_UPDATE": "1"}),
              patch.object(daily.engine, "follow_main") as follow):
            self.assertEqual(daily.run("now", self.repos.ctl), 0)
            follow.assert_not_called()
