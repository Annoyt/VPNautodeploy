# §3. Compose project name = volume prefix

Docker Compose names volumes `<project>_<volume>`. Project name defaults to the parent directory; if you `docker compose up` from a differently-named directory (e.g. `vpn-bot-refactor/`), you'll silently create a parallel set of volumes and lose data. The fixed `name: vpn-bot` in compose protects against this.
