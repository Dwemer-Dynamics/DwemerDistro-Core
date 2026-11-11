"""Focused PG17 retry, rollback-guard and dump-hash regressions. No live distro, cluster or SQL."""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
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
        self.journal(phase="publishing", candidate_id="17id", baseline="base", baseline_format=2,
                     backup=str(m.DATA / "backup.tar"), seal=["17id", "0/1000", "shut down"])
        with patch.object(m, "cluster_id", return_value="17id"), patch.object(m, "running", return_value=False), \
             patch.object(m, "atomic"), patch.object(m, "restore_old") as restore, \
             patch.object(m, "fingerprint", return_value=("base", "raw")) as fingerprint:
            with patch.object(m, "seal", return_value=["17id", "0/1000", "shut down"]):
                m.rollback()
            self.assertEqual((restore.call_count, fingerprint.call_count), (1, 0))
            with patch.object(m, "seal", return_value=["17id", "0/2000", "shut down"]):
                m.rollback()
            self.assertEqual((restore.call_count, fingerprint.call_count), (2, 1))


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
        return m.verify_archive(self.state, m.donor_tree(m.OLD_DATA), m.config_tree())

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
        # Stub only the live-cluster and Windows mount checks; hashing and quarantine checks stay real.
        checks = {
            "check_removal": patch.object(m, "check_removal",
                                          side_effect=lambda state: (m.donor_tree(m.OLD_DATA), m.config_tree())),
            "check_pg17": patch.object(m, "check_pg17"),
            "check_backup": patch.object(m, "check_backup"),
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
        self.assertEqual(mocks["check_removal"].call_count, 2)
        self.assertFalse(m.OLD_DATA.exists())
        self.assertTrue(quarantine.is_dir())
        mocks["check_pg17"].reset_mock()
        mocks["check_backup"].reset_mock()
        eligible, left, reason = m.cleanup_preview()
        self.assertEqual((eligible, left), (True, whole - deleted))
        self.assertIn("interrupted", reason)
        self.assertEqual(m.cleanup_pg15(), whole - deleted)
        self.assertEqual(mocks["check_removal"].call_count, 2)
        self.assertEqual((mocks["check_pg17"].call_count, mocks["check_backup"].call_count), (2, 2))
        self.assertEqual(mocks["check_backup"].call_args.args[1]["backup_sha256"], record["backup_sha256"])
        self.assertFalse(quarantine.exists())
        self.assertEqual(self.journal()["donor_removal"]["stage"], "removed")
        self.assertEqual(m.cleanup_pg15(), 0)
        self.assertEqual(m.cleanup_preview(), (False, 0, "The PG15 data directory was already removed"))
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

    def test_a_changed_quarantine_is_never_removed(self):
        self.state["donor_removal"] = {"stage": "removing", "quarantine": str(m.quarantine_path(self.state)),
                                       "inode": [0, 0], "bytes": 1, "backup_sha256": "0", "backup_bytes": 1}
        m.OLD_DATA.rename(m.quarantine_path(self.state))
        self.save()
        with self.assertRaisesRegex(ValueError, "does not match the removal journal"):
            m.cleanup_pg15()
        self.assertTrue(m.quarantine_path(self.state).is_dir())

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
