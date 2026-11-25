# DwemerDistro global API keys

The top-level API Keys tab edits one shared `api_keys` table; secrets are stored as plain text by explicit operator decision and are always censored in replies. It has no mod selector and never edits native mod badge rows or individual connection credentials. All saved keys are shown once by stable UUID, with their provider and editable display name. Empty provider rows are unsaved placeholders; they consume no key slots.

Paste a replacement into a row and Save. Blank preserves its current secret. Existing global keys rotate in place, preserving the UUID and every current assignment. Creating a key does not automatically bind mods or replace their existing settings. Existing mod-specific keys, profiles and assignments are preserved. Consumers already assigned to a global UUID read its new value after rotation.

The shared provider catalog includes the 13 current CHIM names and custom OpenAI-compatible services. Fixed providers pin their exact API origin. Regional/custom providers require an exact API origin in the same row; that input appears after pasting a replacement. Add custom key creates an inline named row with a provider choice. Arbitrary display names are supported; creating storage does not install new provider adapters. The existing 64 active-key limit and printable ASCII secret validation remain in effect.

Saved values appear as first four + ******** + last four characters. Values of eight characters or fewer stay fully masked. Show/Hide reveals only the selected saved global key. Revealed values and replacement inputs clear on page unload and reload; Hide/save clear the selected reveal. Late reveal replies after navigation are discarded. Saving one row preserves input in other rows.

## Local operations

- `global-status`: global key metadata, masked previews and provider catalog. No legacy or per-connection rows, no complete secrets.
- `global-save {key_id,revision,label,secret}`: revision-checked in-place rotation/rename. Unchanged label plus blank secret is a no-op.
- `global-save {provider,label,origin,secret}`: create a new global key.
- `global-reveal {key_id,revision}`: explicit readback, refusing stale revisions.

Only the root-local fixed CLI exposes these operations. Secrets travel over bounded stdin/private captured stdout, never command arguments, environment or logging callbacks. Storage and validation stay in ChimServer ApiKeyManager; all persistent SQL stays in DBManager. New and replaced keys are stored as plain text. Rows sealed by older runtimes are never converted: they stay sealed and readable with the existing encryption key until an edit replaces the secret, which keeps the ID and references. A metadata-only shared migration extends the allowed provider names without rewriting existing key rows. Existing IDs and references are retained. Unknown write outcomes require reload, without automatic retries. Busy operations exclude distro maintenance and block launcher close.

The older native and advanced connection/import operations remain available for compatible callers, but are absent from this global editor. No automatic import, deduplication, merging, copying, rebinding or deletion occurs. Only explicit Test contacts a provider; loading and saving do not. The old private JSON file is preserved; sync remains a no-op.

## Dashboard rail

`/usr/local/bin/ddistro_dashboard_keys {status|edit|create|test}` is the only Dashboard path. `update.sh` installs it with `/etc/sudoers.d/dwemerdistro-dashboard-keys`, which lets `www-data` run exactly those four root verbs. `status` returns masked rows only (UUID, provider, display name, preview, revision, `test_supported`) plus the installed provider catalog reduced to each provider id and whether a new key needs an entered origin. `edit` accepts exactly `{key_id,revision,label,secret}` on stdin (8 KiB limit) and forwards it to `global-save` under the shared key lock. `create` accepts exactly `{provider,label,origin,secret}` with a required secret; under the same lock it re-reads the catalog, refuses unknown providers and requires `origin` to be null for fixed-origin providers and entered for regional/custom ones, then forwards to `global-save`, where ApiKeyManager still validates, stores and enforces the active-key limit (`limit` is reported as nothing written). `test` accepts exactly `{key_id,revision}` and, under the same lock, runs the launcher's `global-test` (one bounded read-only `ApiKeyCheck` request with the saved key, no generation); it replies only a fixed result (`accepted`, `rejected`, `rate_limited`, `unavailable`, `unsupported`, `reachable`) or `conflict`/`missing`/`busy`, never the provider reply or check message. Reveal, delete, bind, import and native operations are refused. The bridge never runs `install_shared_keys`: an unconfigured store reports `not_configured` until setup runs from the launcher API Keys tab or `sudo /usr/local/bin/install_shared_keys`. Replies carry fixed outcome codes, never adapter diagnostics; a dispatched write with no clear reply reports `unknown` and must be reloaded, not retried.

## Rollout

### PostgreSQL 17 bundle

The pin uses ChimServer commit `804e501154de8353da50c05872700809499a0f31`, which includes
reviewed shared-store manifests for PostgreSQL 15 and 17. It does not enable the separate
ChimServer application schema on PostgreSQL 17. Existing keys and the installer SQL are unchanged.

Reproduce the code-only artifact with:

```sh
python3 tools/build_shared_key_runtime.py /path/to/ChimServer \
  dwemer-shared-keys-3513d9a5bede.tar.gz dwemer-shared-keys-804e501154de.tar.gz
```

The previous archive must match its published checksum. The builder reads canonical Git blobs,
keeps the verified previous installer SQL, and refuses changed shared migrations. It never reads
an installed database, configuration or encryption key. The resulting SHA-256 is
`17605cad8fb79b216d9854a0859924828657caa364d34d6a10a8a605aecdcf35`.

Release prerequisite: publish that exact artifact under `shared-keys-804e501154de` before
merging the new pin. Preparing this change does not publish the release asset.

`sudo ddistro_keys check-ready` verifies an existing store through the runtime account, without
setup, provider requests or returned key metadata. An unconfigured fresh image is allowed;
lost configuration for an initialized store is refused. Update Distro checks readiness on
PostgreSQL 15 and 17 and prints a warning with the reason when keys are not ready; other server
updates continue. `ddistro_pg17_maintenance auto` and `upgrade` check only after they cut over
to PostgreSQL 17 in that run; held, current, unsupported and cleanup-only runs are not checked.
An unready store after cutover prints a warning, keeps PostgreSQL 17 serving and never reverts
accepted writes; the maintenance itself still succeeds. If staging cleanup fails after that
cutover, the warning still prints and the maintenance reports the cleanup failure. An interrupted
check only warns. Recover with full setup from the
launcher API Keys tab or `sudo /usr/local/bin/install_shared_keys`, which reports the readiness
refusal instead of a generic setup failure. If setup was skipped because it already ran for the
same configuration and library, full setup retries its schema and grant steps once for a
configured store, then checks again. It never creates a store or changes credentials, and reports
the refusal if the store is still not ready.

`etc/shared-key-runtime.json` pins the ChimServer runtime bundle by source commit and SHA-256; `update.sh` installs it with `install_shared_keys --install-runtime` (runtime only). Afterwards run full setup once, from the launcher API Keys tab or `sudo /usr/local/bin/install_shared_keys`, to apply the schema and runtime permissions. There is no conversion step: the shared migration changes the schema only, existing encrypted keys stay encrypted and readable, and only newly saved or replaced keys are stored as plain text. Keep the existing encryption key while encrypted keys or older backups exist.

## SOULKILLER saved connectors

After upgrading the server through migration 049, run `sudo ddistro_keys prepare-soulkiller`.
Check the returned warnings. This root-only operation reads the installed server's database and
folder from Core's custom-mod registry, preserves all other consumer entries, and grants `CONNECT`,
schema `USAGE`, and column `SELECT` on
`id`, `kind`, `driver`, `enabled` and `shared_key_id` on `public.ex_saved_connectors` to the shared
metadata role. It creates no server schema and copies no credentials. `prepare-legacy` also
includes this preparation; an absent or incomplete optional server produces a warning rather
than stopping legacy setup. An untrusted registry or changed registered database is refused.

## SYNTH (Fallout 4) references

SYNTH is a native consumer. Its migration creates the view `public.synth_key_references`
(`id`, `kind`, `label`, `shared_key_id`) in the `synth` database. `prepare-legacy` checks that view,
grants the shared metadata role `CONNECT`, schema `USAGE` and `SELECT` on it, and registers `synth` in
the consumer registry. It changes no SYNTH schema and copies no credentials. A missing database or view
produces a warning only; an existing registration stays in place, so key deletion fails closed rather
than stranding SYNTH connectors. The registry accepts `synth` only for the `synth` database and the
`synth_key_references` table. SYNTH's own PHP reads `CHIM_SHARED_DB_*` itself; the Distro does not
host SYNTH, so it exports nothing for it.

## CHEVO (CHIM Evolution) badge references

CHEVO keeps its connectors' `api_badge_id`; a badge refers to a global key through a nullable
`core_api_badge.shared_key_id uuid`. CHEVO's own migration adds that column and the plain view
`CREATE VIEW public.chevo_key_references AS SELECT id, label, shared_key_id FROM public.core_api_badge`,
exactly that: no alias, filter, expression, join or view option, so it lists every badge with or without a
reference. Core changes no CHEVO schema.

CHEVO's database name is installation configuration, so enrollment is explicit and root only:
`echo '{"database":"<name>"}' | sudo ddistro_keys prepare-chevo`. The name must be lower-case and must
not be a maintenance database, `synth`, a `custom_*` database, the shared store or any database of
another Core product or registered consumer. In that database `doctrine_migration_versions` must
record `Chevo\Migrations\Version20261027132500`, `core_api_badge` must be a plain table without row
security that has `id`, `label` and nullable `shared_key_id`, and the server's deparsed view definition
(`pg_get_viewdef` with `search_path` `pg_catalog`) must equal that exact projection in its PostgreSQL 15
or 16+ form; naming only the three columns is not enough. The shared metadata role must not be able to
read `api_key`, write either relation or use their owner role. Core then grants the role `CONNECT`, schema
`USAGE` and `SELECT` on the view only, re-checks those limits, checks that it can read the view and
registers `chevo`. A changed database is refused. `prepare-legacy` only re-grants an enrolled database; it
never enrolls one, and an unavailable database keeps its entry so deletion fails closed.

Deletion counts every badge holding the key, used by a connector or not. CHEVO checks `CAPABILITIES` for
`chevo_badge_binding`, then sets a reference only through
`DistroSharedKeys::bindChevoBadge($pdo, $keyId, $provider, $kind, $endpoint, $write)`. `$pdo` is CHEVO's
own Doctrine `pdo_pgsql` connection (`getConnection()->getNativeConnection()`); Core opens no second
connection as CHEVO's writer. It must be idle (pdo_pgsql reports the server's transaction status), on the
registered server and database, and resolve `core_api_badge` and the view to `public` unqualified. Work
`$write` leaves open is rolled back before the key lock is released. `$provider` is the provider CHEVO's
driver policy requires for the connector, and `$kind`/`$endpoint` are its kind and saved URL. Before the
pinned manager is called, `$provider` must be able to serve `$kind` and the key's immutable stored
provider must equal it exactly, at the endpoint's exact origin; the locked row is checked again. Refusals
are the saved-connector outcomes below plus `provider_mismatch`. `$write` runs one autocommitted statement
and returns the badge's integer id. `bound` is returned only after the guard's own connection reads the
committed reference while the key lock is held. `unknown`, or an exception once `$write` may have run,
means the write may have completed: reload, never retry. Clearing a reference needs no lock.

CHEVO resolves at dispatch only with `DistroSharedKeys::resolveChevo($keyId, $provider, $kind, $savedUrl)`,
with the same exact provider, capability and origin rules; `resolve()` keeps its existing semantics and
never accepts `itt`. `DistroSharedKeys::chevoKeyCatalog()` lists the active global keys for CHEVO's key
picker as `key_id`, `provider`, `label` and `origin` only: no secret, masked preview or diagnostic.

For `llm` and `tts` the provider's stored kind must match. `stt` uses the STT providers below. `itt` (CHEVO
only) accepts `openrouter`, `openai` or `custom_openai`, the keys for CHEVO's chat-completions vision
drivers `openrouter`, `openai` and `local`.

The shared bridge advertises `stt` and `saved_connector_binding`. STT accepts keys explicitly
registered as `openai`, `deepgram`, `inworld` or `custom_openai`. These providers share the same
credential across supported APIs. The bridge authorizes that capability before asking the pinned
manager to check the stored provider kind, exact origin and current status. Other providers are
refused, with no label inference or retry under another kind. Existing LLM/TTS behavior and
sealed legacy keys are preserved; no key conversion is needed. PostgreSQL 17 also requires a
compatible pinned shared-key runtime; the older PostgreSQL 15-only bundle refuses it.

Saved-reference writes hold the manager's shared key-row lock until the server write commits.
They require an idle caller connection and the registered public table; a shadowing search path
is refused. Guard readability is checked before writing. Deletion counts disabled references too,
preserves connector UUIDs, and refuses when any registered consumer is unreadable. Detach a
reference before deleting its key. A vanished installation never implicitly removes its registry
entry: recover that database or inspect and repair its references before changing registration.

## Quick checks

The small Test button checks the saved global key with one read-only GET. It is disabled for unsaved replacement input; save first. The server compares the saved revision before checking. Check results are temporary UI state and never change key records or assignments.

Supported authentication checks: OpenRouter, OpenAI, Inworld, Deepgram, Google, ElevenLabs, Replicate, Cartesia, Groq and DeepL (official Free/Pro origins). Custom OpenAI-compatible services may return Reachable, which does not prove authentication. Azure, Nano-GPT, Ploofy and unsupported origins have no quick check. Disabled buttons explain why.

Each check has a two-second connection timeout, five-second total HTTP timeout, 128 KiB response limit, no retries/redirects/proxies, verified TLS, and exact saved-origin binding. Keys travel only in provider-specific headers. No generation, model invocation or speech synthesis occurs. Accepted confirms the read-only request was accepted, not model access. HTTP 401/403 reports Rejected (authentication or permission); 429 reports Rate limited; network, timeout, malformed response and other errors report Check failed. Response bodies and curl diagnostics never reach the UI or logs.

`global-test {key_id,revision}` returns only fixed safe result/message fields. The provider catalog and key metadata expose `test_supported` so the launcher does not guess support.

## Consumer socket (`ddistro_keys_api`)

Local products in a sibling WSL distro (currently Reign's `ReignServer` distro) use global keys through one Unix
socket at `/mnt/wsl/dwemerdistro-keys/keys.sock`, on WSL2's tmpfs shared by distros on the same kernel. There is no
network listener, Windows exposure or interop. The directory must be root-owned and not group/other-writable or the
server refuses to start; it never repairs an existing path.

- **Trust:** peer UIDs mean nothing across distros, so each request carries a client token. Enrollment is two-phase
  and root only (never a socket operation): `ddistro_keys_api register <id> --providers a,b` stages a new token and
  prints it once with a non-secret stage id beside the current one, `commit <id> --stage <stage>` makes it current
  once the consumer stored it, and `discard <id> --stage <stage>` drops it after a failed provisioning, so the
  previous working token is never lost. A second `register` is refused (`stage_pending`) while a stage exists, and a
  wrong stage id is refused (`stage_mismatch`); a stage left by a crashed run can be dropped explicitly with
  `discard <id> --abandoned` once it is 10 minutes old. Only SHA-256
  digests are kept in `/var/lib/dwemerdistro/keys-api/clients.json` (root 0600, atomic replace under a state lock).
  `revoke <id>` removes the client. Provision the token over stdin straight into the consumer's root-owned file;
  never through argv, environment or logs.
- **Operations:** one JSON line per connection (8 KiB max, 10 s timeout, 8 concurrent handlers). `status` returns
  masked rows and the catalog for the client's providers only. `resolve {key_id, provider, origin}` checks the UUID,
  provider, exact origin and saved value, then reveals at the current revision; a concurrent change returns `changed`
  and is never retried. `edit`/`create` reuse the dashboard rail's request shapes, lock and outcomes (`unknown` means
  reload, never retry). `version` is the only unauthenticated operation. There is no reveal by id, delete, bind, test
  or import, and no secret or metadata cache.
- **Lifecycle:** `start_env` and `update.sh` run `ensure` only when a client is enrolled. `ensure` is serialized by a
  lifecycle lock and starts no other service (PostgreSQL stays with `start_env`; store readiness is reported per
  request). A running server holds a server lock for its life, so a second `serve` never unlinks the active socket; a
  stale server is stopped only when its pid's argv is exactly `python3 -I ddistro_keys_api serve`, and replaced only
  after its exit is confirmed. `hold` runs `ensure` and keeps the distro alive for the
  consumer's explicit Windows keep-alive session. When this distro is stopped the consumer reports the keys as
  unavailable; it has no fallback copy.
