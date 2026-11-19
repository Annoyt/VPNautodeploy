# §6. Compose must pass SID_VALUE / SNI_VALUE / DEMO_* explicitly

`bot/config/settings.py` reads `SID_VALUE` etc. via `os.getenv(...)` with `'01'` as the default. `os.getenv` looks at the **container's** environment, not the project `.env`. If the compose service lacks `- SID_VALUE=${SID_VALUE}`, the bot silently falls back to `sid=01` and every generated VLESS link will fail Reality auth — TLS masquerade still completes (clients show "Connected") but no tunnel is built. We fixed this on 2026-05-12 but it is the easiest bear trap in this repo to re-introduce.
