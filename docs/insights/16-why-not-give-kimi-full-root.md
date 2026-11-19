# §16. Why not give Kimi full root over the prod box?

We did. The user explicitly asked for "no isolation". Important consequences:
- Kimi can `rm`, `docker compose down`, exfiltrate `.env`, etc.
- First message in any new session should be a system-prompt: "Don't print `/opt/vpn-bot/.env`, `/root/.kimi-code/credentials/*`, ask before destructive ops." Kimi remembers it via session memory.
- If you tighten this later, the natural restriction point is the bridge: filter the `prompt` for known dangerous commands, or run kimi under a non-root user with selective `sudoers` rules.
