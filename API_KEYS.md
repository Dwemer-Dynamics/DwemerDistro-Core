# Shared API Keys — CHIM 4 contract

Settings → API Keys uses ChimServer's existing encrypted `api_keys` store and `chim_provider_connections` references. The launcher never reads saved secrets. Multiple keys for the same provider and duplicate display labels remain distinct UUID records. Supported providers come from CHIM 4: OpenRouter, OpenAI, Inworld and an origin-bound custom OpenAI-compatible endpoint.

Core's root-local `ddistro_keys` adapter invokes `/usr/local/lib/dwemerdistro/keys.php` as `www-data`, using `/opt/ChimServer` managers and the same literal CHIM environment assignments as `/etc/init.d/chim4` (`/etc/chim4-s5.env`, optional dialogue and TTS files). It does not execute those files, inherit caller environment or implement another cipher or schema. ChimServer must already be installed, its reviewed shared migrations applied, and its installation encryption key configured. Missing setup fails clearly; no fallback store is created.

Operations: `status`, `create`, `edit`, `test`, `delete`, `bind`, `import-environment`. Mutations accept bounded JSON on stdin. Key edits and connection binds carry expected revisions. A blank secret on edit preserves the saved secret. Explicit Test uses the existing provider check and can contact that provider. Deletion checks CHIM 4 connection usage, including disabled connections; referenced keys cannot be deleted. Only CHIM 4 consumers may bind through this adapter until other products participate in the shared usage check. Credentials never enter profile exports, game messages or job configuration copies.

Global → profile → character settings choose connections. A connection references a saved key UUID. Rotating that record changes future credential resolutions without changing connection IDs, models, profiles or endpoints. Missing, revoked, mismatched or unavailable shared credentials fail closed rather than falling back to copied keys. Normal Codex/Claude account sign-in remains separate.

## Existing installations

Existing legacy HerikaServer, STOBE and Dialectic badges, row IDs, connector/profile assignments and the previous private `/var/lib/dwemerdistro/provider-keys/keys.json` are preserved in place. They are not merged by label, overwritten, removed or automatically imported. The previous `sync` verb remains a no-op for older updater hooks; all cross-product copying is retired. Old copying operations are refused rather than silently changing data.

For a CHIM 4 connection still using an environment credential, select the explicit provider and choose **Import existing connection key**. This uses ChimServer's existing import operation, creates an encrypted key and changes only that connection's credential reference, preserving its endpoint/model and every profile assignment. Finish active requests first: old queued snapshots may be invalidated by a rebind. A failed/unknown import can leave an unused key; reload and inspect before retrying, never blindly repeat it.

Legacy-to-shared conversion and Stobe/Dialectic shared-key adapters are not implemented by this change. Legacy servers continue using their existing credentials. Keep this limitation visible; the screen must not claim that those products already read CHIM 4 shared references. Their future migration must explicitly map product plus badge ID to UUID, retain distinct credentials, validate the provider/origin, integrate deletion usage checks and preserve standalone behavior.

No default provider account is supplied. Normal upgrades preserve keys and encryption files. Redistributable images must exclude credentials; this change does not build or publish an installer, migrate live keys or move a test database to production.
