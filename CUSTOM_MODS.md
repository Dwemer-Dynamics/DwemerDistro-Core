# Custom mods (manifest version 1)

`bin/ddistro_custom_mod` installs and manages user-added PHP 8.2 + PostgreSQL mod
servers from public Git repositories. It is a narrow contract, not a plugin platform:
the distro owns the registry and every operation, and the launcher only calls it with
argument vectors.

```bash
ddistro_custom_mod status [--check-health] --json        # any user (launcher: dwemer)
ddistro_custom_mod check https://host/owner/repo --json   # preview only, changes nothing
ddistro_custom_mod install https://host/owner/repo --expect-commit <sha>   # root
ddistro_custom_mod update <id>                            # root; stays on the installed branch
ddistro_custom_mod branches <id> [--branch <name>] --json  # root; refresh offered branches / preview one
ddistro_custom_mod switch <id> --branch <name> --expect-commit <sha>   # root
ddistro_custom_mod backup <id>                            # root
ddistro_custom_mod unregister <id>                        # root; keeps files and database
ddistro_custom_mod setup-web                              # root; shared loopback listener
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
| `dashboard_path`, `health_path` | Relative paths inside the mod folder. Segments use `A-Z a-z 0-9 . _ -`, may not start with `.`, and may not be `..`. `health_path` must be a tracked file. |
| `requirements` | Exactly `{"php": "8.2", "postgresql": true}`. |
| `setup.config_template` | Tracked `.php` file. It must contain `'<token_placeholder>'` and `'<database_placeholder>'` exactly once each, as quoted PHP strings. |
| `setup.config_path` | Untracked private `.php` file. A repository that tracks it is rejected. |
| `setup.migrate_script` | Tracked `.php` script, safe to run repeatedly. It reads `config_path` and exits non-zero on failure. |
| `branches` | Optional `{"default": "main", "allowed": ["main", "dev", "unstable"]}`. At most 10 branch names; `default` must be in `allowed`. See below. |
| `assets` | Optional `{"icon": "...", "banner": "..."}`; either key may be left out. Each is a relative path (same rules as above) to a tracked `.png`, `.jpg`, or `.jpeg` file. URLs are not accepted. |

Assets are recorded from the installed commit on install and on each successful update,
so a change shows only after **Update**. `status` reports `icon_url` and `banner_url` on
the fixed route `http://127.0.0.1:<CUSTOM_MODS_PORT>/custom-mods/<id>/<path>`, or an empty string when
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
3. It refuses if the id is registered, `/var/www/html/custom-mods/<id>` exists, or the
   database `custom_<id with _>` exists. Existing folders and databases are never adopted.
4. root creates the scoped folder (`dwemer:www-data`, no world access) and the scoped
   database owned by the existing `dwemer` role. The role is never created or changed.
5. As `dwemer`: clone, validate again, check out, write `config_path` from the template
   (only if missing) with a random token and the scoped database name, lint it, and run
   `migrate_script`.
6. root runs `setup-web` (below), then the health URL
   `http://127.0.0.1:<CUSTOM_MODS_PORT>/custom-mods/<id>/<health_path>` must answer 200.
   Only then is the registry entry marked `ready`. A failure is recorded as `failed` with a
   safe message; files and database are kept, and **Update** retries setup.

Repository `install.sh` scripts and any repository code are never run as root. root never
requires repository PHP; the config is generated by text substitution.

## Update, backup, unregister

- Update refuses changed tracked files (there is no force option), backs up the private
  config and database first, fetches, validates the incoming manifest and file list, and
  requires the same `id` and `config_path`. It refuses any incoming file that would land on
  an existing untracked or ignored file or link, then fast-forwards only and migrates as
  `dwemer`.
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
- Dashboard and health links are always built from the fixed route above on
  `CUSTOM_MODS_PORT`, never from a URL in the manifest.
