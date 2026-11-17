# Updating Hermes (the `/ai` agent) on entry

## What runs where (since 2026-11-16)

Hermes **v0.23.1**, the official *managed* install:

- **Code, its venv and the built TUI / web UI:** `/usr/local/lib/hermes-agent-v0.23` (~1 GB).
  - The command: `/usr/local/lib/hermes-agent-v0.23/.hermes/bin/hermes`.
  - `/usr/local/bin/hermes` and `/root/.local/bin/hermes` exec it.
- **Home:** `/root/.hermes-v023` holds `config.yaml`, `.env`, the skills and the state.
  - Its `tools/` holds the runtime: Python 3.14, uv, node, ffmpeg, ripgrep (~1.2 GB). There is no Chromium: the install ran with `--skip-browser`.
  - **`/root/.hermes` is a symlink to it**, so the free-model guard, `deploy_hermes_skills.sh` and the watchdog keep their paths.
  - **Hermes's own launcher goes through the symlink too.** The gateway rewrote `.hermes/bin/hermes` at its first start from the unit (20:27:25), and the launcher now runs `/root/.hermes/tools/python-3.14…/bin/python3`. Never repoint or remove `/root/.hermes` while v0.23 runs.
- **Unit:** `/etc/systemd/system/hermes-api.service`, the same as `systemd/hermes-api.service` in the repo (`HERMES_HOME=/root/.hermes-v023`, `MemoryMax=1200M`). The gateway's RSS is ~290 MB; v0.18.2 used 153 MB.
- **Kept for rollback:** the previous v0.18.2 in `/usr/local/lib/hermes-agent` (its venv is Python 3.11) and its home `/root/.hermes-v0182`.
  - Backups: `/opt/backups/hermes-api.service.bak-<TS>`, `hermes-wrapper.bak-<TS>` and `hermes-home-v0182-<TS>.tar.gz`. `<TS>` is in `/root/.hermes-update-ts`.
  - Rollback script: `/root/hermes-install/rollback.sh` (below).

## Why `pip install` into the old venv does not work

From about v0.21 on, every core dependency in `pyproject.toml` carries `python_version >= '3.14'`. A Python 3.11 venv gets 32 packages and none of the core libraries. Only the official installer (`scripts/install.sh` of the release) works: `hermes pm` brings Python 3.14 and the rest.

## Stand first

Use a Debian container with curl, git and xz. Run the release's `scripts/install.sh --channel stable --dir /opt/hermes-test --hermes-home /root/.hermes --non-interactive --skip-browser` on our `config.yaml` (secrets replaced) and our skills. Point `model.base_url` and each `fallback_providers[].base_url` at a fake OpenAI-compatible server.

Then check:
- `/v1/models` answers with the bearer and refuses a wrong one;
- `/v1/chat/completions` works;
- a 429 on the primary model falls back to the next one;
- the bot's `HermesAgentClient` keeps a conversation going: the second turn carries the history (`X-Hermes-Session-Id`);
- the workspace AGENTS.md is in the system prompt.

Traps met on the 0.23.1 stand:
- The gateway refuses to run as root when the install dir is `/opt/hermes` and `docker/entrypoint.sh` exists. That check is meant for the official image. Install elsewhere, or set `HERMES_ALLOW_ROOT_GATEWAY=1`.
- The API server starts only with an `API_SERVER_KEY` of at least 16 characters; otherwise the log says «No messaging platforms enabled».
- `pkill -f '<words>'` inside `docker exec bash -c '…<words>…'` kills that shell too. Anchor the pattern (`^/root/.hermes/tools/python-3.14`).

## What the v0.23.1 update needed (check again on the next one)

- **`terminal.cwd: /root/hermes-work` in `config.yaml`.** `terminal.working_dir` is ignored. Without `cwd`, the workspace AGENTS.md is not in the system prompt, and that file carries the healthcheck-first rule and the golden rules: 12.4K characters of system prompt without it, 22.5K with it.
- **`config.yaml` migrates on first start** (v0 → v53). Only `max_turns` moved, to `agent.max_turns`. `fallback_providers`, the 18 `auxiliary` tasks and `approvals.mode: smart` stayed as they were. The guard runs fine on the new format.
- **The `.env` in `HERMES_HOME` overrides the process environment.** A test instance on another port needs `API_SERVER_PORT` / `API_SERVER_HOST` changed in its own `.env`. Put the prod values back before the switch.
- **The installer also changes things around ours.** It syncs the bundled skills next to ours, appends `~/.local/bin` to root's PATH in `.bashrc` and `.profile`, and may fail to install `cua-driver`, which we do not need.

## Procedure (as done on 2026-11-16)

The install took ~12 min on entry's single vCPU (8.5 min CPU, 1.0 GB memory peak). `/ai` was down 22 s, during the switch only.

1. **Backups:**
   - the unit and the `/usr/local/bin/hermes` wrapper;
   - a tar of the home without `node/`, `bin/`, `cache/` and `logs/`, into `/opt/backups/…-<TS>`.
2. **Copy the home to the new home.** rsync it without caches, logs, `*.lock`, `gateway.pid`, `gateway_state.json` and the sqlite files. Copy `state.db`, `response_store.db`, `kanban.db` and `verification_evidence.db` with sqlite's backup API.
3. **Install beside the old one, at low priority:**
   ```sh
   systemd-run --unit hermes-install --collect -p Nice=19 -p IOSchedulingClass=idle -p MemoryHigh=1G \
     -E HOME=/root -E HERMES_HOME=<new home> \
     /bin/bash install.sh --commit <release commit> --dir <new dir> --hermes-home <new home> \
     --non-interactive --skip-browser
   ```
4. **Prepare the config.** Add `terminal.cwd` to the new `config.yaml`. In the new `.env`, set `API_SERVER_PORT=4098` and `API_SERVER_HOST=127.0.0.1`.
5. **Test the new gateway beside the old one.** Run it as a transient unit (`systemd-run … <new dir>/.hermes/bin/hermes gateway run`) and check:
   - `/v1/models` on 4098;
   - a real question;
   - «какой протокол лежит?», whose answer must come back as the healthcheck's ИТОГ.
6. **Switch.** First make sure no `/ai` turn is in flight: `ss -Htn state established '( sport = :4097 )'` prints nothing. Then:
   1. Stop `hermes-api-watchdog.timer`, the test unit and `hermes-api`.
   2. rsync the state files again and copy the sqlite databases again.
   3. Copy the old `.env` over the new one (prod port and host).
   4. Run `mv /root/.hermes /root/.hermes-v0182 && ln -s <new home> /root/.hermes`.
   5. Install the new unit and the new wrapper, then `daemon-reload` and start `hermes-api`.
   6. Wait until `/v1/models` answers 200, then start the watchdog timer.
7. **Verify:**
   - `/v1/models` from inside vpn-bot (`host.docker.internal:4097`, `HERMES_API_KEY`) answers 200;
   - a chat through 4097 gets an answer;
   - `systemctl start hermes-model-guard.service` ends with Result `success`;
   - the gateway log has no errors other than upstream 429s.
8. **Rollback** if anything is wrong (≈1 min):
   ```sh
   #!/bin/sh
   set -u
   TS=$(cat /root/.hermes-update-ts)
   systemctl stop hermes-api-watchdog.timer hermes-api.service
   [ -L /root/.hermes ] && rm /root/.hermes
   [ -d /root/.hermes-v0182 ] && mv /root/.hermes-v0182 /root/.hermes
   cp -a "/opt/backups/hermes-api.service.bak-$TS" /etc/systemd/system/hermes-api.service
   cp -a "/opt/backups/hermes-wrapper.bak-$TS" /usr/local/bin/hermes
   systemctl daemon-reload
   systemctl start hermes-api.service hermes-api-watchdog.timer
   ```
9. **After a week without problems,** delete `/usr/local/lib/hermes-agent` (1.7 GB) and `/root/.hermes-v0182`.

## The next update

- **Is there one?** The `hermes-release-notice` timer on entry posts each newer stable release to the AI topic once, with the first line of its notes.
  - It runs once a week, on Mondays at 06:00 UTC (the owner's choice): `scripts/hermes_release_notice.py`, installed by `scripts/deploy_hermes_host.sh`. Two runs in a row that cannot check post one warning.
  - It asks GitHub: Hermes's own check had not seen v0.23.2 12 minutes after it was published (`hermes --version` on entry still said «Up to date»).
  - By hand: `hermes update --check` says whether a newer release is out, and installs nothing. `hermes update --plan` shows what an update would do, again changing nothing. That includes how it would restart the running gateway.
- **The version number says little about the size.** v0.23.2 calls itself a patch release, yet it rolls up ~2,000 PRs merged since v0.23.1. Judge by the notes, and never skip the stand run.
- **In place:** `hermes update --yes` pulls the code, syncs the dependencies and migrates the config. That is enough for a small release, once two things are done:
  - the release has had a stand run;
  - the home and the install dir are backed up.

  It restarts every running gateway itself, `hermes-api.service` included, so start it only when no `/ai` turn is in flight: `ss -Htn state established '( sport = :4097 )'` prints nothing. The directory keeps its name, `hermes-agent-v0.23`, whatever version it then holds.
- **Side by side**, the procedure above: when the release notes change the runtime, the config format or the API server.
