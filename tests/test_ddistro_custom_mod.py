"""Focused checks for bin/ddistro_custom_mod's validation boundaries.

Runs anywhere with Python 3 and touches no distro state:
    python3 tests/test_ddistro_custom_mod.py [path/to/ExampleServer]
"""
import importlib.machinery
import importlib.util
import json
import os
import stat
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


def rules_deny(rules, url):
    """(LocationMatch denies, DirectoryMatch/FilesMatch denies) for a URL under the (source, project, URL) rules."""
    import posixpath
    import re
    source_dir, project_dir, url_pattern = rules
    path = "/var/www/html" + url
    by_dir = re.search(source_dir, path) or re.search(source_dir, posixpath.dirname(path))
    by_file = re.search(project_dir, posixpath.dirname(path)) and re.search(
        "^(" + mod.PROJECT_PRIVATE_FILES + ")$", posixpath.basename(path))
    return bool(re.search(url_pattern, url)), bool(by_dir or by_file)


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

    def assert_rules_rendered(self, text, rules):
        source_dir, project_dir, url_pattern = rules
        vhost, grant = text.index("<VirtualHost"), text.index("\t\tRequire all granted")
        self.assertLess(text.index(f'<DirectoryMatch "{source_dir}">\n\tRequire all denied'), vhost)
        self.assertLess(text.index(f'<DirectoryMatch "{project_dir}">\n\t<FilesMatch "^(config\\.json|\\.env)$">\n'
                                   "\t\tRequire all denied"), vhost)
        self.assertGreater(text.index(f'<LocationMatch "{url_pattern}">\n\t\tRequire all denied'), grant)

    def test_web_site_denies_git_and_kagrenac_source_folders(self):
        import re
        text = mod.web_site_text(19042)
        vhost = text.index("<VirtualHost")
        self.assertLess(text.index(f'<DirectoryMatch "{mod.GIT_DENY_DIR}">\n\tRequire all denied'), vhost)
        grant = text.index("\t\tRequire all granted")
        self.assertGreater(text.index(f'<LocationMatch "{mod.GIT_DENY_URL}">\n\t\tRequire all denied'), grant)
        known = (mod.SOURCE_DENY_DIR, mod.PRIVATE_DENY_DIR, mod.SOURCE_DENY_URL)
        self.assert_rules_rendered(text, known)
        self.assertEqual(text.count("<DirectoryMatch"), 3)
        self.assertEqual(text.count("<LocationMatch"), 3)
        git_urls = ["/custom-mods/any-mod/.git/HEAD", "/custom-mods/any-mod/.git", "/ExampleServer/.git/config",
                    "/custom-mods/any-mod/sub/.git/HEAD", "//custom-mods//any-mod//.git/HEAD"]
        for url in git_urls:
            self.assertTrue(re.search(mod.GIT_DENY_URL, url), url)
        for url in git_urls[:4]:
            self.assertTrue(re.search(mod.GIT_DENY_DIR, "/var/www/html" + url), url)
        # Every known Kagrenac route hides all source folders and the private files at any depth.
        for root in ("/custom-mods/example-server", "/custom-mods/KagrenacServer", "/ExampleServer"):
            for url in [f"{root}/{d}/x" for d in ("client", "ports", "docs", "config", "lib", "sql", "scripts")] + [
                    f"{root}/docs", f"{root}/client/", f"{root}/ports/x/build/a.dll", f"{root}/config/config.php",
                    f"{root}/scripts/migrate.php", f"{root}/config.json", f"{root}/.env", f"{root}/ui/config.json",
                    f"{root}/a/b/.env"]:
                self.assertEqual(rules_deny(known, url), (True, True), url)
            for url in (f"{root}/health.php", f"{root}/ui/", f"{root}/ui/index.php", f"{root}/assets/icon.png",
                        f"{root}/clients.php", f"{root}/libs/x.js", f"{root}/config.json.php", f"{root}/.envrc",
                        f"{root}/api/x.php"):
                self.assertEqual(rules_deny(known, url), (False, False), url)
        self.assertTrue(re.search(mod.SOURCE_DENY_URL, "//custom-mods//KagrenacServer//sql//x.sql"))
        served_urls = ["/custom-mods/any-mod/health.php", "/custom-mods/any-mod/client/x.php",
                       "/custom-mods/any-mod/docs/readme.txt", "/custom-mods/any-mod/config.json",
                       "/custom-mods/any-mod/.env", "/custom-mods/any-mod/lib/x", "/custom-mods/kagrenacserver/sql/x",
                       "/custom-mods/KagrenacServer2/lib/x", "/custom-mods/example-server2/config/x",
                       "/ExampleServer2/config.json", "/custom-mods/KagrenacServer/.gitignore"]
        for url in served_urls:
            self.assertEqual(rules_deny(known, url), (False, False), url)
            self.assertFalse(re.search(mod.GIT_DENY_URL, url) or re.search(mod.GIT_DENY_DIR, "/var/www/html" + url),
                             url)

    def test_opted_in_projects_get_scoped_denials(self):
        import re
        self.assertEqual(mod.web_site_text(19042, []), mod.web_site_text(19042))
        text = mod.web_site_text(19042, ["kag-proj", "kag-two"])
        # The fixed rules are still rendered unchanged, once.
        for pattern in (mod.GIT_DENY_DIR, mod.SOURCE_DENY_DIR, mod.PRIVATE_DENY_DIR, mod.GIT_DENY_URL,
                        mod.SOURCE_DENY_URL):
            self.assertEqual(text.count(f'"{pattern}"'), 1, pattern)
        rules = mod.project_deny_rules(["kag-proj", "kag-two"])
        url_pattern = rules[2]
        self.assertIn(r"(kag\-proj|kag\-two)", url_pattern)
        self.assert_rules_rendered(text, rules)
        # Opting in only adds the project's own blocks.
        added = (mod.deny_dir_text(*rules[:2]),
                 f'\t<LocationMatch "{url_pattern}">\n\t\tRequire all denied\n\t</LocationMatch>\n')
        self.assertEqual(text.replace(added[0], "", 1).replace(added[1], "", 1), mod.web_site_text(19042))

        def denied(url):
            return rules_deny(rules, url)

        for url in ["/custom-mods/kag-proj/" + d + "/x" for d in ("client", "ports", "docs", "config", "lib", "sql",
                                                                   "scripts")] + [
                "/custom-mods/kag-two/config/config.php", "/custom-mods/kag-proj/config.json",
                "/custom-mods/kag-proj/.env", "/custom-mods/kag-proj/client/config.json",
                "/custom-mods/kag-proj/sub/.env"]:
            self.assertEqual(denied(url), (True, True), url)
        self.assertTrue(re.search(url_pattern, "//custom-mods//kag-proj//lib//x.php"))
        for url in ("/custom-mods/kag-proj/health.php", "/custom-mods/kag-proj/ui/", "/custom-mods/kag-proj/api/x.php",
                    "/custom-mods/kag-proj/assets/icon.png", "/custom-mods/kag-proj/clients.php",
                    "/custom-mods/kag-proj/config.json.php", "/custom-mods/kag-proj/libs/x.js",
                    "/custom-mods/kag-proj2/lib/x", "/custom-mods/xkag-proj/config/x", "/custom-mods/any-mod/lib/x",
                    "/custom-mods/any-mod/config.json", "/custom-mods/any-mod/.env", "/custom-mods/any-mod/client/x.php",
                    "/custom-mods/example-server/scripts/migrate.php"):
            self.assertEqual(denied(url), (False, False), url)
        for bad in (["KagrenacServer"], ["herika"], ["a|.*"], ["kag-proj)|(.*"]):
            with self.assertRaises(mod.ModError, msg=bad):
                mod.web_site_text(19042, bad)

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
        self.assertEqual(mod.parse_args(["update", "example-server", "--expect-commit", "a" * 40])["expect_commit"],
                         "a" * 40)
        self.assertEqual(mod.parse_args(["setup-web", "--protect-project", "kag-proj"])["protect_project"], "kag-proj")
        self.assertNotIn("protect_project", mod.parse_args(["setup-web"]))
        for argv in (["setup-web", "--protect-project"], ["setup-web", "--protect-project", "KagrenacServer"],
                     ["setup-web", "--protect-project", "herika"], ["setup-web", "--protect-project", "../x"],
                     ["setup-web", "--protect-project", "a$(id)b"], ["setup-web", "--protect-project", ""],
                     ["setup-web", "--protect-project", "kag-proj", "--protect-project", "kag-two"],
                     ["setup-web", "kag-proj"], ["install", "https://github.com/o/r", "--protect-project", "kag-proj"],
                     ["update", "example-server", "--protect-project", "kag-proj"],
                     ["update", "example-server", "--branch", "dev", "--expect-commit", "a" * 40]):
            with self.assertRaises(mod.ModError, msg=argv):
                mod.parse_args(argv)
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
                      ("STATE_ROOT", "REGISTRY_ROOT", "BACKUP_ROOT", "MOD_ROOT", "LOCK_PATH", "PROTECTED_PROJECTS",
                       "APACHE_DIR")}
        mod.STATE_ROOT = root + "/state"
        mod.REGISTRY_ROOT = mod.STATE_ROOT + "/registry"
        mod.BACKUP_ROOT = mod.STATE_ROOT + "/backups"
        mod.PROTECTED_PROJECTS = mod.STATE_ROOT + "/protected-projects"
        mod.APACHE_DIR = root + "/apache2"
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
                 mod.ensure_mod_root, mod.require_existing_role, mod.database_exists, mod.require_php_security,
                 mod.read_protected_projects)
        mod.require_root = mod.acquire_lock = mod.ensure_mod_root = mod.require_existing_role = lambda: None
        mod.require_php_security = lambda: calls.append("php-security")
        mod.read_protected_projects = lambda: []
        policy = {"default": "main", "allowed": ["main"]}
        mod.inspect_repository = lambda source, local: (mod.parse_manifest(manifest()), "main", "b" * 40, policy)
        mod.restore_entry = lambda entry, manifest_, commit, policy_: calls.append((entry["id"], commit))
        mod.database_exists = lambda name: False
        try:
            state_setup()
            mod.command_install({"url": self.SOURCE, "expect_commit": "b" * 40})
        finally:
            (mod.require_root, mod.acquire_lock, mod.inspect_repository, mod.restore_entry,
             mod.ensure_mod_root, mod.require_existing_role, mod.database_exists, mod.require_php_security,
             mod.read_protected_projects) = saved
        return calls

    def test_install_restores_only_own_tombstone(self):
        mod.write_entry(mod.unregistered_entry(self.entry()))
        self.assertEqual(self.install_with(lambda: None), ["php-security", ("example-server", "b" * 40)])

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
                 "git", "apply_permissions", "check_health", "require_php_security", "read_protected_projects")
        saved = {name: getattr(mod, name) for name in names}

        def update_checkout(entry_, folder, source, local, branch, progress, expect_commit=None, switching=False):
            self.assertEqual((branch, expect_commit, switching), ("dev", "d" * 40, True))
            if failure == "refused":
                raise mod.ModError("Files tracked by the mod were changed locally. Nothing was updated.")
            progress["changed"] = True
            progress["migrated"] = migrated
            entry_.update(branch=branch, commit="d" * 40)
            if failure:
                raise mod.ModError("The mod's database migration failed.")

        mod.require_root = mod.acquire_lock = mod.apply_permissions = mod.require_php_security = lambda *a, **k: None
        mod.read_protected_projects = lambda: []
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
                      checkout_fails=False, switching=True, pin="d" * 40, setup_fails=False):
        """Run update_checkout (a switch to dev, or an update of main) with git answering from the given state.

        The fetched commit is always d*40; the fourth result is the full git argument lists.
        """
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
        if setup_fails:
            mod.run_setup_checks = lambda *a: (_ for _ in ()).throw(mod.ModError("The mod's database migration failed."))
        mod.read_commit_manifest = lambda folder, ref: (mod.parse_manifest(manifest()), set(), "", "d" * 40)
        mod.database_exists = lambda name: True
        mod.database_owner = lambda name: mod.DATABASE_ROLE
        mod.check_health = lambda mod_id, manifest_: (True, "")
        mod.setup_web = lambda: None
        progress = {"changed": False}
        error = None
        self.checked_entry = self.entry()
        try:
            mod.update_checkout(self.checked_entry, mod.MOD_ROOT + "/example-server", self.SOURCE, False,
                                "dev" if switching else "main", progress, expect_commit=pin, switching=switching)
        except mod.ModError as caught:
            error = caught
        finally:
            for name, value in saved.items():
                setattr(mod, name, value)
        self.full_calls = calls
        return error, progress, [args[0] for args in calls]

    def test_pinned_update_installs_only_the_reviewed_fast_forward(self):
        error, progress, calls = self.checkout_with(switching=False)
        self.assertIsNone(error)
        self.assertIn(["merge", "--ff-only", "--quiet", "d" * 40], self.full_calls)
        self.assertNotIn("checkout", calls)
        self.assertTrue(progress["changed"])
        # A different reviewed commit is refused before anything in the checkout changes.
        error, progress, calls = self.checkout_with(switching=False, pin="e" * 40)
        self.assertIn("changed after it was reviewed", str(error))
        self.assertNotIn("merge", calls)
        self.assertNotIn("merge-base", calls)
        self.assertFalse(progress["changed"])
        # The pin never permits divergence: a non-fast-forward stays refused even when it matches.
        for pin in ("d" * 40, None):
            error, progress, calls = self.checkout_with(switching=False, pin=pin, dest_ancestor=False)
            self.assertIn("not a fast-forward", str(error), pin)
            self.assertNotIn("merge", calls)
            self.assertNotIn("checkout", calls)
            self.assertFalse(progress["changed"])
        # A pinned update needs the checkout on the registered branch and commit before backup or fetch.
        for state in ({"head": "b" * 40}, {"current": "other"}, {"current": ""}):
            error, progress, calls = self.checkout_with(switching=False, **state)
            self.assertIn("not at its registered branch and commit", str(error), state)
            self.assertNotIn("fetch", calls)
            self.assertNotIn("merge", calls)
            self.assertFalse(progress["changed"])
        # Without a pin, update keeps its earlier behavior: no drift check, fast-forward only.
        error, progress, calls = self.checkout_with(switching=False, pin=None, head="b" * 40, current="other")
        self.assertIsNone(error)
        self.assertNotIn("symbolic-ref", calls)
        self.assertIn(["merge", "--ff-only", "--quiet", "d" * 40], self.full_calls)
        # A failure after the fast-forward leaves the registry naming the checked-out commit, so a pinned retry runs.
        error, progress, _ = self.checkout_with(switching=False, setup_fails=True)
        self.assertIn("migration failed", str(error))
        self.assertTrue(progress["changed"])
        self.assertEqual((self.checked_entry["branch"], self.checked_entry["commit"]), ("main", "d" * 40))
        # A switch still requires its reviewed commit.
        error, progress, calls = self.checkout_with(pin=None)
        self.assertIn("needs the reviewed commit", str(error))
        self.assertEqual(calls, [])

    def test_update_passes_pin_and_refusal_keeps_mod_ready(self):
        mod.write_entry(self.entry())
        seen = []
        names = ("require_root", "acquire_lock", "checked_folder", "refresh_policy", "update_checkout",
                 "read_protected_projects", "require_php_security")
        saved = {name: getattr(mod, name) for name in names}

        def update_checkout(entry_, folder, source, local, branch, progress, expect_commit=None, switching=False):
            seen.append((branch, expect_commit, switching))
            raise mod.ModError("Branch main changed after it was reviewed. Nothing was changed.")

        mod.require_root = mod.acquire_lock = mod.refresh_policy = mod.require_php_security = lambda *a, **k: None
        mod.checked_folder = lambda e: mod.MOD_ROOT + "/example-server"
        mod.update_checkout = update_checkout

        def damaged():
            raise mod.ModError("protected-projects is not a private root-owned list (0600). Nothing was changed.")
        try:
            # A damaged protection list stops update before the registry, checkout, config or database change.
            mod.read_protected_projects = damaged
            with self.assertRaises(mod.ModError) as caught:
                mod.command_update({"id": "example-server", "expect_commit": "e" * 40})
            self.assertIn("private root-owned list", str(caught.exception))
            self.assertEqual(seen, [])
            self.assertEqual(mod.read_entry("example-server"), self.entry())
            mod.read_protected_projects = lambda: []
            with self.assertRaises(mod.ModError):
                mod.command_update({"id": "example-server", "expect_commit": "e" * 40})
            for bad in ("E" * 40, "e" * 39, "HEAD", ""):
                with self.assertRaises(mod.ModError, msg=bad):
                    mod.command_update({"id": "example-server", "expect_commit": bad})
        finally:
            for name, value in saved.items():
                setattr(mod, name, value)
        self.assertEqual(seen, [("main", "e" * 40, False)])
        entry = mod.read_entry("example-server")
        self.assertEqual((entry["state"], entry["commit"]), ("ready", "a" * 40))
        self.assertIn("refused", entry["message"])

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

    def as_root_owner(self, files=True):
        """Report temp paths (and opened files) as root-owned, as they are when the manager runs as root."""
        real_lstat, real_fstat, root = os.lstat, os.fstat, self.temp.name

        def rooted(s):
            return os.stat_result((s.st_mode, s.st_ino, s.st_dev, s.st_nlink, 0, 0, s.st_size, 0, 0, 0))
        os.lstat = lambda path, *a, **k: (rooted(real_lstat(path, *a, **k)) if str(path).startswith(root)
                                          else real_lstat(path, *a, **k))
        self.addCleanup(setattr, os, "lstat", real_lstat)
        if files:
            os.fstat = lambda fd: rooted(real_fstat(fd))
            self.addCleanup(setattr, os, "fstat", real_fstat)

    def test_protected_projects_persist_privately_and_fail_closed(self):
        if os.name == "nt":
            self.skipTest("needs POSIX modes and O_NOFOLLOW")
        self.assertEqual(mod.read_protected_projects(), [])
        self.as_root_owner()
        # Order only (no crash is simulated): file data synced, then renamed, then the folder synced.
        events, real_fsync, real_replace = [], os.fsync, os.replace

        def fsync(fd):
            events.append("sync-folder" if stat.S_ISDIR(os.fstat(fd).st_mode) else "sync-file")
            real_fsync(fd)

        def replace(*args):
            events.append("rename")
            real_replace(*args)
        os.fsync, os.replace = fsync, replace
        try:
            mod.protect_project("kag-proj")
        finally:
            os.fsync, os.replace = real_fsync, real_replace
        self.assertEqual(events, ["sync-file", "rename", "sync-folder"])
        mod.protect_project("abc-proj")
        mod.protect_project("kag-proj")
        self.assertEqual(mod.read_protected_projects(), ["abc-proj", "kag-proj"])
        self.assertEqual(os.stat(mod.PROTECTED_PROJECTS).st_mode & 0o777, 0o600)
        self.assertNotIn("protected-projects", os.listdir(mod.REGISTRY_ROOT))
        mod.write_entry(self.entry())
        self.assertEqual([e["id"] for e in mod.list_entries()], ["example-server"])
        # Unregistering a mod never drops its protection.
        mod.write_entry(mod.unregistered_entry(self.entry()))
        self.assertEqual(mod.read_protected_projects(), ["abc-proj", "kag-proj"])
        os.chmod(mod.PROTECTED_PROJECTS, 0o644)
        with self.assertRaises(mod.ModError):
            mod.read_protected_projects()
        for content in ("", "kag-proj", "kag-proj\nkag-proj\n", "KagrenacServer\n", "herika\n", "\n", "a|.*\n",
                        "kag-proj\n\n", "".join("proj-%02d\n" % i for i in range(33)), "kag-pr\xf6j\n"):
            with open(mod.PROTECTED_PROJECTS, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.chmod(mod.PROTECTED_PROJECTS, 0o600)
            with self.assertRaises(mod.ModError, msg=content[:40]):
                mod.read_protected_projects()
        with self.assertRaises(mod.ModError):
            mod.protect_project("new-proj")
        os.unlink(mod.PROTECTED_PROJECTS)
        target = mod.STATE_ROOT + "/elsewhere"
        with open(target, "w") as handle:
            handle.write("kag-proj\n")
        os.chmod(target, 0o600)
        os.symlink(target, mod.PROTECTED_PROJECTS)
        with self.assertRaises(mod.ModError):
            mod.read_protected_projects()
        os.unlink(mod.PROTECTED_PROJECTS)
        os.chmod(mod.STATE_ROOT, 0o775)
        with self.assertRaises(mod.ModError):
            mod.read_protected_projects()
        os.chmod(mod.STATE_ROOT, 0o755)
        # A full list refuses a new id and keeps the saved ones.
        with open(mod.PROTECTED_PROJECTS, "w") as handle:
            handle.write("".join("proj-%02d\n" % i for i in range(32)))
        os.chmod(mod.PROTECTED_PROJECTS, 0o600)
        with self.assertRaises(mod.ModError):
            mod.protect_project("kag-proj")
        self.assertEqual(len(mod.read_protected_projects()), 32)

    def test_protected_list_must_be_root_owned(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("needs POSIX modes and a non-root test user")
        os.makedirs(mod.STATE_ROOT)
        with open(mod.PROTECTED_PROJECTS, "w") as handle:
            handle.write("kag-proj\n")
        os.chmod(mod.PROTECTED_PROJECTS, 0o600)
        os.chmod(mod.STATE_ROOT, 0o755)
        # Without a root-owned folder, refused; with one, the file's real (non-root) owner is still refused.
        with self.assertRaises(mod.ModError) as caught:
            mod.read_protected_projects()
        self.assertIn("root-owned folder", str(caught.exception))
        self.as_root_owner(files=False)
        with self.assertRaises(mod.ModError) as caught:
            mod.read_protected_projects()
        self.assertIn("private root-owned list", str(caught.exception))

    def test_protected_list_fifo_is_refused_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("needs POSIX FIFOs")
        import threading
        os.makedirs(mod.STATE_ROOT)
        os.chmod(mod.STATE_ROOT, 0o755)
        os.mkfifo(mod.PROTECTED_PROJECTS, 0o600)
        self.as_root_owner()
        outcome = []

        def read():
            try:
                mod.read_protected_projects()
            except mod.ModError as error:
                outcome.append(str(error))
        # A blocking open would wait forever for a writer; the daemon thread keeps a regression from hanging the run.
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(10)
        self.assertFalse(reader.is_alive(), "opening the FIFO blocked")
        self.assertEqual(len(outcome), 1)
        self.assertIn("private root-owned list", outcome[0])
        with self.assertRaises(mod.ModError):
            mod.protect_project("kag-proj")
        self.assertTrue(stat.S_ISFIFO(os.lstat(mod.PROTECTED_PROJECTS).st_mode))

    def test_protect_project_needs_root_before_any_write(self):
        if os.name == "nt":
            self.skipTest("needs os.geteuid")
        real = os.geteuid
        os.geteuid = lambda: 1000
        try:
            with self.assertRaises(mod.ModError) as caught:
                mod.command_setup_web({"protect_project": "kag-proj"})
        finally:
            os.geteuid = real
        self.assertIn("must run as root", str(caught.exception))
        self.assertFalse(os.path.lexists(mod.STATE_ROOT))
        self.assertFalse(os.path.lexists(mod.LOCK_PATH))

    def test_failed_apply_keeps_saved_protection_and_no_partial_site(self):
        if os.name == "nt":
            self.skipTest("needs POSIX modes and O_NOFOLLOW")
        self.as_root_owner()
        for folder in ("sites-available", "sites-enabled"):
            os.makedirs(mod.APACHE_DIR + "/" + folder)
        site = mod.APACHE_DIR + "/sites-available/" + mod.WEB_SITE + ".conf"
        names = ("require_root", "acquire_lock", "apache_running", "port_free", "run", "apply_apache")
        saved = {name: getattr(mod, name) for name in names}

        class Result:
            returncode = 0

        def rejected(reload, port):
            raise mod.ModError("Apache rejected the custom mod listener on port 19000.")
        mod.require_root = mod.acquire_lock = lambda: None
        mod.apache_running = lambda: False
        mod.port_free = lambda port: True
        mod.run = lambda command, **kwargs: Result()
        mod.apply_apache = rejected
        try:
            with self.assertRaises(mod.ModError) as caught:
                mod.command_setup_web({"protect_project": "kag-proj"})
            self.assertIn("saved but not active", str(caught.exception))
            self.assertEqual(mod.read_protected_projects(), ["kag-proj"])
            self.assertFalse(os.path.lexists(site))
            mod.apply_apache = lambda reload, port: None
            # The explicit retry without the flag applies the saved list.
            mod.command_setup_web({})
            with open(site) as handle:
                text = handle.read()
            self.assertEqual(text, mod.web_site_text(19000, ["kag-proj"]))
            self.assertIn(r"(custom-mods/(kag\-proj))/(client|", text)
            self.assertEqual(mod.read_protected_projects(), ["kag-proj"])
        finally:
            for name, value in saved.items():
                setattr(mod, name, value)

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
