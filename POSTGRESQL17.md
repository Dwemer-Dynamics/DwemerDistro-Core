# PostgreSQL 17 migration rehearsal

`bin/ddistro_stage_pg17` stages a stopped PG17 cluster from a **trusted, offline PG15 recovery copy**. Use it in an isolated Linux environment. It has no launcher or startup hook. It inventories the source catalog, including closed databases, and recreates every database with UTF8. CHIM normally uses `dwemer`, Dialectic uses `dialectic`, and Stobe uses `stobe`. The names are discovered, not hardcoded.

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
