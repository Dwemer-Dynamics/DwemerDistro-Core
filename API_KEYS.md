# DwemerDistro global API keys

The top-level API Keys tab edits one encrypted shared `api_keys` table. It has no mod selector and never edits native mod badge rows or individual connection credentials. All saved keys are shown once by stable UUID, with their provider and editable display name. Empty provider rows are unsaved placeholders; they consume no key slots.

Paste a replacement into a row and Save. Blank preserves its current secret. Existing global keys rotate in place, preserving the UUID and every current assignment. Creating a key does not automatically bind mods or replace their existing settings. Existing mod-specific keys, profiles and assignments are preserved. Consumers already assigned to a global UUID read its new value after rotation.

The shared provider catalog includes the 13 current CHIM names and custom OpenAI-compatible services. Fixed providers pin their exact API origin. Regional/custom providers require an exact API origin in the same row; that input appears after pasting a replacement. Add custom key creates an inline named row with a provider choice. Arbitrary display names are supported; creating storage does not install new provider adapters. The existing 64 active-key limit and printable ASCII secret validation remain in effect.

Saved values appear as first four + ******** + last four characters. Values of eight characters or fewer stay fully masked. Show/Hide reveals only the selected saved global key. Revealed values and replacement inputs clear on page unload and reload; Hide/save clear the selected reveal. Late reveal replies after navigation are discarded. Saving one row preserves input in other rows.

## Local operations

- `global-status`: global key metadata, masked previews and provider catalog. No legacy or per-connection rows, no complete secrets.
- `global-save {key_id,revision,label,secret}`: revision-checked in-place rotation/rename. Unchanged label plus blank secret is a no-op.
- `global-save {provider,label,origin,secret}`: create a new encrypted global key.
- `global-reveal {key_id,revision}`: explicit readback, refusing stale revisions.

Only the root-local fixed CLI exposes these operations. Secrets travel over bounded stdin/private captured stdout, never command arguments, environment or logging callbacks. Encryption and validation stay in ChimServer ApiKeyManager; all persistent SQL stays in DBManager. Provider/origin and UUID are authenticated encryption associated data. A metadata-only shared migration extends the allowed provider names without rewriting existing key rows. Existing ciphertexts, IDs and references are retained. Unknown write outcomes require reload, without automatic retries. Busy operations exclude distro maintenance and block launcher close.

The older native and advanced connection/import operations remain available for compatible callers, but are absent from this global editor. No automatic import, deduplication, merging, copying, rebinding or deletion occurs. Only explicit Test contacts a provider; loading and saving do not. The old private JSON file is preserved; sync remains a no-op.

## Quick checks

The small Test button checks the saved global key with one read-only GET. It is disabled for unsaved replacement input; save first. The server compares the saved revision before checking. Check results are temporary UI state and never change key records or assignments.

Supported authentication checks: OpenRouter, OpenAI, Inworld, Deepgram, Google, ElevenLabs, Replicate, Cartesia, Groq and DeepL (official Free/Pro origins). Custom OpenAI-compatible services may return Reachable, which does not prove authentication. Azure, Nano-GPT, Ploofy and unsupported origins have no quick check. Disabled buttons explain why.

Each check has a two-second connection timeout, five-second total HTTP timeout, 128 KiB response limit, no retries/redirects/proxies, verified TLS, and exact saved-origin binding. Keys travel only in provider-specific headers. No generation, model invocation or speech synthesis occurs. Accepted confirms the read-only request was accepted, not model access. HTTP 401/403 reports Rejected (authentication or permission); 429 reports Rate limited; network, timeout, malformed response and other errors report Check failed. Response bodies and curl diagnostics never reach the UI or logs.

`global-test {key_id,revision}` returns only fixed safe result/message fields. The provider catalog and key metadata expose `test_supported` so the launcher does not guess support.
