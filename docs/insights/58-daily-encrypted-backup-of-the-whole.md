# §58. Daily encrypted backup of the whole service → the «Бэкапы» topic (2026-11-17)

Until now entry's live `bot.db` (67 MB) had no automated backup, only the copies taken by hand before deploys in entry's `/opt/backups`. Exit's `vpn-bot-backup.timer` (`scripts/backup.sh`, §17) kept 7 local tarballs. Nothing left either host. The owner's decisions (2026-11-17): a forum topic «Бэкапы» in the admin group, age with the private key on his machine only, and everything that makes recovery simpler, Hermes' settings included. The DE reserve stays out (the bot re-provisions its clients).

**Installed on entry on 2026-11-17,** on the owner's word, by `docs/runbooks/backup_restore.md`, «Установка». That section stays as the procedure for a reinstall or a new entry.
- The bot was deployed at `a963b2c`, and age 1.1.1 came from apt. zstd was already there and was left alone: `apt-get install` upgrades an installed package, and here it would have pulled a new libzstd onto prod.
- The first archive went out at 04:58 UTC: 58.4 MB, in two parts. It opened with the owner's key, and every file's sha256 and every database's `integrity_check` matched.
- **Size:** ~58 MB a day after zstd, so it always comes in two parts. Most of it is Hermes' `state.db` (183 MB raw) and `bot.db` (64 MB); it grows with `state.db`. A run takes ~2 min and 25 s of CPU. The work directory peaks at ~0.5 GB, and seven kept archives take ~410 MB at today's size, so keep about 1 GB free on entry.
- **Absent, and that is normal:**
  - entry has no `/etc/shadow-tls*`: its settings are in the unit's ExecStart, which is in the archive and in the facts;
  - exit has no `/etc/iptables`: its live `iptables-save` is in the facts;
  - exit has no `/etc/hy2-auth-relay.env`: the file is optional (§56);
  - the GeoIP city database (127 MB) is over the 20-MB cap, so it is only listed. The bot downloads a missing one itself. The ASN and country databases (18 MB) are packed.

- **What runs where.**
  - Entry, as root: `scripts/service_backup.py`, from the root-owned copy `scripts/install_service_backup.sh` puts in `/usr/local/lib/nekovpn-backup/` (never the rsync target, §36). Timer `nekovpn-backup.timer`: daily 01:40 UTC ± 10 min, `Persistent=true`. Work and local copies go to `/var/lib/nekovpn-backup` (0700).
  - Exit: nothing is installed. The same file runs there over the healthcheck's ssh alias (§50), in one round-trip: `ssh exit-node python3 -I - --collect exit`, the file on stdin.
  - The bot: `scripts/backup_send.py` (logic in `bot/services/backups.py`) uploads through the bot's `TG_PROXY_URLS` pool. Entry cannot reach api.telegram.org itself (§24).
- **The archive** `nekovpn-backup-<UTC>.tar.zst.age`: tar, then zstd -12, then age to the public key(s) in `/etc/nekovpn-backup/recipients.txt`. Inside:
  - `README.md`: every path with what it is, what was absent or skipped, the restore order;
  - `MANIFEST.json`: every file with its size and sha256, the databases' `integrity_check` and row counts;
  - `entry/entry.tar` and `exit/exit.tar`: each node's files at their own paths (relative to /, owners and modes kept), plus `nekovpn-facts/` (live `iptables-save`, `docker ps` / `inspect` of our containers, `systemctl cat`, package and tool versions, `crontab -l`). `nekovpn-manifest.json` closes each part; a part without it was cut short and is left out.
- **What goes in** (the item tables in the script; a test keeps `HERMES_SKILLS` equal to the repo's `skills/`):
  - entry: the bot's volume, the local panel's `x-ui.db`, `/opt/vpn-bot/.env` and compose, the sidecars' configs, haproxy, shadow-tls, `/etc/ru-exit`, `/etc/ssh-relay`, iptables and ufw, `/etc/systemd/system`, our root copies, the age public key, Hermes (below), `/opt/site-vpn-kzstomsk`;
  - exit: `x-ui.db`, compose and `.env`, the panel's certs, `/etc/hysteria`, the units and the entry-hop drop-in, Caddy (`/etc/caddy` and its certificates), `/etc/nekovpn-hop`, `/etc/xhr-exit*`, `/etc/ssh-exit*`, `/etc/exit-dpi.env`, the hy2 relay, tinyproxy, acme.sh (with its Cloudflare token) and `/etc/ssl/{sub,cdn}`;
  - both: `/root/.ssh`, sshd config and host keys (the same fingerprint after a move), crontabs, sysctl, `gai.conf`, docker's `daemon.json`, logrotate, the journald cap, text files in `/usr/local/{bin,sbin}`.
  - Hermes: settings and state only, each path named: `config.yaml`, `.env`, `auth.json`, `SOUL.md`, `memories/`, `cron/`, our skills, consistent copies of `state.db`, `response_store.db` and `kanban.db`, and `/root/hermes-work/AGENTS.md`.
    - Its runtime stays out: v0.23.1 keeps about 1.5 GB in `tools/` (its own Python, node, ffmpeg), plus caches, `logs/`, `sandboxes/`, 44 stock skills, `*.lock` and `*.pid`.
    - After the v0.23 update `~/.hermes` may be a link to `/root/.hermes-v023`. The files then go in under the real directory, with the link next to them, so GNU tar puts the same layout back.
    - The same holds for any linked directory on the way to an item. Absolute targets count from the node's own root, so nothing outside it is ever followed.
  - Out: logs, caches, venvs, images. Binaries in `/usr/local/{bin,sbin}` and any file over 20 MB are listed in the README, not packed. The foreign container `cavitator` is never inspected, and its lines are dropped from the container lists.
- **bot.db is copied inside the container**, as the bot's user (`docker exec vpn-bot python3 -I -c …`, sqlite's backup API into the container's `/tmp`, streamed out and deleted). bot.db is in WAL mode and the bot runs as UID 1000. A root process on the host that opens it while the bot holds no connection creates root-owned `-wal`/`-shm` files, and the bot's next write fails. The panels' `x-ui.db` copies are made on the host: 3x-ui runs as root.
- **Nothing a remote host made is unpacked on entry.** Exit's part and the container's bytes are listed and kept whole, so a compromised exit or bot cannot write files on entry as root. Their streams start with a marker line (an ssh banner before it is dropped) and are capped.
- **Sending.**
  - `backup_send.py send` refuses anything that is not an age file, a wrong sha256 (a cut transfer), and a name that is not `nekovpn-backup-<UTC>.tar.zst.age`. A bug upstream cannot post a plaintext archive into the group.
  - Over 45 MB, the archive goes in parts `.001`, `.002`… (the Bot API takes 50 MB). The caption says how to join them.
  - **The topic** is created on the first send (`createForumTopic` in `FORUM_GROUP_ID`) and stored in `app_settings.backup_topic` (`{chat_id, thread_id, created}`). It is created anew when Telegram says the thread is gone, or when the group changed, never on any other failure: a dead proxy makes no topic. `backup_send.py topic --set ID` pins an existing one.
  - `TelegramClient`: `send_document_bytes(raise_errors=True)` raises instead of returning None, and every `_request` failure carries Telegram's `tg_description`, so the sender tells a deleted topic from a dead proxy.
- **Record and alert.** `app_settings.backup_state` = `{installed, last_ok, last_sent, last_run}`.
  - The installer writes `installed` (`backup_send.py installed`), each send writes `last_sent` (and `last_ok` when complete), the script reports failures (`backup_send.py fail`).
  - `backup:stale` (AlertManager `check_backup`, critical, `min_cycles` 2) fires when no complete backup went out for 26 h, when a run failed or went partial before the first complete one, or when the timer, installed 26 h ago, never ran. The detail names the last run's reason.
  - Critical because a warn stays on the dashboard (`ALERT_TG_MIN_SEVERITY` is critical on prod), and a missing backup is the one thing nobody notices until the day it is needed.
  - No state = not installed: silence. A failed read judges nothing. `install_service_backup.sh --remove` clears the state (`backup_send.py forget`).
- **Exit codes**: 0 = complete and sent; 1 = sent without a part (exit unreachable, bot.db's copy failed) or with a missing required file; 2 = nothing sent (the archive, if made, stays in `/var/lib/nekovpn-backup/archives`, the last 7 kept, all encrypted). `--dry-run` lists paths and sizes and leaves nothing behind; `--no-send` keeps the archive locally. One run at a time (a lock). systemd's stop (the unit's 45-min timeout, a shutdown) unwinds instead of killing: children killed, the plaintext work directory removed, the bot told. A run killed outright leaves it to the next run, which removes it first.
- **The key.**
  - Generated 2026-11-17 on the owner's machine with Debian's `age-keygen` 1.2.1, in a throwaway container without network: private `~/.config/nekovpn-signing/nekovpn-backup-age.key` (0600), public `.pub` next to it, `age18kpa37v26zpe3t0hzaznfwur8g9e95hcl6awzta9yue7ard3e5cqjtk48j`.
  - Back it up offline with the APK key: losing it makes every archive useless.
  - The installer accepts only an `age1…` public key; a secret key is refused and never echoed. Replacing an installed key needs `--force`. The script refuses to run while the recipients file holds a secret key.
- **Restore**: `docs/runbooks/backup_restore.md`. Its check block (decrypt, unpack, sha256 of every file against the manifest, `PRAGMA integrity_check` of both databases) runs as written in the tests. Restoring a database means removing the old `-wal`/`-shm` first, or sqlite applies the old journal to the restored file.
- **Exit's old local timer** (`vpn-bot-backup.timer`, §17) is unchanged: a second, local copy.
- **Tests**:
  - `tests/unit/test_service_backup.py`: the collector in-process; whole runs of the script as a file with fake ssh / docker / zstd / age and tripwires first in PATH; the restore stand with real `age-keygen` / `age` / `zstd`;
  - `tests/integration/test_backup_send.py`: the sender, the topic, the state and the CLI, on a real sqlite;
  - `tests/integration/test_backup_alert.py`: the verdict and a real AlertManager tick;
  - `tests/unit/test_install_service_backup_sh.py`: the installer and the units;
  - `TestRaiseErrors` in `tests/unit/test_telegram_send_document.py`.

  CI installs age and zstd and sets `BACKUP_REQUIRE_AGE=1`, so the stand fails rather than skips. All run first in `nekovpn-tests:py311`.

  68 mutants killed, each on a throwaway copy with a fresh bytecode cache, restored by a positional reverse replacement with a hash check. They cover the secret-key and recipient checks, the excludes, the caps and their edges, binaries, links leaving the root, the reserved live `-wal`, the part's closing manifest, the stream marker and cap, the ssh retry, part and archive completeness, the foreign-container filter, pruning, the age output check, the send result, the lock, the plaintext work dir, integrity problems, the dry run, the bot.db copy, only-age, sha256, the topic rules, the alert's edges and severity, the installer's guards, `tg_description`, the stop handling, and Hermes' files and the link resolution. One equivalent mutant led to removing a redundant branch in `split_parts`.
