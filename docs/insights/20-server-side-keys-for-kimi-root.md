# §20. Server-side keys for Kimi (/root/.kimi-code/.env)

Kimi runs unisolated as root on the prod box (<exit-host>). To let it diagnose the entry node (<entry-host>) without prompting for credentials and to let it sync the repo, two private keys live next to it:
- `/root/.ssh/entry_node_kimi` — SSH key for `root@<entry-host>` (entry). Public half is added to that box's `~root/.ssh/authorized_keys`.
- `/root/.ssh/github_kimi` — SSH key for `git@github.com`. Public half is registered on the GitHub repo as a deploy key with write access.
- `/root/.ssh/config` aliases: `Host entry-node` → <entry-host>, `Host github.com` → github with the right IdentityFile.
- `/root/.gitconfig` sets `user.name=kimi-bot` / `user.email=kimi-bot@local` so commits Kimi creates aren't anonymous.
- `/root/.kimi-code/.env` exports: `ENTRY_NODE_IP=<entry-host>`, `ENTRY_NODE_SSH_KEY=/root/.ssh/entry_node_kimi`, `REPO_PATH=/opt/vpn-bot`, `GITHUB_SSH_KEY=/root/.ssh/github_kimi`. The Kimi bridge already inherits the process env, so these reach the CLI for free.

**Security note**: anyone with shell on <exit-host> can read those keys. Rotate any time the host is suspected compromised. The user accepted this tradeoff for the convenience of one-click skill execution.
