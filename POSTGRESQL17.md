# PostgreSQL 17 migration rehearsal

## Launcher upgrade and rollback

The normal launcher distro update upgrades a standard PG15 installation without a database prompt. The launcher waits while a supported Windows game is running, creates a private Windows backup folder, and records the system release only after database maintenance succeeds. After verifying the candidate, Core deletes the temporary source cluster copy, numbered logical dumps, and staging role dump. It keeps the original PG15 cluster and verified external backup for recovery, until an explicit PG15 donor removal (see below). A completed upgrade skips on the next update. A failed or interrupted upgrade restarts PG15 and is retried automatically once. After two failed automatic attempts PG15 keeps serving, but the database maintenance step of every update fails with a message until an operator chooses **Upgrade** to try again or **Roll back** to stay on PG15. Only an explicit rollback holds the installation on PG15.

In **Settings → Database Maintenance**, choose **Upgrade** with the games idle. Select a Windows folder with room for a cold database backup. The launcher creates a private subfolder and checks free space on both the backup drive and the drive holding the WSL disk. The maintenance command supports the running Debian `15/main` cluster on port 5432 and registers the verified candidate as `17/dwemer` on the same port. App hosts, socket paths and login credentials stay in use. The operation installs missing PG17 client/server and pgvector packages on Bookworm or Trixie. It does not perform a Debian release upgrade.

Core stops the donor, creates and compares a physical backup of its data and Debian configuration, then stages all databases. The backup's recovery JSON records the original startup setting and cluster identifier. Keep the tar and JSON together. The tar contains private database records and password verifiers. A failure before publication restarts the original PG15 cluster. The old data directory remains in place.

**Roll back** fences the candidate and compares its logical contents with the checkpoint taken before it accepted client writes. A difference stops rollback and restarts PG17. A match restores the original PG15 cluster and startup setting. Both clusters are retained. If a machine loses power or the process is killed during maintenance, both clusters may be stopped. The next update recovers the root-owned journal at `/var/lib/dwemerdistro/pg17-maintenance/state.json` before it starts PostgreSQL. It does nothing unless the journal records an unfinished phase. It checks the PG15 and PG17 cluster identifiers against the journal. A candidate whose `pg_control` still shows the clean shutdown recorded after verification never accepted a connection, so PG15 is restored without starting PG17. Otherwise rollback runs the comparison described above. If PG17 might hold accepted writes but can neither be compared nor restarted, PG15 stays stopped and the update fails until an operator recovers it. A retried check restores an interrupted closed-database restriction on the matching candidate before comparing the data. Configuration changes are included in the comparison. Clusters with extension-managed tables or sequences require separate migration review. Diagnostic details go to the private `maintenance.log` beside the journal.

## Removing the PG15 donor

After a successful upgrade the stopped PG15 data directory still uses space inside the distro. Nothing removes it automatically. The launcher can offer an explicit removal:

- `ddistro_pg17_maintenance cleanup-preview` prints `{"eligible": bool, "bytes": int, "reason": string}` and exits 0, including when removal is refused or another maintenance operation holds the lock. `bytes` is the space the removal would free, or 0 when it is not eligible. An eligible preview also includes `"token"`, 64 lowercase hex characters. An ineligible preview has no token.
- `ddistro_pg17_maintenance cleanup-pg15 [--confirm-preview <token>]` prints `{"freed_bytes": int}` and exits 0 on success. Any refusal or failure prints a message on stderr and exits nonzero. A malformed token, or the option on another action, is a usage error with exit 2.

The token is a SHA-256 over the quick-check results the preview showed. It covers the maintenance identity, journal phase and removal stage (untouched donor, `quarantine` or `removing`), the PG17 candidate and PG15 donor identifiers, and the donor or quarantine path and inode. It also covers the exact `bytes` and the file identities of the Windows tar and recovery JSON, plus the recovery JSON's SHA-256. The identity of a file is its device, inode, mode, link count, owner, size, mtime and ctime. The preview does not hash the tar. The launcher should pass the token it showed. With a token, `cleanup-pg15` repeats the preview under the same maintenance locks before it changes anything, including before it repairs a journal. It refuses with `The cleanup preview is stale ...; nothing was removed. Preview again` if removal is no longer eligible (for example already removed) or the token differs. That happens, for example, when an interrupted removal now has to resume, or the donor, size or backup files changed. A matching token only confirms the operator's intent. The checks and full archive verification below still run. Without the option, `cleanup-pg15` behaves as before for direct use. A repeated call after completion then returns 0.

Removal is offered only when all of these hold:

- The journal phase is `active`, the application socket serves PG17 from `/var/lib/postgresql/17/dwemer`, and its system identifier, maintenance marker and `verified.json` match the journal.
- PG15 `15/main` is stopped, `start.conf` is `disabled`, `pg_control` shows a clean shutdown, and the system identifier matches the recorded donor.
- `pg_lsclusters` reports no other cluster at, inside or above the donor data directory. The directory is not a symlink or mount point and contains no symlinks, special files, mounts or bind mounts.
- `/etc/postgresql/15/main` is a real directory holding `postgresql.conf`, `pg_hba.conf`, `pg_ident.conf` and `start.conf`, with no links or special files.
- The Windows tar and its recovery JSON are still present on a mounted Windows volume. The JSON describes this donor, and its `original_start_conf` matches the startup setting recorded in the journal.

The preview runs these quick checks. `cleanup-pg15` also reads the whole tar once before it removes anything. It compares every donor file and directory, and every file and directory of the retained `/etc/postgresql/15/main`, with the archive: contents, size, mode and owner. The archive must hold exactly those two trees. Removal therefore stops if the PG15 configuration was edited after the backup. Upgrades from this version also record the archive's SHA-256 and size in the root-owned journal when the backup is written, so a replaced or damaged tar is refused even when its contents still compare. Archives from earlier versions have no recorded checksum and are verified by the comparison alone. In both cases removal records the verified archive's SHA-256 and size in its journal before the rename. Removal also stops if PG15 was started after the backup, because `pg_control` and `postmaster.opts` then differ.

Removal journals its progress in `state.json`. It then renames the data directory to `/var/lib/postgresql/15/.<maintenance id>-pg15-removing` in the same directory, so the rename is atomic. After that it deletes the quarantined copy without following symlinks. If the command is interrupted, run it again. Before the rename it re-verifies an untouched donor. After the rename it checks again that the verified PG17 serves the application socket, that the tar and recovery JSON are still on a mounted Windows volume and describe this donor, and that the tar's size and SHA-256 match the removal journal. It also refuses a quarantine that is or contains a mount point, link or special file. Only then does it finish deleting the quarantine. `freed_bytes` reports what that call deleted: the whole donor on a first run, the remaining quarantine on a resumed run, and 0 on a repeated call after completion, which changes nothing. `cleanup-preview` during an interrupted removal reports the remaining quarantine size. A quarantine whose inode differs from the journal is never deleted.

Only the donor data directory is removed. The Windows tar and recovery JSON, `/etc/postgresql/15/main` (still `disabled`), the PG15 packages, the PG17 cluster and the retained staging directory stay in place.

`bytes` and `freed_bytes` count allocated blocks, not apparent file size. A hard-linked file counts only when every link is inside the donor. This space is freed inside the distro's ext4 filesystem. The Windows `ext4.vhdx` file does not shrink by itself. Reclaiming host disk space needs a separate step on Windows with WSL shut down: compact the VHDX, or use a sparse VHDX with trimmed free space. Core does not perform that step.

After removal, normal Core updates, `status` and `recover` continue to use PG17. **Roll back** refuses before it stops or changes either cluster. It also refuses when the donor data directory is missing without a removal journal. Returning to PG15 then means restoring the tar by hand. Restore it as root with `tar -C / -xpf <tar>`, then follow the recovery JSON. An interrupted upgrade or rollback whose donor was deleted by hand is not recovered automatically. The update stops with an error until an operator resolves it.

`bin/ddistro_stage_pg17` stages a stopped PG17 cluster from a **trusted, offline PG15 recovery copy**. Use it in an isolated Linux environment. The launcher maintenance command invokes it after the external backup is verified. It inventories the source catalog, including closed databases, and recreates every database with UTF8. CHIM normally uses `dwemer`, Dialectic uses `dialectic`, and Stobe uses `stobe`. The names are discovered, not hardcoded.

## Inputs

1. Capture a recoverable copy outside the installed distro. Freeze writers before a final capture, shut down PG15 cleanly and capture the distro/configuration as well as its data. Verify recovery before attempting cutover. Do not use a running cluster directory, or a live VHDX copied through Windows.
2. On the rehearsal host install both sets of server binaries under `/usr/lib/postgresql/{15,17}/bin`, the correct extensions for each major (including pgvector), Python 3.9+ and GNU coreutils. The staging account is the Linux user `postgres`. Both clusters use private Unix sockets with peer authentication. Only trusted database contents and extension code belong in this environment.
3. Use private native Linux storage. Allow space for a source copy, archives, destination, sort files and logs. The tool checks Linux free space against four times source file size; check backing-volume space on Windows separately. A growing WSL VHDX can exhaust the host first. Preserve external tablespaces/mounts separately. Linked data paths, replication slots, subscriptions, foreign tables and unsupported locale providers require explicit handling; the tool stops on them.
4. Keep the original authentication files, configuration, installed mod files, backups and media. Per-server files and Windows mount paths are outside the SQL transfer.

## Run

From a checkout in the isolated distro, with a new staging directory under a private parent writable by postgres:

```bash
sudo -u postgres python3 bin/ddistro_stage_pg17 /path/to/recovered/15/main /path/to/private/run-001
```

Private outputs include source database metadata, archives, globals containing role password verifiers, diagnostic logs, and the stopped candidate at `pg17/`. Keep the directory private. `verified.json` is written after the source and destination manifests match and both staging servers have shut down. It covers the database transfer, not authentication configuration or mod runtime acceptance.

Each supported table and materialized view is compared using a SHA-256 digest over sorted COPY records. Sequences are checked by last value and called state. Large object chunks and metadata, relation owners, role attributes, database grants/settings, relation grants, installed extension names, database ownership, connection restrictions and creation defaults are checked. Extensions restore using the available PG17 version. Version-specific semantics still require application checks.

Invalid SQL_ASCII text stops the restore. Keep the original bytes and review the error privately. Do not replace invalid bytes, drop records or mark the run verified. Retry with a fresh staging path after correcting the source **copy** with a documented, reversible mapping. After an abrupt machine/process failure, the installation stays on PG15; retain the failed directory for diagnosis. Never reuse a partial PG17 candidate.

## Reproduce the fixture check

```bash
sudo -u postgres bash tests/pg17_rehearsal.sh
```

The test prints its private temporary path. It uses synthetic data, SQL_ASCII and UTF8 databases, non-ASCII text, pgvector, role grants, sequence state, binary data, a large object and a closed database. It checks the original donor bytes, a new UTF8 playthrough database, and return to PG15 on the same private endpoint. It also requires a restore containing invalid text to fail.

## Deployment acceptance

Before any switch, verify the external PG15 recovery copy, backing disk headroom, PG17 authentication, expected port/socket, and the per-mod database names and credentials. Run existing playthrough read/write, backup/restore and restart checks through CHIM, Dialectic and Stobe. Keep startup/bootstrap disabled until populated databases have been accounted for. Keep writers frozen through rollback validation. Switching back after PG17 accepts new writes requires reconciling those writes first. The staging tool does not automate these release operations.
