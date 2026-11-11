"""Focused PG17 retry, rollback-guard and dump-hash regressions. No live distro, cluster or SQL."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
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


class Journal(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        paths = {"STATE": base / "state.json", "DATA": base, "OLD_DATA": base / "15" / "main",
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

    def test_retained_runs_never_adopt_an_unmarked_destination(self):
        root, conf = m.DATA / "root", m.DATA / "conf"
        root.mkdir()
        building = conf / ("." + TAG + "-config")
        building.mkdir(parents=True)
        (m.NEW / "dwemer-maintenance-id").unlink()
        self.journal(candidate_id="17id")
        with patch.object(m, "ROOT", root), patch.object(m, "CONF", conf), patch.object(m, "verify_active"):
            with self.assertRaisesRegex(ValueError, "lacks the retained PG17 cluster's maintenance marker"):
                m.upgrade(None)
            self.assertTrue(m.NEW.is_dir() and building.is_dir() and m.STATE.is_file())
            # A marked destination and an unpublished build of that run are retained, never reused.
            (m.NEW / "dwemer-maintenance-id").write_text(TAG)
            with self.assertRaisesRegex(ValueError, "Select a Windows backup directory"):
                m.upgrade(None)
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


class DonorRemoval(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(os.path.realpath(temp.name))
        paths = {"STATE": base / "state.json", "DATA": base, "OLD_DATA": base / "15" / "main",
                 "OLD": base / "etc15", "NEW": base / "17"}
        for key, value in paths.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.log = None
        (m.OLD_DATA / "base" / "1").mkdir(parents=True)
        (m.OLD_DATA / "PG_VERSION").write_text("15\n")
        (m.OLD_DATA / "base" / "1" / "1259").write_bytes(b"r" * 20000)
        (m.OLD / "conf.d").mkdir(parents=True)
        for name in ("postgresql.conf", "pg_hba.conf", "pg_ident.conf"):
            (m.OLD / name).write_text("# " + name + "\n")
        (m.OLD / "start.conf").write_text("disabled\n")
        self.archive = base / "backup.tar"
        self.write_archive(self.archive, tarfile.GNU_FORMAT)
        self.state = {"phase": "active", "work": str(base / TAG), "backup": str(self.archive),
                      "candidate_id": "17id", "donor_id": "15id", "old_start": "auto\n"}
        self.sidecar = self.archive.with_suffix(".recovery.json")
        self.sidecar.write_text(json.dumps({"postgres_major": 15, "system_identifier": "15id", "data": str(m.OLD_DATA),
                                            "config": str(m.OLD), "original_start_conf": "auto\n"}))
        self.save()

    def write_archive(self, path, form):
        with tarfile.open(path, "w", format=form) as archive:
            for tree in (m.OLD_DATA, m.OLD):
                archive.add(tree, arcname=str(tree.relative_to("/")))

    def save(self):
        m.STATE.write_text(json.dumps(self.state))

    def journal(self):
        return json.loads(m.STATE.read_text())

    def bind(self):
        self.state["backup_sha256"] = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.state["backup_bytes"] = self.archive.stat().st_size

    def verify(self):
        return m.verify_archive(self.state, m.donor_tree(m.OLD_DATA), m.config_tree())[:2]

    def test_archive_check_supports_legacy_and_bound_archives(self):
        expected = (hashlib.sha256(self.archive.read_bytes()).hexdigest(), self.archive.stat().st_size)
        self.assertEqual(self.verify(), expected)
        self.bind()
        self.assertEqual(self.verify(), expected)

    def test_archive_must_match_the_whole_retained_configuration(self):
        hba = m.OLD / "pg_hba.conf"
        hba.write_text("host all all 0.0.0.0/0 trust\n")
        with self.assertRaisesRegex(ValueError, "differs from its Windows backup"):
            self.verify()
        self.write_archive(self.archive, tarfile.GNU_FORMAT)
        (m.OLD / "conf.d" / "extra.conf").write_text("port = 5433\n")
        with self.assertRaisesRegex(ValueError, "does not hold the whole"):
            self.verify()
        (m.OLD / "conf.d" / "extra.conf").unlink()
        hba.unlink()
        with self.assertRaisesRegex(ValueError, "configuration is incomplete"):
            self.verify()

    def test_replaced_damaged_or_mismatched_archives_stop_removal(self):
        self.bind()
        self.write_archive(self.archive, tarfile.PAX_FORMAT)
        with self.assertRaisesRegex(ValueError, "changed since it was created"):
            self.verify()
        self.state.pop("backup_sha256")
        self.verify()
        self.archive.write_bytes(self.archive.read_bytes()[:3000])
        with self.assertRaises(ValueError):
            self.verify()
        self.write_archive(self.archive, tarfile.GNU_FORMAT)
        (m.OLD_DATA / "base" / "1" / "1259").write_bytes(b"w" * 20000)
        with self.assertRaisesRegex(ValueError, "differs from its Windows backup"):
            self.verify()
        self.write_archive(self.archive, tarfile.GNU_FORMAT)
        (m.OLD_DATA / "postmaster.opts").write_text("started again\n")
        with self.assertRaisesRegex(ValueError, "does not hold the whole"):
            self.verify()
        self.archive.unlink()
        with self.assertRaises(OSError):
            self.verify()

    def test_links_are_refused_and_allocation_counts_whole_inodes(self):
        os.link(m.OLD_DATA / "base" / "1" / "1259", m.OLD_DATA / "inner-link")
        entries = m.donor_tree(m.OLD_DATA)
        once = sum(info.st_blocks * 512 for name, info in entries.items() if name != "inner-link")
        self.assertEqual(m.allocated_bytes(entries), once)
        os.link(m.OLD_DATA / "PG_VERSION", m.DATA / "outside-link")
        partial = m.donor_tree(m.OLD_DATA)
        self.assertEqual(m.allocated_bytes(partial), once - partial["PG_VERSION"].st_blocks * 512)
        (m.OLD_DATA / "pg_wal").symlink_to(m.DATA)
        with self.assertRaisesRegex(ValueError, "links, special files or mounts"):
            m.donor_tree(m.OLD_DATA)

    def resumable(self):
        # Stub only the live-cluster and Windows mount checks; hashing, backup identity and quarantine checks stay real.
        checks = {
            "check_removal": patch.object(m, "check_removal", side_effect=lambda state: (
                m.donor_tree(m.OLD_DATA), m.config_tree(), m.check_backup(state))),
            "check_pg17": patch.object(m, "check_pg17"),
            "check_backup": patch.object(m, "check_backup", wraps=m.check_backup),
            "windows_volume": patch.object(m, "windows_volume"),
            "cluster_dirs": patch.object(m, "cluster_dirs", return_value=[("15", "main", m.OLD_DATA)]),
            "mounts_within": patch.object(m, "mounts_within", return_value=[]),
        }
        mocks = {name: patcher.start() for name, patcher in checks.items()}
        for patcher in checks.values():
            self.addCleanup(patcher.stop)
        return mocks

    def interrupt_after_rename(self):
        size = (m.OLD_DATA / "base" / "1" / "1259").stat().st_blocks * 512

        def partial(path):
            (Path(path) / "base" / "1" / "1259").unlink()
            raise KeyboardInterrupt

        with patch.object(m.shutil, "rmtree", side_effect=partial), self.assertRaises(KeyboardInterrupt):
            m.cleanup_pg15()
        return size

    def test_interrupted_removal_resumes_from_its_journal(self):
        quarantine = m.quarantine_path(self.state)
        mocks = self.resumable()
        whole = m.allocated_bytes(m.donor_tree(m.OLD_DATA))
        with patch.object(m.os, "rename", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                m.cleanup_pg15()
        record = self.journal()["donor_removal"]
        # A legacy archive without a journaled checksum is bound before the quarantine rename.
        self.assertEqual((record["stage"], record["backup_sha256"], record["backup_bytes"]),
                         ("quarantine", hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                          self.archive.stat().st_size))
        self.assertIsNone(m.removal(self.journal()))
        deleted = self.interrupt_after_rename()
        # Each fresh attempt rechecks everything after the long comparison.
        self.assertEqual(mocks["check_removal"].call_count, 4)
        self.assertFalse(m.OLD_DATA.exists())
        self.assertTrue(quarantine.is_dir())
        mocks["check_pg17"].reset_mock()
        mocks["check_backup"].reset_mock()
        eligible, left, reason, _ = m.cleanup_preview()
        self.assertEqual((eligible, left), (True, whole - deleted))
        self.assertIn("interrupted", reason)
        with patch.object(m, "file_digest", wraps=m.file_digest) as digest:
            self.assertEqual(m.cleanup_pg15(), whole - deleted)
        self.assertEqual(mocks["check_removal"].call_count, 4)
        # The resumed run hashes the backup once and rechecks PG17 and the backup before deleting.
        self.assertEqual((mocks["check_pg17"].call_count, mocks["check_backup"].call_count, digest.call_count),
                         (3, 3, 1))
        self.assertEqual(mocks["check_backup"].call_args.args[1]["backup_sha256"], record["backup_sha256"])
        self.assertFalse(quarantine.exists())
        self.assertEqual(self.journal()["donor_removal"]["stage"], "removed")
        self.assertEqual(m.cleanup_pg15(), 0)
        self.assertEqual(m.cleanup_preview(), (False, 0, "The PG15 data directory was already removed", None))
        self.assertTrue((m.OLD / "start.conf").is_file() and self.archive.is_file())

    def test_resume_rechecks_backup_digest_and_nested_mounts(self):
        quarantine = m.quarantine_path(self.state)
        mocks = self.resumable()
        self.interrupt_after_rename()
        original = self.archive.read_bytes()
        self.archive.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(ValueError, "changed since it was verified"):
            m.cleanup_pg15()
        self.archive.write_bytes(original)
        mocks["mounts_within"].return_value = [str(quarantine / "base")]
        with self.assertRaisesRegex(ValueError, "mount point; it was kept"):
            m.cleanup_preview()
        with self.assertRaisesRegex(ValueError, "mount point; it was kept"):
            m.cleanup_pg15()
        self.assertTrue(quarantine.is_dir())
        self.assertEqual(self.journal()["donor_removal"]["stage"], "removing")

    def refused_fresh(self, change, message):
        real = m.verify_archive

        def comparing(*args):
            result = real(*args)
            change()
            return result

        with patch.object(m, "verify_archive", comparing), \
             patch.object(m.shutil, "rmtree", side_effect=AssertionError("rmtree")):
            with self.assertRaisesRegex(ValueError, message):
                m.cleanup_pg15()
        self.assertNotIn("donor_removal", self.journal())
        self.assertTrue((m.OLD_DATA / "base" / "1" / "1259").is_file())

    def test_fresh_removal_rechecks_donor_and_backup_after_the_long_comparison(self):
        self.resumable()
        relation = m.OLD_DATA / "base" / "1" / "1259"
        before = relation.stat()

        def rewritten():
            # Same size and restored mtime; only ctime records the write. Its clock is coarse, so wait a tick.
            time.sleep(0.05)
            relation.write_bytes(b"w" * 20000)
            os.utime(relation, ns=(before.st_atime_ns, before.st_mtime_ns))

        self.refused_fresh(rewritten, "changed during verification; nothing was removed")
        relation.write_bytes(b"r" * 20000)
        self.write_archive(self.archive, tarfile.GNU_FORMAT)

        def replaced():
            copy = self.archive.with_name("copy.tar")
            copy.write_bytes(self.archive.read_bytes())
            copy.replace(self.archive)

        self.refused_fresh(replaced, "changed during verification; nothing was removed")

        def linked():
            target = self.sidecar.with_name("elsewhere.json")
            self.sidecar.replace(target)
            self.sidecar.symlink_to(target)

        self.refused_fresh(linked, "recovery JSON is missing or linked")

    def test_a_backup_that_changes_while_it_is_hashed_is_refused(self):
        archive, grown = self.archive, []

        class Growing(m.Hashed):
            # A writer appends to the same file once the hash has started.
            def read(self, size=-1):
                if not grown:
                    grown.append(True)
                    with open(archive, "ab") as grow:
                        grow.write(b"\0")
                return super().read(size)

        self.resumable()
        with patch.object(m, "Hashed", Growing), \
             self.assertRaisesRegex(ValueError, "changed while it was verified; the PG15 data was kept"):
            m.cleanup_pg15()
        self.assertNotIn("donor_removal", self.journal())
        self.write_archive(self.archive, tarfile.GNU_FORMAT)
        self.interrupt_after_rename()
        grown.clear()
        with patch.object(m, "Hashed", Growing), patch.object(m.shutil, "rmtree", side_effect=AssertionError):
            with self.assertRaisesRegex(ValueError, "changed while it was verified; the PG15 data was kept"):
                m.cleanup_pg15()
        self.assertTrue(m.quarantine_path(self.state).is_dir())

    def test_fresh_removal_checks_the_renamed_inode_before_deleting(self):
        self.resumable()
        decoy = m.DATA / "decoy"
        decoy.mkdir()
        real = os.rename
        with patch.object(m.os, "rename", side_effect=lambda source, target: real(decoy, target)), \
             patch.object(m.shutil, "rmtree", side_effect=AssertionError("rmtree")):
            with self.assertRaisesRegex(ValueError, "does not match the removal journal; it was kept"):
                m.cleanup_pg15()
        self.assertTrue(m.quarantine_path(self.state).is_dir() and (m.OLD_DATA / "PG_VERSION").is_file())
        self.assertEqual(self.journal()["donor_removal"]["stage"], "quarantine")

    def test_resume_refuses_a_linked_recovery_json(self):
        self.resumable()
        self.interrupt_after_rename()
        target = self.sidecar.with_name("elsewhere.json")
        self.sidecar.replace(target)
        self.sidecar.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "recovery JSON is missing or linked"):
            m.cleanup_pg15()
        self.assertTrue(m.quarantine_path(self.state).is_dir())

    def test_a_changed_quarantine_is_never_removed(self):
        self.state["donor_removal"] = {"stage": "removing", "quarantine": str(m.quarantine_path(self.state)),
                                       "inode": [0, 0], "bytes": 1, "backup_sha256": "0", "backup_bytes": 1}
        m.OLD_DATA.rename(m.quarantine_path(self.state))
        self.save()
        with self.assertRaisesRegex(ValueError, "does not match the removal journal"):
            m.cleanup_pg15()
        self.assertTrue(m.quarantine_path(self.state).is_dir())

    def test_confirmed_preview_is_rechecked_before_any_change(self):
        self.resumable()
        stale = "The cleanup preview is stale"
        refuse = patch.object(m.os, "rename", side_effect=AssertionError("rename"))
        fresh = m.cleanup_preview()
        self.assertTrue(fresh[0])
        self.assertRegex(fresh[3], r"^[0-9a-f]{64}$")
        self.assertEqual(m.cleanup_preview(), fresh)
        # A journal written before an interrupted rename is only repaired after the token matches.
        journaled = dict(self.state, donor_removal={
            "stage": "quarantine", "quarantine": str(m.quarantine_path(self.state)),
            "inode": [0, 0], "bytes": 1, "backup_sha256": "0", "backup_bytes": 1})
        top = os.lstat(m.OLD_DATA)
        journaled["donor_removal"]["inode"] = [top.st_dev, top.st_ino]
        m.STATE.write_text(json.dumps(journaled))
        with refuse, self.assertRaisesRegex(ValueError, stale):
            m.cleanup_pg15("0" * 64)
        self.assertIn("donor_removal", self.journal())
        self.save()
        # The donor size and the recovery JSON identity are both bound without hashing the archive.
        extra = m.OLD_DATA / "base" / "1" / "2619"
        extra.write_bytes(b"x" * 8192)
        with patch.object(m, "Hashed", side_effect=AssertionError("hashed")):
            grown = m.cleanup_preview()
        self.assertNotEqual((grown[1], grown[3]), (fresh[1], fresh[3]))
        with refuse, self.assertRaisesRegex(ValueError, stale):
            m.cleanup_pg15(fresh[3])
        extra.unlink()
        self.assertEqual(m.cleanup_preview(), fresh)
        self.sidecar.write_text(self.sidecar.read_text() + " ")
        with refuse, self.assertRaisesRegex(ValueError, stale):
            m.cleanup_pg15(fresh[3])
        shown = m.cleanup_preview()[3]
        # A removal that became resumable since the preview needs a new one, and the journal stays as it was.
        self.interrupt_after_rename()
        before = m.STATE.read_bytes()
        with patch.object(m.shutil, "rmtree", side_effect=AssertionError("rmtree")),              self.assertRaisesRegex(ValueError, stale):
            m.cleanup_pg15(shown)
        self.assertEqual(m.STATE.read_bytes(), before)
        resumed = m.cleanup_preview()
        self.assertTrue(resumed[0])
        self.assertEqual(m.cleanup_pg15(resumed[3]), resumed[1])
        # Completed: a confirmed request is refused as stale, while the unconfirmed CLI form still returns 0.
        with self.assertRaisesRegex(ValueError, stale + r" \(The PG15 data directory was already removed\)"):
            m.cleanup_pg15(resumed[3])
        self.assertEqual(m.cleanup_pg15(), 0)

    def test_confirm_preview_only_takes_a_lowercase_token_for_cleanup(self):
        script = str(Path(__file__).resolve().parents[1] / "bin" / "ddistro_pg17_maintenance")
        for args in (["cleanup-pg15", "--confirm-preview", "A" * 64], ["cleanup-pg15", "--confirm-preview", "a" * 63],
                     ["cleanup-preview", "--confirm-preview", "a" * 64]):
            run = subprocess.run([sys.executable, script, *args], capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, args)
            self.assertIn("--confirm-preview", run.stderr)

    def test_rollback_refuses_before_touching_clusters_without_a_donor(self):
        untouched = {name: patch.object(m, name, side_effect=AssertionError(name))
                     for name in ("running", "cluster", "fingerprint", "atomic", "cluster_id")}
        for patcher in untouched.values():
            patcher.start()
            self.addCleanup(patcher.stop)
        (m.OLD_DATA / "PG_VERSION").unlink()
        with self.assertRaisesRegex(ValueError, "PG15 data directory is missing"):
            m.rollback()
        self.state["donor_removal"] = {"stage": "removed", "quarantine": str(m.quarantine_path(self.state)),
                                       "inode": [0, 0], "bytes": 1, "backup_sha256": "0", "backup_bytes": 1}
        self.save()
        with self.assertRaisesRegex(ValueError, "PostgreSQL 17 keeps serving"):
            m.rollback()


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


if __name__ == "__main__":
    unittest.main()
