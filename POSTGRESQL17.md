# PostgreSQL 17 migration rehearsal

## Launcher upgrade and rollback

The normal launcher distro update upgrades a standard PG15 installation without a database prompt. The launcher waits while a supported Windows game is running and records the system release only after database maintenance succeeds. Core never creates, requires or deletes a Windows backup. The original PG15 cluster stays in the distro as the recovery copy, stopped and with `start.conf` set to `disabled`, indefinitely. After verifying the candidate, and only while the original donor is still in place with the recorded system identifier, Core deletes the temporary source cluster copy, numbered logical dumps and staging role dump, after both automatic and manual upgrades. It deletes nothing while PG15 is running or not `disabled`, while the donor path or any parent is linked, or while the staging directory is, holds or sits on a mount point (bind mounts included) or holds links or special files. If cleanup fails after cutover, PG17 keeps serving and `ddistro_pg17_maintenance status` prints `cleanup-needed`. The next update, **Upgrade** and the dedicated `ddistro_pg17_maintenance cleanup-staging` command then retry only the cleanup, under the maintenance locks. They never stage again, check staging space, or stop or start either cluster. `cleanup-staging` refuses, changing nothing, unless the journal records a serving PG17 whose cluster identifier and marked configuration match it. Once the cleanup is done it succeeds without changing the journal. A completed upgrade (`current`) skips on the next update. `status` also prints `needed`, `held` or `unsupported`. It refuses an unknown journal phase. A failed or interrupted upgrade restarts PG15 and is retried automatically once. After two failed automatic attempts PG15 keeps serving, but the database maintenance step of every update fails with a message until an operator chooses **Upgrade** to try again or **Roll back** to stay on PG15. Only an explicit rollback holds the installation on PG15.

In **Settings → Database Maintenance**, choose **Upgrade** with the games idle. The maintenance commands are `ddistro_pg17_maintenance auto` and `ddistro_pg17_maintenance upgrade`, with no path. Earlier launchers pass a Windows backup folder as an extra argument. Core accepts it so those launchers keep working, and never reads, checks or writes it. The maintenance command supports the running Debian `15/main` cluster on port 5432 and registers the verified candidate as `17/dwemer` on the same port. App hosts, socket paths and login credentials stay in use. The operation installs missing PG17 client/server and pgvector packages on Bookworm or Trixie. It does not perform a Debian release upgrade.

Core checks that the distro filesystem has four times the PG15 data size free, stops the donor, disables its startup, then stages all databases from it. The stager copies the stopped donor and runs PG15 only on that private copy, so the original data directory is never modified. The distro grows inside its Windows `ext4.vhdx`, so the Windows drive holding it also needs headroom; Core cannot check that drive. A failure before publication restarts the original PG15 cluster.

When the distro filesystem is too small, the upgrade stops before it changes anything. Its message gives the space required, the space available and the shortfall, then the largest databases, schemas and tables (each table's size includes its indexes and TOAST, with the TOAST share shown) and the total size of CHIM's request logs in the live data and its saves. `maintenance.log` holds the longer list.

## Fresh PostgreSQL 17 installs

Fresh installer images ship one PostgreSQL 17 cluster, `17/main` or `17/dwemer`, with no PG15 donor and no migration journal. There is nothing to migrate, so `status` and `auto` report `current` and change nothing. Core recognises this layout only when all of the following hold:

- There is no journal and no archived migration (`dwemer-stage-*`) in `/var/lib/dwemerdistro/pg17-maintenance`.
- No PG15 configuration or data exists.
- `/etc/postgresql` holds only `17` with that one cluster, and `/var/lib/postgresql` holds only its versioned data directory, with no staging copy.
- Beside that data directory, `/var/lib/postgresql/17` holds nothing but Dashboard cluster recovery copies named exactly `<name>.before-recovery-YYYYMMDD-HHMMSS` or `<name>.rejected-recovery-YYYYMMDD-HHMMSS`, each an unlinked, unregistered, stopped directory with `PG_VERSION` `17`. Core neither removes nor adopts them.
- No path is linked.
- `PG_VERSION` is `17`.
- `pg_conftool` registers that data directory on port 5432.

The cluster's identity is the system identifier in its own `pg_control`. If PostgreSQL runs or anything answers on the application socket, the server there must report version 17, that data directory and that identifier. Otherwise the cluster must be in a primary's stopped state with `start.conf` `auto` or `manual`. Failing either check exits 1 and changes nothing.

Any other journal-less layout without PG15, such as several, linked or unregistered clusters, stays `unsupported`. An installation with `15/main`, and every journaled migration, keeps the rules above. Their donor and candidate checks are never replaced by this reading. **Upgrade** and **Roll back** have nothing to act on here and refuse without changing anything.

## CHIM request logs

CHIM's request logs (`audit_request`) are troubleshooting records. Before HerikaServer kept them out of saves, every Playthrough Save copied them, so they can take up a lot of the `dwemer` database. The launcher's **Clear request logs** button (Settings → Database Maintenance) uses two commands. Both take the same maintenance locks as an upgrade and refuse while a PostgreSQL 17 upgrade or rollback is unfinished, while PostgreSQL is not serving, or while CHIM is not installed:

- `ddistro_pg17_maintenance request-logs-preview` changes nothing. It prints JSON with `token`, `total_bytes`, `tables` (`schema`, `kind` `live` or `save`, `saves`, `bytes`, `rows_estimate`) and `skipped` (`schema`, `reason`, `bytes`). Only `public.audit_request` and the `audit_request` table of a registered CHIM save schema whose manifest's `tables` list names it are included, and only when the table still has HerikaServer's original columns (`request text`, `result text`, `created_at timestamp without time zone`, `rowid bigint`; added columns are fine). Both commands also refuse unless the application socket serves the distro's own cluster: 15/main when nothing was journaled (or, without any PG15, a recognised [fresh PostgreSQL 17 install](#fresh-postgresql-17-installs)), the journaled PG15 donor after a rollback, or the journaled PG17 candidate after an upgrade, each checked against the data directory's `pg_control` identity. Unregistered, temporary, legacy-manifest or unrecognised schemas, other tables and other games' databases are never included.
- `ddistro_pg17_maintenance request-logs-clear --confirm-preview <token>` empties exactly the previewed tables in one transaction with `TRUNCATE ONLY ... RESTRICT`, never `CASCADE`. It takes CHIM's Playthrough Save lock, so it refuses while a save, switch, import or cleanup runs, and it locks the tables with a five-second timeout. It then recomputes the preview digest and refuses if the serving cluster, or any table, its columns, schema, manifest or save registration changed. New log rows do not make a preview stale; they are cleared too. It prints `cleared_tables`, `before_bytes`, `after_bytes`, `reclaimed_bytes` (what the tables actually released; the preview's `total_bytes` is only the tables' occupied size, an upper bound, because each emptied table keeps a small allocation) and `filesystem_freed_bytes` (the change in free space on the distro filesystem). The WSL virtual disk does not shrink on its own.

Neither command can see Windows. The launcher refuses both while a supported game is running.

**Roll back** fences the candidate and compares its logical contents with the checkpoint taken before it accepted client writes. A difference stops rollback and restarts PG17. A match restores the original PG15 cluster and startup setting. Both clusters are retained. Rollback journals its check before it stops or disables PG17. If it is interrupted, the next update runs the comparison, even if PG17 started again and accepted writes in between. Changed data keeps PG17 serving. An interrupted explicit rollback still ends held on PG15. An interrupted recovery of a failed upgrade still counts as a failed automatic attempt. If a machine loses power or the process is killed during maintenance, both clusters may be stopped. The next update recovers the root-owned journal at `/var/lib/dwemerdistro/pg17-maintenance/state.json` before it starts PostgreSQL. It does nothing unless the journal records an unfinished phase. It checks the PG15 and PG17 cluster identifiers against the journal. A candidate whose `pg_control` still shows the clean shutdown recorded after verification never accepted a connection, so PG15 is restored without starting PG17. Otherwise rollback runs the comparison described above. If PG17 might hold accepted writes but can neither be compared nor restarted, PG15 stays stopped and the update fails until an operator recovers it. A retried check restores an interrupted closed-database restriction on the matching candidate before comparing the data. Configuration changes are included in the comparison. Clusters with extension-managed tables or sequences require separate migration review. Diagnostic details go to the private `maintenance.log` beside the journal.

## PG15 donor retention

Core no longer removes the PG15 donor, for any migration. The stopped data directory at `/var/lib/postgresql/15/main`, `/etc/postgresql/15/main` (still `disabled`), the PG15 packages and the retained staging directory stay in place. Windows backups that earlier releases wrote stay where they are; Core never deletes or changes them. Delete one by hand only once you no longer need it.

Earlier launchers may still call the removal commands. They keep their reply formats, but never remove anything or change the journal:

- `ddistro_pg17_maintenance cleanup-preview` prints `{"eligible": false, "bytes": 0, "reason": string}` and exits 0, with no `"token"`.
- `ddistro_pg17_maintenance cleanup-pg15` prints a refusal ending in `nothing was removed` on stderr and exits nonzero: 1, or 75 while another maintenance operation holds the lock. With `--confirm-preview <token>` it refuses with `The cleanup preview is stale (<reason>); nothing was removed. Preview again`. A malformed token, or the option on another action, is still a usage error with exit 2. For a removal an earlier release completed, `cleanup-pg15` without a token still prints `{"freed_bytes": 0}` and exits 0, as before.

Journals written by this version have no `backup` entry. Earlier releases' removal requires one, so it also refuses these migrations if an earlier Core is reinstalled.

### Donors removed by earlier releases

Earlier releases could remove the donor after verifying a Windows tar and recovery JSON, journaling `donor_removal` in `state.json`. Those journals are kept and still honoured:

- `status`, `recover` and normal updates continue to use PG17. An update that has not yet cleaned the staging directory settles without deleting it, because it may hold the last PG15 data in the distro.
- **Roll back** refuses before it stops or changes either cluster. It also refuses when the donor data directory is missing without a removal journal.
- An interrupted removal is never resumed. A removal stopped after its rename leaves `/var/lib/postgresql/15/.<maintenance id>-pg15-removing`: complete at the `quarantine` stage, partly deleted at the `removing` stage. The preview and rollback messages name it. If nothing is left there, it is reported as already removed.

Returning to PG15 after such a removal means restoring the tar by hand. Restore it as root with `tar -C / -xpf <tar>`, then follow the recovery JSON. An interrupted upgrade or rollback whose donor was deleted by hand is not recovered automatically. The update stops with an error until an operator resolves it.

## Rehearsal staging

`bin/ddistro_stage_pg17` stages a stopped PG17 cluster from a **trusted, offline PG15 recovery copy**. Use it in an isolated Linux environment. The launcher maintenance command invokes it on the stopped, fenced donor. It inventories the source catalog, including closed databases, and recreates every database with UTF8. CHIM normally uses `dwemer`, Dialectic uses `dialectic`, and Stobe uses `stobe`. The names are discovered, not hardcoded.

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

Before any switch, verify the retained PG15 donor or recovery copy, backing disk headroom, PG17 authentication, expected port/socket, and the per-mod database names and credentials. Run existing playthrough read/write, backup/restore and restart checks through CHIM, Dialectic and Stobe. Keep startup/bootstrap disabled until populated databases have been accounted for. Keep writers frozen through rollback validation. Switching back after PG17 accepts new writes requires reconciling those writes first. The staging tool does not automate these release operations.
