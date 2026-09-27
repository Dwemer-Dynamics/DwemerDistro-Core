# Optional LM Studio component

Run `sudo /usr/local/bin/install_lmstudio` after updating DwemerDistro. This installs the reviewed upstream headless engine for `dwemer`. It does not download a selected language model, load one, enable autostart, or change game providers. The upstream bootstrap script is checksum pinned; refresh it only after reviewing a new upstream version.

The launcher opens `/Dwemer-Dashboard/lmstudio.php` with a private access key in the URL fragment. The page exchanges it for an eight-hour session and removes the fragment. You can also run `ddistro_lmstudio access` as `dwemer` and paste the key. Do not share the key. Management requests require the session and a CSRF token; `www-data` may invoke only the restricted helper as `dwemer`.

The OpenAI-compatible base URL inside WSL is `http://127.0.0.1:1234/v1`; the full chat URL is `http://127.0.0.1:1234/v1/chat/completions`. Port 1234 is reserved for this component. Stop another server using that port before starting LM Studio. The manager remains usable while inference is stopped. It polls only while visible and never generates on page load.

Presets live in `/etc/dwemerdistro-lmstudio.json`, shared by Quickstart and the web manager. They pin a Hugging Face repository, revision, file, size and SHA-256. Downloads resume after interruption, verify the whole file, and import through `lms`. Custom inputs accept Hugging Face URLs (for example `https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF@Q4_K_M`) or LM Studio catalog IDs; those use upstream selection and verification. No model is labelled recommended without separate game-quality testing.

Manager state and temporary downloads are under `/home/dwemer/.config/dwemerdistro/lmstudio` (private to `dwemer`). LM Studio manages its own model directory, normally `/home/dwemer/.lmstudio/models`. Updates and reinstall preserve these locations. Turning off autostart or stopping the engine leaves downloads in place. There is deliberately no destructive remove-model or delete-runtime action.

One manager job runs at a time. Load accepts context length, GPU offload percentage and idle TTL. It checks free RAM/VRAM with 2 GB headroom; upstream guardrails still apply and context increases memory needs. Unload before switching. Test requests require an already loaded model, stream up to 192 tokens and retain bounded output. Thinking applies only to that test, defaults to the model default, and unsupported values produce an error.

The integration was developed against llmster `0.0.25-1` / lms commit `69d945a`. Core, Dashboard and Launcher changes must ship together. Existing onboarding completion stays valid. In-game provider quality and large-model fit must be measured separately from successful component installation.
