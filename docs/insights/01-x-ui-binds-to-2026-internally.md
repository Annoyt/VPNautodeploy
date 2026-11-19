# §1. x-ui binds to 2026 internally, not 2053

The 3x-ui web/API listens on port **2026** inside its container (`Web server running HTTP on [::]:2026` in logs). Several old compose files declared `2053:2053` mapping, and `XUI_API_URL=http://3x-ui:2053` — that combination cannot work. The bot only reached x-ui via Docker's internal bridge anyway, but healthchecks broke. Always use 2026.
