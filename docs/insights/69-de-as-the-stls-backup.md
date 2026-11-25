# §69. DE as the stls backup: when exit's tunnel is down, stls goes to DE (2026-11-25)

On 2026-11-25 the hosting powered exit off for 37 minutes (11:53–12:30 UTC), and nothing that goes through exit worked. The owner asked to send users through DE in such a case, all users and not only the paid ones: «давай в случае сбоя всех». This covers stls, the main protocol since 2026-11-19. Like §64 and §68 it was set up by hand on prod; this section describes it, with the two scripts in full.

## Why a copy of the keys on DE

Inside stls is Shadowsocks-2022 (`2022-blake3-aes-128-gcm`, multi-user): one server key plus one key per user, on exit's inbound :8444. DE knew none of them: its panel only holds the paid users' VLESS Reality keys (§68). So DE got its own Shadowsocks-2022 inbound with the same keys, and entry got a switch in front of the two.

## The path

```
client -> entry :443 shadow-tls -> 127.0.0.1:19443 haproxy (frontend/backend stls_out)
            server exit 127.0.0.1:19442  (sshd -R from exit, nekovpn-revssh) -> exit :8444
            server de   172.17.0.1:19544 backup (sshd -R from DE, nekovpn-revssh-de) -> DE 127.0.0.1:18444
```

## On DE

- `nekovpn-de-backup.service` runs `/usr/local/x-ui/bin/xray-linux-amd64 run -c /etc/nekovpn-de-backup/config.json`.
  - It uses the panel's own xray 26.6.22 and `XRAY_LOCATION_ASSET=/usr/local/x-ui/bin`.
  - The settings are `MemoryMax=200M` and `ConditionPathExists` on the config. It takes about 6 MB.
  - The inbound listens on 127.0.0.1:18444 only. Users reach it through the tunnel and nothing else.
  - Its log level is `warning`, with no access log.
- `/usr/local/sbin/nekovpn-de-backup-apply` (below) builds that config from the key list on stdin.
  - It is the forced command of exit's push key, set in root's `authorized_keys`: `restrict,from="84.75.76.109",command="/usr/local/sbin/nekovpn-de-backup-apply"`.
  - Only the keys come from exit. The inbound, the outbounds and the routing are fixed in the script, so a compromised exit cannot open anything but this loopback inbound.
  - Its routing is the guards of DE's own xray (ports 62789/11111/8081, `geoip:private`, bittorrent) plus DE's public address.
  - It checks every key's length and every email, runs `xray run -test`, and restarts the backup only when the config changed.
- The tunnel (`nekovpn-revssh-de`, §68) got a fourth forward, `-R 172.17.0.1:19544:127.0.0.1:18444`.

## On exit

- `nekovpn-de-backup-push.timer` runs every 5 minutes, and 3 minutes after boot. It starts `nekovpn-de-backup-push.service`, which runs `/usr/local/sbin/nekovpn-de-backup-push` (below).
- The script reads the running config (`docker exec 3x-ui cat bin/config.json`). Disabled and expired clients are already out of it.
- It sends DE the :8444 inbound's method and server key and each client's email and key: `ssh -T` with `/root/.ssh/de_backup_push`, DE's host key pinned in `/etc/nekovpn-de-backup/known_hosts`.
- When exit is down nothing is sent, and DE keeps the last list.

## On entry

- `/etc/ssh/sshd_config.d/61-nekovpn-de.conf` lets `nekovpn-de` hold 172.17.0.1:19544 as well (§68).
- haproxy: frontend and backend `stls_out` (block at the end of `haproxy.cfg`; backup `haproxy.cfg.bak-stls-failover-20261125-140900`):
  - `server exit 127.0.0.1:19442 check inter 10s fall 2 rise 2 observe layer4 error-limit 10 on-error mark-down`;
  - `server de 172.17.0.1:19544 check inter 30s fall 2 rise 2 backup`;
  - `no log`: one line per stls connection would be too much. Server state changes still go to `/var/log/haproxy.log` as `stls_out/exit is DOWN/UP`.
  - The checks are TCP connects to the sshd listeners. exit's xray logs nothing for such an empty connection (checked on 2026-11-25), so the DPI reporter (§66) does not count them as rejects.
- `shadow-tls.service`: `--server 127.0.0.1:19443`, was 19442. Backup `/root/shadow-tls.service.bak-stls-failover-20261125-140941`. The NekoVPN server-first twin (`:2444`, §62) goes through shadow-tls and gets the same failover.

## How it behaves

- **When exit's tunnel closes cleanly** (exit reboots, its sshd stops), entry's sshd drops the listener 19442 at once. haproxy marks `exit` DOWN after two checks, about 20 s, and new stls connections go to DE.
- **After a hard power-off,** as on 2026-11-25, entry's sshd holds the dead session until ClientAlive gives up (15 s × 4). Until then connections hang. The failover comes about 1.5 minutes after the cut.
- **When exit comes back,** its tunnel reconnects and `exit` is UP again after two checks. New connections go to exit; sessions already on DE stay there until the client reconnects.
- **Not covered:**
  - hy2: it exists only on exit;
  - Reality and ws: off;
  - xhr;
  - the SSH emergency path.
- **During a failover:**
  - traffic through DE is not counted against quotas: exit's panel counts, DE's backup does not;
  - users created since the last push cannot connect, and nobody would without the backup either.
- **The stls probe goes the users' way,** so during a failover it reads OK through DE. Exit's outage shows on `reach:via:tun`, hy2 and the other protocols. The bot's alerts get out through DE's proxy (§68).
- **DE is small:** 1 CPU, 925 MB, and other people's services on it. It carries everyone for a short outage, not for days.

## Checked on 2026-11-25

- **DE directly** (a throwaway sing-box with the probe user's stls key, to 172.17.0.1:19544): `generate_204` 204; the address seen outside is 212.60.153.208; Telegram answers 302.
- **Through haproxy** (127.0.0.1:19443): 204; the address seen outside is exit's 84.75.76.109, the primary.
- **After switching shadow-tls,** the stls probe through the users' real path answered 204 from exit's address. Within seconds there were 35 new connections, no errors.
- **Pushes:** 88 clients went to DE. The second push answered `unchanged`.
- **No forced failover drill was run.** It needs either a dead exit or a runtime switch for haproxy (there is no stats socket).

## Rollback

- entry: put back the shadow-tls backup (`--server 127.0.0.1:19442`), `systemctl daemon-reload && systemctl restart shadow-tls`.
- entry: remove the `stls_out` block, `haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy`.
- exit: `systemctl disable --now nekovpn-de-backup-push.timer`.
- DE: `systemctl disable --now nekovpn-de-backup`, and remove the `exit-de-backup-push` line from root's `authorized_keys` (backup `authorized_keys.bak-debackup-20261125-140348`).
- Drop the fourth `-R` from `nekovpn-revssh-de` and `172.17.0.1:19544` from `61-nekovpn-de.conf`.

## Troubleshooting

- Which way stls goes now:
  - `ss -tn state established '( dport = :19442 )'` counts the connections to exit;
  - `ss -tn state established '( dport = :19544 )'` counts the connections to DE;
  - `grep stls_out /var/log/haproxy.log` shows the state changes.
- Pushes, on exit: `journalctl -u nekovpn-de-backup-push`. Each run prints `sent N clients; DE: …`, and DE answers `installed`, `unchanged` or `refused: <reason>`.
- The backup, on DE: `systemctl status nekovpn-de-backup`, and `/etc/nekovpn-de-backup/config.json` (root only, it holds the keys).

## `/usr/local/sbin/nekovpn-de-backup-apply` (DE)

```python
#!/usr/bin/python3 -I
"""Forced command of exit's push key (2026-11-25): builds this host's stls backup,
Shadowsocks-2022 on 127.0.0.1:18444, from the key list exit sends on stdin.

stdin: {"method": ..., "password": <server key>, "clients": [{"email": ..., "password": ...}]}
Only the keys come from exit. The inbound, outbounds and routing are fixed here, so a
compromised exit cannot open anything but this loopback inbound. Users reach it only
through the DE -> entry tunnel (nekovpn-revssh-de, 172.17.0.1:19544 on entry), when
entry's haproxy finds exit's tunnel down.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile

DIR = '/etc/nekovpn-de-backup'
CFG = os.path.join(DIR, 'config.json')
XRAY = '/usr/local/x-ui/bin/xray-linux-amd64'
ENV = {'XRAY_LOCATION_ASSET': '/usr/local/x-ui/bin', 'PATH': '/usr/sbin:/usr/bin:/sbin:/bin'}
UNIT = 'nekovpn-de-backup'
METHODS = {'2022-blake3-aes-128-gcm': 16, '2022-blake3-aes-256-gcm': 32}
EMAIL = re.compile(r'^[A-Za-z0-9_.@+-]{1,128}$')
OWN_IPS = ['212.60.153.208']


def fail(msg):
    print('refused: ' + msg)
    sys.exit(1)


def key_ok(k, n):
    try:
        return isinstance(k, str) and len(base64.b64decode(k, validate=True)) == n
    except Exception:
        return False


def unit_state():
    return subprocess.run(['systemctl', 'is-active', UNIT], capture_output=True,
                          text=True, env=ENV).stdout.strip()


try:
    d = json.loads(sys.stdin.buffer.read(4_000_000))
except Exception:
    fail('not json')
method, server, clients = d.get('method'), d.get('password'), d.get('clients')
if method not in METHODS:
    fail('method')
n = METHODS[method]
if not key_ok(server, n):
    fail('server key')
if not isinstance(clients, list) or not 1 <= len(clients) <= 5000:
    fail('client list')
out, seen = [], set()
for c in clients:
    if not isinstance(c, dict):
        fail('client')
    email, key = c.get('email'), c.get('password')
    if not isinstance(email, str) or not EMAIL.match(email) or email in seen:
        fail('email')
    if not key_ok(key, n):
        fail('client key')
    seen.add(email)
    out.append({'email': email, 'password': key})

cfg = {
    'log': {'loglevel': 'warning', 'access': 'none'},
    'inbounds': [{
        'tag': 'stls-backup', 'listen': '127.0.0.1', 'port': 18444, 'protocol': 'shadowsocks',
        'settings': {'method': method, 'password': server, 'network': 'tcp,udp', 'clients': out},
        'sniffing': {'enabled': True, 'destOverride': ['http', 'tls']},
    }],
    'outbounds': [
        {'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'AsIs'}},
        {'tag': 'blocked', 'protocol': 'blackhole', 'settings': {}},
    ],
    # The same guards as DE's own xray, plus DE's public address: users of the
    # backup have no business with this host's panel, proxy or other services.
    'routing': {'domainStrategy': 'AsIs', 'rules': [
        {'type': 'field', 'port': '62789,11111,8081', 'outboundTag': 'blocked'},
        {'type': 'field', 'ip': ['geoip:private'] + OWN_IPS, 'outboundTag': 'blocked'},
        {'type': 'field', 'protocol': ['bittorrent'], 'outboundTag': 'blocked'},
    ]},
}
data = (json.dumps(cfg, indent=1) + '\n').encode()
old = open(CFG, 'rb').read() if os.path.exists(CFG) else b''
if old == data:
    if unit_state() != 'active':
        subprocess.run(['systemctl', 'restart', UNIT], env=ENV)
    print('unchanged, %d clients, backup %s' % (len(out), unit_state()))
    sys.exit(0)
fd, tmp = tempfile.mkstemp(dir=DIR, prefix='.config.', suffix='.json')
with os.fdopen(fd, 'wb') as f:
    f.write(data)
os.chmod(tmp, 0o600)
r = subprocess.run([XRAY, 'run', '-test', '-c', tmp], capture_output=True, text=True, env=ENV)
if r.returncode != 0:
    os.unlink(tmp)
    lines = (r.stdout + r.stderr).strip().splitlines()
    fail('xray -test: ' + (lines[-1][:200] if lines else 'rc %d' % r.returncode))
os.replace(tmp, CFG)
subprocess.run(['systemctl', 'restart', UNIT], env=ENV)
print('installed %d clients, backup %s' % (len(out), unit_state()))
```

## `/usr/local/sbin/nekovpn-de-backup-push` (exit)

```python
#!/usr/bin/python3 -I
"""Every 5 minutes (nekovpn-de-backup-push.timer): send DE the keys of this exit's stls
inbound (:8444, Shadowsocks-2022), so DE's backup (nekovpn-de-backup there) lets in the
same users when this exit is down and entry's haproxy sends stls to DE (2026-11-25).

Only the inbound's method and server key and each client's email and key leave this host.
The config is the running one (3x-ui's bin/config.json), so disabled and expired clients
are already out. DE builds its own config from the list and restarts only on a change.
"""
import json
import subprocess
import sys

DE = 'root@212.60.153.208'
SSH = ['ssh', '-T', '-i', '/root/.ssh/de_backup_push', '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
       '-o', 'ConnectTimeout=15', '-o', 'UserKnownHostsFile=/etc/nekovpn-de-backup/known_hosts',
       '-o', 'StrictHostKeyChecking=yes', DE]

raw = subprocess.run(['docker', 'exec', '3x-ui', 'cat', 'bin/config.json'],
                     capture_output=True, check=True, timeout=30).stdout
inbound = next(i for i in json.loads(raw)['inbounds']
               if i.get('port') == 8444 and i.get('protocol') == 'shadowsocks')
st = inbound['settings']
payload = {
    'method': st['method'],
    'password': st['password'],
    'clients': [{'email': c['email'], 'password': c['password']} for c in st.get('clients', [])],
}
r = subprocess.run(SSH, input=json.dumps(payload).encode(), capture_output=True, timeout=120)
out = r.stdout.decode(errors='replace').strip().splitlines()
err = r.stderr.decode(errors='replace').strip().splitlines()
print('sent %d clients; DE: %s' % (len(payload['clients']), (out or err or ['no answer'])[-1]))
sys.exit(r.returncode)
```
