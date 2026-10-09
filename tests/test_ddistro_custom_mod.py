"""Focused checks for bin/ddistro_custom_mod's validation boundaries.

Runs anywhere with Python 3 and touches no distro state:
    python3 tests/test_ddistro_custom_mod.py [path/to/ExampleServer]
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest

import types

# grp/pwd exist only on Unix; the validation code under test never calls them.
for _name in ("grp", "pwd"):
    sys.modules.setdefault(_name, types.ModuleType(_name)) if os.name == "nt" else None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("ddistro_custom_mod", os.path.join(ROOT, "bin", "ddistro_custom_mod"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
mod = importlib.util.module_from_spec(_spec)
_loader.exec_module(mod)
# Never read the machine's /etc/dwemerdistro_services.conf; parse_custom_port is tested directly.
mod.CUSTOM_PORT = mod.CUSTOM_PORT_DEFAULT
EXAMPLE_SERVER = None

MANIFEST = {
    "schema_version": 1, "id": "example-server", "name": "Example AI Server", "description": "d",
    "dashboard_path": "ui/", "health_path": "health.php",
    "requirements": {"php": "8.2", "postgresql": True},
    "setup": {"config_template": "config/config.example.php", "config_path": "config/config.php",
              "token_placeholder": "CHANGE_ME_TOKEN", "database_placeholder": "example_ai_mod",
              "migrate_script": "scripts/migrate.php"},
}
TREE = [("100644", "blob", p) for p in ("dwemer-mod.json", "health.php", "config/config.example.php",
                                         "scripts/migrate.php", "ui/index.php")]


def manifest(**changes):
    data = json.loads(json.dumps(MANIFEST))
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return json.dumps(data)


class UrlTests(unittest.TestCase):
    def test_accepts_plain_https(self):
        for url in ("https://github.com/owner/repo", "https://github.com/owner/repo.git",
                    "https://gitlab.com/group/sub/repo/"):
            self.assertEqual(mod.validate_repository_url(url), url)

    def test_rejects_unsafe(self):
        for url in ("http://github.com/o/r", "https://u:p@github.com/o/r", "https://github.com/o/r?x=1",
                    "https://github.com/o/r#x", "https://github.com:443/o/r", "https://github.com/o/r;id",
                    "https://github.com/o/$(id)", "https://github.com/o/r r", "https://github.com/o/../r",
                    "https://localhost/o/r", "https://127.0.0.1/o/r", "https://nas.local/o/r", "https://github.com",
                    "ssh://github.com/o/r", "git@github.com:o/r.git", "file:///tmp/r", "https://github.com/o/%2e",
                    "https://github.com/o/r\n", "https://github.com/o/r\x07", "-uhttps://github.com/o/r",
                    "https://GitHub.com/o/r", "https://github.com/" + "a" * 300, "--upload-pack=x"):
            with self.assertRaises(mod.ModError, msg=url):
                mod.validate_repository_url(url)


class ManifestTests(unittest.TestCase):
    def test_valid(self):
        parsed = mod.parse_manifest(manifest())
        self.assertEqual(parsed["id"], "example-server")
        self.assertEqual(mod.database_name_for(parsed["id"]), "custom_example_server")
        self.assertEqual(mod.mod_urls("example-server", parsed),
                         ("http://127.0.0.1:19000/custom-mods/example-server/ui/",
                          "http://127.0.0.1:19000/custom-mods/example-server/health.php"))

    def test_custom_port_is_read_as_bounded_data(self):
        self.assertEqual(mod.parse_custom_port("XTTS_PORT=8020\n"), 19000)
        self.assertEqual(mod.parse_custom_port('CUSTOM_MODS_PORT=19000\n# CUSTOM_MODS_PORT=1\nCUSTOM_MODS_PORT="19999"\n'),
                         19999)
        for bad in ("18999", "20000", "8081", "", "19000 x", "$(id)", "19000; id", "'19001'", '"19001', "1e4"):
            with self.assertRaises(mod.ModError, msg=bad):
                mod.parse_custom_port("CUSTOM_MODS_PORT=" + bad)
        with self.assertRaises(mod.ModError):
            mod.parse_custom_port("export CUSTOM_MODS_PORT=19001")

    def test_web_site_listens_only_on_loopback_port(self):
        text = mod.web_site_text(19042)
        self.assertIn("Listen 127.0.0.1:19042\n", text)
        self.assertTrue(mod.config_uses_port(text, 19042))
        self.assertFalse(mod.config_uses_port(text, 1904))
        self.assertTrue(mod.config_uses_port("Listen 0.0.0.0:19042", 19042))
        self.assertTrue(mod.config_uses_port("<VirtualHost *:19042>", 19042))
        self.assertFalse(mod.config_uses_port("Listen 8081\n<VirtualHost *:8081>", 19042))

    def test_rejects_bad_ids(self):
        for bad in ("ab", "Example", "-abc", "abc-", "a--b", "a_b", "../x", "herika", "stobe", "custom-mods",
                    "a" * 33, "example-test\n", 7, None):
            with self.assertRaises(mod.ModError, msg=bad):
                mod.parse_manifest(manifest(id=bad))

    def test_rejects_schema_and_unknown_fields(self):
        for raw in (manifest(schema_version=2), manifest(schema_version=True), manifest(schema_version=None),
                    manifest(install_command="bash install.sh"), manifest(project_url="http://x.example/a"),
                    manifest(requirements={"php": "8.2", "postgresql": True, "root": True}),
                    "[]", "{", b"\xff"):
            with self.assertRaises(mod.ModError, msg=str(raw)[:60]):
                mod.parse_manifest(raw)

    def test_rejects_bad_relative_paths(self):
        for bad in ("/etc/passwd", "../x.php", "a/../b.php", ".git/config", "ui/.htaccess", "a//b.php",
                    "a\\b.php", "http://evil.example/", "", "ui/%2e%2e/", "~/x.php", "a b.php", "health.php\n", "x" * 201):
            with self.assertRaises(mod.ModError, msg=bad):
                mod.parse_manifest(manifest(health_path=bad))
            with self.assertRaises(mod.ModError, msg=bad):
                mod.parse_manifest(manifest(dashboard_path=bad))
        setup = dict(MANIFEST["setup"], config_path="../../etc/cron.d/x.php")
        with self.assertRaises(mod.ModError):
            mod.parse_manifest(manifest(setup=setup))
        setup = dict(MANIFEST["setup"], migrate_script="install.sh")
        with self.assertRaises(mod.ModError):
            mod.parse_manifest(manifest(setup=setup))
        setup = dict(MANIFEST["setup"], token_placeholder="CHANGE_ME_TOKEN\n")
        with self.assertRaises(mod.ModError):
            mod.parse_manifest(manifest(setup=setup))

    def test_optional_assets(self):
        self.assertEqual(mod.parse_manifest(manifest())["assets"], {})
        parsed = mod.parse_manifest(manifest(assets={"icon": "ui/images/icon.png", "banner": "ui/b.JPEG"}))
        self.assertEqual(parsed["assets"], {"icon": "ui/images/icon.png", "banner": "ui/b.JPEG"})
        self.assertEqual(mod.parse_manifest(manifest(assets={"banner": "b.jpg"}))["assets"], {"banner": "b.jpg"})
        for bad in ([], "icon.png", {"logo": "a.png"}, {"icon": "https://evil.example/a.png"},
                    {"icon": "../a.png"}, {"icon": "/etc/a.png"}, {"icon": "ui/.a.png"}, {"icon": "a.gif"},
                    {"icon": "a.svg"}, {"icon": "config/config.php"}, {"banner": "a.png.php"}, {"icon": 7}):
            with self.assertRaises(mod.ModError, msg=bad):
                mod.parse_manifest(manifest(assets=bad))


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.manifest = mod.parse_manifest(manifest())

    def test_valid_tree(self):
        self.assertIn("health.php", mod.validate_tree(TREE, self.manifest))

    def test_rejects_symlinks_submodules_private_paths(self):
        for extra in (("120000", "blob", "ui/link"), ("160000", "commit", "vendor/x"),
                      ("100644", "blob", "config/config.php"), ("100644", "blob", "config/config.php/x"),
                      ("100644", "blob", "sub/.git/config"), ("100644", "blob", "a/../b")):
            with self.assertRaises(mod.ModError, msg=extra):
                mod.validate_tree(TREE + [extra], self.manifest)

    def test_assets_must_be_tracked_regular_files(self):
        with_assets = mod.parse_manifest(manifest(assets={"icon": "ui/icon.png"}))
        self.assertIn("ui/icon.png", mod.validate_tree(TREE + [("100644", "blob", "ui/icon.png")], with_assets))
        for tree in (TREE, TREE + [("120000", "blob", "ui/icon.png")], TREE + [("040000", "tree", "ui/icon.png")]):
            with self.assertRaises(mod.ModError):
                mod.validate_tree(tree, with_assets)

    def test_requires_setup_files(self):
        with self.assertRaises(mod.ModError):
            mod.validate_tree([e for e in TREE if e[2] != "scripts/migrate.php"], self.manifest)

    def test_parse_ls_tree(self):
        raw = b"100644 blob abc\thealth.php\x00120000 blob def\tui/link\x00"
        self.assertEqual(mod.parse_ls_tree(raw), [("100644", "blob", "health.php"), ("120000", "blob", "ui/link")])


class ConfigTests(unittest.TestCase):
    TEMPLATE = "<?php\nreturn ['token' => 'CHANGE_ME_TOKEN', 'database' => ['name' => 'example_ai_mod']];\n"

    def test_render(self):
        text = mod.render_config(self.TEMPLATE, MANIFEST["setup"], "f" * 48, "custom_example_server")
        self.assertIn("'" + "f" * 48 + "'", text)
        self.assertIn("'custom_example_server'", text)
        self.assertNotIn("CHANGE_ME", text)

    def test_rejects_ambiguous_templates(self):
        for template in (self.TEMPLATE.replace("'CHANGE_ME_TOKEN'", "CHANGE_ME_TOKEN"),
                         self.TEMPLATE + "// CHANGE_ME_TOKEN\n",
                         self.TEMPLATE.replace("<?php", "")):
            with self.assertRaises(mod.ModError):
                mod.render_config(template, MANIFEST["setup"], "f" * 48, "custom_example_server")


class BranchAndRedactionTests(unittest.TestCase):
    def test_default_branch_from_symref(self):
        self.assertEqual(mod.parse_default_branch("ref: refs/heads/trunk\tHEAD\nabc\tHEAD\n"), "trunk")
        self.assertEqual(mod.parse_default_branch("ref: refs/heads/codex/simple-ai-example\tHEAD\n"),
                         "codex/simple-ai-example")
        for bad in ("abc\tHEAD\n", "ref: refs/heads/-x\tHEAD\n", "ref: refs/heads/a..b\tHEAD\n", ""):
            with self.assertRaises(mod.ModError):
                mod.parse_default_branch(bad)
        self.assertIsNone(mod.BRANCH_PATTERN.match("main\n"))
        self.assertIsNone(mod.SHA_PATTERN.match("a" * 40 + "\n"))

    def test_redaction(self):
        mod.SECRETS_TO_REDACT.append("s3cr3t-token-value")
        try:
            self.assertNotIn("s3cr3t", mod.redact("failed with s3cr3t-token-value"))
            self.assertNotIn("hunter2", mod.redact("password=hunter2"))
        finally:
            mod.SECRETS_TO_REDACT.remove("s3cr3t-token-value")


TAB, NL = chr(9), chr(10)


class BranchPolicyTests(unittest.TestCase):
    HEADS = {"main": "a" * 40, "dev": "b" * 40, "unstable": "c" * 40}

    def test_optional_branches_section(self):
        self.assertIsNone(mod.parse_manifest(manifest())["branches"])
        parsed = mod.parse_manifest(manifest(branches={"default": "main", "allowed": ["main", "dev", "unstable"]}))
        self.assertEqual(parsed["branches"], {"default": "main", "allowed": ["main", "dev", "unstable"]})
        for bad in ({"default": "dev", "allowed": ["main"]}, {"default": "main"}, {"default": "main", "allowed": []},
                    {"default": "main", "allowed": ["main", "main"]}, {"default": "main", "allowed": "main"},
                    {"default": "main", "allowed": ["main", "../x"]}, {"default": "main", "allowed": ["main", "-x"]},
                    {"default": "main", "allowed": ["main", "a..b"]}, {"default": "main", "allowed": ["main", "x.lock"]},
                    {"default": "main", "allowed": ["main", "x y"]}, {"default": "main", "allowed": ["main", 3]},
                    {"default": "main", "allowed": ["main"], "extra": 1},
                    {"default": "main", "allowed": ["b%d" % i for i in range(11)]}, ["main"]):
            with self.assertRaises(mod.ModError, msg=bad):
                mod.parse_manifest(manifest(branches=bad))

    def test_remote_refs_and_policy_from_default_branch(self):
        output = ("ref: refs/heads/main" + TAB + "HEAD" + NL + "a" * 40 + TAB + "HEAD" + NL +
                  "".join(sha + TAB + "refs/heads/" + name + NL for name, sha in self.HEADS.items()))
        head, heads = mod.parse_remote_refs(output)
        self.assertEqual((head, heads), ("main", self.HEADS))
        # Missing section: the repository default branch alone (backward compatible).
        self.assertEqual(mod.branch_policy(mod.parse_manifest(manifest()), "main", heads),
                         {"default": "main", "allowed": ["main"]})
        authored = mod.parse_manifest(manifest(branches={"default": "dev", "allowed": ["main", "dev"]}))
        self.assertEqual(mod.branch_policy(authored, "main", heads), {"default": "dev", "allowed": ["main", "dev"]})
        missing = mod.parse_manifest(manifest(branches={"default": "main", "allowed": ["main", "gone"]}))
        with self.assertRaises(mod.ModError):
            mod.branch_policy(missing, "main", heads)

    def test_stored_policy_is_offline_and_backward_compatible(self):
        self.assertEqual(mod.stored_policy({"branch": "trunk"}), {"default": "trunk", "allowed": ["trunk"]})
        self.assertEqual(mod.stored_policy({"branch": "dev", "branches": {"default": "main", "allowed": ["main"]}}),
                         {"default": "main", "allowed": ["main"]})
        self.assertEqual(mod.stored_policy({"branch": "dev", "branches": {"default": "x", "allowed": ["main"]}}),
                         {"default": "dev", "allowed": ["dev"]})


class IncomingCollisionTests(unittest.TestCase):
    def test_untracked_or_ignored_files_block_update(self):
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "config"))
            with open(os.path.join(folder, "config", "config.php"), "w") as handle:
                handle.write("<?php return [];")
            with open(os.path.join(folder, "notes"), "w") as handle:
                handle.write("x")
            found = mod.incoming_collisions(folder, {"config/config.php", "notes/readme.md", "new/file.php"})
            self.assertEqual(found, ["config/config.php", "notes/readme.md"])


class CliTests(unittest.TestCase):
    def test_argument_parsing(self):
        self.assertEqual(mod.parse_args(["update", "example-server"])["id"], "example-server")
        switch = mod.parse_args(["switch", "example-server", "--branch", "dev", "--expect-commit", "a" * 40])
        self.assertEqual((switch["branch"], switch["expect_commit"]), ("dev", "a" * 40))
        self.assertEqual(mod.parse_args(["branches", "example-server", "--json"])["id"], "example-server")
        for argv in (["update", "example-server", "--force"], ["update", "../x"], ["update"],
                     ["install", "https://github.com/o/r", "https://github.com/o/s"],
                     ["update", "example-server", "--local-source", "/tmp/x"], ["purge", "example-server"],
                     ["check", "https://github.com/o/r", "--expect-commit", "a" * 40],
                     ["update", "example-server", "--branch", "dev"], ["switch", "example-server", "--branch", "dev"],
                     ["switch", "example-server", "--expect-commit", "a" * 40], ["check", "https://github.com/o/r",
                                                                                 "--branch", "dev"]):
            with self.assertRaises(mod.ModError, msg=argv):
                mod.parse_args(argv)

    def test_local_source_requires_root(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaises((mod.ModError, AttributeError)):
            mod.resolve_source({"local_source": "/tmp/x.bundle"})


class RegistryLifecycleTests(unittest.TestCase):
    """Registry, tombstone, and lock-identity logic against a temporary state root (no root, no DB)."""

    SOURCE = "https://github.com/owner/example-server"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = self.temp.name.replace("\\", "/")
        self.saved = {name: getattr(mod, name) for name in
                      ("STATE_ROOT", "REGISTRY_ROOT", "BACKUP_ROOT", "MOD_ROOT", "LOCK_PATH")}
        mod.STATE_ROOT = root + "/state"
        mod.REGISTRY_ROOT = mod.STATE_ROOT + "/registry"
        mod.BACKUP_ROOT = mod.STATE_ROOT + "/backups"
        mod.MOD_ROOT = root + "/custom-mods"
        mod.LOCK_PATH = root + "/lock.d"
        self.chown = getattr(os, "chown", None)
        os.chown = lambda *args, **kwargs: None

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(mod, name, value)
        if self.chown is None:
            del os.chown
        else:
            os.chown = self.chown
        self.temp.cleanup()

    def entry(self, state="ready"):
        parsed = mod.parse_manifest(manifest())
        return {"schema_version": 1, "id": "example-server", "name": "Example AI Server", "description": "",
                "project_url": "", "repository": self.SOURCE, "local_source": False, "branch": "main",
                "commit": "a" * 40, "folder": mod.MOD_ROOT + "/example-server", "database": "custom_example_server",
                "dashboard_path": "ui/", "health_path": "health.php", "setup": parsed["setup"], "state": state,
                "message": "", "installed_at": "", "updated_at": ""}

    def test_unregister_hides_entry_and_same_source_is_restorable(self):
        mod.write_entry(self.entry())
        mod.write_entry(mod.unregistered_entry(self.entry()))
        self.assertEqual(mod.list_entries(), [])
        with self.assertRaises(mod.ModError):
            mod.read_entry("example-server")
        tombstone = mod.read_entry("example-server", include_unregistered=True)
        self.assertEqual(tombstone["unregistered_from"], "ready")
        self.assertIsNone(mod.tombstone_problem(tombstone, "example-server"))
        self.assertEqual(mod.registry_state("example-server", self.SOURCE, False)[0], "restorable")
        self.assertEqual(mod.registry_state("example-server", "https://github.com/other/fork", False)[0], "conflict")
        self.assertEqual(mod.registry_state("example-server", self.SOURCE, True)[0], "conflict")
        preview = mod.preview_document(mod.parse_manifest(manifest()), "main", "b" * 40, self.SOURCE, False,
                                       {"default": "main", "allowed": ["main"]})
        self.assertEqual((preview["registered"], preview["restorable"], preview["installed_commit"]),
                         (False, True, "a" * 40))
        if os.name != "nt":
            self.assertEqual(os.stat(mod.registry_path("example-server")).st_mode & 0o777, 0o644)

    def test_restore_keeps_installed_version_and_update_stays_explicit(self):
        tombstone = mod.unregistered_entry(self.entry())
        restored = mod.restored_entry(tombstone, "a" * 40, "b" * 40, setup_present=True)
        self.assertEqual((restored["state"], restored["commit"]), ("ready", "a" * 40))
        self.assertIn("choose Update", restored["message"])
        self.assertNotIn("unregistered_from", restored)
        self.assertEqual(restored["setup"], tombstone["setup"])
        same = mod.restored_entry(tombstone, "a" * 40, "a" * 40, setup_present=True)
        self.assertNotIn("newer", same["message"])
        missing = mod.restored_entry(tombstone, "a" * 40, "a" * 40, setup_present=False)
        self.assertEqual(missing["state"], "failed")
        failed = mod.restored_entry(mod.unregistered_entry(self.entry("installing")), "c" * 40, "c" * 40, True)
        self.assertEqual((failed["state"], failed["commit"]), ("failed", "c" * 40))

    def test_tampered_tombstone_is_not_restorable(self):
        for key, value in (("folder", "/var/www/html/HerikaServer"), ("database", "dwemer"),
                           ("commit", "HEAD"), ("unregistered_from", "installing"), ("setup", {})):
            tombstone = mod.unregistered_entry(self.entry())
            tombstone[key] = value
            self.assertIsNotNone(mod.tombstone_problem(tombstone, "example-server"), key)
        self.assertIsNotNone(mod.tombstone_problem(self.entry(), "example-server"))

    def install_with(self, state_setup):
        calls = []
        saved = (mod.require_root, mod.acquire_lock, mod.inspect_repository, mod.restore_entry,
                 mod.ensure_mod_root, mod.require_existing_role, mod.database_exists)
        mod.require_root = mod.acquire_lock = mod.ensure_mod_root = mod.require_existing_role = lambda: None
        policy = {"default": "main", "allowed": ["main"]}
        mod.inspect_repository = lambda source, local: (mod.parse_manifest(manifest()), "main", "b" * 40, policy)
        mod.restore_entry = lambda entry, manifest_, commit, policy_: calls.append((entry["id"], commit))
        mod.database_exists = lambda name: False
        try:
            state_setup()
            mod.command_install({"url": self.SOURCE, "expect_commit": "b" * 40})
        finally:
            (mod.require_root, mod.acquire_lock, mod.inspect_repository, mod.restore_entry,
             mod.ensure_mod_root, mod.require_existing_role, mod.database_exists) = saved
        return calls

    def test_install_restores_only_own_tombstone(self):
        mod.write_entry(mod.unregistered_entry(self.entry()))
        self.assertEqual(self.install_with(lambda: None), [("example-server", "b" * 40)])

    def test_install_rejects_registered_conflicting_and_unrelated_existing(self):
        def other_source():
            tombstone = mod.unregistered_entry(self.entry())
            tombstone["repository"] = "https://github.com/other/fork"
            mod.write_entry(tombstone)
        for setup, text in ((lambda: mod.write_entry(self.entry()), "already registered"),
                            (other_source, "different repository"),
                            (lambda: os.makedirs(mod.MOD_ROOT + "/example-server"), "already exists")):
            with self.subTest(text):
                with self.assertRaises(mod.ModError) as caught:
                    self.install_with(setup)
                self.assertIn(text, str(caught.exception))
                for name in os.listdir(mod.REGISTRY_ROOT) if os.path.isdir(mod.REGISTRY_ROOT) else []:
                    os.unlink(os.path.join(mod.REGISTRY_ROOT, name))

    def test_stale_operation_needs_attention_but_live_owner_stays_busy(self):
        entry = self.entry("updating")
        self.assertEqual(mod.status_entry(entry, False)["state"], "failed")
        os.makedirs(mod.LOCK_PATH)
        with open(mod.LOCK_PATH + "/pid", "w") as handle:
            handle.write(f"{os.getpid()}\n")
        # Another live lock holder (official server operation) does not make this entry busy.
        entry["operation_pid"] = os.getpid() + 1
        self.assertEqual(mod.status_entry(entry, False)["state"], "failed")
        entry["operation_pid"] = os.getpid()
        self.assertEqual(mod.status_entry(entry, False)["state"], "updating")
        self.assertEqual(mod.status_entry(self.entry("ready"), False)["state"], "ready")

    def switch_with(self, failure, migrated=False, healthy=True, reviewed="d" * 40):
        """Run command_switch with the network, git, and setup steps replaced."""
        entry = dict(self.entry(), branches={"default": "main", "allowed": ["main", "dev"]})
        mod.write_entry(entry)
        git_calls = []
        names = ("require_root", "acquire_lock", "checked_folder", "inspect_repository", "update_checkout",
                 "git", "apply_permissions", "check_health")
        saved = {name: getattr(mod, name) for name in names}

        def update_checkout(entry_, folder, source, local, branch, progress, expect_commit=None):
            self.assertEqual((branch, expect_commit), ("dev", "d" * 40))
            if failure == "refused":
                raise mod.ModError("Files tracked by the mod were changed locally. Nothing was updated.")
            progress["changed"] = True
            progress["migrated"] = migrated
            entry_.update(branch=branch, commit="d" * 40)
            if failure:
                raise mod.ModError("The mod's database migration failed.")

        mod.require_root = mod.acquire_lock = mod.apply_permissions = lambda *a, **k: None
        mod.checked_folder = lambda e: mod.MOD_ROOT + "/example-server"
        mod.inspect_repository = lambda source, local, branch=None, policy_only=False: (
            mod.parse_manifest(manifest()), branch, "d" * 40, {"default": "main", "allowed": ["main", "dev"]})
        mod.update_checkout = update_checkout
        mod.git = lambda args, **kwargs: git_calls.append(args)
        mod.check_health = lambda mod_id, manifest_: (healthy, "")
        error = None
        try:
            mod.command_switch({"id": "example-server", "branch": "dev", "expect_commit": reviewed})
        except mod.ModError as caught:
            error = caught
        finally:
            for name, value in saved.items():
                setattr(mod, name, value)
        return error, mod.read_entry("example-server"), git_calls

    def test_switch_success_records_new_branch(self):
        error, entry, _ = self.switch_with(None)
        self.assertIsNone(error)
        self.assertEqual((entry["branch"], entry["commit"], entry["state"]), ("dev", "d" * 40, "ready"))
        self.assertNotIn("operation_pid", entry)

    def test_switch_refusals_change_nothing(self):
        error, entry, calls = self.switch_with("refused")
        self.assertIsNotNone(error)
        self.assertEqual((entry["branch"], entry["commit"], entry["state"]), ("main", "a" * 40, "ready"))
        self.assertIn("refused", entry["message"])
        self.assertEqual(calls, [])
        error, entry, _ = self.switch_with(None, reviewed="e" * 40)
        self.assertIn("changed after it was reviewed", str(error))
        self.assertEqual((entry["branch"], entry["state"]), ("main", "ready"))

    def test_failed_switch_returns_code_and_never_claims_database_rollback(self):
        error, entry, calls = self.switch_with("migration", migrated=True)
        self.assertIsNotNone(error)
        self.assertEqual(calls, [["checkout", "--quiet", "-B", "main", "a" * 40]])
        self.assertEqual((entry["branch"], entry["commit"], entry["state"]), ("main", "a" * 40, "ready"))
        self.assertIn("returned to branch main", entry["message"])
        self.assertIn("not rolled back", entry["message"])
        _, entry, _ = self.switch_with("migration", migrated=True, healthy=False)
        self.assertEqual(entry["state"], "failed")

    def checkout_with(self, head="a" * 40, current="main", dest_exists=False, dest_ancestor=True,
                      checkout_fails=False):
        """Run update_checkout as a switch to dev with git answering from the given folder state."""
        calls = []

        class Result:
            def __init__(self, code=0, out=b""):
                self.returncode, self.stdout = code, out

        def git(args, **kwargs):
            calls.append(args)
            if args[0] == "rev-parse":
                return Result(out=head.encode() + b"\n")
            if args[0] == "symbolic-ref":
                return Result(out=current.encode() + b"\n")
            if args[0] == "show-ref":
                return Result(0 if dest_exists else 1)
            if args[0] == "merge-base":
                return Result(0 if dest_ancestor else 1)
            if args[0] == "checkout" and checkout_fails:
                raise mod.ModError("Could not switch branches.")
            return Result()

        names = ("git", "backup", "read_commit_manifest", "apply_permissions", "write_config_if_missing",
                 "database_exists", "database_owner", "run_setup_checks", "check_health", "setup_web")
        saved = {name: getattr(mod, name) for name in names}
        mod.git = git
        mod.backup = mod.apply_permissions = mod.write_config_if_missing = mod.run_setup_checks = lambda *a: None
        mod.read_commit_manifest = lambda folder, ref: (mod.parse_manifest(manifest()), set(), "", "d" * 40)
        mod.database_exists = lambda name: True
        mod.database_owner = lambda name: mod.DATABASE_ROLE
        mod.check_health = lambda mod_id, manifest_: (True, "")
        mod.setup_web = lambda: None
        progress = {"changed": False}
        error = None
        try:
            mod.update_checkout(self.entry(), mod.MOD_ROOT + "/example-server", self.SOURCE, False, "dev",
                                progress, expect_commit="d" * 40)
        except mod.ModError as caught:
            error = caught
        finally:
            for name, value in saved.items():
                setattr(mod, name, value)
        return error, progress, [args[0] for args in calls]

    def test_switch_refuses_unregistered_head_or_divergent_destination(self):
        for state in ({"head": "b" * 40}, {"current": "other"}, {"current": ""}):
            error, progress, calls = self.checkout_with(**state)
            self.assertIn("not at its registered branch and commit", str(error), state)
            self.assertNotIn("fetch", calls)
            self.assertNotIn("checkout", calls)
            self.assertFalse(progress["changed"])
        error, progress, calls = self.checkout_with(dest_exists=True, dest_ancestor=False)
        self.assertIn("local commits on branch dev", str(error))
        self.assertNotIn("checkout", calls)
        self.assertFalse(progress["changed"])
        error, progress, calls = self.checkout_with(dest_exists=True, dest_ancestor=True)
        self.assertIsNone(error)
        self.assertIn("checkout", calls)
        self.assertTrue(progress["changed"])

    def test_failed_switch_checkout_is_not_a_change(self):
        error, progress, calls = self.checkout_with(checkout_fails=True)
        self.assertIn("Could not switch branches", str(error))
        self.assertFalse(progress["changed"])

    def test_status_asset_urls_use_fixed_route_for_installed_files(self):
        folder = mod.MOD_ROOT + "/example-server"
        os.makedirs(folder + "/ui")
        with open(folder + "/ui/icon.png", "wb") as handle:
            handle.write(b"\x89PNG")
        old = self.entry()
        self.assertEqual((mod.status_entry(old, False)["icon_url"], mod.status_entry(old, False)["banner_url"]), ("", ""))
        entry = dict(old, assets={"icon": "ui/icon.png", "banner": "ui/missing.jpg"})
        document = mod.status_entry(entry, False)
        self.assertEqual(document["icon_url"], "http://127.0.0.1:19000/custom-mods/example-server/ui/icon.png")
        self.assertEqual(document["banner_url"], "")
        for tampered in ({"icon": "https://evil.example/a.png"}, {"icon": "../x/ui/icon.png"}, "ui/icon.png"):
            self.assertEqual(mod.status_entry(dict(old, assets=tampered), False)["icon_url"], "", tampered)

    def test_permission_failures_propagate(self):
        folder = mod.MOD_ROOT + "/example-server"
        os.makedirs(folder)
        saved = mod.run
        results = []

        class Result:
            def __init__(self, code):
                self.returncode = code
        try:
            mod.run = lambda command, **kwargs: results.append(command) or Result(1 if len(results) == 2 else 0)
            with self.assertRaises(mod.ModError):
                mod.apply_permissions(folder)
            self.assertEqual(len(results), 2)
            mod.run = lambda command, **kwargs: Result(0)
            mod.apply_permissions(folder)
            with self.assertRaises(mod.ModError):
                mod.apply_permissions(mod.MOD_ROOT)
            with self.assertRaises(mod.ModError):
                mod.apply_permissions(folder + "\n")
            saved_islink = os.path.islink
            os.path.islink = lambda path: path == mod.MOD_ROOT
            try:
                with self.assertRaises(mod.ModError):
                    mod.apply_permissions(folder)
            finally:
                os.path.islink = saved_islink
        finally:
            mod.run = saved


class ExampleServerContractTests(unittest.TestCase):
    def test_example_server_manifest_and_template(self):
        if not EXAMPLE_SERVER:
            self.skipTest("ExampleServer path not given")
        with open(os.path.join(EXAMPLE_SERVER, "dwemer-mod.json"), "rb") as handle:
            parsed = mod.parse_manifest(handle.read())
        with open(os.path.join(EXAMPLE_SERVER, parsed["setup"]["config_template"]), "rb") as handle:
            text = mod.render_config(handle.read(), parsed["setup"], "e" * 48, mod.database_name_for(parsed["id"]))
        self.assertIn("'custom_example_server'", text)
        for path in [parsed["health_path"]] + list(parsed["assets"].values()):
            self.assertTrue(os.path.isfile(os.path.join(EXAMPLE_SERVER, path)), path)
        self.assertTrue(os.path.isfile(os.path.join(EXAMPLE_SERVER, parsed["setup"]["migrate_script"])))


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        EXAMPLE_SERVER = sys.argv.pop(1)
    unittest.main(verbosity=1)
