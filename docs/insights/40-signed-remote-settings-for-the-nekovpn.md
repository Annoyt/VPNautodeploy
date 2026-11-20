# §40. Signed remote settings for the NekoVPN app (2026-10-25, client plan E14)

`GET /neko/settings` serves `bot/neko/settings.json` byte for byte (`public, max-age=300`, 404 when the file is missing). The file is signed OFFLINE: the Ed25519 key is `~/.config/nekovpn-signing/neko-settings-ed25519.pem` on the dev machine, next to the APK key, and never on a server. Neither a mirror, a CDN edge nor this server can forge settings. The app accepts only a document signed with the public key it was built with (`scripts/neko_settings.py` `PUBLIC_KEY` = client `nekoSettingsPublicKey`), and only with a bigger `version`.

- **Edit, sign, publish:**
  1. Edit `bot/neko/settings.source.json`.
  2. Run `python3 scripts/neko_settings.py sign`. It validates, sets `version` = signed version + 1 and `signed_at`, and writes `settings.json` as `{kid, doc: base64(compact sorted JSON), sig}`.
  3. Run `python3 scripts/neko_settings.py verify`.
  4. Commit and deploy (`scripts/deploy_to_entry.sh`).

  `test_is_the_source_signed` fails while source and signed file differ.
- **Fields (schema 1):**
  - `endpoints`: 1–10 https origins where the app fetches `/neko/settings`; the main domain and the CF mirror today.
  - `min_version`: `x.y.z-neko.n` or null; below it the app asks the user to update.
  - `message`: `{id, ru, en?, url?}` or null; shown once per `id`, so give every new message a new id.
  - `flags`: bool / number / string values that later features read. `flags.netstat` must be a bool: `false` stops the app's network diagnostics (NEKO6 D2); no key means on.
  - `bypass_apps` (NEKO6 D1, optional; older apps skip it): Android package names the app sends past the VPN, each `^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$`, none twice, at most 300.
  - `mail` (NEKO6 D1, optional): the service mailbox for the app's `mailto:` links, or null. Only letters, digits and `._+-` before the `@`: nothing that ends the address or adds a header to the link.
- **Key loss or rotation** needs a client release carrying the new public key first, because apps trust only the key they carry. Back up the key offline with the APK key.
- **Tests:** `tests/integration/test_neko_settings.py`. It checks the committed file against the key and the source, the validation, a round trip and tampering, the version, and the endpoint. 12 mutants killed with a positional restore; `TestNeko6Fields` (the D1 fields, 2026-11-20) 12 more. `cryptography` is in requirements-dev.txt only, never in the image.
