#!/usr/bin/env python3
"""Release-package support for ddistro_custom_mod; no repository code runs as root."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import urllib.parse
import urllib.request
import zipfile

MAX_DESCRIPTOR = 4 * 1024 * 1024
MAX_PACKAGE = 512 * 1024 * 1024
MAX_FILES = 20000
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
GITHUB = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def relative(path):
    if not isinstance(path, str) or not path or len(path) > 200 or "\\" in path:
        raise ValueError("Package paths must be relative POSIX paths.")
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", p) for p in path.split("/")):
        raise ValueError("Package paths cannot contain hidden or traversal segments.")
    if any(p.endswith(".") or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", p.split(".")[0]) for p in path.split("/")):
        raise ValueError("Package paths cannot contain Windows device names or trailing dots.")
    return path


def validate_distribution(manifest):
    features = manifest.get("features", [])
    if not isinstance(features, list) or len(features) > 16 or any(not isinstance(f, str) or not f.strip() or len(f) > 120 or any(ord(c) < 32 for c in f) for f in features):
        raise ValueError("features must list at most 16 short gameplay descriptions.")
    testing = manifest.get("testing", {})
    if not isinstance(testing, dict) or set(testing) - {"summary", "evidence_path"}:
        raise ValueError("testing may contain summary and a repository-relative evidence_path.")
    if "summary" in testing and (not isinstance(testing["summary"], str) or len(testing["summary"]) > 500):
        raise ValueError("testing.summary must be author-reported text of at most 500 characters.")
    if "evidence_path" in testing:
        relative(testing["evidence_path"])
    distribution = manifest.get("distribution")
    game = manifest.get("game")
    if not isinstance(distribution, dict) or set(distribution) != {"server", "client"}:
        raise ValueError("Version 2 distribution needs server and client rules.")
    if not isinstance(game, dict) or set(game) - {"id", "name", "executable", "steam_app_id", "version", "loader", "loader_path", "executable_sha256", "enable_in_game"}:
        raise ValueError("Version 2 game metadata contains unsupported fields.")
    for key in ("id", "name", "executable"):
        if not isinstance(game.get(key), str) or not game[key] or len(game[key]) > 200:
            raise ValueError("Game id, name and executable are required.")
    relative(game["executable"])
    if game.get("loader_path"):
        relative(game["loader_path"])
    if game.get("executable_sha256") and not SHA256.fullmatch(game["executable_sha256"]):
        raise ValueError("game.executable_sha256 must be a SHA-256 digest.")
    if game.get("steam_app_id") is not None and (type(game["steam_app_id"]) is not int or game["steam_app_id"] <= 0):
        raise ValueError("game.steam_app_id must be a positive integer.")
    for spec in distribution.values():
        if not isinstance(spec, dict) or set(spec) - {"root", "include", "exclude", "install"}:
            raise ValueError("Package rules contain unsupported fields.")
        root = spec.get("root", ".")
        if not isinstance(root, str):
            raise ValueError("Package root must be a relative directory.")
        root = root.rstrip("/")
        if root != ".":
            relative(root)
        for key in ("include", "exclude"):
            patterns = spec.get(key, [] if key == "exclude" else None)
            if not isinstance(patterns, list) or len(patterns) > 64 or (key == "include" and not patterns):
                raise ValueError("Package include/exclude lists must contain at most 64 patterns.")
            for pattern in patterns:
                if not isinstance(pattern, str) or len(pattern) > 200 or any(
                        p in ("", ".", "..") or not re.fullmatch(r"[A-Za-z0-9_.*?][A-Za-z0-9._*?-]*", p)
                        for p in pattern.split("/")):
                    raise ValueError("Unsupported package glob.")
    install = distribution["client"].get("install", {})
    if not isinstance(install, dict) or set(install) != {"profile", "path", "config_path"} or install["profile"] != "game-directory":
        raise ValueError("Client install must use the supported game-directory profile.")
    relative(install["path"])
    relative(install["config_path"])
    if not install["config_path"].endswith(".json"):
        raise ValueError("Client config_path must be JSON.")
    if not manifest.get("setup", {}).get("config_path", "").startswith("config/"):
        raise ValueError("Release private config must live beneath the protected config/ directory.")


def inventory(package):
    files = package.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError("Release package needs 1-20,000 file receipts.")
    result = {}
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise ValueError("Invalid release file receipt.")
        path = relative(item["path"])
        if not SHA256.fullmatch(str(item["sha256"])) or type(item["size"]) is not int or item["size"] < 0:
            raise ValueError("Invalid release file digest or size.")
        total += item["size"]
        result[path] = item
    folded = {p.casefold() for p in result}
    if len(result) != len(files) or len(folded) != len(files) or total > MAX_PACKAGE:
        raise ValueError("Package has duplicate paths or exceeds 512 MiB.")
    if any('/'.join(p.split('/')[:i]).casefold() in folded for p in result for i in range(1, len(p.split('/')))):
        raise ValueError("Package contains a file/directory collision.")
    return result


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or parsed.hostname not in {"github.com", "api.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"} or parsed.port not in (None, 443):
            raise ValueError("Release download redirected outside GitHub.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def open_url(url):
    return urllib.request.build_opener(GitHubRedirect()).open(
        urllib.request.Request(url, headers={"User-Agent": "DwemerDistro-Mod-Releases", "Accept": "application/vnd.github+json"}), timeout=60)


def api(path):
    with open_url("https://api.github.com/" + path) as response:
        raw = response.read(MAX_DESCRIPTOR + 1)
    if len(raw) > MAX_DESCRIPTOR:
        raise ValueError("GitHub metadata is too large.")
    return json.loads(raw)


def digest_file(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inspect_release(m, args):
    local = args.get("local_release")
    assets = {}
    if local:
        m.require_root()
        path = os.path.abspath(local)
        if path != local or m.path_has_linked_part(path) or not os.path.isfile(path):
            raise ValueError("--local-release needs a plain absolute release descriptor path.")
        with open(path, "rb") as handle:
            raw = handle.read(MAX_DESCRIPTOR + 1)
        base = os.path.dirname(path)
        release_id = 0
    else:
        repository = m.validate_repository_url(args.get("url", ""))
        match = GITHUB.fullmatch(repository)
        if not match:
            raise ValueError("Release discovery currently supports public GitHub repositories.")
        owner, repo = match.groups()
        suffix = "latest"
        if args.get("tag"):
            if not TAG.fullmatch(args["tag"]):
                raise ValueError("Unsupported release tag.")
            suffix = "tags/" + args["tag"]
        release = api(f"repos/{owner}/{repo}/releases/{suffix}")
        if release.get("draft") or release.get("prerelease") or not TAG.fullmatch(release.get("tag_name", "")):
            raise ValueError("Choose a published stable release.")
        tag = release["tag_name"]
        if args.get("tag") and args["tag"] != tag:
            raise ValueError("GitHub returned a different release tag.")
        for asset in release.get("assets", []):
            expected_url = f"https://github.com/{owner}/{repo}/releases/download/{tag}/{asset['name']}"
            if asset.get("browser_download_url") == expected_url and asset.get("state") == "uploaded":
                assets[asset["name"]] = asset
        if "kagrenac-release.json" not in assets:
            raise ValueError("This release has no kagrenac-release.json package descriptor.")
        with open_url(assets["kagrenac-release.json"]["browser_download_url"]) as response:
            raw = response.read(MAX_DESCRIPTOR + 1)
        base = None
        release_id = release["id"]
    if len(raw) > MAX_DESCRIPTOR:
        raise ValueError("Release descriptor exceeds 4 MiB.")
    document = json.loads(raw)
    if not isinstance(document, dict) or set(document) != {"schema_version", "repository", "commit", "version", "manifest", "packages"} or type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("Unsupported release descriptor.")
    if not GITHUB.fullmatch(document["repository"]) or not m.SHA_PATTERN.fullmatch(document["commit"]) or not TAG.fullmatch(document["version"]):
        raise ValueError("Invalid release identity.")
    if not local:
        if document["repository"].lower().rstrip("/") != repository.removesuffix(".git").lower().rstrip("/") or document["version"] != tag:
            raise ValueError("Release repository or version does not match GitHub.")
        ref = api(f"repos/{owner}/{repo}/git/ref/tags/{tag}")["object"]
        for _ in range(4):
            if ref["type"] != "tag":
                break
            ref = api(f"repos/{owner}/{repo}/git/tags/{ref['sha']}")["object"]
        if ref["type"] != "commit" or ref["sha"] != document["commit"]:
            raise ValueError("Release descriptor is not bound to the published tag's commit.")
    manifest = m.parse_manifest(json.dumps(document["manifest"]))
    if manifest["schema_version"] != 2 or manifest.get("project_url", "").rstrip("/") != document["repository"]:
        raise ValueError("Release manifest must use version 2 and the same repository.")
    packages = document["packages"]
    if not isinstance(packages, dict) or set(packages) != {"server", "client"}:
        raise ValueError("Release must contain matching client and server packages.")
    for kind, package in packages.items():
        if not isinstance(package, dict) or set(package) != {"asset", "sha256", "size", "files"}:
            raise ValueError("Invalid release package metadata.")
        relative(package["asset"])
        if "/" in package["asset"] or not package["asset"].endswith(".zip") or not SHA256.fullmatch(str(package["sha256"])):
            raise ValueError("Packages must name SHA-256 pinned ZIP release assets.")
        if type(package["size"]) is not int or not 0 < package["size"] <= MAX_PACKAGE:
            raise ValueError("Package exceeds 512 MiB.")
        paths = inventory(package)
        private = manifest["setup"]["config_path"] if kind == "server" else manifest["distribution"]["client"]["install"]["config_path"]
        if any(p.casefold() == private.casefold() or p.casefold().startswith(private.casefold() + "/") or private.casefold().startswith(p.casefold() + "/") or p.casefold() == private.casefold() + ".lock" or
               p.lower().endswith((".pem", ".key", ".pfx", ".dump")) or p.split("/")[-1].lower() in {"config.json", "credentials.json", "secrets.json"}
               for p in paths):
            raise ValueError("Release package includes private configuration or runtime credentials.")
        if not local:
            asset = assets.get(package["asset"], {})
            if asset.get("size") != package["size"] or asset.get("digest") not in (None, "sha256:" + package["sha256"]):
                raise ValueError("Release package metadata differs from its GitHub asset.")
    if packages["server"]["asset"] == packages["client"]["asset"]:
        raise ValueError("Server and client packages must be separate assets.")
    return document, manifest, hashlib.sha256(raw).hexdigest(), base, assets, release_id


def unpack(m, document, manifest, base, assets, target):
    package = document["packages"]["server"]
    archive_path = os.path.join(target, "server.zip")
    if base:
        source = os.path.join(base, package["asset"])
        if m.path_has_linked_part(source) or not os.path.isfile(source):
            raise ValueError("Local release asset is missing or linked.")
        shutil.copyfile(source, archive_path)
    else:
        with open_url(assets[package["asset"]]["browser_download_url"]) as response, open(archive_path, "xb") as output:
            count = 0
            while chunk := response.read(65536):
                count += len(chunk)
                if count > package["size"]:
                    raise ValueError("Release download exceeds its declared size.")
                output.write(chunk)
    if os.path.getsize(archive_path) != package["size"] or digest_file(archive_path) != package["sha256"]:
        raise ValueError("Server package digest does not match the checked release.")
    files = inventory(package)
    stage = os.path.join(target, "payload")
    os.mkdir(stage, 0o755)
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) != len(files) or len({e.filename for e in entries}) != len(entries) or {e.filename for e in entries} != set(files):
            raise ValueError("ZIP entries differ from the checked file receipts.")
        for entry in entries:
            item = files[entry.filename]
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)) or entry.file_size != item["size"]:
                raise ValueError("ZIP contains a link, special file or incorrect size.")
            path = os.path.join(stage, relative(entry.filename))
            os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
            with archive.open(entry) as source, open(path, "xb") as output:
                shutil.copyfileobj(source, output, 65536)
            os.chmod(path, 0o644)
            if digest_file(path) != item["sha256"]:
                raise ValueError("A packaged file failed its SHA-256 check.")
    raw = Path(stage, "dwemer-mod.json").read_bytes()
    if json.loads(raw) != document["manifest"]:
        raise ValueError("Packaged manifest differs from the checked descriptor.")
    m.validate_tree([("100644", "blob", p) for p in files], manifest)
    template = Path(stage, manifest["setup"]["config_template"]).read_text(encoding="utf-8")
    m.render_config(template, manifest["setup"], "validation_only_token", "validation_only_database")
    return stage, template


def verify_files(folder, old_files, new_files=None, interrupted=False):
    new_files = new_files or {}
    for path in set(old_files) | set(new_files):
        relative(path)
        target = Path(folder, path)
        cursor = target
        while cursor != Path(folder):
            if cursor.is_symlink():
                raise ValueError("Installed package path contains a link: " + path)
            cursor = cursor.parent
        if not target.exists():
            if path in old_files and not interrupted:
                raise ValueError("A packaged file was removed locally: " + path)
            continue
        if path not in old_files and not interrupted:
            raise ValueError("Package would overwrite an unowned file: " + path)
        allowed = {old_files[path]["sha256"]} if path in old_files else set()
        if interrupted and path in new_files:
            allowed.add(new_files[path]["sha256"])
        if not target.is_file() or digest_file(target) not in allowed:
            raise ValueError("A packaged file was edited locally: " + path)


def apply_payload(stage, folder, receipt):
    # Invoked only as dwemer. A raced destination link cannot grant root writes.
    record = json.loads(Path(receipt).read_text(encoding="utf-8"))
    old, new = record["old"], record["new"]
    verify_files(folder, old, new, record["interrupted"])
    for path in sorted(set(old) - set(new), reverse=True):
        target = Path(folder, relative(path))
        if target.exists():
            target.unlink()
    for path, item in new.items():
        target = Path(folder, relative(path))
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o2750)
        source = Path(stage, path)
        if digest_file(source) != item["sha256"]:
            raise ValueError("Staged payload changed.")
        temp = target.with_name(target.name + ".release-" + str(os.getpid()))
        with open(source, "rb") as inp, open(temp, "xb") as output:
            shutil.copyfileobj(inp, output, 65536)
        os.chmod(temp, 0o640)
        os.replace(temp, target)


def prepare_config_access(m, folder, manifest):
    """Prepare web/CLI access before migration; retain contents and an existing lock's inode."""
    relative_path = manifest["setup"]["config_path"]
    parent = os.path.dirname(os.path.join(folder, relative_path))
    if m.path_has_linked_part(parent) or not os.path.isdir(parent):
        raise ValueError("The private config directory is missing or linked.")
    os.chown(parent, m.pwd_uid(m.WEB_GROUP), m.group_id(m.WEB_GROUP))
    os.chmod(parent, 0o2770)
    for path in (os.path.join(folder, relative_path), os.path.join(folder, relative_path) + ".lock"):
        if not os.path.lexists(path):
            continue
        if os.path.islink(path) or not os.path.isfile(path):
            raise ValueError("The private config/lock must be regular files.")
        os.chown(path, m.pwd_uid(m.WEB_GROUP), m.group_id(m.WEB_GROUP), follow_symlinks=False)
        os.chmod(path, 0o660)


def command(manager, args):
    m = SimpleNamespace(**manager)
    try:
        if args["command"] == "release-pair":
            # Private stdout goes only to the launcher's ACL-protected config writer, never its log.
            m.require_root()
            entry = m.read_entry(args["id"])
            if entry.get("install_mode") != "release" or entry.get("state") != "ready":
                raise ValueError("Pairing requires an installed, healthy release server.")
            folder = m.checked_folder(entry)
            path = entry["setup"]["config_path"]
            if m.path_has_links(folder, path):
                raise ValueError("Private config path contains a link.")
            script = ('$c=require $argv[1]; if(!is_array($c)||!is_string($c["token"]??null)||strlen($c["token"])<16){exit(1);}'
                      'echo json_encode(["server_url"=>$argv[2],"token"=>$c["token"],"session_id"=>$argv[3],'
                      '"allowed_actions"=>$c["allowed_actions"]??[],"voice_enabled"=>(bool)($c["tts"]["enabled"]??false)]);')
            result = m.run(["php", "-r", script, os.path.join(folder, path),
                            m.route_base(entry["id"], entry.get("server_folder", "")), entry["id"]], user=m.APP_USER)
            if result.returncode:
                raise ValueError("Could not read this server's pairing configuration.")
            sys.stdout.buffer.write(result.stdout)
            return
        if args["command"] == "release-check":
            doc, manifest, pin, _, _, _ = inspect_release(m, args)
            print(json.dumps({"schema_version": 1, "release_hash": pin, "release": doc,
                              "folder": m.entry_folder({"id": manifest["id"], "server_folder": manifest["server_folder"]})}))
            return
        m.require_root()
        m.acquire_lock()
        m.require_php_security()
        m.read_protection_lists()
        previous = None
        if args["command"] == "release-update":
            previous = m.read_entry(args["id"])
            if previous.get("install_mode") != "release":
                raise ValueError("This is a branch installation; use its existing Update command.")
            args = dict(args, url=previous["repository"])
            if previous.get("pending_release") and not args.get("tag"):
                args["tag"] = previous["pending_release"]["tag"]
        doc, manifest, pin, base, assets, release_id = inspect_release(m, args)
        if pin != args.get("expect_release") or doc["commit"] != args.get("expect_commit"):
            raise ValueError("Release changed after preview; check it again.")
        mod_id = manifest["id"]
        if not previous:
            state, found = m.registry_state(mod_id, doc["repository"], False)
            if state != "none":
                if found and found.get("install_mode") == "release" and found["repository"] == doc["repository"]:
                    previous = dict(found)
                    previous.pop("unregistered_from", None)
                else:
                    raise ValueError("Mod id is already claimed; no existing installation is adopted.")
        if previous and (previous["id"] != mod_id or previous["repository"] != doc["repository"] or
                         previous["setup"] != manifest["setup"] or previous.get("server_folder", "") != manifest["server_folder"]):
            raise ValueError("Release identity, server folder or private configuration contract changed.")
        pending = previous.get("pending_release") if previous else None
        if pending and pending["descriptor_sha256"] != pin:
            raise ValueError("Retry the interrupted release before installing another version.")
        folder = m.checked_folder(previous) if previous else m.entry_folder({"id": mod_id, "server_folder": manifest["server_folder"]})
        files = inventory(doc["packages"]["server"])
        old_files = previous.get("files", {}) if previous else {}
        if previous:
            verify_files(folder, old_files, files, bool(pending))
            if m.database_exists(previous["database"]) and m.database_owner(previous["database"]) != m.DATABASE_ROLE:
                raise ValueError("The installed database owner differs from its registered role.")
        else:
            if manifest["server_folder"]:
                m.require_safe_web_root()
                if m.server_folder_claims(manifest["server_folder"], mod_id):
                    raise ValueError("Server folder is already claimed.")
            elif not os.path.isdir(m.MOD_ROOT):
                m.ensure_mod_root()
            if os.path.lexists(folder) or m.database_exists(m.database_name_for(mod_id)):
                raise ValueError("Server folder or database already exists; nothing is adopted.")
        with tempfile.TemporaryDirectory(prefix="ddistro-release-") as temporary:
            os.chmod(temporary, 0o755)
            stage, template = unpack(m, doc, manifest, base, assets, temporary)
            if previous:
                m.backup(previous)
                entry = dict(previous)
            else:
                if manifest["server_folder"]:
                    m.protect_root(manifest["server_folder"])
                else:
                    m.protect_project(mod_id)
                m.setup_web()
                if manifest["server_folder"]:
                    m.create_direct_root(folder)
                else:
                    os.mkdir(folder, 0o750)
                    os.chown(folder, m.pwd_uid(m.APP_USER), m.group_id(m.WEB_GROUP))
                entry = {"id": mod_id, "repository": doc["repository"], "local_source": False,
                         "folder": folder, "server_folder": manifest["server_folder"],
                         "database": m.database_name_for(mod_id), "setup": manifest["setup"],
                         "installed_at": m.now_utc(), "files": {}}
            release_record = {"tag": doc["version"], "descriptor_sha256": pin, "github_release_id": release_id}
            entry.update(install_mode="release", state="updating" if previous else "installing", operation_pid=os.getpid(),
                         pending_release=release_record, name=manifest["name"], description=manifest["description"],
                         project_url=manifest["project_url"], dashboard_path=manifest["dashboard_path"],
                         health_path=manifest["health_path"], assets=manifest["assets"], updated_at=m.now_utc())
            m.write_entry(entry)
            receipt = os.path.join(temporary, "apply.json")
            Path(receipt).write_text(json.dumps({"old": old_files, "new": files, "interrupted": bool(pending)}), encoding="utf-8")
            os.chmod(receipt, 0o644)
            try:
                result = m.run(["python3", os.path.abspath(__file__), "--apply", stage, folder, receipt], user=m.APP_USER, timeout=900)
                if result.returncode:
                    raise ValueError("Package files could not be applied. Retry the same release; private data was kept.")
                if not m.database_exists(entry["database"]):
                    m.create_database(entry["database"])
                m.write_config_if_missing(folder, manifest, template, mod_id)
                prepare_config_access(m, folder, manifest)
                m.run_setup_checks(folder, manifest)
                m.setup_web()
                healthy, message = m.check_health(mod_id, manifest)
                if not healthy:
                    raise ValueError(message)
            except (ValueError, m.ModError) as error:
                entry.pop("operation_pid", None)
                entry.update(state="failed", message=str(error))
                m.write_entry(entry)
                raise
            entry.pop("operation_pid", None)
            entry.pop("pending_release", None)
            entry.update(state="ready", message="Server installed; install/pair the matching game client.", commit=doc["commit"],
                         branch="", files=files, release=release_record, updated_at=m.now_utc())
            m.write_entry(entry)
            m.ok(manifest["name"] + " server release " + doc["version"] + " installed.")
    except (ValueError, KeyError, TypeError, OSError, RecursionError, zipfile.BadZipFile) as error:
        raise m.ModError(str(error))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--apply" and os.geteuid() != 0:
        try:
            apply_payload(*sys.argv[2:])
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
            sys.exit(1)
    else:
        sys.exit("Use ddistro_custom_mod release-check/install/update.")
