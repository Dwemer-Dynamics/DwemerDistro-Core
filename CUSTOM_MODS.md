# Custom mods

## Published client/server packages (manifest version 2)

Version 1 Git/branch installations keep their existing lifecycle. Version 2 adds
`game` and `distribution` metadata and installs release packages, never the whole
monorepo. Authors select runtime files before publishing; a source root such as
`server/` is stripped from the server ZIP. Health/setup/artwork paths describe the
installed server. Core's ready state proves the server only; Windows client progress
and private pairing belong to the launcher.

```bash
ddistro_custom_mod release-check https://github.com/owner/mod --json
sudo ddistro_custom_mod release-install https://github.com/owner/mod --tag v1.0.0 \
  --expect-commit <commit> --expect-release <descriptor-sha256>
sudo ddistro_custom_mod release-update my-mod --tag v1.1.0 \
  --expect-commit <commit> --expect-release <descriptor-sha256>
```

Each published stable release contains `kagrenac-release.json` and separate server/client
ZIP assets. The generated descriptor has schema_version 1, repository, exact commit,
version/tag, the full version 2 manifest, and packages.server/client. Each package has
asset, sha256, compressed size and files (relative path, sha256, unpacked size).
The GitHub tag must resolve to that commit; default-branch changes are irrelevant.
Changed preview pins, unexpected redirects or mismatched assets are refused. Prefer
immutable releases. Authors maintain only `dwemer-mod.json`; packaging generates receipts.

Version 2 game metadata: required id/name/executable; optional steam_app_id, version,
loader, loader_path, executable_sha256, enable_in_game. Distribution has server/client
selections: root (default `.`), include and exclude lists. The client also has install
{profile: "game-directory", path, config_path}. That profile places files beneath a
selected game folder and writes private Kagrenac JSON pairing. It is not evidence that
the native engine adapter works. There are no manifest shell commands or elevated hooks.
KAGRENAC's `docs/PUBLISHING.md` specifies the glob rules and publishing commands.

Optional top-level features (up to 16 short strings) and testing (summary/evidence_path)
describe gameplay and author-reported checks. Evidence paths are repository-relative,
not server paths; docs excluded from installation remain available at the pinned commit.

Catalog text comes from the manifest: `name` is the mod title, `game.name` its game and
optional top-level `author` (see [Repository manifest](#repository-manifest)) its credit.
Install, update and branch switch record `game_name` and `author` in the registry, and
`status` reports both (empty when absent). A version 1 manifest has no game, so none is
shown. Older registry entries show none until their next normal update; a field the
author removes is cleared then. Status never fetches it.

Limits: 4 MiB descriptor, 20,000 files, 512 MiB compressed/unpacked per package.
Only regular relative files are accepted; links, special files, private config,
duplicate/case-colliding paths and traversal are refused. Every ZIP member is checked
against its receipt. Files and PHP migrations are applied as dwemer, never root.
Existing ids, folders and databases are not adopted.

Registry entries mark install_mode `release`, release identity and owned file receipts.
Updates refuse local edits/collisions and back up config/database before changing code.
They remove obsolete owned files and retain private config, its lock inode and unknown
files. Interrupted operations record pending_release; retry that exact descriptor before
another version. Failed migration/health does not claim automatic database rollback.
Protection is installed before files. Unregister keeps files/database; adding the same
reviewed repository can restore its package install.

`release-pair <id>` is private launcher transport: fixed PHP runs as dwemer to read the
ready server's token/route into private stdout. Never display or log it. The launcher
writes an ACL-protected config and preserves an existing matching config byte-for-byte.
There is no provider call or game launch.

Administrator fixtures may use `release-check|release-install --local-release
/absolute/path/kagrenac-release.json`; release-update accepts it with an id. Only root
may use this mode. It validates actual local packages but does not prove GitHub
publication or gameplay. Normal players use public GitHub release APIs.

## Legacy branch installations (manifest version 1)

`bin/ddistro_custom_mod` installs and manages user-added PHP 8.2 + PostgreSQL mod
servers from public Git repositories. It is a narrow contract, not a plugin platform:
the distro owns the registry and every operation, and the launcher only calls it with
argument vectors.

```bash
ddistro_custom_mod status [--check-health] --json        # any user (launcher: dwemer)
ddistro_custom_mod check https://host/owner/repo --json   # preview only, changes nothing
ddistro_custom_mod install https://host/owner/repo --expect-commit <sha>   # root
ddistro_custom_mod update <id> [--expect-commit <sha>]    # root; stays on the installed branch
ddistro_custom_mod branches <id> [--branch <name>] --json  # root; refresh offered branches / preview one
ddistro_custom_mod switch <id> --branch <name> --expect-commit <sha>   # root
ddistro_custom_mod backup <id>                            # root
ddistro_custom_mod unregister <id>                        # root; keeps files and database
ddistro_custom_mod setup-web                              # root; shared loopback listener
ddistro_custom_mod setup-web --protect-project <id>       # root; opt a project folder into source denials
ddistro_custom_mod setup-web --protect-root <Name>Server  # root; the same for /var/www/html/<Name>Server
ddistro_custom_mod migrate-root <id> --server-folder <Name>Server   # root; move an install there
ddistro_custom_mod migrate-root <id> --drop-legacy-url    # root; retire the old URL alias
```

## Shared web port

All custom mods share one loopback Apache listener, `CUSTOM_MODS_PORT` in the root-owned
`/etc/dwemerdistro_services.conf` (default `19000`, allowed `19000`-`19999`). Each mod
gets its own path (`/custom-mods/<id>/`) and its own database; there are no per-mod
ports, daemons, or automatic port allocation. Official server ports (`8081`, `8083`,
`8088`, and the rest) and their Apache sites are unchanged. A manually installed
`/var/www/html/ExampleServer` is served on the same port at `/ExampleServer`.

The file is read as data, never sourced: only a line `CUSTOM_MODS_PORT=<number>` is
accepted, and any other value or a file that is not root-owned and root-writable only is
refused with a clear message.

`setup-web` writes only its own site file,
`/etc/apache2/sites-available/dwemerdistro-custom-mods.conf`, with
`Listen 127.0.0.1:<port>`, `DocumentRoot /var/www/html`, and access limited to
`/ExampleServer` and `/custom-mods/<id>`. It enables that site, runs
`apache2ctl configtest`, and reloads Apache only if it is running. It refuses, changing
nothing, when another enabled Apache config already uses the port, another program
already listens on it, its site file was not written by it (or is not a root-owned file
only root can change), or its site link points elsewhere. When nothing changed it only
repeats `apache2ctl configtest`. If the test or reload fails, its previous file comes back. Startup (`start_env`) runs it before Apache
starts, and install, update, switch, and restore run it before the health check. `status`
and `check` only read the port.

The same site file also denies, with explicit Apache rules and no `.htaccess`:

- `.git` metadata at any depth under `/var/www/html/custom-mods/<folder>` and
  `/var/www/html/ExampleServer`;
- for the known KAGRENAC AI Mod Maker install paths, `/custom-mods/example-server`
  (launcher id), `/custom-mods/KagrenacServer` (manual folder) and `/ExampleServer` (legacy):
  the source-only folders `client/`, `ports/`, `docs/`, `config/`, `lib/`, `sql/` and
  `scripts/` at the project root (with any config or build output inside them), and the
  files `config.json` and `.env` at any depth. Their other paths (root PHP API, `ui/`,
  assets, health) are served as before.

The `DirectoryMatch` denials sit outside the custom mod virtual host, so they also apply on
other Apache listeners that serve `/var/www/html`, such as the official ports, unless one
of those sites grants the paths again in its own `DirectoryMatch`, `Files` or `Location`
section; `LocationMatch` copies follow the custom listener's own grant. Other mods' routes
and the official sites are otherwise unchanged. A manually named or forked install in any
other folder is **not** covered for those folders and files unless it is opted in as
below; otherwise its owner must add equivalent server-side rules. After **Update Distro**, the rules take effect the next time
`setup-web` runs (distro start, a custom mod install, update, switch or restore, or
`sudo ddistro_custom_mod setup-web`). Check every listener over HTTP and expect `403`, for
example `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:19000/custom-mods/KagrenacServer/.git/HEAD`.

### Protected projects (provisional, for the KAGRENAC setup wrapper)

```bash
sudo ddistro_custom_mod setup-web --protect-project <id>
```

Opts `/var/www/html/custom-mods/<id>` into extra denials, then runs `setup-web` as usual. Run
it **before** placing the project's source there (installed, cloned or a manual local dev
copy). Each opted-in id gets, on every listener as above, the same denials as the known
KAGRENAC paths:

- the folders `client/`, `ports/`, `docs/`, `config/`, `lib/`, `sql/` and `scripts/` at the
  project root, with everything inside them;
- the files `config.json` and `.env` at any depth in the project.

The project's other paths (root PHP API, UI, assets, health) are served as before, and
other mods get none of these rules. The known paths are always covered and need no opt-in. So an opted-in project must not serve its dashboard,
health file or assets from those folders. `<id>` follows the manifest id rules (3-32
lowercase letters, digits, single hyphens; not a reserved name); the id is validated and
regex-escaped, never passed to a shell. The option may be given once.

- The list is `/var/lib/dwemerdistro-custom-mods/protected-projects`: one id per line,
  root-owned, mode `0600`, at most 32 ids, outside the registry so `status` never reads it.
  It is written atomically (synced to disk, then renamed, then the folder synced) and read
  back to check its real owner, mode and content before the command reports it saved.
- The id is saved before Apache is changed. If `apache2ctl configtest` or the reload fails,
  the previous site file comes back (it never has some new rules and not others), the
  command exits non-zero, saying the protection is saved but not active, and the id
  stays in the list. Do not place the source until `sudo ddistro_custom_mod setup-web`
  succeeds; every later run applies the saved list.
- Every `setup-web` run (with or without the option, including the ones install, update,
  switch, restore and distro start make) applies the whole list. If the list is a link,
  not a plain root-owned `0600` file in a root-owned folder, is empty, has an invalid or
  repeated id, or is too long, `setup-web` stops and changes nothing, so the listener is
  never rewritten without protection that was asked for. A missing list means no opted-in
  projects; an empty one is treated as damage, since it is never written empty. Install,
  update and branch switch read the list first and stop before changing any code, config
  or database if it is damaged.
- Protection is never dropped automatically: unregister and update keep it, and this
  version has no command to remove an id.
- Without any opted-in id the site file has only the fixed rules above.

### Direct server folders

A manifest with `"server_folder": "CommonGroundServer"` installs to
`/var/www/html/CommonGroundServer` instead of `/var/www/html/custom-mods/<id>`; its URL is
`http://127.0.0.1:<CUSTOM_MODS_PORT>/CommonGroundServer/`. The id, the database
`custom_<id>`, the registry entry, backups and every lifecycle command stay as above. Without
the field nothing changes.

- The name is 7-47 ASCII letters or digits, starts with a capital letter and ends in
  `Server`. Official and KAGRENAC product names (`HerikaServer`, `StobeServer`,
  `ExampleServer`, `KagrenacServer` and the rest of the reserved ids plus `Server`) are refused.
  Install refuses an existing entry of that name in any letter case and a name another
  registry entry (removed ones included) already uses; nothing is adopted.
- `/var/www/html` itself must be owned by root or `dwemer` (DwemerDistro ships it as
  `dwemer:www-data`, mode `2755`), with no group or other write, reached without links; it is
  checked and never changed. Because `dwemer` may add names there, install refuses a folder of
  that name that appears meanwhile, and root sets the new folder's owner and mode only through a
  handle that does not follow links.
- Before the folder is created, the name is saved in the root-only list
  `/var/lib/dwemerdistro-custom-mods/protected-roots` (same rules as `protected-projects`, at most
  64 names, never removed) and `setup-web` applies, on every listener, the denials the known
  KAGRENAC paths get (`.git` at any depth, `client/`, `ports/`, `docs/`, `config/`, `lib/`,
  `sql/`, `scripts/`, and `config.json` and `.env` at any depth). So `dashboard_path`,
  `health_path` and `assets` may not be in those folders. The custom listener then grants that
  exact name only; `/var/www/html` is never granted as a whole. Every root named in the list or
  in any registry entry, removed ones included, keeps these rules.
- Status, check, update, switch, backup, restore and the health check always use the
  registered folder. An update whose manifest names another `server_folder` (or adds or drops
  it) keeps the installed folder and says so; only `migrate-root` moves an install. An update
  or switch of a mod registered with a `server_folder` (a migrated one too, whose manifest may
  still have none) is refused before the fast-forward if the incoming `dashboard_path`,
  `health_path` or `assets` are in a denied folder or file.
- `setup-web --protect-root <Name>Server` saves and applies the same protection and grant
  without installing anything, for a manual install there: run it before placing the source.

### Moving an installed mod (`migrate-root`)

```bash
sudo ddistro_custom_mod migrate-root dead-drop --server-folder DeadDropServer
```

Moves a **ready** `custom-mods/<id>` install, under the shared lock, in one rename on the same
filesystem. Nothing is copied: owners, modes, the private config and its lock keep their
inodes; the database, token, history and settings are untouched. It refuses, changing
nothing, when the entry is not `ready` or already moved, the folder is not the plain
registered `custom-mods/<id>` checkout, tracked files changed or the checkout is not at its
registered branch and commit, the destination exists in any letter case or is claimed by
another entry, `/var/www/html` fails the owner and mode check above, the two folders are on
different filesystems, or the app's config lock (`<setup.config_path>.lock`) is held by a
running save (the lock is then held until the move finishes). A missing lock is not created; a
save that starts during the move uses the old absolute paths and saves nothing once the folder
has moved. Repository code and Git never run as root.

Steps: backup (config, database dump, the registry entry and this manager's Apache site, in
the backups folder below) → save and apply the destination's protection → wait (up to 20
seconds) until the custom listener answers with it: a graceful reload returns before Apache
serves the new config and the port was already open, so only answers count — an unknown path
under the still-missing `/<Name>Server` must be `404` (the old config answers `403`) while
`.git/HEAD`, `config/config.php`, `lib/` and `config.json` stay `403`, on several fresh
connections in a row; otherwise it refuses and nothing is moved → move record saved in the
registry entry (`migration`: exact source, destination, backup folder and the folder's device
and inode) → rename → registry switch (still with the record) → `setup-web` with an `Alias` from
the old `/custom-mods/<id>` URL to the same folder (no copy, no link) → health `200` and `403`
for `.git/HEAD` and the private config on both URLs, retried until that reload answers (same
20-second limit) → record removed. Registry writes are synced. If anything after the rename
fails, the folder, registry entry and listener are put back and the message says so (or names
exactly what could not be undone, with the backup).

**Interrupted move.** If the process dies part way (WSL shutdown, kill), the registry entry
keeps the move record: `status` shows the mod as failed with the command to run, and every
other command (update, switch, backup, branches, unregister, restore, `--drop-legacy-url`)
refuses. Run the same command again:

```bash
sudo ddistro_custom_mod migrate-root dead-drop --server-folder DeadDropServer
```

It checks that the record names exactly this mod, `custom-mods/<id>`, `/var/www/html/<Name>Server`
and a backup folder of this mod, and that the entry still matches the recorded one. Then exactly
one of the two paths must exist and be the recorded folder (same device and inode, no link) with
its checkout clean at the registered branch and commit. If it is still at `custom-mods/<id>`,
the move starts again from the beginning (new backup); if it is already at `/<Name>Server`, the
move is finished (registry, alias, route checks), or undone if those checks fail. If both paths
exist, neither does, the folder is a different one, the checkout changed, or the record is
damaged, it refuses and changes nothing; nothing is adopted. Resolve that by hand with the
backup the message names.

The new URL is canonical: `status` reports it as `dashboard_url`/`health_url` and the old one
as `legacy_url`. Game clients that hardcode the old URL keep working through the alias until
they are rebuilt and paired with the new URL; changing a config alone does not update a
compiled client. Then retire the alias with `migrate-root <id> --drop-legacy-url`. Stop the
game and other clients during the move: a request that is running while the folder moves can
fail.

To change the port: edit `CUSTOM_MODS_PORT` as root, run
`sudo ddistro_custom_mod setup-web` (the old custom listener is replaced; official ports
are not touched), then update every client pairing URL (for example `server_url`) to the
new port. **Update Distro** keeps an existing `/etc/dwemerdistro_services.conf`, so the
chosen port and the other service ports stay as they are; it installs the default file
only when none exists, and stops with an error, changing nothing, if the existing one is
a link or not a root-owned file only root can change.

Admin/test only (root, never used by the launcher): `check|install --local-source
/absolute/path/to/repo.git`, where the path is a local Git repository. A bundle must be
cloned first: `git clone --bare /path/example.bundle /path/example.git`, then
`check|install --local-source /path/example.git`. Updates of such an install fetch from
the same path.

## Repository manifest

The repository must contain `dwemer-mod.json` at its root on its default branch. Unknown
fields are rejected.

```json
{
  "schema_version": 1,
  "id": "example-server",
  "name": "Example AI Server",
  "description": "Optional short text, at most 300 characters.",
  "project_url": "https://example.com/optional-public-page",
  "dashboard_path": "ui/",
  "health_path": "health.php",
  "requirements": { "php": "8.2", "postgresql": true },
  "setup": {
    "config_template": "config/config.example.php",
    "config_path": "config/config.php",
    "token_placeholder": "CHANGE_ME_TOKEN",
    "database_placeholder": "example_ai_mod",
    "migrate_script": "scripts/migrate.php"
  }
}
```

| Field | Rule |
|---|---|
| `id` | 3-32 characters, `a-z`, `0-9`, single hyphens, starts with a letter, not an official product name. Never changes after install. |
| `name`, `description` | Plain text, at most 60 and 300 characters. `description` and `project_url` are optional. |
| `author` | Optional author or team display name: text of at most 80 characters after trimming, with no control characters. Allowed in versions 1 and 2. Without it the launcher shows the repository owner. |
| `dashboard_path`, `health_path` | Relative paths inside the mod folder. Segments use `A-Z a-z 0-9 . _ -`, may not start with `.`, and may not be `..`. `health_path` must be a tracked file. |
| `requirements` | Exactly `{"php": "8.2", "postgresql": true}`. |
| `setup.config_template` | Tracked `.php` file. It must contain `'<token_placeholder>'` and `'<database_placeholder>'` exactly once each, as quoted PHP strings. |
| `setup.config_path` | Untracked private `.php` file. A repository that tracks it is rejected. |
| `setup.migrate_script` | Tracked `.php` script, safe to run repeatedly. It reads `config_path` and exits non-zero on failure. |
| `branches` | Optional `{"default": "main", "allowed": ["main", "dev", "unstable"]}`. At most 10 branch names; `default` must be in `allowed`. See below. |
| `server_folder` | Optional `"<Name>Server"`: install directly in `/var/www/html/<Name>Server`. See [Direct server folders](#direct-server-folders). |
| `assets` | Optional `{"icon": "...", "banner": "..."}`; either key may be left out. Each is a relative path (same rules as above) to a tracked `.png`, `.jpg`, or `.jpeg` file. URLs are not accepted. |

Assets are recorded from the installed commit on install and on each successful update,
so a change shows only after **Update**. `status` reports `icon_url` and `banner_url` on
the mod's fixed route (`http://127.0.0.1:<CUSTOM_MODS_PORT>/custom-mods/<id>/<path>`, or
`/<server_folder>/<path>`), or an empty string when
an asset is not declared or its file is missing or a link. Manifests without `assets`
work as before.

The repository tree may not contain symbolic links, submodules, or `.git` paths.

## Branches

`branches` lets the author offer more than one branch. Only the manifest on the
repository's default branch (the remote `HEAD`) counts; a `branches` section on any other
branch is ignored, so a branch cannot widen its own offer. Every listed branch must
exist on the remote, or the check fails. Without the section, the repository default
branch is the only branch, exactly as before.

- `check` and install use the authored `default`. Every other offered branch must declare
  the same `id`.
- The registry keeps the last checked policy. `status` reports it (`branches`,
  `branches_checked_at`) without contacting the remote. It is refreshed by check,
  install, update, `branches`, and switch.
- **Update** always stays on the installed branch, even if the author changed `default`
  or stopped offering that branch. Such a branch is reported as no longer offered; the
  mod is never moved silently.
- **Switch** (`branches --branch` to review the exact commit, then `switch` with that
  commit) is refused for a branch that is not offered or does not exist, a changed id or
  `config_path`, changed tracked files, or incoming files that are unsafe, private, or
  would replace local files. It reuses the Update steps (backup, keep the private config,
  migrate as `dwemer`, health check), but the target does not have to be a fast-forward.
- If a switch fails after the code changed, the code is returned to the previous branch
  and commit. **Database changes made by the migration are not rolled back**; the backup
  taken just before the switch is in the backups folder below.

Because users move between branches in both directions, every offered branch's
`migrate_script` must accept a database migrated by any other offered branch: use
additive, idempotent changes (`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`),
never drop or rename what another offered branch still reads, and do not fail on
columns or tables you do not know.

## What install does

1. `check` discovers the remote default branch with `git ls-remote --symref`, makes a
   shallow no-checkout clone in a private temp folder as `dwemer`, validates the manifest
   and file list, and prints a preview with the exact commit. Git runs without user or
   system config, credential helpers, prompts, or hooks, and only over `https`.
2. `install` repeats the check under the shared server-manager lock and refuses if the
   commit differs from the previewed one.
3. It refuses if the id is registered, `/var/www/html/custom-mods/<id>` (or its `server_folder`) exists, or the
   database `custom_<id with _>` exists. Existing folders and databases are never adopted.
4. root creates the scoped folder (`dwemer:www-data`, no world access) and the scoped
   database owned by the existing `dwemer` role. The role is never created or changed.
5. As `dwemer`: clone, validate again, check out, write `config_path` from the template
   (only if missing) with a random token and the scoped database name, lint it, and run
   `migrate_script`.
6. root runs `setup-web` (below), then the health URL
   `http://127.0.0.1:<CUSTOM_MODS_PORT>/custom-mods/<id>/<health_path>` (or `/<server_folder>/...`) must answer 200.
   Only then is the registry entry marked `ready`. A failure is recorded as `failed` with a
   safe message; files and database are kept, and **Update** retries setup.

Repository `install.sh` scripts and any repository code are never run as root. root never
requires repository PHP; the config is generated by text substitution.

Install, Update, and branch switching first require the shared PHP security update
(`ensure_php_security`); if it is incomplete they stop before changing mod files or data
(shared PHP may then still need repair or a restart).

## Update, backup, unregister

- Update refuses changed tracked files (there is no force option), backs up the private
  config and database first, fetches, validates the incoming manifest and file list, and
  requires the same `id` and `config_path`. It refuses any incoming file that would land on
  an existing untracked or ignored file or link, then fast-forwards only and migrates as
  `dwemer`.
- `update <id> --expect-commit <40-hex sha>` does the same, but only to exactly that
  reviewed commit of the installed branch. If the fetched branch is at a different commit,
  it refuses before the checkout, config or database change (a ready mod stays ready,
  with the refusal as its message); check the branch again and retry with the new commit. The pin
  never allows a non-fast-forward or a branch change; that stays **Switch**. Before the
  backup or fetch, the checkout must be on the registered branch at the registered commit;
  otherwise it refuses and changes nothing. Once the checkout has moved, the registry records
  the new commit even if a later step fails, so a retry starts from what is installed. A
  `--local-source` install keeps fetching from its registered path.
- Backups live in `/var/lib/dwemerdistro-custom-mods/backups/<id>/` (root only, `0700`):
  `config.php` and a `pg_dump -Fc` of the mod's database.
- Unregister hides the mod from the list and keeps its folder, private config, and
  database. The registry keeps a root-owned `unregistered` record (not shown by `status`).
- Adding the same repository again restores that record without reinstalling: the id,
  repository, scoped folder and database names must match it, the kept checkout must be
  at the recorded commit with a valid manifest and the same `config_path`, and the
  database must be owned by `dwemer`. Files are not reset and nothing is migrated; a newer
  repository commit is installed only by **Update**. A mod removed while setup was
  unfinished comes back as needing **Update**. Any other existing folder or database with
  the same name is still refused and never adopted.
- Entries left `installing`/`updating` by a manager run that no longer holds the lock are
  reported as needing attention; **Update** retries.

## Registry and permissions

- Registry: `/var/lib/dwemerdistro-custom-mods/registry/<id>.json`, root-owned, `0755`
  folders and `0644` files with explicit modes, so root's `0077` umask cannot hide it from
  `dwemer` status reads. It holds no tokens or passwords.
- The manager is installed with mode `0755` by `update.sh` for the same reason.
- Operations share `/run/lock/dwemerdistro-server-manager.d` with `ddistro_server`.
- Dashboard and health links are always built from the registered route above on
  `CUSTOM_MODS_PORT`, never from a URL in the manifest.
