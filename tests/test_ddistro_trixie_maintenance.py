"""Focused Debian 13 upgrade regressions: sources, solver plans, venvs, PHP settings, journal and fence.
No live distro, APT, dpkg, PostgreSQL or network."""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch


def load(name):
    path = str(Path(__file__).resolve().parents[1] / "bin" / name)
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("ddistro_trixie_maintenance")

SIMULATION = """\
Inst libssl3t64 (3.5.1-1+deb13u1 Debian:13.2/stable, Debian-Security:13/stable-security [amd64])
Inst postgresql-17 [17.6-1.pgdg120+1] (17.7-1.pgdg130+1 apt.postgresql.org:trixie-pgdg [amd64])
Inst php8.4-cli (8.4.27-1~deb13u1 Debian-Security:13/stable-security [amd64])
Remv libssl3 [3.0.17-1~deb12u2]
Remv php8.2-cli [8.2.29-1~deb12u1]
Conf libssl3t64 (3.5.1-1+deb13u1 Debian:13.2/stable [amd64])
"""


def elf(needed):
    """A minimal little-endian ELF64 with one PT_LOAD, one PT_DYNAMIC and the given DT_NEEDED names."""
    strtab = b"\0" + b"".join(name.encode() + b"\0" for name in needed)
    phoff, phnum = 64, 2
    dyn_off = phoff + phnum * 56
    entries, offset = [], 1
    for name in needed:
        entries.append((1, offset))
        offset += len(name) + 1
    str_off = dyn_off + (len(entries) + 2) * 16
    entries += [(5, str_off), (0, 0)]
    dynamic = b"".join(struct.pack("<qQ", tag, value) for tag, value in entries)
    size = str_off + len(strtab)
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, phoff, 0, 0, 64, 56,
                                                                       phnum, 0, 0, 0)
    load_ph = struct.pack("<IIQQQQQQ", 1, 4, 0, 0, 0, size, size, 4096)
    dyn_ph = struct.pack("<IIQQQQQQ", 2, 4, dyn_off, dyn_off, dyn_off, len(dynamic), len(dynamic), 8)
    return header + load_ph + dyn_ph + dynamic + strtab


PGDG_KEY = "/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc"
# The GitHub CLI entry and keyring exactly as the 3.4.5 installer leaves them: GitHub's published
# keyring holds its current signing key and the key it replaced.
GITHUB_KEY = "/etc/apt/keyrings/githubcli-archive-keyring.gpg"
GITHUB_SIGNING, GITHUB_RETIRED = m.AGNOSTIC_SOURCES["github-cli"]["fingerprints"]
GITHUB_STOCK = f"deb [arch=amd64 signed-by={GITHUB_KEY}] https://cli.github.com/packages stable main"
KEYRINGS = {PGDG_KEY: [m.PGDG_FINGERPRINT], GITHUB_KEY: [GITHUB_RETIRED, GITHUB_SIGNING],
            "/usr/share/keyrings/githubcli-archive-keyring.gpg": [GITHUB_SIGNING],
            "/etc/apt/keyrings/github-retired.gpg": [GITHUB_RETIRED],
            "/etc/apt/keyrings/github-extra.gpg": [GITHUB_SIGNING, GITHUB_RETIRED, "0" * 40],
            "/etc/apt/keyrings/pgdg-and-github.gpg": [m.PGDG_FINGERPRINT, GITHUB_SIGNING],
            "/usr/share/keyrings/other.gpg": ["0" * 40]}


def with_keyrings(test):
    """Signed-By keyrings resolve to their primary fingerprints without gpg or real files."""
    return patch.object(m, "keyring_fingerprints", side_effect=KEYRINGS.get)(test)


class Sources(unittest.TestCase):
    @with_keyrings
    def test_debian_and_pgdg_move_to_trixie_while_other_sources_stay_byte_for_byte(self, _keys):
        text = ("deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware\n"
                "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] http://security.debian.org/debian-security "
                "bookworm-security main\n"
                "# deb http://deb.debian.org/debian bookworm-backports main\n"
                f"deb [arch=amd64 signed-by={GITHUB_KEY}] https://cli.github.com/packages stable main\n"
                f"deb [signed-by={PGDG_KEY}] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main\n")
        rewritten = m.rewrite_suites(text, deb822=False).splitlines()
        self.assertEqual(rewritten[0], "deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware")
        self.assertEqual(rewritten[1], "deb [signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] "
                                       "http://security.debian.org/debian-security trixie-security main")
        self.assertEqual(rewritten[2:4], text.splitlines()[2:4])
        self.assertEqual(rewritten[4], f"deb [signed-by={PGDG_KEY}] http://apt.postgresql.org/pub/repos/apt trixie-pgdg main")
        kinds = [m.classify_source(e) for e in map(m.parse_one_line_source, text.splitlines()) if e]
        self.assertEqual(kinds, ["debian", "debian", "agnostic", "pgdg"])

    @with_keyrings
    def test_source_identity_is_the_exact_host_path_suites_and_signing_never_a_substring(self, _keys):
        def kind(line):
            return m.classify_source(m.parse_one_line_source(line))

        pgdg = f"deb [signed-by={PGDG_KEY}] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main"
        self.assertEqual(kind(pgdg), "pgdg")
        for variant in (pgdg.replace("apt.postgresql.org", "apt.postgresql.org.example.net"),
                        pgdg.replace("apt.postgresql.org/pub", "mirror.example/apt.postgresql.org/pub"),
                        pgdg.replace("https://", "https://user@"), pgdg.replace(".org/", ".org:8443/"),
                        pgdg.replace("/pub/repos/apt", "/pub/repos/apt-archive"),
                        pgdg.replace(PGDG_KEY, "/usr/share/keyrings/other.gpg"), pgdg.replace(PGDG_KEY, "/missing.gpg"),
                        pgdg.replace(f"[signed-by={PGDG_KEY}] ", ""), pgdg.replace("signed-by", "trusted=yes signed-by"),
                        pgdg.replace(" main", " main 17"), pgdg.replace("bookworm-pgdg", "bookworm-pgdg-testing")):
            self.assertEqual(kind(variant), "other", variant)
        # Debian's own hosts and keyring only; a /debian path elsewhere is a custom feed.
        for line in ("deb http://deb.debian.org/debian bookworm main", "deb https://ftp.de.debian.org/debian/ bookworm main",
                     "deb http://ftp.debian.org/debian bookworm-backports contrib",
                     "deb [arch=amd64 lang=none] http://deb.debian.org/debian bookworm-updates main",
                     "deb-src http://deb.debian.org/debian-security bookworm-security main"):
            self.assertEqual(kind(line), "debian", line)
        for line in ("deb https://mirror.example.net/debian bookworm main",
                     "deb https://deb.debian.org.example.net/debian bookworm main",
                     "deb http://mirrors.kernel.org/debian-security bookworm-security main",
                     "deb http://ftp.de.debian.org/debian-security bookworm-security main",
                     "deb http://deb.debian.org/debian bookworm-security main",
                     "deb http://deb.debian.org/debian-security bookworm main",
                     "deb [trusted=yes] http://deb.debian.org/debian bookworm main",
                     "deb [signed-by=/usr/share/keyrings/other.gpg] http://deb.debian.org/debian bookworm main",
                     "deb [arch=arm64] http://deb.debian.org/debian bookworm main",
                     "deb http://deb.debian.org/debian bookworm main extras",
                     "deb ftp://deb.debian.org/debian bookworm main", "deb mirror+file:/etc/apt/mirrors.txt bookworm main"):
            self.assertEqual(kind(line), "other", line)
        github = GITHUB_STOCK
        self.assertEqual(kind(github), "agnostic")
        # The single-key keyring repair_github_cli_keyring wrote before GitHub published both keys.
        self.assertEqual(kind(github.replace(GITHUB_KEY, "/usr/share/keyrings/githubcli-archive-keyring.gpg")), "agnostic")
        self.assertEqual(kind(github.replace("packages ", "packages/ ")), "agnostic")
        for variant in (github.replace("/packages", "/packagesx"), github.replace("/packages", "/packages/stable"),
                        github.replace("https://", "http://"), github.replace("cli.github.com", "cli.github.com.example.net"),
                        github.replace("cli.github.com", "mirror.example/cli.github.com"),
                        github.replace("https://", "https://user@"), github.replace(".com/", ".com:443/"),
                        github.replace("packages ", "packages?x=1 "), github.replace("deb ", "deb-src ", 1),
                        github.replace(" stable", " bookworm"), github.replace(" stable", " trixie"),
                        github.replace(" main", " main extra"), github.replace(" main", " contrib"),
                        github.replace("arch=amd64", "arch=arm64"), github.replace("arch=amd64", "arch=amd64 trusted=yes"),
                        github.replace("arch=amd64", "arch=amd64 allow-insecure=yes"), github.replace("arch=amd64 ", "lang=none "),
                        github.replace(f" signed-by={GITHUB_KEY}", ""), github.replace(GITHUB_KEY, "githubcli-archive-keyring.gpg"),
                        github.replace(GITHUB_KEY, f"{GITHUB_KEY},/usr/share/keyrings/other.gpg"),
                        github.replace(GITHUB_KEY, "/usr/share/keyrings/other.gpg"), github.replace(GITHUB_KEY, "/missing.gpg"),
                        github.replace(GITHUB_KEY, "/etc/apt/keyrings/github-retired.gpg"),
                        github.replace(GITHUB_KEY, "/etc/apt/keyrings/github-extra.gpg"),
                        github.replace(GITHUB_KEY, "/etc/apt/keyrings/pgdg-and-github.gpg"),
                        github.replace(GITHUB_KEY, PGDG_KEY)):
            self.assertEqual(kind(variant), "other", variant)
        # The deb822 form of the same entry; any extra field or option keeps it unknown.
        stanza = ("Types: deb\nURIs: https://cli.github.com/packages\nSuites: stable\nComponents: main\n"
                  f"Architectures: amd64\nSigned-By: {GITHUB_KEY}\n")
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(stanza)], ["agnostic"])
        for extra in ("Trusted: yes\n", "Allow-Insecure: yes\n", "X-Repolib-Name: gh\n"):
            self.assertEqual([m.classify_source(e) for e in m.parse_deb822(stanza + extra)], ["other"], extra)
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(stanza.replace("Types: deb", "Types: deb deb-src"))],
                         ["other"])
        # A deb822 stanza is one source: a second, unrecognised URI keeps the whole stanza unknown.
        single = "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: bookworm\nComponents: main\n"
        stanza = single.replace("/debian\n", "/debian https://mirror.example.net/debian\n")
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(stanza)], ["other"])
        self.assertEqual(m.rewrite_suites(stanza, deb822=True), stanza)
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(single)], ["debian"])
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(single + "X-Repolib-Name: custom\n")], ["other"])

    def test_deb822_suites_are_rewritten_only_in_debian_stanzas(self):
        text = ("Types: deb\nURIs: https://deb.debian.org/debian\nSuites: bookworm bookworm-updates\nComponents: main\n"
                "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n"
                "Types: deb\nURIs: https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/\n"
                "Suites: /\n")
        rewritten = m.rewrite_suites(text, deb822=True)
        self.assertIn("Suites: trixie trixie-updates\n", rewritten)
        self.assertTrue(rewritten.endswith("Suites: /\n"))
        kinds = [m.classify_source(entry) for entry in m.parse_deb822(text)]
        self.assertEqual(kinds, ["debian", "other"])

    # /etc/apt/sources.list as the official 3.4.5 installer leaves it on Bookworm.
    STOCK = ("deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware\n"
             "deb http://deb.debian.org/debian-security bookworm-security main contrib non-free non-free-firmware\n"
             "deb http://deb.debian.org/debian bookworm-updates main contrib non-free non-free-firmware\n"
             "deb http://deb.debian.org/debian testing main non-free contrib\n")

    def entries(self, text):
        found = [e for e in map(m.parse_one_line_source, text.splitlines()) if e]
        for entry in found:
            entry["kind"], entry["file"] = m.classify_source(entry), "/etc/apt/sources.list"
        return found

    def test_only_the_exact_stock_testing_line_is_recognised(self):
        self.assertEqual([e["kind"] for e in self.entries(self.STOCK)], ["debian", "debian", "debian", "stock-testing"])
        self.assertEqual(m.classify_source(m.parse_one_line_source(
            "deb   http://deb.debian.org/debian  testing main non-free contrib  # stock")), "stock-testing")
        for variant in ("deb http://deb.debian.org/debian testing main", "deb http://deb.debian.org/debian sid main",
                        "deb https://deb.debian.org/debian testing main non-free contrib",
                        "deb [trusted=yes] http://deb.debian.org/debian testing main non-free contrib",
                        "deb http://deb.debian.org/debian forky main non-free contrib",
                        "deb-src http://deb.debian.org/debian testing main non-free contrib"):
            self.assertEqual(m.classify_source(m.parse_one_line_source(variant)), "other", variant)
        deb822 = m.parse_deb822("Types: deb\nURIs: http://deb.debian.org/debian\nSuites: testing\n"
                                "Components: main non-free contrib\n")
        self.assertEqual(m.classify_source(deb822[0]), "other")
        self.assertEqual(m.debian_components(self.entries(self.STOCK)), ["main", "contrib", "non-free", "non-free-firmware"])

    def test_the_stock_testing_line_is_disabled_and_no_bookworm_or_rolling_suite_stays_active(self):
        entries = self.entries(self.STOCK)
        covered = m.main_archive_components(entries)
        self.assertEqual(covered, {"main", "contrib", "non-free", "non-free-firmware"})
        rewritten = m.rewrite_suites(self.STOCK, deb822=False, covered=covered)
        lines = rewritten.splitlines()
        self.assertEqual(lines[0], "deb http://deb.debian.org/debian trixie main contrib non-free non-free-firmware")
        self.assertTrue(lines[3].startswith("# Disabled by ddistro_trixie_maintenance"))
        self.assertTrue(lines[3].endswith(": deb http://deb.debian.org/debian testing main non-free contrib"))
        self.assertEqual(len(lines), 4)
        self.assertEqual(m.source_problems(self.entries(rewritten)), [])
        self.assertEqual([p["code"] for p in m.source_problems(entries)], ["apt-sources"] * 4)
        # Components only the testing line carried move to a stable Trixie line; reruns change nothing.
        text = "deb http://deb.debian.org/debian bookworm main\ndeb http://deb.debian.org/debian testing main non-free contrib"
        once = m.rewrite_suites(text, False, m.main_archive_components(self.entries(text)))
        self.assertTrue(once.endswith("\ndeb http://deb.debian.org/debian trixie non-free contrib"))
        self.assertEqual(m.rewrite_suites(once, False, m.main_archive_components(self.entries(once))), once)
        self.assertEqual(m.source_problems(self.entries(once)), [])

    def test_unknown_third_party_sources_are_classified_for_deferral(self):
        entry = m.parse_one_line_source("deb [arch=amd64] https://packages.microsoft.com/debian/12/prod bookworm main")
        self.assertEqual(m.classify_source(entry), "other")
        self.assertEqual(m.debian_components([{"kind": "debian", "components": ["non-free", "main"]}]), ["main", "non-free"])


class Preferences(unittest.TestCase):
    # /etc/apt/preferences.d/stable as the 3.4.5 installer writes it.
    STOCK = ("Package: *\nPin: release a=bookworm\nPin-Priority: 900\n\n"
             "Package: *\nPin: release a=testing\nPin-Priority: 300\n")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.parts = base / "preferences.d"
        self.parts.mkdir()
        for key, value in {"APT_PREFERENCES": (base / "preferences", self.parts),
                           "PREFERENCES_BACKUP": base / "backup"}.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sync = patch.object(m, "sync_dir")
        sync.start()
        self.addCleanup(sync.stop)

    def test_only_the_stock_pin_file_is_accepted_by_meaning(self):
        self.assertEqual(m.stock_preferences(), (None, []))
        stable = self.parts / "stable"
        for same in (self.STOCK, "# stock\npackage:   *\nPIN: release archive=Testing\nPin-Priority: 300\n\n\n"
                                 "Explanation: release first\nPackage: *\nPin: release a=bookworm\nPin-Priority:900\n"):
            stable.write_text(same)
            self.assertEqual(m.stock_preferences(), (stable, []), same)
        for other in (self.STOCK.replace("900", "1001"), self.STOCK.replace("a=testing", "a=unstable"),
                      self.STOCK.replace("Package: *", "Package: php*", 1), self.STOCK.replace("a=bookworm", "n=bookworm"),
                      self.STOCK + "\nPackage: *\nPin: release a=sid\nPin-Priority: 100\n",
                      self.STOCK.split("\n\n")[0] + "\n", self.STOCK.replace("Pin-Priority: 300", "Pin-Priority: 300\n x")):
            stable.write_text(other)
            self.assertEqual([p["code"] for p in m.stock_preferences()[1]], ["apt-preferences"], other)
        stable.write_text(self.STOCK)
        (self.parts / "extra.pref").write_text("")
        self.assertEqual(m.stock_preferences()[0], None)
        (self.parts / "extra.pref").unlink()
        m.APT_PREFERENCES[0].write_text("Package: *\nPin: release a=bookworm\nPin-Priority: 900\n")
        self.assertEqual(m.stock_preferences()[0], None)
        m.APT_PREFERENCES[0].write_text("\n")
        self.assertEqual(m.stock_preferences(), (stable, []))
        stable.rename(self.parts / "bookworm")
        self.assertEqual(m.stock_preferences()[0], None)

    def test_the_cutover_keeps_the_original_and_pins_only_trixie_release_suites(self):
        stable = self.parts / "stable"
        stable.write_text(self.STOCK)
        self.assertEqual([p["code"] for p in m.trixie_preference_problems()], ["apt-preferences"])
        self.assertEqual(m.persist_preferences({"preferences": False}), [])
        self.assertEqual(stable.read_text(), self.STOCK)
        with patch.object(m.os, "fchown", create=True), patch.object(m.os, "fchmod", create=True):
            self.assertEqual(m.persist_preferences({"preferences": True}), [str(stable)])
        self.assertEqual((m.PREFERENCES_BACKUP / "stable").read_text(), self.STOCK)
        self.assertEqual(stable.read_text(), m.TRIXIE_PREFERENCES)
        pins = m.TRIXIE_PREFERENCES.split("\n", 2)[2]
        self.assertNotIn("bookworm", pins)
        self.assertNotIn("testing", pins)
        for suite in ("n=trixie\n", "n=trixie-updates\n", "n=trixie-security\n", "origin apt.postgresql.org\n"):
            self.assertIn(suite, pins)
        self.assertEqual(m.trixie_preference_problems(), [])
        # A rerun changes nothing and keeps the first backup.
        self.assertEqual(m.persist_preferences({"preferences": True}), [str(stable)])
        self.assertEqual((m.PREFERENCES_BACKUP / "stable").read_text(), self.STOCK)
        stable.unlink()
        self.assertEqual(m.trixie_preference_problems(), [])

    def test_the_drift_guard_skips_the_apt_files_the_cutover_rewrites(self):
        conffiles = "/etc/apt/preferences.d/stable 0123\n/etc/apt/sources.list 4567\n"
        with patch.object(m, "output", return_value=conffiles), patch.object(m.os.path, "isfile", return_value=True), \
             patch.object(m, "PG_CONF", Path("/nonexistent")), patch.object(m, "PG17_STATE", Path("/nonexistent")), \
             patch.object(m, "WSL_CONF", Path("/nonexistent")), patch.object(m, "sha256_file") as digest, \
             patch.object(Path, "read_bytes", side_effect=AssertionError("read")), \
             patch.object(Path, "is_dir", return_value=False), patch.object(Path, "is_file", return_value=False):
            self.assertEqual(m.guarded_digests(), {})
            digest.assert_not_called()

    @staticmethod
    def dpkg(*packages):
        """CONFFILE_QUERY output: (package, version, [(path, md5, *flags)])."""
        return "".join(f"{name} {version}\n" + "".join(f" {' '.join(row)}\n" for row in rows) + "\n"
                       for name, version, rows in packages)

    @staticmethod
    def md5(text):
        return hashlib.md5(text.encode()).hexdigest()

    def test_unedited_vendor_conffiles_may_take_their_new_vendor_version_and_nothing_else_may_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            sudoers, sites, pg = root / "sudoers.d", root / "sites-available", root / "postgresql" / "15" / "main"
            for directory in (sudoers, sites, pg):
                directory.mkdir(parents=True)
            readme, ssl, default = sudoers / "README", sites / "default-ssl.conf", sites / "000-default.conf"
            policy, cluster, link = sudoers / "dwemer", pg / "postgresql.conf", sites / "alias.conf"
            for path, text in ((readme, "old readme\n"), (ssl, "old ssl\n"), (default, "edited site\n"),
                               (policy, "dwemer ALL=(postgres) NOPASSWD: /usr/bin/psql\n"), (cluster, "port = 5433\n")):
                path.write_text(text)
            link.symlink_to(default.name)
            before = self.dpkg(("sudo", "1.9.13p3-1+deb12u2", [(str(readme), self.md5("old readme\n"))]),
                               ("apache2", "2.4.62-1~deb12u2", [(str(ssl), self.md5("old ssl\n")),
                                                                (str(default), self.md5("stock site\n"))]))
            dpkg_text = [before]
            with patch.object(m, "output", side_effect=lambda *a, **k: dpkg_text[0]), \
                 patch.object(m, "GUARDED_ROOTS", (root / "postgresql", sudoers, sites)), \
                 patch.object(m, "PG17_STATE", Path("/nonexistent")), patch.object(m, "WSL_CONF", Path("/nonexistent")):
                guarded = {path: digest for path, digest in m.guarded_digests().items() if path.startswith(str(root))}
                self.assertEqual(guarded[str(readme)]["conffile"]["package"], "sudo")
                self.assertEqual(guarded[str(ssl)]["conffile"]["version"], "2.4.62-1~deb12u2")
                # Edited conffiles, Core's sudo policy, cluster files and symlinks keep exact digests.
                for path in (default, policy, cluster):
                    self.assertEqual(guarded[str(path)], m.sha256_file(path))
                self.assertTrue(guarded[str(link)].startswith("symlink:000-default.conf:"))
                self.assertEqual(m.guarded_changes(guarded), [])

                # Debian 13 replaces both unedited conffiles with its own version: accepted.
                readme.write_text("new readme\n")
                ssl.write_text("new ssl\n")
                after = self.dpkg(("sudo", "1.9.16p2-3", [(str(readme), self.md5("new readme\n"))]),
                                  ("apache2", "2.4.65-2", [(str(ssl), self.md5("new ssl\n")),
                                                           (str(default), self.md5("stock site 13\n"))]))
                dpkg_text[0] = after
                self.assertEqual(m.guarded_changes(guarded), [])

                def changed(text):
                    dpkg_text[0] = text
                    return m.guarded_changes(guarded)

                readme_row = (str(readme), self.md5("new readme\n"))
                ssl_rows = [(str(ssl), self.md5("new ssl\n"))]
                for case, text in (
                        ("same version", self.dpkg(("sudo", "1.9.13p3-1+deb12u2", [readme_row]), ("apache2", "2.4.65-2", ssl_rows))),
                        ("old md5 kept", self.dpkg(("sudo", "1.9.16p2-3", [(str(readme), self.md5("old readme\n"))]),
                                                   ("apache2", "2.4.65-2", ssl_rows))),
                        ("other package", self.dpkg(("sudo-ldap", "1.9.16p2-3", [readme_row]), ("apache2", "2.4.65-2", ssl_rows))),
                        ("two owners", self.dpkg(("sudo", "1.9.16p2-3", [readme_row]), ("sudo-ldap", "1.9.16p2-3", [readme_row]),
                                                 ("apache2", "2.4.65-2", ssl_rows))),
                        ("obsolete", self.dpkg(("sudo", "1.9.16p2-3", [readme_row + ("obsolete",)]), ("apache2", "2.4.65-2", ssl_rows))),
                        ("unknown flag", self.dpkg(("sudo", "1.9.16p2-3", [readme_row + ("later",)]), ("apache2", "2.4.65-2", ssl_rows))),
                        ("unconfigured", self.dpkg(("sudo", "1.9.16p2-3", [(str(readme), "newconffile")]), ("apache2", "2.4.65-2", ssl_rows))),
                        ("no record", self.dpkg(("apache2", "2.4.65-2", ssl_rows)))):
                    self.assertEqual(changed(text), [str(readme)], case)

                # A local edit after the upgrade, a missing or renamed file and a symlink in its place all fail.
                dpkg_text[0] = after
                readme.write_text("new readme, edited\n")
                self.assertEqual(m.guarded_changes(guarded), [str(readme)])
                readme.write_text("new readme\n")
                ssl.rename(sites / "default-ssl.conf.renamed")
                self.assertEqual(m.guarded_changes(guarded), [str(ssl)])
                (sites / "default-ssl.conf.renamed").rename(sites / "elsewhere.conf")
                ssl.symlink_to("elsewhere.conf")
                self.assertEqual(m.guarded_changes(guarded), [str(ssl)])
                ssl.unlink()
                (sites / "elsewhere.conf").rename(ssl)
                self.assertEqual(m.guarded_changes(guarded), [])

                # Exact digests: even a dpkg record matching the new content cannot excuse them.
                policy.write_text("dwemer ALL=(ALL) NOPASSWD: ALL\n")
                default.write_text("stock site 13\n")
                cluster.write_text("port = 5432\n")
                # The symlink to the edited site changes with it: links are compared by target and content.
                self.assertEqual(m.guarded_changes(guarded), sorted(map(str, (cluster, default, link, policy))))
                # A retargeted symlink, and recorded values of an unknown shape, count as changed.
                default.write_text("edited site\n")
                self.assertNotIn(str(link), m.guarded_changes(guarded))
                link.unlink()
                link.symlink_to(policy.name)
                self.assertIn(str(link), m.guarded_changes(guarded))
                self.assertEqual(m.guarded_changes({str(readme): {"other": 1}, str(ssl): 7}), sorted([str(readme), str(ssl)]))

    def test_only_a_single_unflagged_matching_conffile_reached_without_symlinks_counts_as_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            (root / "real").mkdir()
            path = root / "real" / "README"
            path.write_text("stock\n")
            (root / "via").symlink_to("real")
            good = (str(path), self.md5("stock\n"))
            self.assertEqual(m.stock_conffile(str(path), m.conffile_records(self.dpkg(("sudo", "1", [good]))))["md5"],
                             self.md5("stock\n"))
            for case, text in (("edited", self.dpkg(("sudo", "1", [(str(path), self.md5("vendor\n"))]))),
                               ("flagged", self.dpkg(("sudo", "1", [good + ("remove-on-upgrade",)]))),
                               ("two records", self.dpkg(("sudo", "1", [good]), ("other", "1", [good + ("obsolete",)]))),
                               ("bad md5", self.dpkg(("sudo", "1", [(str(path), "newconffile")]))),
                               ("no version", "sudo\n " + " ".join(good) + "\n"),
                               ("no package", " " + " ".join(good) + "\n")):
                self.assertIsNone(m.stock_conffile(str(path), m.conffile_records(text)), case)
            via = str(root / "via" / "README")
            self.assertIsNone(m.stock_conffile(via, m.conffile_records(self.dpkg(("sudo", "1", [(via, good[1])])))))


class Plan(unittest.TestCase):
    def setUp(self):
        compare = patch.object(m, "version_lt", side_effect=lambda a, b: a < b)
        compare.start()
        self.addCleanup(compare.stop)
        self.packages = {
            "postgresql-17": {"status": "ii", "version": "17.6-1.pgdg120+1", "source": "postgresql-17", "arch": "amd64"},
            "postgresql-15": {"status": "ii", "version": "15.14-0+deb12u1", "source": "postgresql-15", "arch": "amd64"},
            "php8.2-cli": {"status": "ii", "version": "8.2.29-1~deb12u1", "source": "php8.2", "arch": "amd64"},
        }
        self.policy = {"postgresql-15": {"target": True}, "postgresql-17": {"target": True}}

    def test_simulation_is_parsed_into_upgrades_installs_and_removals(self):
        plan = m.parse_plan(SIMULATION + "The following packages have been kept back:\n  foo bar\n")
        self.assertEqual(plan["upgrade"]["postgresql-17"]["from"], "17.6-1.pgdg120+1")
        self.assertEqual(sorted(plan["install"]), ["libssl3t64", "php8.4-cli"])
        self.assertEqual(sorted(plan["remove"]), ["libssl3", "php8.2-cli"])
        self.assertEqual(plan["kept_back"], ["foo", "bar"])

    def test_only_requested_php_and_automatic_packages_may_be_removed(self):
        plan = m.parse_plan(SIMULATION)
        php_map = {"php8.2-cli": "php8.4-cli"}
        self.assertEqual(m.validate_plan(plan, self.packages, set(), php_map, [], self.policy), [])
        problems = m.validate_plan(plan, self.packages, {"libssl3"}, php_map, [], self.policy)
        self.assertEqual([p["code"] for p in problems], ["apt-removes-manual"])
        plan["remove"]["postgresql-15"] = "15.14-0+deb12u1"
        problems = m.validate_plan(plan, self.packages, set(), php_map, [], self.policy)
        self.assertEqual([p["code"] for p in problems], ["apt-removes-protected"])

    def test_downgrades_kept_back_and_unrebuilt_postgresql_are_refused(self):
        plan = m.parse_plan(SIMULATION)
        plan["upgrade"]["postgresql-17"]["to"] = "17.5-1"
        plan["kept_back"] = ["nvidia-cuda-toolkit"]
        policy = {"postgresql-15": {"target": False}}
        codes = [p["code"] for p in m.validate_plan(plan, self.packages, set(), {"php8.2-cli": "php8.4-cli"}, [], policy)]
        self.assertEqual(codes, ["apt-downgrade", "apt-kept-back", "pg-not-rebuilt"])

    def test_venvs_need_their_interpreter_and_every_system_library_they_load(self):
        plan = m.parse_plan(SIMULATION)
        venvs = [{"path": "/home/dwemer/a/venv", "kind": "retain", "python": "3.11", "sonames": ["libssl.so.3", "libz.so.1"]},
                 {"path": "/opt/b", "kind": "unknown", "python": "3.9", "sonames": []}]
        owners = {"libssl.so.3": {"libssl3"}, "libz.so.1": {"zlib1g"}}
        codes = [p["code"] for p in m.validate_plan(plan, self.packages, set(), {"php8.2-cli": "php8.4-cli"}, venvs,
                                                    self.policy, owners, set())]
        self.assertEqual(codes, ["venv-unknown", "venv-library"])
        # The t64 replacement ships the same soname, so the library survives.
        codes = [p["code"] for p in m.validate_plan(plan, self.packages, set(), {"php8.2-cli": "php8.4-cli"}, venvs,
                                                    self.policy, owners, {"libssl.so.3"})]
        self.assertEqual(codes, ["venv-unknown"])
        plan["remove"]["python3.11"] = ""
        codes = [p["code"] for p in m.validate_plan(plan, self.packages, set(), {}, venvs[:1], self.policy)]
        self.assertIn("venv-interpreter", codes)

    def test_packages_from_testing_that_are_newer_than_trixie_defer(self):
        text = ("libfoo1:\n  Installed: 2.0-1\n  Candidate: 2.0-1\n  Version table:\n"
                " *** 2.0-1 100\n        100 /var/lib/dpkg/status\n"
                "     1.9-2 500\n        500 https://deb.debian.org/debian trixie/main amd64 Packages\n"
                "python3.9:\n  Installed: 3.9.2-1\n  Candidate: 3.9.2-1\n  Version table:\n"
                " *** 3.9.2-1 100\n        100 /var/lib/dpkg/status\n")
        policy = m.parse_policy(text)
        self.assertEqual((m.ahead_packages(policy), m.obsolete_packages(policy)), (["libfoo1"], ["python3.9"]))
        codes = [p["code"] for p in m.validate_plan(m.parse_plan(""), {}, set(), {}, [], policy)]
        self.assertEqual(codes, ["apt-ahead-of-trixie"])

    def test_renames_need_signed_package_evidence_and_dropped_packages_need_the_trixie_lists(self):
        plan = m.parse_plan(SIMULATION + "Remv python3-distutils [3.11.2-3]\nRemv libzip4 [1.7.3-1+b1]\n"
                            "Inst libzip5 (1.11.3-2 Debian:13.2/stable [amd64])\n")
        root = "/usr/lib/x86_64-linux-gnu/"
        files = {"libssl3t64": [root + "libssl.so.3", root + "libcrypto.so.3"], "libzip5": [root + "libzip.so.5"],
                 "php8.4-cli": ["/usr/bin/php8.4"]}
        relations = {"libssl3t64": m.parse_relations("Replaces: libssl3\nProvides: libssl3 (= 3.5.1-1+deb13u1)\n"),
                     "libzip5": m.parse_relations("Replaces: libzip4 (<< 1.8)\n")}
        old_files = {"libssl3": [root + "libssl.so.3", root + "libcrypto.so.3"], "libzip4": [root + "libzip.so.4"]}
        transitions = m.find_transitions(plan, files, relations, old_files)
        self.assertEqual(transitions, {"libssl3": "libssl3t64"})
        # Without Provides, the successor must ship every soname of the old package.
        relations["libssl3t64"] = m.parse_relations("Replaces: libssl3,\n libfoo\n")
        self.assertEqual(m.find_transitions(plan, files, relations, old_files), {"libssl3": "libssl3t64"})
        files["libssl3t64"] = [root + "libssl.so.3"]
        self.assertEqual(m.find_transitions(plan, files, relations, old_files), {})
        manual = {"libssl3", "python3-distutils", "libzip4"}
        policy = dict(self.policy, **{"python3-distutils": {"installed": "3.11.2-3", "target": False, "in_target": False}})
        php_map = {"php8.2-cli": "php8.4-cli"}
        problems = m.validate_plan(plan, self.packages, manual, php_map, [], policy, transitions=transitions)
        self.assertEqual([p["detail"] for p in problems], ["The solution removes the manually installed package libzip4."])
        self.assertEqual(m.dropped_packages(plan, manual, policy), ["python3-distutils"])
        # A dropped name still present in Trixie, or a removal with no evidence, defers.
        policy["python3-distutils"].update(in_target=True, target=True)
        codes = [p["code"] for p in m.validate_plan(plan, self.packages, manual, php_map, [], policy)]
        self.assertEqual(codes, ["apt-removes-manual"] * 3)

    def test_stock_cuda_dev_header_and_venv_removals_need_bounded_evidence(self):
        cuda = {"status": "ii", "version": "11.8.89~11.8.0-5~deb12u1", "source": "nvidia-cuda-toolkit", "arch": "amd64"}
        packages = dict(self.packages, **{"libcupti11.8": cuda, "libcuinj64-11.8": cuda, "libaccinj64-11.8": cuda,
                                          "libcupti-dev": cuda})
        plan = m.parse_plan(SIMULATION + "Remv libcupti11.8 [11.8.89~11.8.0-5~deb12u1]\n"
                            "Remv libcuinj64-11.8 [11.8.89~11.8.0-5~deb12u1]\nRemv libaccinj64-11.8 [11.8.89~11.8.0-5~deb12u1]\n"
                            "Remv libtinfo-dev [6.4-4]\nRemv python3.11-venv [3.11.2-6+deb12u5]\n"
                            "Inst libcupti12 (12.4.127~12.4.1-2 Debian:13.2/stable [amd64])\n"
                            "Inst libcuinj64-12.4 (12.4.127~12.4.1-2 Debian:13.2/stable [amd64])\n"
                            "Inst libcupti-dev [11.8.89~11.8.0-5~deb12u1] (12.4.127~12.4.1-2 Debian:13.2/stable [amd64])\n"
                            "Inst libncurses-dev [6.4-4] (6.5+20250216-2 Debian:13.2/stable [amd64])\n")
        manual = {"libcupti11.8", "libcuinj64-11.8", "libaccinj64-11.8", "libtinfo-dev", "python3.11-venv"}
        gone = {"installed": "x", "target": False, "in_target": False}
        policy = dict(self.policy, **{name: dict(gone) for name in manual})
        # Same library, newer series, from Debian; libaccinj64 has no successor in this plan.
        self.assertEqual(m.superseded_cuda(plan, packages, manual, policy),
                         {"libcupti11.8": "libcupti12", "libcuinj64-11.8": "libcuinj64-12.4"})
        self.assertEqual(m.superseded_cuda(plan, dict(packages, **{"libcupti11.8": dict(cuda, source="cuda")}), manual,
                                           policy), {"libcuinj64-11.8": "libcuinj64-12.4"})
        self.assertEqual(m.superseded_cuda(plan, packages, manual, dict(policy, **{"libcupti11.8": {"in_target": True},
                                                                                   "libcuinj64-11.8": {}})), {})
        # An upgraded package that declares Replaces and Provides takes over a dev header name.
        relations = {"libncurses-dev": m.parse_relations("Replaces: libtinfo-dev\nProvides: libtinfo-dev (= 6.5)\n")}
        self.assertEqual(m.find_transitions(plan, {}, relations, {}), {"libtinfo-dev": "libncurses-dev"})
        self.assertEqual(m.find_transitions(plan, {}, {}, {}), {})
        # The venv package goes only while its interpreter stays.
        self.assertEqual(m.dropped_packages(plan, manual, policy), ["python3.11-venv"])
        transitions = dict(m.find_transitions(plan, {}, relations, {}), **m.superseded_cuda(plan, packages, manual, policy))
        problems = m.validate_plan(plan, packages, manual, {"php8.2-cli": "php8.4-cli"}, [], policy, transitions=transitions)
        self.assertEqual([p["detail"] for p in problems],
                         ["The solution removes the manually installed package libaccinj64-11.8."])
        plan["remove"]["python3.11"] = ""
        self.assertEqual(m.dropped_packages(plan, manual, policy), [])

    def test_unpackaged_users_of_replaced_libraries_are_found_within_a_bound(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "bin").mkdir()
            Path(root, "bin", "profiler").write_bytes(elf(["libcupti.so.11.8", "libc.so.6"]))
            Path(root, "bin", "tool").write_bytes(elf(["libc.so.6"]))
            Path(root, "notes.txt").write_text("libcupti.so.11.8")
            self.assertEqual(m.unpackaged_users({"libcupti.so.11.8"}, (root,)), ([str(Path(root, "bin", "profiler"))], True))
            self.assertEqual(m.unpackaged_users({"libcuinj64.so.11.8"}, (root,)), ([], True))
            self.assertEqual(m.unpackaged_users({"libcuinj64.so.11.8"}, (root,), limit=2), ([], False))

    def test_a_relay_replaces_a_new_mail_daemon_and_kept_back_cuda_is_requested_once(self):
        cuda = {"status": "ii", "version": "11.8", "source": "nvidia-cuda-toolkit", "arch": "amd64"}
        packages = dict(self.packages, **{"libcupti-dev": cuda})
        plan = {"install": {"exim4-daemon-light": {}, "exim4-base": {}}, "kept_back": []}
        self.assertEqual(m.adjust_request(["php8.4-cli", "php8.2-cli-"], plan, packages, False), ["esmtp-run"])
        self.assertEqual(m.adjust_request(["php8.4-cli", "esmtp-run"], plan, packages, False), [])
        self.assertEqual(m.adjust_request([], plan, packages, True), [])
        # msmtp-mta ships msmtpd.service, so APT choosing it is a new server too.
        self.assertEqual(m.adjust_request([], {"install": {"msmtp-mta": {}}, "kept_back": []}, packages, False), ["esmtp-run"])
        plan = {"install": {"esmtp-run": {}}, "kept_back": ["libcupti-dev", "php8.2-cli"]}
        self.assertEqual(m.adjust_request(["esmtp-run"], plan, packages, False), ["libcupti-dev"])
        self.assertEqual(m.adjust_request(["esmtp-run", "libcupti-dev"], plan, packages, False), [])
        status = "ii \tgpg-wks-server\tgpg-wks\nrc \tpostfix\tmail-transport-agent\nii \tnullmailer\tlib, mail-transport-agent (= 1)\n"
        self.assertTrue(m.has_mta(status))
        self.assertFalse(m.has_mta(status.rsplit("ii", 1)[0]))

    def test_each_removed_manual_cuda_profiling_library_gets_its_one_trixie_successor(self):
        cuda = {"status": "ii", "version": "11.8.89~11.8.0-5~deb12u1", "source": "nvidia-cuda-toolkit", "arch": "amd64"}
        packages = dict(self.packages, **{"libcupti11.8": cuda, "libcuinj64-11.8": cuda, "libaccinj64-11.8": cuda})
        plan = m.parse_plan("Remv libcupti11.8 [1]\nRemv libcuinj64-11.8 [1]\nRemv libaccinj64-11.8 [1]\n"
                            "Inst libcupti12 (12.4.127~12.4.1-2 Debian:13.2/stable [amd64])\n")
        manual = {"libcupti11.8", "libcuinj64-11.8", "libaccinj64-11.8"}
        shown = ("Package: libaccinj64-12.4\nSource: nvidia-cuda-toolkit (12.4.1-2)\nVersion: 12.4.127~12.4.1-2\n\n"
                 "Package: libcuinj64-12.4\nSource: nvidia-cuda-toolkit\n\nPackage: libcupti12\nSource: nvidia-cuda-toolkit\n\n"
                 "Package: libcupti11.8\nSource: nvidia-cuda-toolkit\n\nPackage: libcuinj64-13.0\n")
        available = m.parse_package_sources(shown)
        self.assertEqual(available["libcuinj64-13.0"], "libcuinj64-13.0")
        # libcupti11.8 already has libcupti12; the series-13 name is not from nvidia-cuda-toolkit.
        self.assertEqual(m.adjust_request(["esmtp-run"], plan, packages, True, manual, available),
                         ["libaccinj64-12.4", "libcuinj64-12.4"])
        self.assertEqual(m.adjust_request(["libaccinj64-12.4", "libcuinj64-12.4"], plan, packages, True, manual, available), [])
        # Only installed, manually installed stock packages; an ambiguous choice adds nothing.
        self.assertEqual(m.cuda_successors(plan, packages, {"libcupti11.8"}, available), [])
        self.assertEqual(m.cuda_successors(plan, dict(packages, **{"libaccinj64-11.8": dict(cuda, status="rc")}),
                                           {"libaccinj64-11.8"}, available), [])
        self.assertEqual(m.cuda_successors(plan, packages, {"libaccinj64-11.8"},
                                           dict(available, **{"libaccinj64-12.6": "nvidia-cuda-toolkit"})), [])

    def test_doc_only_libtinfo_dev_is_taken_over_by_libncurses_dev_providing_it(self):
        plan = m.parse_plan("Remv libtinfo-dev [6.4-4]\nInst libncurses-dev [6.4-4] (6.5+20250216-2 Debian:13.2/stable [amd64])\n")
        relations = {"libncurses-dev": m.parse_relations("Replaces: ncurses-dev\nProvides: libtinfo-dev, ncurses-dev\n")}
        doc = ["/.", "/usr", "/usr/share", "/usr/share/doc", "/usr/share/doc/libtinfo-dev",
               "/usr/share/doc/libtinfo-dev/copyright"]
        self.assertEqual(m.find_transitions(plan, {}, relations, {"libtinfo-dev": doc}), {"libtinfo-dev": "libncurses-dev"})
        # Any payload outside /usr/share/doc, no payload record, or no Provides: still no evidence.
        for old_files in ({"libtinfo-dev": doc + ["/usr/lib/x86_64-linux-gnu/libtinfo.so"]}, {}):
            self.assertEqual(m.find_transitions(plan, {}, relations, old_files), {})
        relations = {"libncurses-dev": m.parse_relations("Replaces: ncurses-dev\nProvides: ncurses-dev\n")}
        self.assertEqual(m.find_transitions(plan, {}, relations, {"libtinfo-dev": doc}), {})
        # The exception is for that pair only.
        plan = m.parse_plan("Remv libfoo-dev [1]\nInst libncurses-dev [6.4-4] (6.5 Debian:13.2/stable [amd64])\n")
        relations = {"libncurses-dev": m.parse_relations("Provides: libfoo-dev\n")}
        self.assertEqual(m.find_transitions(plan, {}, relations, {"libfoo-dev": ["/usr/share/doc/libfoo-dev/x"]}), {})

    def test_packages_come_only_from_debian_or_pinned_pgdg(self):
        plan = m.parse_plan(SIMULATION + "Inst pgdg-only (1.0 apt.postgresql.org:trixie-pgdg [amd64])\n"
                            "Inst libfoo [1] (2 Debian-Security:13/stable-security [amd64])\n"
                            "Inst bar (1 PostgreSQL for Debian/Ubuntu repository:trixie-pgdg [amd64])\n"
                            "Inst baz (1 Other:1/stable [amd64])\n")
        self.assertEqual([p["detail"].split()[0] for p in m.origin_problems(plan)], ["bar", "baz", "pgdg-only"])
        self.assertTrue(m.debian_origin("Debian:13.2/stable, Debian-Security:13/stable-security"))

    def test_php_security_check_never_takes_the_lock_this_helper_holds(self):
        text = (Path(__file__).resolve().parents[1] / "bin" / "ensure_php_security").read_text()
        check = text.index('if [ "$MODE" = check ]; then')
        self.assertLess(check, text.index('flock -w 900 9'))
        self.assertIn("    finish\nfi\n", text[check:text.index('flock -w 900 9')])
        self.assertIn("    exit 0\n}", text[text.index("finish() {"):text.index("config_manifest() {")])

    def test_only_php_counterparts_may_bring_a_new_service(self):
        plan = {"install": {"php8.4-fpm": {}, "newd": {}}}
        files = {"php8.4-fpm": ["/etc/init.d/php8.4-fpm"], "newd": ["/lib/systemd/system/newd.service"]}
        problems = m.new_service_problems(plan, files, {"php8.2-fpm": "php8.4-fpm"})
        self.assertEqual([p["detail"].split()[4] for p in problems], ["newd,"])
        self.assertEqual(m.post_upgrade_service("php8.2-fpm"), "php8.4-fpm")

    def test_policy_marks_packages_without_a_trixie_candidate_as_obsolete(self):
        text = ("libicu72:\n  Installed: 72.1-3\n  Candidate: 72.1-3\n  Version table:\n"
                " *** 72.1-3 100\n        100 /var/lib/dpkg/status\n"
                "postgresql-15:\n  Installed: 15.14-0+deb12u1\n  Candidate: 15.15-1.pgdg130+1\n  Version table:\n"
                "     15.15-1.pgdg130+1 600\n        600 https://apt.postgresql.org/pub/repos/apt trixie-pgdg/main amd64 Packages\n"
                " *** 15.14-0+deb12u1 100\n        100 /var/lib/dpkg/status\n")
        policy = m.parse_policy(text)
        self.assertTrue(policy["postgresql-15"]["target"])
        self.assertEqual(m.obsolete_packages(policy), ["libicu72"])

    def test_php_counterparts_are_requested_in_the_same_transaction(self):
        with patch.object(m, "REIGN", Path(tempfile.gettempdir())):
            args = m.requested_changes({"php8.2-cli": "php8.4-cli", "libapache2-mod-php8.2": "libapache2-mod-php8.4"}, {})
        self.assertEqual(args, ["libapache2-mod-php8.4", "php8.4-cli", "libicu76", "libapache2-mod-php8.2-", "php8.2-cli-"])

    def test_deb_listings_keep_link_paths_and_find_new_services(self):
        listing = ("drwxr-xr-x root/root         0 2025-08-01 10:00 ./etc/init.d/\n"
                   "-rwxr-xr-x root/root      1024 2025-08-01 10:00 ./etc/init.d/newd\n"
                   "lrwxrwxrwx root/root         0 2025-08-01 10:00 ./usr/lib/x86_64-linux-gnu/libz.so.1 -> libz.so.1.3\n")
        paths = m.parse_deb_listing(listing)
        self.assertEqual(paths, ["/etc/init.d/", "/etc/init.d/newd", "/usr/lib/x86_64-linux-gnu/libz.so.1"])
        self.assertEqual(m.service_files(paths), ["/etc/init.d/", "/etc/init.d/newd"])

    def test_wsl_boot_may_only_run_the_fresh_image_initializer(self):
        self.assertEqual(m.wsl_boot_problems("[boot]\ncommand = /usr/local/bin/initialize_fresh_distro\n"), [])
        self.assertEqual(m.wsl_boot_problems("[network]\nsystemd=true\n"), [])
        codes = [p["code"] for p in m.wsl_boot_problems("[boot]\nsystemd = true\ncommand=service postgresql start\n")]
        self.assertEqual(codes, ["wsl-boot", "wsl-boot"])
        self.assertEqual(m.wsl_boot_problems('[boot]\nsystemd=false\ncommand="/usr/local/bin/initialize_fresh_distro"\n'), [])
        for text in ('[boot]\nsystemd="true"\n', "[Boot]\nsystemd=TRUE\n", "[boot]\nsystemd = yes\n",
                     "[boot]\ncommand = '/usr/local/bin/initialize_fresh_distro; service apache2 start'\n"):
            self.assertEqual([p["code"] for p in m.wsl_boot_problems(text)], ["wsl-boot"], text)

    def test_space_is_read_from_the_simulation_summary(self):
        text = "Need to get 812 MB of archives.\nAfter this operation, 1,204 MB of additional disk space will be used.\n"
        self.assertEqual(m.plan_space(text), (812 * 1000 ** 2, 1204 * 1000 ** 2))
        cached = "Need to get 12.5 MB/812 MB of archives.\nAfter this operation, 1,204 MB of additional disk space will be used.\n"
        self.assertEqual(m.plan_space(cached), (12_500_000, 1204 * 1000 ** 2))
        self.assertEqual(m.plan_space(cached, total=True), (812 * 1000 ** 2, 1204 * 1000 ** 2))
        self.assertEqual(m.upgrade_bytes(cached), (812 + 1204) * 1000 ** 2 + m.SPACE_MARGIN)
        freed = "Need to get 0 B/3 MB of archives.\nAfter this operation, 40 MB disk space will be freed.\n"
        self.assertEqual(m.upgrade_bytes(freed), 3 * 1000 ** 2 + m.SPACE_MARGIN)
        self.assertGreater(m.upgrade_bytes(""), 0)

    @unittest.skipUnless(hasattr(m.os, "statvfs"), "statvfs is POSIX only")
    def test_used_bytes_counts_every_inode_once_and_sparse_files_in_full(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "a").write_bytes(b"x" * 1000)
            with open(Path(temp, "sparse"), "wb") as stream:
                stream.truncate(50 * 1024 ** 2)
            m.os.link(Path(temp, "a"), Path(temp, "a-link"))
            info = m.os.statvfs(temp)
            used = m.rootfs_used_bytes(temp)
            self.assertGreaterEqual(used, 1024 + 50 * 1024 ** 2)
            self.assertGreaterEqual(used, (info.f_blocks - info.f_bfree) * info.f_frsize)

    def test_instance_id_is_opaque_stable_and_absent_without_an_own_machine_id(self):
        with tempfile.TemporaryDirectory() as temp:
            machine, fresh = Path(temp, "machine-id"), Path(temp, "fresh-image")
            self.assertIsNone(m.instance_id(machine, fresh))
            machine.write_text("0123456789abcdef0123456789abcdef\n")
            first = m.instance_id(machine, fresh)
            self.assertRegex(first, r"^ddi1-[0-9a-f]{40}$")
            self.assertNotIn("0123456789abcdef", first)
            self.assertEqual(m.instance_id(machine, fresh), first)
            machine.write_text("fedcba9876543210fedcba9876543210\n")
            self.assertNotEqual(m.instance_id(machine, fresh), first)
            fresh.write_text("")
            self.assertIsNone(m.instance_id(machine, fresh))
            fresh.unlink()
            for value in ("", "uninitialized\n", "0" * 32 + "\n"):
                machine.write_text(value)
                self.assertIsNone(m.instance_id(machine, fresh), value)


WSL_RELEASE = "6.18.58.1-microsoft-standard-WSL2"
NVIDIA = {"status": "ii", "version": "535.216.01-1~deb12u1", "source": "nvidia-graphics-drivers", "arch": "amd64"}
MODULE_UPGRADE = ("Inst libcuda1 [535.216.01-1~deb12u1] (550.163.01-2 Debian:13.2/stable [amd64])\n"
                  "Inst nvidia-smi [535.216.01-1~deb12u1] (550.163.01-2 Debian:13.2/stable [amd64])\n"
                  "Inst nvidia-kernel-dkms [535.216.01-1~deb12u1] (550.163.01-2 Debian:13.2/stable [amd64])\n")
# The maintainer script dh_dkms 3.2.0 generated for nvidia-kernel-dkms 550.163.01-2, as shipped.
POSTINST = ("#!/bin/sh\nset -e\n# Automatically added by dh_dkms/3.2.0-1\nDKMS_NAME=nvidia-current\n"
            "DKMS_PACKAGE_NAME=$DKMS_NAME-dkms\nDKMS_VERSION=550.163.01\n\npostinst_found=0\n")
LISTING = ("drwxr-xr-x root/root         0 2025-04-18 10:00 ./usr/src/nvidia-current-550.163.01/\n"
           "-rw-r--r-- root/root      1092 2025-04-18 10:00 ./usr/src/nvidia-current-550.163.01/dkms.conf\n")
OVERRIDE = {"module": "nvidia-current", "version": "550.163.01", "kernel": WSL_RELEASE}


def deb_output(postinst=POSTINST, listing=LISTING):
    """m.output for dpkg-deb on the downloaded module package."""
    return lambda *args, **kwargs: postinst if "--info" in args else listing if "-c" in args else ""


class NvidiaModule(unittest.TestCase):
    """The stock NVIDIA DKMS module on a WSL kernel: its Trixie postinst builds for every kernel with
    headers, and the WSL kernel build fails after the recovery copy. The package, its manual mark and
    the modules for every other kernel stay; one DKMS override skips the WSL kernel."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        paths = {"KERNEL_RELEASE": base / "osrelease", "WSL_LIB": base / "wsl" / "lib", "DKMS_CONF": base / "etc" / "dkms",
                 "APT_DIR": base / "apt"}
        for key, value in paths.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.KERNEL_RELEASE.write_text(WSL_RELEASE + "\n")
        m.WSL_LIB.mkdir(parents=True)
        m.DKMS_CONF.mkdir(parents=True)
        (m.APT_DIR / "archives").mkdir(parents=True)
        (m.APT_DIR / "archives" / "nvidia-kernel-dkms_550.163.01-2_amd64.deb").write_bytes(b"deb")
        self.path = m.DKMS_CONF / f"nvidia-current-550.163.01-{WSL_RELEASE}.conf"
        self.packages = {"nvidia-kernel-dkms": dict(NVIDIA), "libcuda1": dict(NVIDIA), "nvidia-smi": dict(NVIDIA),
                         "dkms": dict(NVIDIA, version="3.0.10-8+deb12u1", source="dkms")}

    def test_the_override_names_exactly_one_module_version_and_kernel(self):
        path, text = m.dkms_override(**OVERRIDE)
        self.assertEqual(path, self.path)
        self.assertTrue(text.endswith('\nBUILD_EXCLUSIVE_KERNEL="^$"\n'))
        self.assertEqual([line for line in text.splitlines() if not line.startswith("#")], ['BUILD_EXCLUSIVE_KERNEL="^$"'])
        self.assertEqual([p.name for p in m.dkms_later_overrides(**OVERRIDE)],
                         [f"nvidia-current-550.163.01-{WSL_RELEASE}-x86_64.conf", f"nvidia-current--{WSL_RELEASE}.conf",
                          f"nvidia-current--{WSL_RELEASE}-x86_64.conf"])
        self.assertNotEqual(m.dkms_override("nvidia-current", "550.163.02", WSL_RELEASE)[1], text)

    def test_writing_is_repeatable_and_user_overrides_are_never_replaced(self):
        self.assertEqual(m.dkms_override_blockers(**OVERRIDE), [])
        m.write_dkms_override(OVERRIDE)
        written = self.path.stat()
        self.assertEqual((stat_mode(written), self.path.read_text()), (0o644, m.dkms_override(**OVERRIDE)[1]))
        # Preflight again, a resumed upgrade or a repeated write keep the same file.
        self.assertEqual(m.dkms_override_blockers(**OVERRIDE), [])
        m.write_dkms_override(OVERRIDE)
        self.assertEqual(self.path.stat().st_ino, written.st_ino)
        self.assertEqual(sorted(p.name for p in m.DKMS_CONF.iterdir()), [self.path.name])
        # Overrides read before it stay and do not block: this file only narrows the WSL build.
        (m.DKMS_CONF / "nvidia-current.conf").write_text('BUILD_EXCLUSIVE_KERNEL="^6\\."\n')
        (m.DKMS_CONF / "nvidia-current-550.163.01.conf").write_text("MAKE[0]=make\n")
        self.assertEqual(m.dkms_override_blockers(**OVERRIDE), [])
        # A user's file at the path, edited or not ours, and a symlink even to identical content.
        self.path.write_text(m.dkms_override(**OVERRIDE)[1] + "# mine\n")
        self.assertEqual(len(m.dkms_override_blockers(**OVERRIDE)), 1)
        with self.assertRaises(ValueError):
            m.write_dkms_override(OVERRIDE)
        self.assertTrue(self.path.read_text().endswith("# mine\n"))
        self.path.unlink()
        target = m.DKMS_CONF.parent / "elsewhere.conf"
        target.write_text(m.dkms_override(**OVERRIDE)[1])
        self.path.symlink_to(target)
        self.assertEqual(len(m.dkms_override_blockers(**OVERRIDE)), 1)
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(len(m.dkms_override_blockers(**OVERRIDE)), 1)
        self.path.rmdir()
        # Files DKMS reads later would decide instead, dangling links included.
        for later in m.dkms_later_overrides(**OVERRIDE):
            later.symlink_to(m.DKMS_CONF / "missing.conf")
            self.assertEqual(len(m.dkms_override_blockers(**OVERRIDE)), 1, later.name)
            with self.assertRaises(ValueError):
                m.write_dkms_override(OVERRIDE)
            self.assertFalse(self.path.exists())
            later.unlink()
        # /etc/dkms itself redirected elsewhere.
        for child in m.DKMS_CONF.iterdir():
            child.unlink()
        m.DKMS_CONF.rmdir()
        m.DKMS_CONF.symlink_to(m.DKMS_CONF.parent)
        self.assertEqual(m.dkms_override_blockers(**OVERRIDE), [f"{m.DKMS_CONF} is not a directory"])

    def test_cleanup_removes_only_its_own_content(self):
        m.write_dkms_override(OVERRIDE)
        m.remove_dkms_override(OVERRIDE)
        self.assertFalse(self.path.exists())
        m.remove_dkms_override(OVERRIDE)
        self.path.write_text("BUILD_EXCLUSIVE_KERNEL=\"^$\"\n")
        m.remove_dkms_override(OVERRIDE)
        self.assertTrue(self.path.exists())

    def test_the_module_identity_comes_from_the_downloaded_package(self):
        deb = m.APT_DIR / "archives" / "nvidia-kernel-dkms_550.163.01-2_amd64.deb"
        with patch.object(m, "output", side_effect=deb_output()):
            self.assertEqual(m.dkms_identity(deb), ("nvidia-current", "550.163.01"))
        for postinst, listing in ((POSTINST.replace("DKMS_VERSION=550.163.01", "DKMS_VERSION=$(uname)"), LISTING),
                                  (POSTINST + "DKMS_NAME=other\n", LISTING), (POSTINST.replace("DKMS_NAME", "NAME"), LISTING),
                                  (POSTINST.replace("nvidia-current", "../../tmp/x"), LISTING), (POSTINST, "")):
            with patch.object(m, "output", side_effect=deb_output(postinst, listing)):
                self.assertIsNone(m.dkms_identity(deb), postinst)

    def test_only_a_wsl_kernel_and_a_changed_module_get_the_override(self):
        plan = m.parse_plan(MODULE_UPGRADE)
        with patch.object(m, "output", side_effect=deb_output()):
            self.assertEqual(m.module_override(plan), (OVERRIDE, []))
            self.assertEqual(m.module_override(m.parse_plan(MODULE_UPGRADE.splitlines()[0])), (None, []))
            for release in ("6.1.0-33-amd64", "6.12.48+deb13-amd64", WSL_RELEASE + "+", "6.18.58.1-microsoft-custom-WSL2"):
                m.KERNEL_RELEASE.write_text(release + "\n")
                self.assertEqual(m.module_override(plan), (None, []), release)
            m.KERNEL_RELEASE.write_text(WSL_RELEASE + "\n")
            m.WSL_LIB.rmdir()
            self.assertEqual(m.module_override(plan), (None, []))
            m.WSL_LIB.mkdir()
            m.dkms_later_overrides(**OVERRIDE)[1].write_text("BUILD_EXCLUSIVE_KERNEL=.\n")
            override, problems = m.module_override(plan)
            self.assertEqual((override, [p["code"] for p in problems]), (None, ["nvidia-kernel-module"]))
            m.dkms_later_overrides(**OVERRIDE)[1].unlink()
        (m.APT_DIR / "archives" / "nvidia-kernel-dkms_550.163.01-2_amd64.deb").unlink()
        self.assertEqual([p["code"] for p in m.module_override(plan)[1]], ["nvidia-kernel-module"])

    def rehearse(self, manual=frozenset()):
        """rehearse with APT simulated: the solution upgrades the module with libcuda1 and nvidia-smi."""
        calls = []

        def simulate(args):
            calls.append(list(args))
            return 0, MODULE_UPGRADE

        done = type("Done", (), {"returncode": 0, "stdout": b""})()
        patches = [patch.object(m, "php_inventory", return_value=({}, [])),
                   patch.object(m, "venv_inventory", return_value=[]), patch.object(m, "prepare_apt_dir"),
                   patch.object(m, "apt_update"), patch.object(m, "apt_mark", return_value=set(manual)),
                   patch.object(m, "output", side_effect=deb_output()),
                   patch.object(m, "simulate", side_effect=simulate), patch.object(m.subprocess, "run", return_value=done),
                   patch.object(m, "check_space", return_value=[]), patch.object(m, "new_package_files", return_value={}),
                   patch.object(m, "system_soname_owners", return_value={}), patch.object(m, "package_files", return_value=[]),
                   patch.object(m, "package_relations", return_value={}), patch.object(m, "extension_problems", return_value=[]),
                   patch.object(m, "version_lt", return_value=False)]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return m.rehearse(self.packages, [], {}), calls

    def test_preflight_keeps_the_module_package_and_plans_the_override_without_writing_it(self):
        # The official image installed all four manually; that changes nothing here.
        result, calls = self.rehearse(manual={"nvidia-kernel-dkms", "libcuda1", "nvidia-smi", "dkms"})
        self.assertEqual((result["problems"], result["args"], calls), ([], [], [[]]))
        self.assertEqual(sorted(result["plan"]["upgrade"]), ["libcuda1", "nvidia-kernel-dkms", "nvidia-smi"])
        self.assertEqual((result["plan"]["remove"], result["dkms_override"]), ({}, OVERRIDE))
        self.assertEqual(list(m.DKMS_CONF.iterdir()), [])
        # A user's override that DKMS would read last defers before the recovery copy.
        later = m.dkms_later_overrides(**OVERRIDE)[0]
        later.write_text('BUILD_EXCLUSIVE_KERNEL=".*"\n')
        result, _ = self.rehearse()
        self.assertEqual(([p["code"] for p in result["problems"]], result["dkms_override"]), (["nvidia-kernel-module"], None))
        later.unlink()
        # Not a WSL kernel: nothing is planned and nothing defers for the module.
        m.KERNEL_RELEASE.write_text("6.12.48+deb13-amd64\n")
        result, _ = self.rehearse()
        self.assertEqual((result["problems"], result["dkms_override"]), ([], None))

    def transaction(self, state, simulation=MODULE_UPGRADE, output=None):
        """Run m.transaction with APT and dpkg recorded; returns the recorded commands and whether the
        override existed at each."""
        commands = []

        def run(*args, **kwargs):
            commands.append((" ".join(str(a) for a in args[:3]), self.path.is_file()))
            return type("Done", (), {"returncode": 0})()

        patches = [patch.object(m, "apt_sources", return_value=[]), patch.object(m, "install_policy"),
                   patch.object(m, "prepare_apt_dir"), patch.object(m, "apt_update"),
                   patch.object(m, "simulate", return_value=(0, simulation)), patch.object(m, "check_space", return_value=[]),
                   patch.object(m, "output", side_effect=output or deb_output()), patch.object(m, "run", side_effect=run),
                   patch.object(m.subprocess, "run", side_effect=lambda args, **kwargs: run(*args)),
                   patch.object(m, "save_state"), patch.object(m.signal, "signal")]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            m.transaction(state)
        return commands

    def test_the_upgrade_writes_the_override_before_dpkg_and_resumes_with_it(self):
        digest = m.hashlib.sha256(json.dumps([[], []]).encode()).hexdigest()
        state = {"args": [], "plan_digest": digest, "step": "lists", "dkms_override": dict(OVERRIDE)}
        commands = self.transaction(state)
        self.assertEqual(commands[0], ("apt-get -o Dir::Etc::sourcelist=" + str(m.APT_DIR / "sources.list"), False))
        self.assertEqual([present for command, present in commands[1:]], [True] * (len(commands) - 1))
        self.assertTrue(any(c.startswith("dpkg --configure") for c, _ in commands))
        self.assertEqual(state["step"], "post")
        # Interrupted after dpkg started, with the override lost: restored before dpkg resumes.
        self.path.unlink()
        state["step"] = "dpkg"
        commands = self.transaction(state)
        self.assertEqual(commands[0], ("dpkg --configure -a", True))
        # Changed since: the resumed transaction stops before dpkg touches the module.
        self.path.write_text('BUILD_EXCLUSIVE_KERNEL=""\n')
        state["step"] = "dpkg"
        with self.assertRaises(ValueError):
            self.transaction(state)

    def test_the_upgrade_refuses_an_override_that_no_longer_matches(self):
        digest = m.hashlib.sha256(json.dumps([[], []]).encode()).hexdigest()
        state = {"args": [], "plan_digest": digest, "step": "lists", "dkms_override": dict(OVERRIDE)}
        newer = POSTINST.replace("550.163.01", "550.163.02")
        cases = [{"output": deb_output(newer, LISTING.replace("550.163.01", "550.163.02"))},
                 {"simulation": MODULE_UPGRADE.splitlines()[0] + "\n"}]
        for case in cases:
            with self.assertRaises(m.Deferred) as caught:
                self.transaction(dict(state), **case)
            self.assertEqual(caught.exception.reasons[0]["code"], "plan-changed")
            self.assertFalse(self.path.exists())
        m.KERNEL_RELEASE.write_text("6.12.48+deb13-amd64\n")
        with self.assertRaises(m.Deferred):
            self.transaction(dict(state))
        self.assertFalse(self.path.exists())

    def test_cancelling_removes_the_override_it_wrote(self):
        m.write_dkms_override(OVERRIDE)
        state = {"dkms_override": dict(OVERRIDE)}
        with patch.object(m, "stop_private_pg17"), patch.object(m, "CLOSED_OPEN", m.APT_DIR / "none"), \
                patch.object(m, "FENCE", m.APT_DIR / "fence"), patch.object(m, "remove_policy"), \
                patch.object(m, "save_state"):
            m.unfence(state)
        self.assertFalse(self.path.exists())


def stat_mode(info):
    return info.st_mode & 0o777


class Inventory(unittest.TestCase):
    def test_elf_needed_reads_dynamic_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp, "_x.cpython-311-x86_64-linux-gnu.so")
            path.write_bytes(elf(["libssl.so.3", "libc.so.6"]))
            self.assertEqual(m.elf_needed(path), ["libssl.so.3", "libc.so.6"])
            Path(temp, "text.so").write_bytes(b"not an elf")
            self.assertIsNone(m.elf_needed(Path(temp, "text.so")))

    def test_venvs_are_found_and_classified_and_bundled_libraries_are_not_system_needs(self):
        with tempfile.TemporaryDirectory() as temp:
            venv = Path(temp, "dwemer", "chatterbox", "venv")
            (venv / "lib" / "site-packages" / "pkg.libs").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("home = /usr/bin\ninclude-system-site-packages = false\nversion = 3.11.2\n")
            (venv / "lib" / "site-packages" / "_m.so").write_bytes(elf(["libbundled.so.1", "libssl.so.3"]))
            (venv / "lib" / "site-packages" / "pkg.libs" / "libbundled.so.1").write_bytes(b"x")
            uv = Path(temp, "dwemer", "uvenv")
            uv.mkdir(parents=True)
            (uv / "pyvenv.cfg").write_text("home = /home/dwemer/.local/share/uv/python/cpython-3.12/bin\nversion_info = 3.12.4\n")
            self.assertEqual(m.find_venvs([temp]), sorted([str(venv), str(uv)]))
            self.assertEqual(m.classify_venv(str(venv), m.read_pyvenv(str(venv))), ("retain", "3.11"))
            self.assertEqual(m.classify_venv(str(uv), m.read_pyvenv(str(uv))), ("independent", "3.12"))
            self.assertEqual(m.classify_venv("x", {"home": "/usr/bin", "version": "3.9.2"}), ("unknown", "3.9"))
            self.assertEqual(m.venv_needed_sonames(str(venv)), ["libssl.so.3"])

    def test_repointed_venv_links_keep_their_own_owner_and_group(self):
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp, "venv", "bin", "python")
            link.parent.mkdir(parents=True)
            # A dangling relative target: reading its owner must not follow it.
            os.symlink("python3", link)
            info = os.lstat(link)
            interpreter = f"/usr/bin/python{m.SOURCE_PYTHON}"
            chowned = []
            isfile = os.path.isfile
            # The venv's recorded owner differs from the link's actual owner and group.
            state = {"venvs": [{"path": str(link.parents[1]), "kind": "retain", "owner": info.st_uid + 1000}]}
            with patch.object(m, "run"), patch.object(m, "save_state"), \
                    patch.object(m.os.path, "isfile", lambda p: p == interpreter or isfile(p)), \
                    patch.object(m.os, "lchown", lambda p, u, g: chowned.append((os.readlink(p), u, g)), create=True):
                m.repoint_venvs(state)
            self.assertEqual(chowned, [(interpreter, info.st_uid, info.st_gid)])
            self.assertEqual(os.readlink(link), interpreter)
            self.assertEqual(os.listdir(link.parent), ["python"])
            self.assertEqual(state["venv_links"], {str(link): "python3"})

    def test_only_c_class_locales_without_special_collations_pass(self):
        inventory = {"databases": [["dwemer", True, "c", "C.UTF-8", "C.UTF-8", "UTF8", "2.36"],
                                   ["stobe", True, "c", "en_US.UTF-8", "en_US.UTF-8", "UTF8", "2.36"]],
                     "collations": {"dwemer": ["de-x-icu"]},
                     "closed": [m.reason("pg-closed-database", "archive")]}
        codes = [p["code"] for p in m.collation_problems(inventory)]
        self.assertEqual(codes, ["pg-collation", "pg-collation", "pg-closed-database"])

    # The laptop layout after the merged PG17 migration: template0 and the retained template1, both closed.
    RETAINED = "ddistro_template1_before_utf8_20261021214026"
    DATABASES = [["dwemer", True, "c", "C.UTF-8", "C.UTF-8", "UTF8", "2.36"],
                 [RETAINED, False, "c", "C", "C", "UTF8", None],
                 ["template0", False, "c", "C.UTF-8", "C.UTF-8", "UTF8", "2.36"]]

    def identity(self, **changes):
        traits = dict(oid=16384, name=self.RETAINED, template=False, allow=False, limit=-1, bootstrap=True, acl=True,
                      space="pg_default", settings=0, shared=0, size=7_500_000)
        traits.update(changes)
        return [[5, "dwemer", False, True, -1, False, False, "pg_default", 0, 12, 90_000_000],
                list(traits.values()), [4, "template0", True, False, -1, True, True, "pg_default", 0, 0, 7_400_000]]

    def test_the_retained_template_is_recognised_by_every_trait_and_never_by_name_alone(self):
        self.assertEqual(m.classify_closed(self.DATABASES, self.identity()), ([self.RETAINED], []))
        for change in ({"name": "archive"}, {"template": True}, {"bootstrap": False}, {"acl": False},
                       {"settings": 1}, {"shared": 2}, {"limit": 5}, {"space": "fast"}, {"size": 900_000_000}):
            databases = [row[:] for row in self.DATABASES]
            if "name" in change:
                databases[1][0] = change["name"]
            retained, problems = m.classify_closed(databases, self.identity(**change))
            self.assertEqual((retained, [p["code"] for p in problems]), ([], ["pg-closed-database"]), change)
        databases = [row[:] for row in self.DATABASES]
        databases[1][3] = "en_US.UTF-8"
        self.assertEqual(m.classify_closed(databases, self.identity())[0], [])
        self.assertEqual(m.classify_closed(self.DATABASES, self.identity(), utf8_pending=True)[0], [])
        # A template0 that is no longer a template is not skipped as built in.
        identity = self.identity()
        identity[2][2] = False
        self.assertEqual([p["code"] for p in m.classify_closed(self.DATABASES, identity)[1]], ["pg-closed-database"])

    def test_a_privately_inspected_retained_template_must_be_empty(self):
        inventory = {"objects": {self.RETAINED: 0}, "extensions": {self.RETAINED: [["plpgsql", "1.0", True]]}}
        self.assertEqual(m.retained_problems(inventory), [])
        inventory["extensions"][self.RETAINED].append(["vector", "0.8.1", True])
        self.assertEqual([p["code"] for p in m.retained_problems(inventory)], ["pg-closed-database"])
        inventory = {"objects": {self.RETAINED: 3}, "extensions": {self.RETAINED: []}}
        self.assertEqual([p["code"] for p in m.retained_problems(inventory)], ["pg-closed-database"])

    def test_private_inspection_journals_the_open_database_and_closes_it_even_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            statements = []

            def psql(socket, statement, db="postgres", readonly=True):
                statements.append((db, statement, readonly))
                if db != "postgres":
                    self.assertTrue(journal.is_file())
                    raise ValueError("connection lost")
                return ""

            journal = Path(temp, "closed-open.json")
            with patch.object(m, "CLOSED_OPEN", journal), patch.object(m, "psql", side_effect=psql), \
                 patch.object(m, "sync_dir"):
                with self.assertRaises(ValueError):
                    m.inspect_closed("/sock", self.RETAINED)
                self.assertFalse(journal.exists())
                self.assertEqual([s[1].split()[-1] for s in statements if s[0] == "postgres"], ["true", "false"])
                self.assertTrue(all(not s[2] for s in statements if s[0] == "postgres"))
                # A journal left by a killed helper is closed again before anything else.
                journal.write_text(json.dumps([self.RETAINED]))
                statements.clear()
                m.reclose_databases("/sock")
                self.assertEqual(statements, [("postgres", f'ALTER DATABASE "{self.RETAINED}" ALLOW_CONNECTIONS false',
                                               False)])
                self.assertFalse(journal.exists())

    def test_the_digest_covers_database_identity_but_not_sizes(self):
        base = {"roles": [], "role_settings": [], "databases": self.DATABASES, "identity": self.identity(),
                "extensions": {}, "objects": {self.RETAINED: 0}}
        later = json.loads(json.dumps(base))
        later["identity"][1][-1] += 8192
        self.assertEqual(m.inventory_digest(base), m.inventory_digest(later))
        later["identity"][1][3] = True
        self.assertNotEqual(m.inventory_digest(base), m.inventory_digest(later))
        later = json.loads(json.dumps(base))
        later["identity"][1][0] = 16999
        self.assertNotEqual(m.inventory_digest(base), m.inventory_digest(later))

    def test_inventory_digest_ignores_collation_versions_and_availability(self):
        base = {"roles": [["postgres", True]], "role_settings": [["", "dwemer", ["search_path=public"]]],
                "databases": [["dwemer", True, "c", "C.UTF-8", "C.UTF-8", "UTF8", "2.36"]],
                "extensions": {"dwemer": [["vector", "0.8.1", True]]}}
        later = json.loads(json.dumps(base))
        later["databases"][0][6] = "2.41"
        later["extensions"]["dwemer"][0][2] = False
        self.assertEqual(m.inventory_digest(base), m.inventory_digest(later))
        later["role_settings"] = []
        self.assertNotEqual(m.inventory_digest(base), m.inventory_digest(later))

    def test_extension_versions_must_survive_the_new_packages(self):
        inventory = {"extensions": {"dwemer": [["vector", "0.8.1", True], ["plpgsql", "1.0", True]]}}
        plan = {"upgrade": {"postgresql-17-pgvector": {}}, "install": {}}
        root = "/usr/share/postgresql/17/extension/"
        files = {"postgresql-17-pgvector": [root + "vector.control", root + "vector--0.8.0.sql"]}
        self.assertEqual([p["code"] for p in m.extension_problems(inventory, plan, files)], ["pg-extension-version"])
        files["postgresql-17-pgvector"].append(root + "vector--0.8.0--0.8.1.sql")
        self.assertEqual(m.extension_problems(inventory, plan, files), [])

    def test_php_settings_carry_only_explicit_known_changed_values(self):
        previous = {"memory_limit": "16384M", "upload_max_filesize": "20G", "session.sid_length": "26",
                    "max_execution_time": "30", "removed.setting": "1"}
        current = {"memory_limit": "128M", "upload_max_filesize": "2M", "session.sid_length": "32",
                   "max_execution_time": "30"}
        carried, dropped = m.carried_settings(previous, current)
        self.assertEqual(carried, {"memory_limit": "16384M", "upload_max_filesize": "20G"})
        self.assertEqual(sorted(dropped), ["removed.setting", "session.sid_length"])
        self.assertEqual(m.ini_quote('a"b'), '"a\\"b"')


def stack_elf(flags, payload=b"\xcc" * 4096):
    """A little-endian ELF64 shared object with only a PT_GNU_STACK header of the given flags."""
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack("<HHIQQQIHHHHHH", 3, 62, 1, 0, 64, 0, 0, 64, 56,
                                                                       1, 0, 0, 0)
    return header + struct.pack("<IIQQQQQQ", m.PT_GNU_STACK, flags, 0, 0, 0, 0, 0, 16) + payload


class ExecutableStack(unittest.TestCase):
    """The stock LocalWhisper CTranslate2 4.4.0 wheel bundles a library whose PT_GNU_STACK asks for an
    executable stack (RWE); Debian 13's glibc 2.41 refuses to dlopen it. Only those exact stock bits
    are fixed, after a probe copy imported, and nothing is written before the transaction."""

    RELATIVE = "lib/python3.11/site-packages/ctranslate2.libs/libctranslate2-d3638643.so.4.4.0"
    RECORD = "lib/python3.11/site-packages/ctranslate2-4.4.0.dist-info/RECORD"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.venv = str(Path(temp.name, "python-stt"))
        self.lib = Path(self.venv, self.RELATIVE)
        self.lib.parent.mkdir(parents=True)
        self.bits = stack_elf(7)
        self.lib.write_bytes(self.bits)
        self.lib.chmod(0o755)
        (self.lib.parent.parent / "ctranslate2").mkdir()
        (self.lib.parent.parent / "ctranslate2" / "__init__.py").write_text("")
        self.digest = hashlib.sha256(self.bits).hexdigest()
        self.record = Path(self.venv, self.RECORD)
        self.record.parent.mkdir()
        self.stock_line = m.record_line("ctranslate2.libs/libctranslate2-d3638643.so.4.4.0", self.digest, len(self.bits))
        self.record_text = (f"ctranslate2/__init__.py,sha256=abc,0\r\n{self.stock_line}\r\n"
                            "ctranslate2-4.4.0.dist-info/RECORD,,\r\n")
        self.record.write_bytes(self.record_text.encode())
        self.record.chmod(0o644)
        self.stock = dict(m.EXECSTACK_STOCK)
        known = dict(next(iter(m.EXECSTACK_STOCK.values())), bytes=len(self.bits), sha256=self.digest)
        patcher = patch.object(m, "EXECSTACK_STOCK", {(self.venv, self.RELATIVE): known})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info = {"path": self.venv, "kind": "retain", "python": "3.11", "owner": os.getuid(), "sonames": [],
                     "execstack": [self.RELATIVE]}

    def fixed_record(self, fix):
        return self.record_text.replace(self.stock_line, fix["record_new"])

    def test_the_stock_constants_name_the_reported_wheel_and_the_probe_compiles(self):
        (venv, relative), known = next(iter(self.stock.items()))
        self.assertEqual(Path(venv, relative).as_posix(), "/home/dwemer/python-stt/" + self.RELATIVE)
        self.assertEqual((known["bytes"], known["sha256"], known["record"]),
                         (71690457, "3fdf882ba5de43ac05bace3538dd980715848deec75b563d6e7fa6a79c3bdd78", self.RECORD))
        compile(m.EXECSTACK_PROBE, "probe", "exec")

    def test_only_executable_stack_requests_are_found_and_the_cleared_digest_is_exact(self):
        Path(self.venv, "lib", "plain.so").write_bytes(stack_elf(6))
        Path(self.venv, "lib", "nostack.so").write_bytes(elf(["libc.so.6"]))
        Path(self.venv, "lib", "text.so").write_bytes(b"not an elf")
        self.assertEqual(m.venv_execstack(self.venv), [self.RELATIVE])
        offset, flags = m.elf_stack(self.lib)
        self.assertEqual((offset, flags), (64 + 4, 7))
        cleared = bytearray(self.bits)
        cleared[offset:offset + 4] = struct.pack("<I", 6)
        self.assertEqual(m.cleared_digest(self.lib, offset, flags), hashlib.sha256(cleared).hexdigest())
        # More files than can be read: reported as incomplete, which defers.
        self.assertEqual(m.venv_execstack(self.venv, limit=1)[-1:], [None])
        self.assertEqual([p["code"] for p in m.rehearse_execstack([dict(self.info, execstack=[None])])[1]],
                         ["venv-execstack"])

    def test_private_builds_other_venvs_and_redirected_paths_defer_and_stay_untouched(self):
        fix, problem = m.execstack_fix(self.info, self.RELATIVE)
        self.assertIsNone(problem)
        self.assertEqual((fix["record_old"], fix["flags"]), (self.stock_line, 7))
        cases = [("other venv", dict(self.info, path=self.venv + "-other"), None),
                 ("own interpreter", dict(self.info, kind="independent"), None),
                 ("another owner", dict(self.info, owner=os.getuid() + 1), None),
                 ("private build", self.info, lambda: self.lib.write_bytes(stack_elf(7, b"\x90" * 4096))),
                 ("RECORD differs", self.info, lambda: self.record.write_text(self.stock_line.replace("sha256=", "sha256=x")))]
        for label, venv, change in cases:
            if change:
                change()
            before = (self.lib.read_bytes(), self.record.read_bytes())
            fix, problem = m.execstack_fix(venv, self.RELATIVE)
            self.assertEqual((fix, problem["code"]), (None, "venv-execstack"), label)
            self.assertEqual((self.lib.read_bytes(), self.record.read_bytes()), before, label)
            self.lib.write_bytes(self.bits)
            self.record.write_bytes(self.record_text.encode())
        # A library reached through a symlinked directory is never edited where it points.
        moved = Path(self.venv).with_name("elsewhere")
        os.rename(self.lib.parent, moved)
        os.symlink(moved, self.lib.parent)
        self.assertEqual(m.execstack_fix(self.info, self.RELATIVE)[1]["code"], "venv-execstack")

    def test_a_failed_probe_rejects_the_plan_before_the_solver_or_any_write(self):
        seen = []

        def probe(fix, site=""):
            copy = Path(site, "ctranslate2.libs", self.lib.name)
            seen.append((m.elf_stack(copy)[1], hashlib.sha256(copy.read_bytes()).hexdigest() == fix["patched_sha256"],
                         Path(site, "ctranslate2", "__init__.py").is_file(), os.stat(copy).st_mode & 0o777))
            return None, "exited 1: ImportError"

        before = (self.lib.read_bytes(), self.record.read_bytes(), os.stat(self.lib).st_mode)
        with patch.object(m, "execstack_probe", side_effect=probe), \
                patch.object(m, "venv_inventory", return_value=[self.info]), \
                patch.object(m, "php_inventory", return_value=({}, [])), patch.object(m, "prepare_apt_dir") as apt:
            with self.assertRaises(m.Deferred) as caught:
                m.rehearse({}, [], {})
        self.assertEqual([r["code"] for r in caught.exception.reasons], ["venv-execstack"])
        self.assertIn("ImportError", caught.exception.reasons[0]["detail"])
        apt.assert_not_called()
        # The probe imported a cleared copy beside its package, never the venv's own file.
        self.assertEqual(seen, [(6, True, True, 0o755)])
        self.assertEqual((self.lib.read_bytes(), self.record.read_bytes(), os.stat(self.lib).st_mode), before)
        self.assertEqual(list(Path("/var/tmp").glob("ddistro-trixie-execstack-*")), [])
        # An unknown executable-stack library defers without any probe.
        with patch.object(m, "execstack_probe") as never:
            fixes, problems = m.rehearse_execstack([dict(self.info, execstack=["lib/other.so"])])
        never.assert_not_called()
        self.assertEqual((fixes, [p["code"] for p in problems]), ([], ["venv-execstack"]))
        with patch.object(m, "execstack_probe", return_value=("speech", None)):
            fixes, problems = m.rehearse_execstack([self.info])
        self.assertEqual((problems, fixes[0]["probed"]), ([], "speech"))

    def test_the_fix_is_applied_once_keeps_owner_mode_and_other_record_lines_and_resumes(self):
        fix, _ = m.execstack_fix(self.info, self.RELATIVE)
        self.assertEqual(m.execstack_drift([fix]), [])
        with patch.object(m, "sync_dir"):
            m.apply_execstack([fix])
            fixed = os.stat(self.lib)
            self.assertEqual((m.elf_stack(self.lib)[1], m.sha256_file(self.lib)), (6, fix["patched_sha256"]))
            self.assertEqual((fixed.st_mode & 0o777, fixed.st_uid, fixed.st_size), (0o755, os.getuid(), len(self.bits)))
            self.assertEqual(self.record.read_bytes().decode(), self.fixed_record(fix))
            self.assertEqual(os.stat(self.record).st_mode & 0o777, 0o644)
            self.assertEqual(sorted(p.name for p in self.lib.parent.iterdir()), [self.lib.name])
            # Rerun: nothing changes.
            m.apply_execstack([fix])
            self.assertEqual(os.stat(self.lib).st_ino, fixed.st_ino)
            # Interrupted between the library and its RECORD line, with the temporary file of an
            # interrupted run and a bystander: only the RECORD line is written and both are kept.
            self.record.write_bytes(self.record_text.encode())
            Path(str(self.lib) + ".trixie-0").write_bytes(b"partial")
            Path(str(self.lib) + ".trixie-private").write_bytes(b"theirs")
            m.apply_execstack([fix])
            self.assertEqual(self.record.read_bytes().decode(), self.fixed_record(fix))
            self.assertEqual(os.stat(self.lib).st_ino, fixed.st_ino)
            self.assertEqual(sorted(p.name for p in self.lib.parent.iterdir()),
                             [self.lib.name, self.lib.name + ".trixie-0", self.lib.name + ".trixie-private"])
            self.assertEqual(Path(str(self.lib) + ".trixie-0").read_bytes(), b"partial")
            self.assertEqual(Path(str(self.lib) + ".trixie-private").read_bytes(), b"theirs")
            # Changed by anyone else: refused before it is touched.
            private = stack_elf(7, b"\x90" * 4096)
            self.lib.write_bytes(private)
            with self.assertRaises(ValueError):
                m.apply_execstack([fix])
            self.assertEqual(self.lib.read_bytes(), private)
        self.assertEqual(m.execstack_drift([fix]), [str(self.lib)])

    def snapshot(self):
        return (self.lib.read_bytes(), os.lstat(self.lib).st_ino, os.path.islink(self.record), self.record.read_bytes(),
                sorted(p.name for p in self.lib.parent.iterdir()), sorted(p.name for p in self.record.parent.iterdir()))

    def test_an_invalid_record_or_library_stops_before_either_is_written(self):
        fix, _ = m.execstack_fix(self.info, self.RELATIVE)
        cleared = fix["record_new"]
        elsewhere = Path(self.venv).with_name("RECORD-elsewhere")
        elsewhere.write_bytes(self.record_text.encode())
        cases = [("line missing", lambda: self.record.write_text("ctranslate2/__init__.py,sha256=abc,0\n")),
                 ("line twice", lambda: self.record.write_text(f"{self.stock_line}\n{self.stock_line}\n")),
                 ("both lines", lambda: self.record.write_text(f"{self.stock_line}\n{cleared}\n")),
                 ("cleared line beside the stock library", lambda: self.record.write_text(f"{cleared}\n")),
                 ("other line break", lambda: self.record.write_bytes(f"{self.stock_line}\x0b\n".encode())),
                 ("not UTF-8", lambda: self.record.write_bytes(self.record_text.encode() + b"\xff\n")),
                 ("RECORD symlinked", lambda: (self.record.unlink(), self.record.symlink_to(elsewhere))),
                 ("library symlinked", lambda: (os.rename(self.lib, elsewhere.with_name("lib-elsewhere")),
                                                self.lib.symlink_to(elsewhere.with_name("lib-elsewhere"))))]
        with patch.object(m, "sync_dir"):
            for label, change in cases:
                change()
                before = self.snapshot()
                with self.assertRaises(ValueError, msg=label):
                    m.apply_execstack([fix])
                self.assertEqual(self.snapshot(), before, label)
                for path in (self.lib, self.record):
                    path.unlink()
                self.lib.write_bytes(self.bits)
                self.record.write_bytes(self.record_text.encode())
            # Held by another owner (library and RECORD are both checked): nothing is written.
            before = self.snapshot()
            with self.assertRaises(ValueError):
                m.apply_execstack([dict(fix, owner=os.getuid() + 1)])
            self.assertEqual(self.snapshot(), before)
            # Interrupted after the library was cleared, then the RECORD changed: the library stays as it is.
            m.apply_execstack([fix])
            self.record.write_text("ctranslate2/__init__.py,sha256=abc,0\n")
            before = self.snapshot()
            with self.assertRaises(ValueError):
                m.apply_execstack([fix])
            self.assertEqual(self.snapshot(), before)

    def test_a_failed_write_removes_only_its_own_temporary_file(self):
        fix, _ = m.execstack_fix(self.info, self.RELATIVE)
        bystander = Path(str(self.lib) + ".trixie-" + "0" * 32)
        bystander.write_bytes(b"theirs")
        real_write = m.write_cleared
        created = []

        def corrupt(fix, source, target):
            real_write(fix, source, target)
            created.append(target)
            with open(target, "r+b") as stream:
                stream.write(b"X")

        def race(fix, source, target):
            real_write(fix, source, target)
            created.append(target)
            self.record.write_text("private,,\n")

        with patch.object(m, "sync_dir"):
            for writer in (corrupt, race):
                before = self.snapshot()
                with patch.object(m, "write_cleared", side_effect=writer), self.assertRaises(ValueError):
                    m.apply_execstack([fix])
                self.assertFalse(os.path.lexists(created[-1]), writer.__name__)
                after = self.snapshot()
                self.assertEqual(after[:2] + after[4:], before[:2] + before[4:], writer.__name__)
                self.assertEqual(bystander.read_bytes(), b"theirs")
            # The concurrent RECORD change is kept, not overwritten.
            self.assertEqual(self.record.read_text(), "private,,\n")
        # atomic removes the temporary file it created, and nothing else, when it fails.
        target = Path(self.venv, "target")
        target.write_bytes(b"old")
        neighbour = Path(self.venv, "target.trixie-" + "1" * 32)
        neighbour.write_bytes(b"theirs")
        with patch.object(m.os, "fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            m.atomic(target, b"new", 0o644)
        self.assertEqual(sorted(p.name for p in Path(self.venv).glob("target*")), [target.name, neighbour.name])
        self.assertEqual((target.read_bytes(), neighbour.read_bytes()), (b"old", b"theirs"))

    def test_the_speech_probe_runs_the_model_localwhisper_is_configured_with(self):
        home = Path(self.venv).with_name("remote-faster-whisper")
        cache = home / "models"
        revision = "a" * 40

        def model(path, size):
            path.mkdir(parents=True)
            (path / "model.bin").write_bytes(b"\0" * size)
            (path / "config.json").write_text("{}")
            return os.path.normpath(path)

        tiny = model(cache / "models--Systran--faster-whisper-tiny" / "snapshots" / ("b" * 40), 1)
        stale = model(cache / "models--Systran--faster-whisper-small" / "snapshots" / ("c" * 40), 2)
        small = model(cache / "models--Systran--faster-whisper-small" / "snapshots" / revision, 3)
        (cache / "models--Systran--faster-whisper-small" / "refs").mkdir()
        (cache / "models--Systran--faster-whisper-small" / "refs" / "main").write_text(revision)
        numbat = model(home / "numbat-base-skyrim-en", 4)
        roots = (str(cache), str(home / "absent"))
        for name, expected in (("small", small), ("numbat-base-skyrim-en", numbat)):
            (home / f"config-{name}.yaml").write_text(
                f"daemon:\n  port: 9876\nfaster_whisper:\n  model_cache_dir: {cache}/\n  model: {name}\n  device: cuda\n")
            (home / "config.yaml").unlink(missing_ok=True)
            (home / "config.yaml").symlink_to(home / f"config-{name}.yaml")
            found = m.whisper_models(roots, home=str(home))
            self.assertEqual(found[0], expected, name)
            self.assertEqual(sorted(found), sorted({tiny, stale, small} | {expected}), name)
        # No usable config.yaml (disabled, or linked outside LocalWhisper): the smallest model.
        outside = Path(self.venv).with_name("config-outside.yaml")
        outside.write_text("faster_whisper:\n  model: numbat-base-skyrim-en\n")
        for config in (None, outside):
            (home / "config.yaml").unlink(missing_ok=True)
            if config:
                (home / "config.yaml").symlink_to(config)
            self.assertEqual(m.whisper_models(roots, home=str(home))[0], tiny)

    def test_the_transaction_returns_to_the_fence_when_the_stock_file_changed(self):
        fix, _ = m.execstack_fix(self.info, self.RELATIVE)
        self.lib.write_bytes(stack_elf(7, b"\x90" * 4096))
        digest = m.hashlib.sha256(json.dumps([[], []]).encode()).hexdigest()
        state = {"args": [], "plan_digest": digest, "step": "lists", "execstack": [fix]}
        with patch.object(m, "apt_sources", return_value=[]), patch.object(m, "install_policy"), \
                patch.object(m, "prepare_apt_dir"), patch.object(m, "apt_update"), \
                patch.object(m, "simulate", return_value=(0, "")), patch.object(m, "check_space", return_value=[]), \
                patch.object(m, "run") as run, patch.object(m, "module_override", return_value=(None, [])), \
                patch.object(m.subprocess, "run") as direct, patch.object(m, "save_state"):
            with self.assertRaises(m.Deferred) as caught:
                m.transaction(state)
        self.assertEqual(caught.exception.reasons[0]["code"], "plan-changed")
        self.assertFalse(any("dpkg" in str(c) for c in run.call_args_list))
        self.assertFalse(any("--no-download" in c.args[0] or c.args[0][:2] == ["dpkg", "--configure"]
                             for c in direct.call_args_list if c.args))
        self.assertEqual(state["step"], "lists")

    def test_verify_requires_the_planned_file_and_a_passing_import_on_debian_13(self):
        fix, _ = m.execstack_fix(self.info, self.RELATIVE)
        fix["probed"] = "speech"
        self.assertEqual([p["code"] for p in m.execstack_problems([fix])], ["venv-execstack"])
        with patch.object(m, "sync_dir"):
            m.apply_execstack([fix])
        with patch.object(m, "execstack_probe", return_value=("speech", None)) as probe:
            self.assertEqual(m.execstack_problems([fix]), [])
        self.assertEqual(probe.call_args.args, (fix,))
        for result in ((None, "exited 1: ImportError: cannot enable executable stack"), ("import", None)):
            with patch.object(m, "execstack_probe", return_value=result):
                self.assertEqual([p["code"] for p in m.execstack_problems([fix])], ["venv-execstack"], result)


class Journal(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        paths = {"ROOT": self.base / "root", "STATE": self.base / "root" / "state.json", "FENCE": self.base / "fence",
                 "PG17_STATE": self.base / "pg17.json", "POLICY": self.base / "policy-rc.d",
                 "POLICY_ASIDE": self.base / "root" / "policy.saved", "CLOSED_OPEN": self.base / "root" / "closed-open.json",
                 "MACHINE_ID": self.base / "machine-id", "FRESH_IMAGE": self.base / "fresh-image",
                 "NEW_CONF": self.base / "conf17"}
        for key, value in paths.items():
            patcher = patch.object(m, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.ROOT.mkdir()
        m.NEW_CONF.mkdir()
        sync = patch.object(m, "sync_dir")
        sync.start()
        self.addCleanup(sync.stop)

    def journal(self, **state):
        m.STATE.write_text(json.dumps({"token": "t" * 64, "dpkg_status": "d", "seal": ["id", "0/1", "now", "shut down"],
                                       **state}))

    PG17_JOURNAL = {"phase": "active", "cleaned": True, "work": "/var/lib/postgresql/dwemer-stage-" + "a" * 32,
                    "candidate_id": "7400000000000000017"}

    def control(self, identity="7400000000000000017", state="shut down"):
        return patch.object(m, "controldata", return_value={"Database system identifier": identity,
                                                              "Database cluster state": state})

    def test_status_reads_only_and_reports_deferral_until_pg17_is_current(self):
        (m.NEW_CONF / "dwemer-maintenance-id").write_text("dwemer-stage-" + "a" * 32 + "\n")
        before = sorted(self.base.rglob("*"))
        with patch.object(m, "os_release", return_value={"ID": "debian", "VERSION_ID": "12"}), self.control():
            report = m.status_report()
            self.assertEqual((report["state"], report["reasons"][0]["code"]), ("deferred", "pg17-not-current"))
            m.PG17_STATE.write_text(json.dumps(dict(self.PG17_JOURNAL, cleaned=False)))
            self.assertEqual(m.status_report()["state"], "deferred")
            m.PG17_STATE.write_text(json.dumps(self.PG17_JOURNAL))
            self.assertEqual(m.status_report()["state"], "needed")
            # The journal alone is not trusted: the cluster on disk must be the journaled one.
            with self.control(identity="7400000000000000099"):
                report = m.status_report()
            self.assertEqual((report["state"], report["reasons"][0]["code"]), ("deferred", "pg17-identity"))
            self.journal(phase="upgrading")
            self.assertEqual(m.status_report()["state"], "interrupted")
            self.journal(phase="bogus")
            self.assertEqual(m.status_report()["state"], "failed")
            self.journal(phase="verified")
            self.assertEqual(m.status_report()["state"], "verified")
        m.MACHINE_ID.write_text("0123456789abcdef0123456789abcdef\n")
        with patch.object(m, "os_release", return_value={"ID": "debian", "VERSION_ID": "13", "VERSION_CODENAME": "trixie"}):
            m.STATE.unlink()
            report = m.status_report()
            self.assertEqual((report["state"], report["release"], report["codename"]), ("not-needed", "13", "trixie"))
            self.assertEqual(report["instance_id"], m.instance_id())
            self.assertRegex(report["instance_id"], r"^ddi1-")
        self.assertEqual(sorted(p for p in self.base.rglob("*") if p not in (m.PG17_STATE, m.STATE, m.MACHINE_ID)), before)

    def fenced(self, running=False, seal=None, digest="d"):
        m.FENCE.write_text("fence\n")
        return [patch.object(m, "pg_running", return_value=running),
                patch.object(m, "pg17_seal", return_value=seal or ["id", "0/1", "now", "shut down"]),
                patch.object(m, "service_running", return_value=False),
                patch.object(m, "dpkg_status_digest", return_value=digest)]

    def test_the_capture_token_and_an_unchanged_fence_are_required(self):
        self.journal(phase="fenced")
        state = m.load_state()
        patches = self.fenced()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(m.check_fence(state, "t" * 64), [])
        self.assertEqual([p["code"] for p in m.check_fence(state, "u" * 64)], ["capture-token"])
        with patch.object(m, "pg17_seal", return_value=["id", "0/2", "later", "shut down"]):
            self.assertEqual([p["code"] for p in m.check_fence(state, "t" * 64)], ["fence"])
        m.FENCE.unlink()
        self.assertEqual([p["code"] for p in m.check_fence(state, "t" * 64)], ["fence"])

    def test_a_changed_solution_returns_to_the_fence_and_a_failure_stays_resumable(self):
        self.journal(phase="fenced")
        for p in self.fenced():
            p.start()
            self.addCleanup(p.stop)
        m.POLICY_ASIDE.write_text("installed\n")
        m.POLICY.write_text("exit 101\n")
        with patch.object(m, "transaction", side_effect=m.Deferred([m.reason("plan-changed", "x")])):
            with self.assertRaises(m.Deferred):
                m.upgrade("t" * 64, "a" * 64, 10)
        state = m.load_state()
        self.assertEqual((state["phase"], state["recovery"]["sha256"]), ("fenced", "a" * 64))
        self.assertFalse(m.POLICY.exists())
        with patch.object(m, "transaction", side_effect=ValueError("network down")):
            with self.assertRaises(ValueError):
                m.upgrade("t" * 64, "a" * 64, 10)
        state = m.load_state()
        self.assertEqual((state["phase"], state["error"]), ("upgrading", "network down"))
        with self.assertRaises(m.Deferred):
            m.upgrade("u" * 64, "a" * 64, 10)

    def test_cancel_is_refused_once_dpkg_may_have_changed_the_system(self):
        self.journal(phase="upgrading", step="dpkg")
        with self.assertRaises(m.Deferred):
            m.cancel()
        self.journal(phase="upgrading", step="lists", new_start="auto")
        m.FENCE.write_text("fence\n")
        with patch.object(m, "dpkg_status_digest", return_value="d"), patch.object(m, "atomic") as atomic, \
             patch.object(m, "save_state") as save:
            self.assertEqual(m.cancel()["result"], "cancelled")
        self.assertFalse(m.FENCE.exists())
        self.assertEqual(atomic.call_args[0][1], "auto\n")
        self.assertEqual(save.call_args[0][1], "cancelled")

    def verified(self, **state):
        self.journal(phase="verified", running=["apache2", "php8.2-fpm"], new_start="auto",
                     verified={"at": 1, "dpkg_status": "v", "seal": ["id", "0/9", "later", "shut down"]}, **state)
        m.FENCE.write_text("fence\n")
        (m.NEW_CONF / "start.conf").write_text("manual\n")
        return [patch.object(m, "dpkg_status_digest", return_value="v"), patch.object(m, "pg_running", return_value=False),
                patch.object(m, "pg17_seal", return_value=["id", "0/9", "later", "shut down"]),
                patch.object(m, "restore_problems", return_value=[]), patch.object(m, "service_running", return_value=False),
                patch.object(m, "run")]

    def test_release_opens_writes_only_after_verify_and_is_crash_idempotent(self):
        self.journal(phase="upgraded")
        with self.assertRaises(m.Deferred):
            m.release()
        for p in self.verified():
            p.start()
            self.addCleanup(p.stop)
        result = m.release()
        self.assertEqual((result["result"], result["restart_services"]), ("current", ["apache2", "php8.4-fpm"]))
        self.assertEqual((m.NEW_CONF / "start.conf").read_text(), "auto\n")
        self.assertFalse(m.FENCE.exists())
        self.assertEqual(m.load_state()["phase"], "current")
        # Repeating it changes nothing and reports the same.
        updated = m.load_state()["updated_at"]
        self.assertEqual(m.release(), result)
        self.assertEqual(m.load_state()["updated_at"], updated)
        # A crash after start.conf and the fence went but before the journal said current: release
        # completes, even though the system has been written to since.
        self.journal(phase="verified", running=[], new_start="auto", verified={"dpkg_status": "old", "seal": []})
        with patch.object(m, "pg_running", return_value=True):
            self.assertEqual(m.release()["result"], "current")
        self.assertEqual(m.load_state()["phase"], "current")
        # A crash after start.conf was restored but with the fence still standing: same checks, then done.
        self.verified()
        (m.NEW_CONF / "start.conf").write_text("auto\n")
        self.assertEqual(m.release()["result"], "current")
        self.assertFalse(m.FENCE.exists())

    def test_release_refuses_while_fenced_if_anything_changed_since_verify(self):
        for change in (patch.object(m, "dpkg_status_digest", return_value="changed"),
                       patch.object(m, "pg_running", return_value=True),
                       patch.object(m, "pg17_seal", return_value=["id", "0/a", "even later", "shut down"])):
            patches = self.verified()
            for p in patches:
                p.start()
            try:
                with change, self.assertRaises(m.Deferred):
                    m.release()
                self.assertEqual((m.NEW_CONF / "start.conf").read_text(), "manual\n")
                self.assertTrue(m.FENCE.exists())
                self.assertEqual(m.load_state()["phase"], "verified")
            finally:
                for p in patches:
                    p.stop()
        patches = self.verified()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        m.CLOSED_OPEN.write_text("[]")
        with self.assertRaises(m.Deferred):
            m.release()

    def test_verify_never_lifts_the_fence(self):
        self.journal(phase="upgraded")
        with patch.object(m, "source_problems", return_value=[]), patch.object(m, "apt_sources", return_value=[]), \
             patch.object(m, "trixie_preference_problems", return_value=[]), patch.object(m, "stop_private_pg17"), patch.object(m, "os_release", return_value={"ID": "debian", "VERSION_ID": "13"}), \
             patch.object(m, "output", return_value=""), patch.object(m, "stale_processes", return_value=[]), \
             patch.object(m.subprocess, "run") as run, patch.object(m, "read_start", return_value="disabled"), \
             patch.object(m, "pg_running", return_value=False), patch.object(m, "controldata", return_value={
                 "Database system identifier": "id"}), patch.object(m, "private_pg17"), \
             patch.object(m, "pg_inventory", return_value={"objects": {}, "extensions": {}, "databases": []}), \
             patch.object(m, "inventory_digest", return_value="i"), patch.object(m, "dpkg_status_digest", return_value="v"), \
             patch.object(m, "pg17_seal", return_value=["id", "0/9", "later", "shut down"]), patch.object(m.os, "sync", create=True):
            run.return_value.returncode = 0
            state = m.load_state()
            state.update(php={}, venvs=[], guarded={}, pg15_id="id", inventory_digest="i", seal=["id"])
            m.save_state(state)
            m.FENCE.write_text("fence\n")
            self.assertEqual(m.verify()["result"], "verified")
            self.assertTrue(m.FENCE.exists())
            self.assertEqual(m.load_state()["verified"]["dpkg_status"], "v")
            # Verify repeats in full while verified; without the fence it fails.
            self.assertEqual(m.verify()["result"], "verified")
            m.FENCE.unlink()
            with self.assertRaises(m.Deferred):
                m.verify()
            self.assertEqual(m.load_state()["phase"], "verified")

    def test_unfence_closes_a_database_left_open_before_the_fence_lifts(self):
        self.journal(phase="fencing", new_start="auto", running=["apache2"], pg17_was_running=True)
        m.FENCE.write_text("fence\n")
        m.CLOSED_OPEN.write_text("[\"x\"]")
        calls = []

        def private(action):
            calls.append(action)
            if action == "start":  # private_pg17 closes the journalled databases on start
                m.CLOSED_OPEN.unlink()

        with patch.object(m, "stop_private_pg17", side_effect=lambda: calls.append("stop-left")), \
             patch.object(m, "private_pg17", side_effect=private), patch.object(m, "restore_problems", return_value=[]), \
             patch.object(m, "pg_running", return_value=False), patch.object(m, "service_running", return_value=False), \
             patch.object(m, "run", side_effect=lambda *a, **k: calls.append(" ".join(a[-2:]))):
            m.unfence(m.load_state())
        self.assertEqual(calls, ["stop-left", "start", "stop", "dwemer start", "apache2 start"])
        self.assertEqual((m.NEW_CONF / "start.conf").read_text(), "auto\n")
        self.assertFalse(m.FENCE.exists())
        self.assertEqual(m.load_state()["phase"], "cancelled")

    def live(self, up, fail=()):
        """Clusters and services that run (`up`, by name, 15 and 17 for the clusters); `run` starts them
        unless named in `fail`, and records every command."""
        old_conf = self.base / "conf15"
        old_conf.mkdir(exist_ok=True)
        (old_conf / "start.conf").write_text("disabled\n")
        m.PG17_STATE.write_text(json.dumps(self.PG17_JOURNAL))
        (m.NEW_CONF / "dwemer-maintenance-id").write_text("dwemer-stage-" + "a" * 32 + "\n")
        self.runs = []

        def run(*args, **kwargs):
            self.runs.append(" ".join(map(str, args)))
            name = 17 if args[0] == "pg_ctlcluster" else args[1]
            if args[-1] != "start" or name in fail:
                raise m.subprocess.CalledProcessError(1, args)
            up.add(name)

        self.start([self.control(), patch.object(m, "OLD_CONF", old_conf), patch.object(m, "stop_private_pg17"),
                    patch.object(m, "socket_serves", return_value=False), patch.object(m, "note"),
                    patch.object(m, "pg_running", side_effect=lambda major, _d: major in up),
                    patch.object(m, "service_running", side_effect=lambda name: name in up),
                    patch.object(m, "dpkg_status_digest", return_value="d"), patch.object(m, "run", side_effect=run)])
        return old_conf

    def test_rollback_restores_exactly_what_ran_at_the_fence_and_a_rerun_finishes_it(self):
        # ASUS case9: the restored Bookworm copy is cancelled cold. PG17 served at the fence, no writer ran.
        up = set()
        self.live(up, fail={17})
        self.journal(phase="fenced", new_start="auto", running=[], pg17_was_running=True,
                     pg17_candidate_id=self.PG17_JOURNAL["candidate_id"])
        m.FENCE.write_text("fence\n")
        result = m.cancel()
        # A failed start lifts the fence anyway, succeeds, and never points back at the recovery copy.
        self.assertEqual((result["result"], result["restore"]["pending"], result["restore"]["failed"]),
                         ("cancelled", True, ["postgresql@17-dwemer"]))
        self.assertNotIn("recovery", result["next"])
        self.assertEqual((m.FENCE.exists(), m.load_state()["phase"]), (False, "cancelled"))
        self.assertTrue(m.status_report()["restore_pending"])
        # The rerun starts only 17/dwemer; once done, a further cancel starts nothing.
        self.runs.clear()
        self.live(up)
        self.assertEqual((m.cancel()["restore"]["pending"], self.runs, up),
                         (False, ["pg_ctlcluster --skip-systemctl-redirect 17 dwemer start"], {17}))
        self.runs.clear()
        self.assertEqual((m.cancel()["result"], self.runs), ("cancelled", []))
        self.assertNotIn("restore_pending", m.status_report())

        # A crash right after the fence lifted: the journaled restore is finished by cancel, and a
        # writer someone already started is neither started nor stopped again.
        up = {"apache2"}
        self.live(up)
        state = {"phase": "cancelled", "running": ["apache2", "php8.2-fpm"], "pg17_was_running": True,
                 "pg17_candidate_id": self.PG17_JOURNAL["candidate_id"]}
        self.journal(**state, restore=m.restore_plan(state, state["running"]))
        self.assertEqual(m.cancel()["restore"]["running"], ["postgresql@17-dwemer", "apache2", "php8.2-fpm"])
        self.assertEqual(self.runs, ["pg_ctlcluster --skip-systemctl-redirect 17 dwemer start", "service php8.2-fpm start"])
        # A journal cancelled before restores were recorded starts nothing; start_env covers it.
        self.runs.clear()
        self.journal(phase="cancelled", running=["apache2"], pg17_was_running=True)
        self.assertEqual((m.cancel()["result"], self.runs), ("cancelled", []))

    def test_nothing_starts_on_a_recovery_copy_an_unverified_candidate_or_beside_pg15(self):
        candidate = self.PG17_JOURNAL["candidate_id"]
        cases = [("recovery-copy", lambda old: patch.dict(os.environ, {"WSL_DISTRO_NAME": "DwemerAI4Skyrim3-TrixieRecovery-1"})),
                 ("pg17-not-current", lambda old: m.PG17_STATE.write_text(json.dumps({**self.PG17_JOURNAL, "phase": "staged"}))),
                 ("pg17-identity", lambda old: self.control(identity="7400000000000000099")),
                 ("pg15-guard", lambda old: (old / "start.conf").write_text("auto\n"))]
        for code, change in cases:
            up = set()
            old = self.live(up)
            with contextlib.ExitStack() as stack:
                changed = change(old)
                if hasattr(changed, "__enter__"):
                    stack.enter_context(changed)
                self.journal(phase="fenced", new_start="auto", running=["apache2"], pg17_was_running=True,
                             pg17_candidate_id=candidate)
                m.FENCE.write_text("fence\n")
                restore = m.cancel()["restore"]
            self.assertEqual(([p["code"] for p in restore["problems"]], restore["pending"], self.runs, up),
                             ([code], True, [], set()), code)
            self.assertFalse(m.FENCE.exists())

    def test_release_restores_the_writers_under_their_trixie_names_and_a_rerun_never_stops_one(self):
        up = set()
        self.live(up, fail={"php8.4-fpm"})
        self.journal(phase="verified", running=["apache2", "php8.2-fpm"], pg17_was_running=True, new_start="auto",
                     pg17_candidate_id=self.PG17_JOURNAL["candidate_id"],
                     verified={"dpkg_status": "v", "seal": ["id", "0/9", "later", "shut down"]})
        m.FENCE.write_text("fence\n")
        (m.NEW_CONF / "start.conf").write_text("manual\n")
        with patch.object(m, "dpkg_status_digest", return_value="v"), \
             patch.object(m, "pg17_seal", return_value=["id", "0/9", "later", "shut down"]):
            result = m.release()
        self.assertEqual((result["result"], result["restore"]["running"], result["restore"]["failed"]),
                         ("current", ["postgresql@17-dwemer", "apache2"], ["php8.4-fpm"]))
        self.assertNotIn("recovery", result["next"])
        self.assertEqual((m.FENCE.exists(), m.load_state()["phase"]), (False, "current"))
        # Released: the rerun checks no fence condition, starts only what is still stopped, stops nothing.
        self.runs.clear()
        self.live(up)
        self.assertFalse(m.release()["restore"]["pending"])
        self.assertEqual((self.runs, up), (["service php8.4-fpm start"], {17, "apache2", "php8.4-fpm"}))
        self.runs.clear()
        self.assertEqual((m.release()["result"], self.runs), ("current", []))

    def test_private_server_is_recognised_by_its_socket(self):
        pid = self.base / "postmaster.pid"
        pid.write_text(f"123\n/var/lib/postgresql/17/dwemer\n1\n5432\n{m.PRIVATE_SOCKET}\n\n")
        self.assertTrue(m.private_pg17_running(pid))
        pid.write_text("123\n/var/lib/postgresql/17/dwemer\n1\n5432\n/var/run/postgresql\n*\n")
        self.assertFalse(m.private_pg17_running(pid))
        self.assertFalse(m.private_pg17_running(self.base / "missing"))

    def offline(self, running=(), serves=False, **control):
        """PG17 as a reopened distro finds it: a journaled, marked 17/dwemer and nothing started yet."""
        (m.NEW_CONF / "dwemer-maintenance-id").write_text("dwemer-stage-" + "a" * 32 + "\n")
        self.calls = []

        def private(action):
            self.calls.append(("private", action))
            return m.PRIVATE_SOCKET

        def inventory(socket, private=False):
            self.calls.append(("inventory", socket, private))
            return {"databases": [], "collations": {}, "closed": [], "retained": [], "objects": {}, "extensions": {}}

        return [self.control(**control), patch.object(m, "pg_running", side_effect=lambda major, _d: major in running),
                patch.object(m, "socket_serves", return_value=serves), patch.object(m, "stop_private_pg17"),
                patch.object(m, "private_pg17", side_effect=private), patch.object(m, "pg_inventory", side_effect=inventory),
                patch.object(m, "note")]

    def start(self, patches):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_stopped_pg17_is_proven_offline_and_inspected_only_on_the_private_socket(self):
        for state in ("shut down", "in production"):
            with contextlib.ExitStack() as stack:
                for p in self.offline(state=state):
                    stack.enter_context(p)
                m.pg17_inventory(self.PG17_JOURNAL)
            # Closed databases are judged from shared catalogs only (private=False): nothing is opened.
            self.assertEqual(self.calls, [("private", "start"), ("inventory", m.PRIVATE_SOCKET, False), ("private", "stop")])
        with contextlib.ExitStack() as stack:
            for p in self.offline(running=(17,)):
                stack.enter_context(p)
            m.pg17_inventory(self.PG17_JOURNAL)
        self.assertEqual(self.calls, [("inventory", m.PUBLIC_SOCKET, False)])
        for kwargs, code in (({"identity": "7400000000000000099"}, "pg17-identity"), ({"state": "in archive recovery"}, "pg17-identity"),
                             ({"running": (15,)}, "pg15-guard")):
            with contextlib.ExitStack() as stack:
                for p in self.offline(**kwargs):
                    stack.enter_context(p)
                with self.assertRaises(m.Deferred) as raised:
                    m.pg17_inventory(self.PG17_JOURNAL)
            self.assertEqual(([r["code"] for r in raised.exception.reasons], self.calls), ([code], []), kwargs)
        (m.NEW_CONF / "dwemer-maintenance-id").write_text("dwemer-stage-" + "b" * 32 + "\n")
        with self.control(), patch.object(m, "pg_running", return_value=False):
            self.assertEqual([r["code"] for r in m.pg17_identity_problems(self.PG17_JOURNAL)], ["pg17-identity"])

    def test_a_wrong_live_server_on_the_application_socket_is_still_refused(self):
        patches = self.offline(serves=True)
        self.start([p for p in patches if p.attribute != "pg_inventory"])
        with patch.object(m, "psql", return_value=f"15|{m.OLD_DATA}") as psql:
            with self.assertRaisesRegex(ValueError, "does not serve PG17"):
                m.pg17_inventory(self.PG17_JOURNAL)
        self.assertEqual((psql.call_args[0][0], self.calls), (m.PUBLIC_SOCKET, []))

    def test_prepare_with_pg17_stopped_fences_without_a_public_start_and_keeps_its_start_conf(self):
        patcher = patch.object(m, "PLAN", m.ROOT / "plan.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        plan_id = "c" * 32
        m.PLAN.write_text(json.dumps({"plan_id": plan_id, "created": time.time(), "dpkg_status": "d", "args": [],
                                      "plan_digest": "p", "php_map": {}, "venvs": []}))
        (m.NEW_CONF / "start.conf").write_text("auto\n")
        runs = []
        self.start(self.offline() + [
            patch.object(m, "pg17_journal", return_value=(self.PG17_JOURNAL, [])),
            patch.object(m, "dpkg_status_digest", return_value="d"), patch.object(m, "apt_sources", return_value=[]),
            patch.object(m, "static_problems", return_value=[]), patch.object(m, "installed_packages", return_value={}),
            patch.object(m, "php_snapshot", return_value={}), patch.object(m, "service_running", return_value=False),
            patch.object(m, "cuda_layout", return_value=None), patch.object(m, "stock_preferences", return_value=(None, [])),
            patch.object(m, "collation_problems", return_value=[]), patch.object(m, "retained_problems", return_value=[]),
            patch.object(m, "inventory_digest", return_value="i"), patch.object(m, "guarded_digests", return_value={}),
            patch.object(m, "pg17_seal", return_value=["7400000000000000017", "0/1", "now", "shut down"]),
            patch.object(m, "run", side_effect=lambda *a, **k: runs.append(a)), patch.object(m.os, "sync", create=True)])
        result = m.prepare(plan_id)
        state = m.load_state()
        self.assertEqual((result["result"], state["phase"], state["pg17_was_running"], state["new_start"], state["running"]),
                         ("fenced", "fenced", False, "auto", []))
        # Only the private inspection after the fence; no service or cluster start, nothing on the public socket.
        self.assertEqual(self.calls, [("private", "start"), ("inventory", m.PRIVATE_SOCKET, True), ("private", "stop")])
        self.assertEqual(runs, [])
        self.assertTrue(m.FENCE.is_file())
        # A cancel restores the start.conf and starts nothing, since nothing was running.
        m.unfence(state)
        self.assertEqual(((m.NEW_CONF / "start.conf").read_text(), runs, m.FENCE.exists()), ("auto\n", [], False))

    def test_cli_validates_upgrade_modes_before_anything_runs(self):
        for argv in (["status", "--prepare"], ["release", "--cancel"], ["upgrade"], ["upgrade", "--prepare", "--cancel"],
                     ["upgrade", "--prepare", "--plan-id", "short"],
                     ["upgrade", "--capture-token", "t" * 64, "--recovery-sha256", "a" * 64]):
            with self.assertRaises(SystemExit) as caught, patch("sys.stderr"):
                m.main(argv)
            self.assertEqual(caught.exception.code, 2, argv)


class GithubCli(unittest.TestCase):
    """The installer's GitHub CLI source stays as it is; the rehearsal may only upgrade an installed gh from it."""
    # gpg --with-colons --show-keys of GitHub's published githubcli-archive-keyring.gpg (2026-11).
    SHOW_KEYS = (
        "pub:e:4096:1:23F3D4EA75716059:1662463626:1788612228::-:::sc::::::23::0:\n"
        "fpr:::::::::2C6106201985B60E6C7AC87323F3D4EA75716059:\n"
        "uid:e::::1725540228::8112F49127753770F34E73A49614E0AFC050A705::GitHub CLI <opensource+cli@github.com>::::::::::0:\n"
        "sub:e:4096:1:E5FAF19590714157:1662463626:1788612228:::::e::::::23:\n"
        "fpr:::::::::5700BAB26C8DE75F3EE323FEE5FAF19590714157:\n"
        "pub:-:4096:1:5612B36462313325:1775559160:::-:::scESC::::::23::0:\n"
        "fpr:::::::::7F38BBB59D064DBCB3D84D725612B36462313325:\n"
        "uid:-::::1775559160::8112F49127753770F34E73A49614E0AFC050A705::GitHub CLI <opensource+cli@github.com>::::::::::0:\n"
        "sub:-:4096:1:F4CAB2C46C97E579:1775559160::::::e::::::23:\n"
        "fpr:::::::::B84252FAAA164D9EBEA2E2C1F4CAB2C46C97E579:\n")
    GH = {"status": "ii", "version": "2.83.0", "source": "gh", "arch": "amd64"}
    FEED = "        500 https://cli.github.com/packages stable/main amd64 Packages\n"

    def test_published_keyring_primaries_exclude_subkeys(self):
        with patch.object(m, "output", return_value=self.SHOW_KEYS):
            self.assertEqual(m.key_fingerprints("k"), [GITHUB_RETIRED, GITHUB_SIGNING])
            # A single-key consumer (PGDG, the vendor feeds) never accepts this keyring.
            self.assertIsNone(m.key_fingerprint("k"))
        with patch.object(m, "output", return_value="\n".join(self.SHOW_KEYS.splitlines()[5:]) + "\n"):
            self.assertEqual(m.key_fingerprint("k"), GITHUB_SIGNING)
        with patch.object(m, "output", return_value=""):
            self.assertIsNone(m.key_fingerprints("k"))
        # A pub record without its fingerprint makes the file unreadable as a whole.
        with patch.object(m, "output", return_value=self.SHOW_KEYS.replace("fpr:::::::::2C61", "uid:::::::::2C61")):
            self.assertIsNone(m.key_fingerprints("k"))

    def prepare(self, packages):
        """prepare_apt_dir against a stand-in keyring, resolved by content so the copy is checked too."""
        by_content = {b"stock": [GITHUB_RETIRED, GITHUB_SIGNING], b"extra": [GITHUB_SIGNING, "0" * 40]}
        with tempfile.TemporaryDirectory() as temp:
            keyring = Path(temp, "githubcli-archive-keyring.gpg")
            keyring.write_bytes(b"stock")
            apt = Path(temp, "apt")
            apt.mkdir()

            def fingerprints(path):
                return by_content.get(Path(path).read_bytes()) if os.path.isfile(path) else None

            with patch.object(m, "keyring_fingerprints", side_effect=fingerprints), patch.object(m, "APT_DIR", apt), \
                    patch.object(m, "pgdg_key", return_value=apt / "pgdg.asc"):
                line = GITHUB_STOCK.replace(GITHUB_KEY, keyring.as_posix())
                entries = [dict(m.parse_one_line_source(line), file="github-cli.list")]
                entries[0]["kind"] = m.classify_source(entries[0])
                self.assertEqual(entries[0]["kind"], "agnostic")
                m.prepare_apt_dir(entries, packages)
                result = (apt / "sources.list").read_text(), (apt / "preferences").read_text()
                copy = apt / "github-cli.gpg"
                joined = "cli.github.com" in result[0]
                self.assertEqual(copy.read_bytes() if copy.exists() else None, b"stock" if joined else None)
                # Replaced after recognition, the keyring's copy is checked again and refused.
                keyring.write_bytes(b"extra")
                if joined:
                    with self.assertRaisesRegex(ValueError, "holds keys other than the published ones"):
                        m.prepare_apt_dir(entries, packages)
                self.assertEqual(keyring.read_bytes(), b"extra")
                return result, copy.as_posix()

    def test_installed_gh_joins_the_rehearsal_with_its_installed_keys_and_only_gh_pinned_in(self):
        (sources, preferences), copy = self.prepare({"gh": self.GH})
        self.assertIn(f"deb [signed-by={copy}] https://cli.github.com/packages stable main\n", sources)
        self.assertEqual(sources.count("cli.github.com"), 1)
        self.assertTrue(preferences.startswith("Package: /^gh$/\nPin: origin cli.github.com\nPin-Priority: 500\n\n"
                                               "Package: *\nPin: origin cli.github.com\nPin-Priority: -1\n\n"))

    def test_without_gh_the_source_adds_nothing(self):
        for packages in ({}, {"gh": dict(self.GH, status="rc")}, {"git": dict(self.GH, source="git")}):
            (sources, preferences), _copy = self.prepare(packages)
            self.assertNotIn("cli.github.com", sources + preferences, packages)

    @with_keyrings
    def test_the_stock_sources_pass_and_github_cli_stays_byte_for_byte_with_no_system_pin(self, _keys):
        # The installer's files: sources.list (Bookworm and testing) and sources.list.d/github-cli.list.
        with tempfile.TemporaryDirectory() as temp:
            sources, parts = Path(temp, "sources.list"), Path(temp, "sources.list.d")
            parts.mkdir()
            sources.write_text(Sources.STOCK)
            (parts / "github-cli.list").write_text(GITHUB_STOCK + "\n")
            with patch.object(m, "APT_SOURCES", sources), patch.object(m, "APT_PARTS", parts):
                entries = m.apt_sources()
        self.assertEqual([e["kind"] for e in entries], ["debian", "debian", "debian", "stock-testing", "agnostic"])
        # None of them defers as apt-third-party.
        self.assertFalse([e for e in entries if e["kind"] in ("other", "vendor-target")])
        rewritten = m.rewrite_suites(GITHUB_STOCK + "\n", deb822=False)
        self.assertEqual(rewritten, GITHUB_STOCK + "\n")
        # verify accepts the untouched entry next to the Trixie ones.
        self.assertEqual(m.source_problems([dict(e, kind=m.classify_source(e), file="github-cli.list")
                                            for e in map(m.parse_one_line_source, rewritten.splitlines())]), [])
        self.assertEqual(m.vendor_keys(entries), [])
        self.assertEqual(m.debian_components(entries), ["main", "contrib", "non-free", "non-free-firmware"])
        self.assertEqual(m.agnostic_feeds(entries, {"gh": self.GH}), {"github-cli": GITHUB_KEY})
        self.assertEqual(m.agnostic_feeds(entries, {}), {})
        self.assertNotIn("cli.github.com", m.vendor_pin_file([]) + m.TRIXIE_PREFERENCES)

    def test_the_source_upgrades_only_an_installed_gh(self):
        policy = m.parse_policy(
            "gh:\n  Installed: 2.83.0\n  Candidate: 2.103.3\n  Version table:\n     2.103.3 500\n" + self.FEED +
            "     2.46.0-3 500\n        500 https://deb.debian.org/debian trixie/main amd64 Packages\n"
            " *** 2.83.0 100\n        100 /var/lib/dpkg/status\n"
            "gh-extension:\n  Installed: 1.0\n  Candidate: 1.0\n  Version table:\n     2.0 -1\n" + self.FEED.replace("500", "-1") +
            " *** 1.0 100\n        100 /var/lib/dpkg/status\n")
        # An installed gh is newer than Debian's gh, yet its own source carries it: neither ahead nor obsolete.
        self.assertTrue(policy["gh"]["vendor"])
        self.assertEqual(m.ahead_packages(policy), [])
        # Any other name from that source is no GitHub CLI package and gets no exemption.
        self.assertFalse(policy["gh-extension"]["vendor"])
        self.assertEqual(m.obsolete_packages(policy), ["gh-extension"])
        upgrade = m.parse_plan("Inst gh [2.83.0] (2.103.3 gh:stable [amd64])\n")
        feeds = m.plan_feeds(upgrade, policy)
        self.assertEqual(feeds, {"gh": "github-cli"})
        self.assertEqual(m.origin_problems(upgrade, feeds) + m.vendor_problems(upgrade, {"gh": self.GH}, feeds), [])
        self.assertEqual([p["code"] for p in m.origin_problems(upgrade)], ["apt-origin"])
        # Added where it was not installed, another package from the feed, or gh removed: deferred.
        added = m.parse_plan("Inst gh (2.103.3 gh:stable [amd64])\n")
        self.assertEqual([p["code"] for p in m.vendor_problems(added, {}, m.plan_feeds(added, policy))], ["vendor-package"])
        other = m.parse_plan("Inst gh-extension [1.0] (2.0 gh:stable [amd64])\n")
        self.assertEqual([p["code"] for p in m.vendor_problems(other, {"gh-extension": self.GH},
                                                               m.plan_feeds(other, policy))], ["vendor-package"])
        removed = m.parse_plan("Remv gh [2.83.0]\n")
        self.assertEqual([p["code"] for p in m.vendor_problems(removed, {"gh": self.GH}, {})], ["vendor-component-removed"])


class Vendors(unittest.TestCase):
    CUDA = ("deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] "
            "https://developer.download.nvidia.com/compute/cuda/repos/debian12/x86_64/ /\n")
    MICROSOFT = ("deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] "
                 "https://packages.microsoft.com/debian/12/prod bookworm main\n")

    def kinds(self, text):
        return [m.classify_source(e) for e in map(m.parse_one_line_source, text.splitlines()) if e]

    def test_only_the_exact_core_vendor_entries_migrate_to_their_signed_debian13_feeds(self):
        self.assertEqual(self.kinds(self.CUDA + self.MICROSOFT), ["vendor", "vendor"])
        for variant in (self.CUDA.replace("signed-by=", "trusted=yes signed-by="),
                        self.CUDA.replace("cuda-archive-keyring", "other"),
                        self.CUDA.replace("debian12", "ubuntu2204"), self.CUDA.replace("deb ", "deb-src ", 1),
                        self.MICROSOFT.replace("amd64,arm64,armhf", "arm64"), self.MICROSOFT.replace(" main", " main extra"),
                        self.MICROSOFT.replace("debian/12", "debian/13").replace("bookworm", "trixie"),
                        "deb [arch=amd64] https://packages.microsoft.com/repos/code stable main"):
            self.assertEqual(self.kinds(variant), ["other"], variant)
        rewritten = m.rewrite_suites(self.CUDA + self.MICROSOFT, deb822=False)
        self.assertEqual(rewritten, (
            "deb [signed-by=/etc/apt/keyrings/dwemerdistro-nvidia-cuda-debian13.gpg] "
            "https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/ /\n"
            "deb [arch=amd64,arm64,armhf signed-by=/etc/apt/keyrings/dwemerdistro-microsoft-debian13.gpg] "
            "https://packages.microsoft.com/debian/13/prod trixie main\n"))
        self.assertEqual(self.kinds(rewritten), ["vendor-target", "vendor-target"])
        self.assertEqual(m.rewrite_suites(rewritten, deb822=False), rewritten)
        entries = [dict(e, kind=m.classify_source(e), file="x.list")
                   for e in map(m.parse_one_line_source, (self.CUDA + rewritten).splitlines())]
        self.assertEqual(m.vendor_keys(entries), ["nvidia-cuda"])
        self.assertEqual(m.vendor_keys(entries, "vendor-target"), ["microsoft", "nvidia-cuda"])
        # verify refuses a vendor entry still on its Debian 12 feed, even with the flat "/" suite.
        self.assertEqual([p["code"] for p in m.source_problems(entries)], ["apt-sources"])
        # The deb822 form of the Microsoft feed migrates the same way; extra fields keep it unknown.
        stanza = ("Types: deb\nURIs: https://packages.microsoft.com/debian/12/prod/\nSuites: bookworm\nComponents: main\n"
                  "Architectures: amd64 arm64\nSigned-By: /usr/share/keyrings/microsoft-prod.gpg\n")
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(stanza)], ["vendor"])
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(stanza + "Trusted: yes\n")], ["other"])
        migrated = m.rewrite_suites(stanza, deb822=True)
        self.assertEqual(migrated, stanza.replace("debian/12", "debian/13").replace("bookworm", "trixie").replace(
            "/usr/share/keyrings/microsoft-prod.gpg", "/etc/apt/keyrings/dwemerdistro-microsoft-debian13.gpg"))
        self.assertEqual([m.classify_source(e) for e in m.parse_deb822(migrated)], ["vendor-target"])

    def test_pins_admit_only_versioned_user_space_components(self):
        pins = m.vendor_pins(["nvidia-cuda", "microsoft"])
        self.assertIn("Package: *\nPin: origin developer.download.nvidia.com\nPin-Priority: -1\n", pins)
        self.assertIn("Package: *\nPin: origin packages.microsoft.com\nPin-Priority: -1\n", pins)
        cuda, microsoft = m.VENDORS["nvidia-cuda"]["packages"], m.VENDORS["microsoft"]["packages"]
        for name in ("cuda-cudart-12-8", "libcublas-12-8", "libcufft-12-8", "cuda-nvcc-13-0", "libcublas-dev-12-8",
                     "cuda-toolkit-12-8-config-common", "cuda-toolkit-12-config-common", "cuda-toolkit-config-common"):
            self.assertRegex(name, cuda)
        for name in ("cuda-drivers", "cuda-drivers-570", "cuda-compat-12-8", "cuda-12-8", "cuda-runtime-12-8",
                     "cuda-toolkit-12-8", "cuda-keyring", "nvidia-driver", "nvidia-kernel-open-dkms", "libnvidia-compute-570",
                     "libcuda1", "linux-image-amd64", "nvidia-open"):
            self.assertNotRegex(name, cuda)
        for name in ("dotnet-sdk-10.0", "dotnet-runtime-10.0", "aspnetcore-runtime-10.0", "dotnet-host",
                     "netstandard-targeting-pack-2.1"):
            self.assertRegex(name, microsoft)
        for name in ("packages-microsoft-prod", "powershell", "moby-engine", "mssql-server"):
            self.assertNotRegex(name, microsoft)

    def test_vendor_packages_keep_installed_series_and_components(self):
        ii = {"status": "ii", "version": "12.8.93-1", "source": "x", "arch": "amd64"}
        packages = {name: ii for name in ("cuda-cudart-12-8", "libcublas-12-8", "libcufft-12-8", "cuda-nvcc-12-8",
                                          "dotnet-sdk-10.0", "packages-microsoft-prod")}
        nvidia = "        500 https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64  Packages\n"
        policy = m.parse_policy(
            "cuda-crt-12-8:\n  Installed: (none)\n  Candidate: 12.8.93-1\n  Version table:\n     12.8.93-1 500\n" + nvidia +
            "cuda-nvcc-13-0:\n  Installed: (none)\n  Candidate: 13.0.88-1\n  Version table:\n     13.0.88-1 500\n" + nvidia +
            "cuda-drivers:\n  Installed: (none)\n  Candidate: 580.1-1\n  Version table:\n     580.1-1 500\n" + nvidia +
            "packages-microsoft-prod:\n  Installed: 1.0-debian12.1.3\n  Candidate: 1.0-debian12.1.3\n  Version table:\n"
            "     1.1-debian13.1 -1\n        -1 https://packages.microsoft.com/debian/13/prod trixie/main amd64 Packages\n"
            " *** 1.0-debian12.1.3 100\n        100 /var/lib/dpkg/status\n"
            "libssl3t64:\n  Installed: (none)\n  Candidate: 3.5.1-1\n  Version table:\n     3.5.1-1 500\n"
            "        500 https://deb.debian.org/debian trixie/main amd64 Packages\n")
        # A pinned-out vendor package keeps its build; it is neither foreign nor obsolete.
        self.assertEqual((m.ahead_packages(policy), m.obsolete_packages(policy)), ([], []))
        plan = m.parse_plan("Inst cuda-crt-12-8 (12.8.93-1 NVIDIA CUDA:/ [amd64])\n"
                            "Inst cuda-nvcc-13-0 (13.0.88-1 NVIDIA CUDA:/ [amd64])\n"
                            "Inst cuda-drivers (580.1-1 Debian:13.2/stable [amd64])\n"
                            "Inst libssl3t64 (3.5.1-1 Debian:13.2/stable [amd64])\nRemv cuda-cudart-12-8 [12.8.90-1]\n")
        feeds = m.plan_feeds(plan, policy)
        self.assertEqual(feeds, {"cuda-crt-12-8": "nvidia-cuda", "cuda-nvcc-13-0": "nvidia-cuda", "cuda-drivers": "nvidia-cuda"})
        # A Debian label in the plan line does not make a vendor package Debian's.
        self.assertEqual(m.origin_problems(plan, feeds), [])
        self.assertEqual([p["code"] for p in m.origin_problems(plan)], ["apt-origin", "apt-origin"])
        self.assertEqual([(p["code"], p["detail"].split()[0]) for p in m.vendor_problems(plan, packages, feeds)],
                         [("vendor-package", "cuda-drivers"), ("vendor-series", "cuda-nvcc-13-0"),
                          ("vendor-component-removed", "The")])
        self.assertEqual(m.vendor_series("cuda-toolkit-12-config-common"), "12")
        self.assertEqual(m.vendor_series("cuda-toolkit-12-8-config-common"), "12-8")
        self.assertIsNone(m.vendor_series("dotnet-host"))

    def test_migrated_feeds_require_their_exact_pin_file(self):
        with tempfile.TemporaryDirectory() as temp:
            parts = Path(temp, "preferences.d")
            parts.mkdir()
            with patch.object(m, "APT_PREFERENCES", (Path(temp, "preferences"), parts)), patch.object(m, "atomic") as atomic:
                self.assertEqual(m.persist_vendor_preferences({"vendors": []}), [])
                self.assertEqual(m.persist_vendor_preferences({"vendors": ["microsoft"]}), [str(parts / m.VENDOR_PINS_NAME)])
                self.assertEqual(atomic.call_args[0][1], m.vendor_pin_file(["microsoft"]))
                self.assertEqual([p["code"] for p in m.trixie_preference_problems(["microsoft"])], ["apt-preferences"])
                (parts / m.VENDOR_PINS_NAME).write_text(m.vendor_pin_file(["microsoft"]))
                (parts / "stable").write_text(m.TRIXIE_PREFERENCES)
                self.assertEqual(m.trixie_preference_problems(["microsoft"]), [])
                self.assertEqual([p["code"] for p in m.trixie_preference_problems()], ["apt-preferences"])
                self.assertEqual([p["code"] for p in m.trixie_preference_problems(["microsoft", "nvidia-cuda"])],
                                 ["apt-preferences"])
        # It sorts before the stock pin file, so its general vendor pins come first.
        self.assertLess(m.VENDOR_PINS_NAME, m.STOCK_PREFERENCES)

    def test_cuda_homes_and_links_are_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp, "cuda-12.8")
            home.mkdir()
            Path(temp, "lib").mkdir()
            try:
                Path(temp, "cuda").symlink_to(home)
            except OSError:
                self.skipTest("symbolic links are unavailable")
            self.assertEqual(m.cuda_layout(Path(temp)), {str(Path(temp, "cuda")): [str(home), os.path.realpath(home)],
                                                         str(home): "dir"})


if __name__ == "__main__":
    unittest.main()
