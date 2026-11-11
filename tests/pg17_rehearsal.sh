#!/bin/bash
set -euo pipefail
export LC_ALL=C
tool=$(realpath "$(dirname "$0")/../bin/ddistro_stage_pg17")
base=$(mktemp -d "${TMPDIR:-/tmp}/pg17-rehearsal.XXXXXXXX")
echo "Test evidence: $base"
pg=/usr/lib/postgresql/15/bin
"$pg/initdb" -D "$base/donor" --locale=C --encoding=SQL_ASCII >/dev/null
"$pg/pg_ctl" -D "$base/donor" -o "-c listen_addresses='' -k $base -p 55431" -l "$base/fixture.log" -w start
export PGHOST=$base PGPORT=55431
"$pg/psql" -X -v ON_ERROR_STOP=1 -d postgres <<'SQL'
CREATE ROLE game LOGIN;
CREATE ROLE reader;
GRANT reader TO game;
CREATE DATABASE campaign OWNER game TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C';
CREATE DATABASE "Archived Mod" OWNER game;
REVOKE CONNECT ON DATABASE campaign FROM PUBLIC;
GRANT CONNECT ON DATABASE campaign TO reader;
ALTER ROLE game IN DATABASE campaign SET work_mem='8MB';
COMMENT ON DATABASE campaign IS 'campaign record';
CREATE TABLE public.postgres_data (label text);
INSERT INTO public.postgres_data VALUES ('also keep me');
\connect campaign
CREATE EXTENSION vector;
CREATE SCHEMA stories AUTHORIZATION game;
SET ROLE game;
CREATE TABLE stories.memories (id bigserial primary key, npc text, embedding vector(3), raw bytea);
INSERT INTO stories.memories(npc,embedding,raw) VALUES ('Éowyn 雪','[1,2,3]',decode('ff00ab','hex'));
GRANT SELECT ON stories.memories TO reader;
SELECT lo_from_bytea(12345,decode('00ff','hex'));
\connect "Archived Mod"
CREATE TABLE history(t text);
INSERT INTO history VALUES ('previous campaign');
\connect postgres
ALTER DATABASE "Archived Mod" ALLOW_CONNECTIONS false;
SQL
"$pg/pg_ctl" -D "$base/donor" -w stop
fingerprint() {
    (cd "$base/donor" && find . -type f -print0 | sort -z | xargs -0 sha256sum) | sha256sum
}
original=$(fingerprint)
python3 "$tool" "$base/donor" "$base/stage"
test -s "$base/stage/verified.json"
test "$(fingerprint)" = "$original"
pg17=/usr/lib/postgresql/17/bin
"$pg17/pg_ctl" -D "$base/stage/pg17" -o "-c listen_addresses='' -k $base -p 55431" -l "$base/candidate.log" -w start
trap '"$pg17/pg_ctl" -D "$base/stage/pg17" -m fast -w stop || true' EXIT
test "$("$pg17/psql" -XAt -d postgres -c "select count(*) from pg_database where encoding <> pg_char_to_encoding('UTF8')")" = 0
test "$("$pg17/psql" -XAt -d campaign -c "select npc || '|' || encode(raw,'hex') || '|' || (embedding <-> '[1,2,3]'::vector)::text from stories.memories")" = 'Éowyn 雪|ff00ab|0'
test "$("$pg17/psql" -XAt -d campaign -c "select encode(lo_get(12345),'hex')")" = 00ff
test "$("$pg17/psql" -XAt -d campaign -c "select pg_has_role('game','reader','member') and has_table_privilege('reader','stories.memories','select')")" = t
test "$("$pg17/psql" -XAt -d postgres -c "select datallowconn from pg_database where datname='Archived Mod'")" = f
"$pg17/createdb" future_campaign
test "$("$pg17/psql" -XAt -d future_campaign -c "show server_encoding")" = UTF8
"$pg17/pg_ctl" -D "$base/stage/pg17" -m fast -w stop
trap - EXIT
# Switch the isolated endpoint back before permitting user writes.
"$pg/pg_ctl" -D "$base/donor" -o "-c listen_addresses='' -k $base -p 55431" -l "$base/rollback.log" -w start
trap '"$pg/pg_ctl" -D "$base/donor" -m fast -w stop || true' EXIT
test "$("$pg/psql" -XAt -d campaign -c 'select count(*) from stories.memories')" = 1
"$pg/psql" -X -v ON_ERROR_STOP=1 -d postgres -c "insert into postgres_data values (convert_from(decode('ff','hex'),'SQL_ASCII'))"
"$pg/pg_ctl" -D "$base/donor" -m fast -w stop
trap - EXIT
bad=$(fingerprint)
if python3 "$tool" "$base/donor" "$base/invalid" >"$base/invalid.log" 2>&1; then
    echo "Invalid bytes passed migration" >&2
    exit 1
fi
grep -qi 'invalid byte sequence' "$base/invalid.log"
test ! -e "$base/invalid/verified.json"
test "$(fingerprint)" = "$bad"
echo "PASS: all databases UTF8, content and permissions checked, rollback and bad-byte refusal verified."
