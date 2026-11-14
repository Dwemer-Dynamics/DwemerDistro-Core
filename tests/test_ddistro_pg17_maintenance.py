"""Focused PG17 retry, rollback-guard, donor-retention and dump-hash regressions. No live distro, cluster or SQL."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

if importlib.util.find_spec("fcntl") is None:
    raise unittest.SkipTest("ddistro_pg17_maintenance targets Linux")


def load(name):
    path = str(Path(__file__).resolve().parents[1] / "bin" / name)
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("ddistro_pg17_maintenance")
TAG = "dwemer-stage-" + "a" * 32
# start.conf as pg_createcluster writes it on Debian, and as the deployed ASUS clone has it.
STOCK_HEADER = (
    "# Automatic startup configuration\n"
    "#   auto: automatically start the cluster\n"
    "#   manual: manual startup with pg_ctlcluster/postgresql@.service only\n"
    "#   disabled: refuse to start cluster\n"
    "# See pg_createcluster(1) for details. When running from systemd,\n"
    "# invoke 'systemctl daemon-reload' after editing this file.\n"
)


def stock(mode, blank=True):
    return STOCK_HEADER + ("\n" if blank else "") + mode + "\n"


class StartConf(unittest.TestCase):
    """start.conf is read as postgresql-common reads it, and anything not provably one mode is refused."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.conf = self.base / "conf"
        self.conf.mkdir()

    def mode(self, content):
        path = self.conf / "start.conf"
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return m.start_mode(self.conf)

    def test_stock_and_plain_settings_are_read(self):
        for mode in ("auto", "manual", "disabled"):
            for content in (mode + "\n", mode, stock(mode), stock(mode, blank=False), "\n  " + mode + " \t\n\n",
                            stock(mode) + "# trailing note\n", "  # indented comment\n" + mode + "  # why\n",
                            mode + "\r\n"):
                self.assertEqual(self.mode(content), mode, repr(content))

    def test_ambiguous_unrecognised_or_unreadable_settings_are_refused(self):
        for content in ("", "\n\n", STOCK_HEADER, stock("auto") + "manual\n", "auto\nauto\n",
                        stock("disabled") + "auto\n", "automatic\n", "Auto\n", "auto manual\n", "auto=1\n",
                        "disable\n", stock("on"), "#auto\n", "﻿auto\n", b"auto\n\xff\n"):
            self.assertIsNone(self.mode(content), repr(content))

    def test_links_and_non_files_are_refused(self):
        target = self.base / "elsewhere"
        target.write_text(stock("auto"))
        (self.conf / "start.conf").symlink_to(target)
        self.assertIsNone(m.start_mode(self.conf))
        (self.conf / "start.conf").unlink()
        (self.conf / "start.conf").mkdir()
        self.assertIsNone(m.start_mode(self.conf))
        self.assertIsNone(m.start_mode(self.base / "missing"))
        real = self.base / "real"
        real.mkdir()
        (real / "start.conf").write_text(stock("auto"))
        (self.base / "linked").symlink_to(real)
        self.assertIsNone(m.start_mode(self.base / "linked"))
        self.assertEqual(m.start_mode(real), "auto")


class Journal(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        paths = {"ROOT": base, "STATE": base / "state.json", "DATA": base, "OLD_DATA": base / "15" / "main",
                 "NEW_DATA": base / "17" / "dwemer", "OLD": base / "etc15", "NEW": base / "17"}
        for key, value in paths.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.NEW.mkdir()
        m.OLD_DATA.mkdir(parents=True)
        (m.OLD_DATA / "PG_VERSION").write_text("15\n")
        (m.NEW / "dwemer-maintenance-id").write_text(TAG)
        m.log = None

    def journal(self, **state):
        m.STATE.write_text(json.dumps({"phase": "rolled-back", "work": str(m.DATA / TAG), **state}))

    def test_exhausted_automatic_retries_fail_while_explicit_rollback_stays_held(self):
        with patch.object(m, "verify_active"):
            self.journal(auto_attempts=m.AUTO_ATTEMPTS)
            self.assertEqual(m.automatic_status(), "held")
            self.journal(retry_auto=True, auto_attempts=m.AUTO_ATTEMPTS - 1)
            self.assertEqual(m.automatic_status(), "needed")
            self.journal(retry_auto=True, auto_attempts=m.AUTO_ATTEMPTS)
            with self.assertRaisesRegex(ValueError, "choose Upgrade to try again or Roll back"):
                m.automatic_status()

    def test_only_an_unchanged_sealed_candidate_skips_fingerprint_verification(self):
        self.journal(phase="publishing", candidate_id="17id", donor_id="15id", baseline="base", baseline_format=2,
                     backup=str(m.DATA / "backup.tar"), seal=["17id", "0/1000", "shut down"])
        with patch.object(m, "cluster_id", side_effect=lambda version, path: f"{version}id"), \
             patch.object(m, "running", return_value=False), \
             patch.object(m, "atomic"), patch.object(m, "restore_old") as restore, \
             patch.object(m, "fingerprint", return_value=("base", "raw")) as fingerprint:
            with patch.object(m, "seal", return_value=["17id", "0/1000", "shut down"]):
                m.rollback()
            self.assertEqual((restore.call_count, fingerprint.call_count), (1, 0))
            with patch.object(m, "seal", return_value=["17id", "0/2000", "shut down"]):
                m.rollback()
            self.assertEqual((restore.call_count, fingerprint.call_count), (2, 1))

    def test_rollback_refuses_a_replaced_donor_before_stopping_pg17(self):
        self.journal(phase="active", candidate_id="17id", donor_id="15id", backup=str(m.DATA / "backup.tar"))
        untouched = {name: patch.object(m, name, side_effect=AssertionError(name))
                     for name in ("running", "cluster", "fingerprint", "atomic", "restore_old")}
        for patcher in untouched.values():
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.object(m, "cluster_id", side_effect=lambda version, path: {15: "new15", 17: "17id"}[version]), \
             self.assertRaisesRegex(ValueError, "differs from the saved donor; rollback changed nothing"):
            m.rollback()
        self.assertEqual(json.loads(m.STATE.read_text())["phase"], "active")

    def rollback_with(self, fails, pg15_running=False):
        # The comparison matches, then PG15 cannot be brought back; fails names the cluster commands that fail.
        self.journal(phase="active", candidate_id="17id", donor_id="15id", baseline="base", baseline_format=2,
                     backup=str(m.DATA / "backup.tar"), old_start="auto\n", retry_auto=True)
        calls, writes = [], []

        def cluster(version, command):
            calls.append((version, command))
            if (version, command) in fails:
                raise subprocess.CalledProcessError(1, "pg_ctlcluster")

        def atomic(path, value, mode):
            if path == m.STATE:
                m.STATE.write_text(value)
            else:
                writes.append((path.parent.name, value))

        def verify_active(major, path):
            if pg15_running and major == 15:
                raise ValueError("unexpected cluster")

        with patch.object(m, "cluster_id", side_effect=lambda version, path: f"{version}id"), \
             patch.object(m, "running", side_effect=lambda version, path: pg15_running and version == 15), \
             patch.object(m, "fingerprint", return_value=("base", "raw")), patch.object(m, "cluster", cluster), \
             patch.object(m, "atomic", atomic), patch.object(m, "verify_active", verify_active):
            with self.assertRaises(ValueError) as caught:
                m.rollback()
        return str(caught.exception), calls, writes, json.loads(m.STATE.read_text())

    def test_pg15_start_failure_after_a_matching_check_fences_pg15_and_serves_pg17_again(self):
        message, calls, writes, state = self.rollback_with({(15, "start")})
        self.assertIn("unchanged PostgreSQL 17 cluster serves again", message)
        self.assertEqual(calls, [(15, "start"), (17, "start")])
        # PG15 is fenced before PG17 starts, and PG17 gets its normal startup setting back.
        self.assertEqual(writes, [("17", "disabled\n"), ("17", "disabled\n"), ("etc15", "manual\n"),
                                  ("etc15", "disabled\n"), ("17", "manual\n"), ("17", "auto\n")])
        self.assertEqual(state["phase"], "active")
        self.assertNotIn("retry_auto", state)

    def test_ambiguous_pg15_start_failures_keep_the_unfinished_journal(self):
        message, calls, _, state = self.rollback_with({(15, "start"), (17, "start")})
        self.assertIn("both retained clusters are stopped", message)
        self.assertEqual((calls, state["phase"]), ([(15, "start"), (17, "start")], "checking"))
        message, calls, _, state = self.rollback_with(set(), pg15_running=True)
        self.assertIn("could not be confirmed stopped, so PostgreSQL 17 stays stopped", message)
        self.assertEqual((calls, state["phase"]), ([(15, "stop")], "checking"))

    def crash_then_recover(self, first, boundary, changed, **journal):
        # first() is killed right after the boundary event; the next update's recover() must leave one cluster
        # serving. up models which cluster runs on the application socket.
        m.OLD.mkdir(exist_ok=True)
        (m.OLD / "start.conf").write_text("disabled\n")
        (m.NEW / "start.conf").write_text("auto\n")
        self.journal(**dict(dict(phase="active", candidate_id="17id", donor_id="15id", baseline="base",
                                 baseline_format=2, old_start="auto\n", cleaned=True), **journal))
        up, real_atomic, armed = {15: False, 17: True}, m.atomic, [boundary]

        def event(name):
            if armed and name == armed[0]:
                armed.clear()
                raise KeyboardInterrupt("killed")

        def atomic(path, value, mode):
            real_atomic(path, value, mode)
            event(json.loads(value)["phase"] if path == m.STATE else f"{path.parent.name}:{value.strip()}")

        def cluster(version, command):
            up[version] = command == "start"
            event(f"{version}:{command}")

        def verify_active(major, path):
            if not up[major] or up[{15: 17, 17: 15}[major]]:
                raise ValueError("unexpected cluster")

        # PG17 started after its sealed shutdown, so its checkpoint no longer matches the seal.
        with patch.object(m, "seal", return_value=["17id", "0/2000", "shut down"]), \
             patch.object(m, "cluster_id", side_effect=lambda version, path: f"{version}id"), \
             patch.object(m, "running", side_effect=lambda version, path: up[version]), \
             patch.object(m, "cluster", cluster), patch.object(m, "atomic", atomic), \
             patch.object(m, "verify_active", verify_active), \
             patch.object(m, "fingerprint", return_value=("changed" if changed else "base", "raw")):
            with self.assertRaises(KeyboardInterrupt):
                first()
            self.assertEqual(json.loads(m.STATE.read_text())["phase"], "checking")
            self.assertTrue(m.recover())
            status = m.automatic_status()
        starts = ((m.OLD / "start.conf").read_text(), (m.NEW / "start.conf").read_text())
        return up, starts, status, json.loads(m.STATE.read_text())

    def test_a_killed_rollback_is_journaled_before_pg17_stops_and_recovers_either_cluster(self):
        for boundary in ("checking", "17:stop", "17:disabled"):
            for changed in (False, True):
                with self.subTest(boundary=boundary, changed=changed):
                    up, starts, status, state = self.crash_then_recover(m.rollback, boundary, changed)
                    if changed:
                        # Writes accepted while PG17 was live keep it serving; PG15 stays fenced.
                        self.assertEqual((up, starts, status, state["phase"]),
                                         ({15: False, 17: True}, ("disabled\n", "auto\n"), "current", "active"))
                    else:
                        # The explicit rollback completes and stays held on PG15.
                        self.assertEqual((up, starts, status, state["phase"]),
                                         ({15: True, 17: False}, ("auto\n", "disabled\n"), "held", "rolled-back"))
                    self.assertNotIn("retry_auto", state)

    def test_a_killed_recovery_of_an_automatic_upgrade_stays_an_automatic_retry(self):
        # The upgrade died while publishing with PG17 live, then its recovery died after fencing PG17.
        up, starts, status, state = self.crash_then_recover(
            m.recover, "17:stop", False, phase="publishing", seal=["17id", "0/1000", "shut down"], auto_attempts=1,
            cleaned=False)
        self.assertEqual((up, starts, status, state["phase"], state["retry_auto"]),
                         ({15: True, 17: False}, ("auto\n", "disabled\n"), "needed", "rolled-back", True))

    def test_retained_runs_never_adopt_an_unmarked_destination(self):
        root, conf = m.DATA / "root", m.DATA / "conf"
        root.mkdir()
        building = conf / ("." + TAG + "-config")
        building.mkdir(parents=True)
        (m.NEW / "dwemer-maintenance-id").unlink()
        self.journal(candidate_id="17id")
        with patch.object(m, "ROOT", root), patch.object(m, "CONF", conf), patch.object(m, "verify_active"), \
             patch.object(m, "STAGER", str(m.DATA / "missing-stager")):
            with self.assertRaisesRegex(ValueError, "lacks the retained PG17 cluster's maintenance marker"):
                m.upgrade()
            self.assertTrue(m.NEW.is_dir() and building.is_dir() and m.STATE.is_file())
            # A marked destination and an unpublished build of that run are retained, never reused.
            (m.NEW / "dwemer-maintenance-id").write_text(TAG)
            with self.assertRaisesRegex(ValueError, "missing its verified PG17 staging tool"):
                m.upgrade()
        self.assertEqual(sorted(path.name for path in root.iterdir()),
                         [TAG + "-config", TAG + "-config-partial", TAG + ".json"])

    def test_only_provable_interrupted_write_leftovers_are_discarded(self):
        root = m.DATA / "root"
        root.mkdir()
        m.OLD.mkdir()
        self.journal()
        leftovers = [root / ("state.json.pg17-" + "1" * 32), m.OLD / ("start.conf.pg17-" + "2" * 32),
                     m.NEW / ("start.conf.pg17-" + "3" * 32)]
        kept = [m.OLD / "start.conf.pg17-notours", root / ("unrelated.pg17-" + "4" * 32)]
        for path in leftovers + kept:
            path.write_text("partial")
        with patch.object(m, "ROOT", root), patch.object(m, "sync_dir"), \
             patch.object(m.os, "geteuid", return_value=os.lstat(root).st_uid, create=True):
            m.discard_temporaries()
            self.assertEqual([path.exists() for path in leftovers + kept], [False, False, False, True, True])
            linked = m.OLD / ("start.conf.pg17-" + "5" * 32)
            linked.write_text("partial")
            os.link(linked, m.DATA / "elsewhere")
            with self.assertRaisesRegex(ValueError, "not a plain single-link root-owned file, so it was kept"):
                m.discard_temporaries()
            self.assertTrue(linked.exists())


class ReopenedDistro(unittest.TestCase):
    """A reopened distro has not started PostgreSQL yet; status proves the stopped PG17 offline."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        paths = {"ROOT": base, "STATE": base / "state.json", "DATA": base, "OLD_DATA": base / "15" / "main",
                 "NEW_DATA": base / "17" / "dwemer", "OLD": base / "etc15", "NEW": base / "etc17"}
        for key, value in paths.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.NEW.mkdir()
        m.OLD.mkdir()
        (m.NEW / "dwemer-maintenance-id").write_text(TAG)
        (m.NEW / "start.conf").write_text("auto\n")
        (m.OLD / "start.conf").write_text("disabled\n")
        m.STATE.write_text(json.dumps({"phase": "active", "cleaned": True, "work": str(m.DATA / TAG),
                                       "candidate_id": "17id", "donor_id": "15id"}))
        m.log = None

    def status(self, running=(), serves=False, identity="17id", state="shut down"):
        control = {"Database system identifier": identity, "Database cluster state": state}
        with patch.object(m, "running", side_effect=lambda version, _d: version in running), \
                patch.object(m, "socket_serves", return_value=serves), \
                patch.object(m, "controldata", return_value=control), \
                patch.object(m, "verify_active", side_effect=ValueError("Unexpected PostgreSQL version")) as verify:
            try:
                return m.automatic_status(), verify.called
            except ValueError as error:
                return str(error), verify.called

    def test_a_stopped_journaled_pg17_is_current_without_any_start_or_query(self):
        self.assertEqual(self.status(), ("current", False))
        self.assertEqual(self.status(state="in production"), ("current", False))
        (m.NEW / "start.conf").write_text("manual\n")
        self.assertEqual(self.status(), ("current", False))
        state = json.loads(m.STATE.read_text())
        m.STATE.write_text(json.dumps(dict(state, cleaned=False)))
        self.assertEqual(self.status(), ("cleanup-needed", False))

    def test_a_live_server_is_still_verified_and_a_wrong_or_changed_cluster_refused(self):
        # Anything serving the application socket, or PG17 running at all, goes through the SQL check.
        self.assertEqual(self.status(serves=True), ("Unexpected PostgreSQL version", True))
        self.assertEqual(self.status(running=(17,)), ("Unexpected PostgreSQL version", True))
        for kwargs in ({"identity": "other"}, {"state": "in archive recovery"}):
            self.assertRegex(self.status(**kwargs)[0], "differs from the maintenance journal", kwargs)
        self.assertRegex(self.status(running=(15,))[0], "PostgreSQL 15 is not stopped and disabled")
        (m.NEW / "dwemer-maintenance-id").write_text("dwemer-stage-" + "b" * 32)
        self.assertRegex(self.status()[0], "differs from the maintenance journal")
        (m.NEW / "dwemer-maintenance-id").write_text(TAG)
        (m.NEW / "start.conf").write_text("disabled\n")
        self.assertRegex(self.status()[0], "differs from the maintenance journal")
        (m.NEW / "start.conf").write_text("auto\n")
        (m.OLD / "start.conf").write_text("manual\n")
        self.assertRegex(self.status()[0], "PostgreSQL 15 is not stopped and disabled")

    def test_stock_commented_start_conf_is_current_and_invalid_ones_are_refused(self):
        # The deployed clone: Debian's six comment lines, then auto; PG15 disabled the same way.
        for new, old in ((stock("auto", blank=False), "disabled\n"), (stock("auto"), stock("disabled")),
                         (stock("manual"), stock("disabled", blank=False))):
            (m.NEW / "start.conf").write_text(new)
            (m.OLD / "start.conf").write_text(old)
            self.assertEqual(self.status(), ("current", False), (new, old))
        # Comments never vouch for a different identity, configuration owner or running PG15.
        self.assertRegex(self.status(identity="other")[0], "differs from the maintenance journal")
        self.assertRegex(self.status(running=(15,))[0], "PostgreSQL 15 is not stopped and disabled")
        (m.NEW / "dwemer-maintenance-id").write_text("dwemer-stage-" + "b" * 32)
        self.assertRegex(self.status()[0], "differs from the maintenance journal")
        (m.NEW / "dwemer-maintenance-id").write_text(TAG)
        for new in (STOCK_HEADER, stock("disabled"), stock("auto") + "manual\n", stock("automatic"), ""):
            (m.NEW / "start.conf").write_text(new)
            self.assertRegex(self.status()[0], "differs from the maintenance journal", repr(new))
        (m.NEW / "start.conf").write_text(stock("auto"))
        for old in (STOCK_HEADER, stock("auto"), stock("disabled") + "auto\n", stock("disabledx"), ""):
            (m.OLD / "start.conf").write_text(old)
            self.assertRegex(self.status()[0], "PostgreSQL 15 is not stopped and disabled", repr(old))
        (m.OLD / "start.conf").write_text("disabled\n")
        target = m.ROOT / "linked-start.conf"
        target.write_text(stock("auto"))
        (m.NEW / "start.conf").unlink()
        (m.NEW / "start.conf").symlink_to(target)
        self.assertRegex(self.status()[0], "differs from the maintenance journal")


class RetainedDonor(unittest.TestCase):
    """Upgrades keep the PG15 donor and never use a Windows backup; earlier journals stay guarded."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(os.path.realpath(temp.name))
        paths = {"ROOT": base / "root", "STATE": base / "root" / "state.json", "CONF": base / "etc",
                 "DATA": base / "lib", "OLD": base / "etc" / "15" / "main", "NEW": base / "etc" / "17" / "dwemer",
                 "OLD_DATA": base / "lib" / "15" / "main", "NEW_DATA": base / "lib" / "17" / "dwemer",
                 "STAGER": str(base / "stager")}
        for key, value in paths.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.log = None
        m.ROOT.mkdir()
        Path(m.STAGER).write_text("")
        (m.OLD_DATA / "pg_tblspc").mkdir(parents=True)
        (m.OLD_DATA / "PG_VERSION").write_text("15\n")
        m.OLD.mkdir(parents=True)
        for name in ("postgresql.conf", "pg_hba.conf", "pg_ident.conf"):
            (m.OLD / name).write_text("# " + name + "\n")
        (m.OLD / "start.conf").write_text("auto\n")
        # A folder an earlier launcher still passes, and a backup an earlier release left in it.
        self.windows = base / "windows"
        self.windows.mkdir()
        self.legacy_tar = self.windows / "Dwemer-PG15-old.tar"
        self.legacy_tar.write_bytes(b"earlier backup")
        self.calls = []
        for name, value in (("sync_dir", None), ("verify_active", None), ("ensure_packages", None),
                            ("running", False), ("socket_serves", True), ("mounts_within", []),
                            ("fingerprint", ("base", "raw")), ("seal", ["17id", "0/1000", "shut down"])):
            patcher = patch.object(m, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, fake in (("query", self.fake_query), ("run", self.fake_run), ("cluster", self.fake_cluster), ("pg", self.fake_pg),
                           ("cluster_id", lambda version, path: f"{version}id")):
            patcher = patch.object(m, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(m.os, "chown", create=True),
                        patch.object(m.shutil, "disk_usage", return_value=types.SimpleNamespace(free=10 ** 12))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_query(self, socket, statement):
        files = {"config_file": "postgresql.conf", "hba_file": "pg_hba.conf", "ident_file": "pg_ident.conf"}
        if statement.startswith("show "):
            return str(m.OLD / files[statement[5:]])
        return "15id"

    def fake_run(self, *args, **kwargs):
        self.calls.append(args)
        return types.SimpleNamespace(stdout=b"1000\t" + str(m.OLD_DATA).encode() if args[0] == "du" else b"")

    def fake_cluster(self, version, command):
        self.calls.append((version, command))

    def fake_pg(self, *args, **kwargs):
        self.calls.append(args)
        if args[1] == m.STAGER:
            work = Path(args[3])
            (work / "pg17").mkdir(parents=True)
            (work / "source-copy" / "base").mkdir(parents=True)
            for name in ("verified.json", "globals.sql", "1.dump", "hba", "source-manifest.json"):
                (work / name).write_text("{}")

    def journal(self):
        return json.loads(m.STATE.read_text())

    def test_automatic_upgrade_keeps_the_donor_and_never_uses_a_windows_backup(self):
        m.automatic_upgrade()
        state = self.journal()
        work = Path(state["work"])
        self.assertEqual((state["phase"], state["cleaned"], state["auto_attempts"]), ("active", True, 1))
        self.assertFalse({"backup", "backup_sha256", "backup_bytes"} & state.keys())
        self.assertFalse(any(call[0] in ("tar", "findmnt") for call in self.calls if isinstance(call[0], str)))
        self.assertEqual(sorted(path.name for path in self.windows.iterdir()), [self.legacy_tar.name])
        self.assertEqual(self.legacy_tar.read_bytes(), b"earlier backup")
        # The donor and its disabled configuration stay; only the verified staging copies go.
        self.assertTrue((m.OLD_DATA / "PG_VERSION").is_file())
        self.assertEqual(((m.OLD / "start.conf").read_text(), (m.NEW / "start.conf").read_text()),
                         ("disabled\n", "auto\n"))
        self.assertEqual(sorted(path.name for path in work.iterdir()), ["hba", "source-manifest.json", "verified.json"])
        self.assertEqual(m.automatic_status(), "current")

    def test_a_stock_commented_pg15_start_conf_carries_over_and_stays_current(self):
        (m.OLD / "start.conf").write_text(stock("auto"))
        m.automatic_upgrade()
        state = self.journal()
        self.assertEqual((state["phase"], state["cleaned"]), ("active", True))
        self.assertEqual(((m.OLD / "start.conf").read_text(), (m.NEW / "start.conf").read_text()),
                         ("disabled\n", stock("auto")))
        with patch.object(m, "socket_serves", return_value=False), \
             patch.object(m, "controldata", return_value={"Database system identifier": state["candidate_id"],
                                                          "Database cluster state": "shut down"}), \
             patch.object(m, "verify_active", side_effect=AssertionError("verify_active")):
            self.assertEqual(m.automatic_status(), "current")

    def test_manual_upgrade_cleans_staging_too(self):
        m.upgrade()
        state = self.journal()
        self.assertEqual((state["phase"], state["cleaned"]), ("active", True))
        self.assertEqual(sorted(path.name for path in Path(state["work"]).iterdir()),
                         ["hba", "source-manifest.json", "verified.json"])

    def test_staging_copies_are_kept_unless_the_original_donor_is_in_place(self):
        # A cleanup failure after cutover leaves PG17 serving, without rollback, and the next update retries.
        with patch.object(m, "cluster_id", lambda version, path: "replaced" if version == 15 else "17id"), \
             patch.object(m, "rollback", side_effect=AssertionError("rollback")), \
             self.assertRaisesRegex(ValueError, "PostgreSQL 17 is serving, but cleanup stopped: .*differs from the "
                                    "saved donor, so the staging copies were kept. The next update retries"):
            m.upgrade()
        state = self.journal()
        work = Path(state["work"])
        self.assertEqual(((m.NEW / "start.conf").read_text(), state["phase"]), ("auto\n", "active"))
        self.assertNotIn("cleaned", state)
        staged = sorted(path.name for path in work.iterdir())
        self.assertIn("source-copy", staged)
        self.assertEqual(m.automatic_status(), "cleanup-needed")
        (m.OLD_DATA / "PG_VERSION").unlink()
        with self.assertRaisesRegex(ValueError, "missing or differs from the saved donor"):
            m.automatic_upgrade()
        (m.OLD_DATA / "PG_VERSION").write_text("15\n")
        with patch.object(m, "cluster_id", lambda version, path: "other" if version == 17 else "15id"), \
             self.assertRaisesRegex(ValueError, "Unexpected PG17 cleanup target"):
            m.discard_staging(self.journal())
        self.assertEqual(sorted(path.name for path in work.iterdir()), staged)
        self.assertNotIn("cleaned", self.journal())

    def test_cleanup_retries_clean_only_even_when_low_on_space_and_refuse_changed_state(self):
        with patch.object(m, "cluster_id", lambda version, path: "replaced" if version == 15 else "17id"), \
             self.assertRaises(ValueError):
            m.upgrade()
        work = Path(self.journal()["work"])
        staged = sorted(path.name for path in work.iterdir())
        pending = m.STATE.read_bytes()
        # No retry may stage, start or stop a cluster, or check staging space.
        for name in ("pg", "cluster", "ensure_packages"):
            patcher = patch.object(m, name, side_effect=AssertionError(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        low = patch.object(m.shutil, "disk_usage", return_value=types.SimpleNamespace(free=0))
        low.start()
        self.addCleanup(low.stop)
        # Changed state or configuration is refused without cleaning anything or starting an upgrade.
        changes = ((dict(phase="rolled-back"), "No serving PostgreSQL 17 migration"),
                   (dict(phase="mystery"), "No serving PostgreSQL 17 migration"),
                   (dict(candidate_id="other"), "differs from the maintenance journal"),
                   (dict(work=str(m.DATA / ("dwemer-stage-" + "c" * 32))), "differs from the maintenance journal"))
        for change, reason in changes:
            with self.subTest(change=change):
                m.STATE.write_text(json.dumps(dict(json.loads(pending), **change)))
                before = m.STATE.read_bytes()
                with self.assertRaisesRegex(ValueError, reason):
                    m.cleanup_staging()
                self.assertEqual((m.STATE.read_bytes(), sorted(path.name for path in work.iterdir())), (before, staged))
        m.STATE.write_text(json.dumps(dict(json.loads(pending), phase="mystery")))
        with self.assertRaisesRegex(ValueError, "Unknown migration state"):
            m.automatic_status()
        m.STATE.write_bytes(pending)
        self.assertEqual((m.automatic_status(), m.STATE.read_bytes()), ("cleanup-needed", pending))
        # Manual Upgrade and the update both retry only the cleanup, whose guard still fails here.
        with patch.object(m, "cluster_id", lambda version, path: "replaced" if version == 15 else "17id"):
            for retry in (m.upgrade, m.automatic_upgrade, m.cleanup_staging):
                with self.subTest(retry=retry.__name__), \
                     self.assertRaisesRegex(ValueError, "PostgreSQL 17 is serving, but cleanup stopped"):
                    retry()
                self.assertEqual((m.STATE.read_bytes(), sorted(path.name for path in work.iterdir())), (pending, staged))
        m.cleanup_staging()
        cleaned = m.STATE.read_bytes()
        self.assertEqual(sorted(path.name for path in work.iterdir()), ["hba", "source-manifest.json", "verified.json"])
        # Repeats are idempotent and keep the completed journal byte for byte.
        m.cleanup_staging()
        m.automatic_upgrade()
        self.assertEqual((m.automatic_status(), m.STATE.read_bytes()), ("current", cleaned))
        with self.assertRaisesRegex(ValueError, "Previous maintenance state"):
            m.upgrade()
        self.assertEqual(m.STATE.read_bytes(), cleaned)

    def test_cleanup_refuses_mounts_links_and_a_live_donor_before_deleting_anything(self):
        with patch.object(m, "cluster_id", lambda version, path: "replaced" if version == 15 else "17id"), \
             self.assertRaises(ValueError):
            m.upgrade()
        work = Path(self.journal()["work"])
        copy = work / "source-copy"
        staged = sorted(path.name for path in work.iterdir())

        def refused(pattern):
            with patch.object(m.shutil, "rmtree", side_effect=AssertionError("rmtree")), \
                 self.assertRaisesRegex(ValueError, pattern):
                m.automatic_upgrade()
            self.assertEqual(sorted(path.name for path in work.iterdir()), staged)
            self.assertTrue(os.path.lexists(copy) and (m.OLD_DATA / "PG_VERSION").is_file())
            self.assertNotIn("cleaned", self.journal())

        # A bind mount keeps the device number, so only the mount table shows it.
        with patch.object(m, "mounts_within", return_value=[str(copy / "base")]):
            refused("is or contains a mount point")
        with patch.object(m, "mounts_within", return_value=[str(work)]):
            refused("is or contains a mount point")
        # The donor must still be stopped and disabled.
        with patch.object(m, "running", return_value=True):
            refused("not stopped and disabled")
        for content in ("auto\n", stock("auto"), STOCK_HEADER, stock("disabled") + "auto\n"):
            (m.OLD / "start.conf").write_text(content)
            refused("not stopped and disabled")
        (m.OLD / "start.conf").write_text("disabled\n")
        (m.OLD_DATA / "postmaster.pid").write_text("1\n")
        refused("not stopped and disabled")
        (m.OLD_DATA / "postmaster.pid").unlink()
        # A linked parent of the donor path.
        real = m.DATA / "real15"
        (m.DATA / "15").rename(real)
        (m.DATA / "15").symlink_to(real)
        refused("missing or differs from the saved donor")
        (m.DATA / "15").unlink()
        real.rename(m.DATA / "15")
        # A link inside the copy, and a linked copy, could lead rmtree to the donor.
        (copy / "base" / "donor").symlink_to(m.OLD_DATA)
        refused("holds links, special files or mounts")
        (copy / "base" / "donor").unlink()
        copy.rename(m.DATA / "real-copy")
        copy.symlink_to(m.OLD_DATA)
        refused("source copy is linked or not a directory")
        copy.unlink()
        (m.DATA / "real-copy").rename(copy)
        (work / "1.dump").unlink()
        (work / "1.dump").symlink_to(m.OLD_DATA / "PG_VERSION")
        refused("is linked or not a plain file")
        (work / "1.dump").unlink()
        (work / "1.dump").write_text("{}")
        # Once every guard passes, the retry cleans up and PG17 keeps serving.
        m.automatic_upgrade()
        self.assertEqual(sorted(path.name for path in work.iterdir()), ["hba", "source-manifest.json", "verified.json"])
        self.assertEqual((self.journal()["cleaned"], m.automatic_status()), (True, "current"))
        self.assertTrue((m.OLD_DATA / "PG_VERSION").is_file())

    def legacy(self, **record):
        # An active journal from an earlier release, with its Windows backup still on disk.
        tag = "dwemer-stage-" + "b" * 32
        state = {"phase": "active", "work": str(m.DATA / tag), "backup": str(self.legacy_tar), "candidate_id": "17id",
                 "donor_id": "15id", "old_start": "auto\n", "backup_sha256": "0" * 64, "backup_bytes": 14}
        (m.DATA / tag).mkdir(parents=True)
        for name in ("verified.json", "globals.sql"):
            (m.DATA / tag / name).write_text("{}")
        m.NEW.mkdir(parents=True)
        (m.NEW / "dwemer-maintenance-id").write_text(tag)
        self.legacy_tar.with_suffix(".recovery.json").write_text("{}")
        if record:
            quarantine = m.quarantine_path(state)
            if record["stage"] != "removed":
                m.OLD_DATA.rename(quarantine)
                info = os.lstat(quarantine)
                record["inode"] = [info.st_dev, info.st_ino]
            else:
                shutil.rmtree(m.OLD_DATA)
            state["donor_removal"] = dict({"quarantine": str(quarantine), "inode": [0, 0], "bytes": 1,
                                           "backup_sha256": "0" * 64, "backup_bytes": 14}, **record)
        m.STATE.write_text(json.dumps(state))
        return state

    def assert_cleanup_refused(self, reason):
        before = m.STATE.read_bytes(), self.legacy_tar.read_bytes()
        self.assertEqual(m.cleanup_preview(), reason)
        with self.assertRaisesRegex(ValueError, re.escape(reason) + "; nothing was removed"):
            m.cleanup_pg15()
        with self.assertRaisesRegex(ValueError, r"The cleanup preview is stale \(" + re.escape(reason)):
            m.cleanup_pg15("a" * 64)
        self.assertEqual((m.STATE.read_bytes(), self.legacy_tar.read_bytes()), before)

    def test_cleanup_is_refused_for_every_migration_and_keeps_old_replies(self):
        keeps = "Core keeps the PostgreSQL 15 data directory and its disabled configuration for recovery"
        m.STATE.write_text(json.dumps({"phase": "rolled-back", "work": str(m.DATA / TAG)}))
        self.assert_cleanup_refused(keeps)
        m.STATE.unlink()
        m.upgrade()
        self.assert_cleanup_refused(keeps)
        # An earlier release's migration with every file its removal used to require is refused as well.
        m.STATE.unlink()
        shutil.rmtree(m.NEW)
        self.legacy()
        self.assert_cleanup_refused(keeps)
        self.assertTrue((m.OLD_DATA / "PG_VERSION").is_file())

    def test_an_interrupted_earlier_removal_is_never_resumed(self):
        state = self.legacy(stage="removing")
        quarantine = m.quarantine_path(state)
        self.assert_cleanup_refused(f"An earlier release's interrupted PG15 removal left {quarantine}; "
                                    "Core no longer removes PostgreSQL 15 data, so it was kept")
        self.assertTrue((quarantine / "PG_VERSION").is_file())
        with self.assertRaisesRegex(ValueError, "partly deleted copy at .* was kept"):
            m.rollback()
        # Interrupted after its last deletion: reported as completed, still without a journal change.
        shutil.rmtree(quarantine)
        self.assert_cleanup_refused_with_success()

    def assert_cleanup_refused_with_success(self):
        before = m.STATE.read_bytes()
        self.assertEqual((m.cleanup_preview(), m.cleanup_pg15()), (m.REMOVED, 0))
        with self.assertRaisesRegex(ValueError, "stale"):
            m.cleanup_pg15("a" * 64)
        self.assertEqual(m.STATE.read_bytes(), before)

    def test_a_completed_earlier_removal_still_reports_success_and_blocks_rollback(self):
        state = self.legacy(stage="removed")
        self.assert_cleanup_refused_with_success()
        with self.assertRaisesRegex(ValueError, "PostgreSQL 17 keeps serving. Restore PG15 by hand from "
                                    + re.escape(str(self.legacy_tar))):
            m.rollback()
        # The staging copies may hold the last PG15 data, so the update settles without deleting them.
        m.automatic_upgrade()
        self.assertTrue((Path(state["work"]) / "globals.sql").is_file())
        self.assertTrue(self.journal()["cleaned"] and self.legacy_tar.is_file())

    def test_rollback_refuses_before_touching_clusters_without_a_donor(self):
        state = self.legacy(stage="quarantine")
        untouched = {name: patch.object(m, name, side_effect=AssertionError(name))
                     for name in ("running", "cluster", "fingerprint", "atomic", "cluster_id")}
        for patcher in untouched.values():
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaisesRegex(ValueError, "complete renamed data directory remains at"):
            m.rollback()
        m.quarantine_path(state).rename(m.OLD_DATA)
        (m.OLD_DATA / "PG_VERSION").unlink()
        state.pop("donor_removal")
        m.STATE.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "missing, so rollback changed nothing. Restore it from .*tar"):
            m.rollback()
        # Journals without an external backup name none.
        state.pop("backup")
        m.STATE.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, "missing, so rollback changed nothing. Restore it before"):
            m.rollback()

    def test_cli_accepts_the_legacy_folder_and_validates_the_retired_token(self):
        script = str(Path(__file__).resolve().parents[1] / "bin" / "ddistro_pg17_maintenance")
        for args in (["cleanup-pg15", "--confirm-preview", "A" * 64], ["cleanup-pg15", "--confirm-preview", "a" * 63],
                     ["cleanup-preview", "--confirm-preview", "a" * 64]):
            run = subprocess.run([sys.executable, script, *args], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, args)
            self.assertIn("--confirm-preview", run.stderr)
        if not hasattr(os, "geteuid") or os.geteuid() == 0:
            self.skipTest("needs a non-root POSIX account")
        for args in (["auto", str(self.windows)], ["upgrade", str(self.windows)], ["auto"], ["upgrade"]):
            run = subprocess.run([sys.executable, script, *args], capture_output=True, text=True)
            self.assertEqual((run.returncode, run.stderr.strip()[-13:]), (2, "root required"), args)
        self.assertEqual(sorted(path.name for path in self.windows.iterdir()), [self.legacy_tar.name])


class BoundedStream(io.BytesIO):
    largest = 0

    def __iter__(self):
        raise AssertionError("line iteration would hold a whole row")

    def readline(self, size=-1):
        assert 0 < size <= 4096, size
        return super().readline(size)

    def read(self, size=-1):
        assert 0 < size <= 1024 * 1024, size
        self.largest = max(self.largest, size)
        return super().read(size)


class Fingerprint(unittest.TestCase):
    def test_dump_hash_is_bounded_and_keeps_the_legacy_digest(self):
        kept = b"--\n-- PostgreSQL database cluster dump\n--\n\\restrict k\n"
        dumped = b"-- Dumped from database version 17.6\n-- Dumped by pg_dump version 17.6\n"
        body = b"COPY t FROM stdin;\n" + b"x" * (5 * 1024 * 1024) + b"\n\\.\n-- Dumped later stays\n"
        stream = BoundedStream(kept + dumped + body)
        process = types.SimpleNamespace(stdout=stream, wait=lambda: 0)
        with tempfile.TemporaryDirectory() as temp, patch.object(m, "NEW", Path(temp) / "conf"), \
             patch.object(m, "NEW_DATA", Path(temp) / "data"), patch.object(m, "ROOT", Path(temp)), \
             patch.object(m, "pg"), patch.object(m, "query", return_value="[]"), \
             patch.object(m.subprocess, "Popen") as popen:
            popen.return_value.__enter__.return_value = process
            m.log = None
            current, legacy = m.fingerprint({"work": str(Path(temp) / TAG), "key": "k"})
        self.assertEqual(current, hashlib.sha256(b"[]" + kept + body).hexdigest())
        self.assertEqual(legacy, hashlib.sha256(b"[]" + kept + dumped + body).hexdigest())
        self.assertEqual(stream.largest, 1024 * 1024)


def manifest(*tables, fmt="chim_selected_tables_v2"):
    return json.dumps({"format": fmt, "tables": list(tables)})


class RequestLogs(unittest.TestCase):
    """CHIM request logs are cleared only in public and verified saves, after a matching preview. No SQL runs."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        for key, value in (("ROOT", base), ("STATE", base / "state.json"), ("DATA", base)):
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.log = None
        self.statements = []
        self.rows = [
            dict(schema_name="public", relid=10, filenode=10, relkind="r", manifest=None, registered=False,
                 saves=[], bytes=6000, rows_estimate=40),
            dict(schema_name="chim_profile_hero", relid=11, filenode=11, relkind="r",
                 manifest=manifest("audit_request", "eventlog"), registered=True, saves=["Hero"], bytes=3000,
                 rows_estimate=-1),
            dict(schema_name="chim_profile_stray", relid=12, filenode=12, relkind="r",
                 manifest=manifest("audit_request"), registered=False, saves=[], bytes=900, rows_estimate=1),
            dict(schema_name="chim_profile_upgrade_1_2", relid=13, filenode=13, relkind="r",
                 manifest=manifest("audit_request"), registered=True, saves=["Stage"], bytes=800, rows_estimate=1),
            dict(schema_name="chim_profile_legacy", relid=14, filenode=14, relkind="r", manifest=None,
                 registered=True, saves=["Old"], bytes=700, rows_estimate=1),
            dict(schema_name="chim_profile_future", relid=15, filenode=15, relkind="r",
                 manifest=manifest("audit_request", fmt="other"), registered=True, saves=["New"], bytes=600,
                 rows_estimate=1),
            dict(schema_name="chim_profile_slim", relid=16, filenode=16, relkind="r", manifest=manifest("eventlog"),
                 registered=True, saves=["Slim"], bytes=500, rows_estimate=1),
            dict(schema_name="chim_profile_split", relid=17, filenode=17, relkind="p",
                 manifest=manifest("audit_request"), registered=True, saves=["Split"], bytes=0, rows_estimate=0),
            dict(schema_name="games", relid=18, filenode=18, relkind="r", manifest=manifest("audit_request"),
                 registered=True, saves=["Odd"], bytes=400, rows_estimate=1),
            dict(schema_name="chim_profile_hooked", relid=19, filenode=19, relkind="r",
                 manifest=manifest("audit_request"), registered=True, saves=["Hooked"], bytes=300, rows_estimate=1,
                 clear_triggers=True),
        ]
        for row in self.rows:
            row.setdefault("clear_triggers", False)
        self.token = "c" * 64
        self.after = "100"
        self.clear_reply = "9000"
        for name, fake in (("query_db", self.fake_query_db), ("socket_serves", lambda: True)):
            patcher = patch.object(m, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        disk = patch.object(m.shutil, "disk_usage", side_effect=[types.SimpleNamespace(free=50),
                                                                  types.SimpleNamespace(free=8050)] * 4)
        disk.start()
        self.addCleanup(disk.stop)

    def fake_query_db(self, database, statement):
        self.statements.append((database, statement))
        if statement.startswith("do $clear$"):
            if isinstance(self.clear_reply, Exception):
                raise self.clear_reply
            return self.clear_reply
        if "json_build_object('state'" in statement:
            return json.dumps({"state": self.token, "rows": self.rows})
        if "from pg_database where datname" in statement:
            return "1"
        if "to_regclass('chim_meta.playthrough_profiles')" in statement:
            return "t"
        if statement.startswith("select coalesce(sum(pg_total_relation_size(oid))"):
            return self.after
        raise AssertionError(statement)

    def test_preview_lists_only_public_and_verified_saves_and_changes_nothing(self):
        preview = m.request_logs_preview()
        self.assertEqual(preview["token"], self.token)
        self.assertEqual([(t["schema"], t["kind"], t["saves"]) for t in preview["tables"]],
                         [("public", "live", []), ("chim_profile_hero", "save", ["Hero"])])
        self.assertEqual(preview["total_bytes"], 9000)
        self.assertIsNone(preview["tables"][1]["rows_estimate"])
        self.assertFalse(any("relid" in table for table in preview["tables"]))
        self.assertEqual({s["schema"]: s["reason"] for s in preview["skipped"]}, {
            "chim_profile_stray": "not registered in Playthrough Saves",
            "chim_profile_upgrade_1_2": "not a CHIM save schema",
            "chim_profile_legacy": "no readable save manifest",
            "chim_profile_future": "no recognised save manifest",
            "chim_profile_slim": "its manifest does not list request logs",
            "chim_profile_split": "not a plain table",
            "games": "not a CHIM save schema",
            "chim_profile_hooked": "has an enabled TRUNCATE trigger",
        })
        self.assertTrue(all(db == "dwemer" or db == "postgres" for db, _ in self.statements))
        self.assertFalse(any(word in sql.lower() for _, sql in self.statements for word in ("truncate only", "truncate table", "delete from", "drop ")))

    def test_clear_truncates_only_previewed_tables_in_one_guarded_transaction(self):
        result = m.request_logs_clear(self.token)
        clears = [sql for _, sql in self.statements if sql.startswith("do $clear$")]
        self.assertEqual(len(clears), 1)
        sql = clears[0]
        self.assertIn("pg_try_advisory_xact_lock(hashtext('chim_playthrough_retention'))", sql)
        self.assertIn("set_config('lock_timeout', '5s', true)", sql)
        # ONLY binds to each name, so every table carries its own.
        self.assertIn('lock table only "public".audit_request, only "chim_profile_hero".audit_request in access exclusive mode', sql)
        self.assertIn('truncate only "public".audit_request, only "chim_profile_hero".audit_request restrict;', sql)
        self.assertIn("lock table only chim_meta.playthrough_profiles in share mode", sql)
        self.assertNotIn("cascade", sql.lower())
        self.assertNotIn("stray", sql)
        self.assertNotIn("hooked", sql)
        # The digest covers each table's triggers, and the locked tables are checked again for enabled
        # TRUNCATE triggers before the truncate.
        self.assertIn("clear_triggers), E'\\n' order by schema_name) from candidates", sql)
        trigger_check = ("if exists(select 1 from pg_trigger t where t.tgrelid = any(array[10,11]::oid[])"
                         " and t.tgtype::int & 32 <> 0 and t.tgenabled <> 'D') then"
                         " raise exception 'A request log has an enabled TRUNCATE trigger; nothing was cleared';")
        self.assertIn(trigger_check, sql)
        # The digest is recomputed after the locks, then the trigger check, then the truncate.
        self.assertLess(sql.index("lock table only"), sql.index(f"is distinct from '{self.token}'"))
        self.assertLess(sql.index(f"is distinct from '{self.token}'"), sql.index(trigger_check))
        self.assertLess(sql.index(trigger_check), sql.index("truncate only"))
        self.assertEqual(result, {"cleared_tables": 2, "before_bytes": 9000, "after_bytes": 100,
                                  "reclaimed_bytes": 8900, "filesystem_freed_bytes": 8000})

    def test_clear_refuses_a_stale_preview_unfinished_maintenance_or_a_stopped_server(self):
        with self.assertRaisesRegex(ValueError, "changed since the preview; nothing was cleared"):
            m.request_logs_clear("d" * 64)
        self.assertFalse(any(sql.startswith("do $clear$") for _, sql in self.statements))
        for phase in ("staging", "publishing", "checking"):
            m.STATE.write_text(json.dumps({"phase": phase}))
            self.statements.clear()
            with self.subTest(phase=phase), self.assertRaisesRegex(ValueError, "unfinished"):
                m.request_logs_clear(self.token)
            self.assertEqual(self.statements, [])
        m.STATE.unlink()
        with patch.object(m, "socket_serves", return_value=False), self.assertRaisesRegex(ValueError, "not running"):
            m.request_logs_preview()
        self.rows = [dict(row, relkind="p") for row in self.rows]
        with self.assertRaisesRegex(ValueError, "no CHIM request logs"):
            m.request_logs_clear(self.token)

    def test_clear_surfaces_lock_timeouts_and_database_refusals(self):
        self.clear_reply = ValueError("canceling statement due to lock timeout")
        with self.assertRaisesRegex(ValueError, "still writing to its request logs; nothing was cleared"):
            m.request_logs_clear(self.token)
        self.clear_reply = ValueError("A CHIM Playthrough Save, switch, import or cleanup is running; nothing was cleared")
        with self.assertRaisesRegex(ValueError, "Playthrough Save, switch"):
            m.request_logs_clear(self.token)

    def test_space_report_names_the_shortage_and_largest_consumers(self):
        databases = [{"name": "dwemer", "bytes": 9 * 1024 ** 3, "open": True},
                     {"name": "closed", "bytes": 1024 ** 3, "open": False}]
        tables = [{"schema": "public", "name": "audit_request", "bytes": 6 * 1024 ** 3, "toast_bytes": 5 * 1024 ** 3},
                  {"schema": "chim_profile_hero", "name": "audit_request", "bytes": 2 * 1024 ** 3, "toast_bytes": 0}]
        real = self.fake_query_db

        def sizes(database, statement):
            if statement == m.SPACE_DATABASES:
                return json.dumps(databases)
            if statement == m.SPACE_RELATIONS:
                self.assertEqual(database, "dwemer")
                return json.dumps(tables)
            return real(database, statement)

        with patch.object(m, "query_db", sizes):
            report = m.space_report(10 * 1024 ** 3, 25 * 1024 ** 3)
        self.assertTrue(report.startswith(
            "needs 40.0 GB free (four times the 10.0 GB PostgreSQL 15 data), 25.0 GB is available, short by 15.0 GB"))
        self.assertIn("Largest databases: dwemer 9.0 GB, closed 1.0 GB", report)
        self.assertIn("Largest schemas: dwemer.public 6.0 GB, dwemer.chim_profile_hero 2.0 GB", report)
        self.assertIn("dwemer public.audit_request 6.0 GB (TOAST 5.0 GB)", report)
        self.assertIn("CHIM request logs use 8.8 KB across the live data and 1 save", report)
        self.assertNotIn("\n", report)
        with patch.object(m, "query_db", side_effect=ValueError("down")):
            self.assertIn("short by 15.0 GB. The databases could not be sized", m.space_report(10 * 1024 ** 3, 25 * 1024 ** 3))

    def test_cli_requires_the_preview_token_to_clear(self):
        script = str(Path(__file__).resolve().parents[1] / "bin" / "ddistro_pg17_maintenance")
        for args in (["request-logs-clear"], ["request-logs-clear", "--confirm-preview", "A" * 64],
                     ["request-logs-preview", "--confirm-preview", "a" * 64]):
            run = subprocess.run([sys.executable, script, *args], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, args)
            self.assertIn("--confirm-preview", run.stderr)


if __name__ == "__main__":
    unittest.main()
