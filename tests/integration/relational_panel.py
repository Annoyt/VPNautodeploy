"""A fake exit panel with the fork's relational semantics, for tests that
put keys back on it or rewrite them (AGENTS.md §23, §44, the 2026-08-19
notes):

- an add of an email the panel still has is delete + re-add: a fresh
  traffic row, used bytes zeroed (the bot's duplicate path);
- an add of a detached email re-attaches the record and leaves its
  client_traffics row, the gate xray/hy2 obey, as it was;
- an in-place update rewrites quota, expiry, enable and limit of that
  row, never its used bytes; only resetTraffic zeroes them.

The reads answer like XUIService's: get_client_sync gives the record an
inbound holds (None when no inbound has the client),
get_client_traffic_sync the traffic row in the bot's shape, and
client_attached_sync says "attached" when it cannot tell.
"""

import time

GB = 1024 ** 3


def now_ms():
    return int(time.time() * 1000)


class Panel:
    """Client records and their traffic rows, keyed by email.
    ``fail_read`` makes both reads answer None, as a panel error does."""

    def __init__(self, fail_add=False, fail_update=False, fail_read=False):
        self.attached = set()
        self.rows = {}
        self.records = {}
        self.calls = []
        self.fail_add = fail_add
        self.fail_update = fail_update
        self.fail_read = fail_read

    def has(self, email, *, used=0, total=10 * GB, expiry=0, enable=True,
            limit=1, attached=True):
        self.rows[email] = {'used': used, 'total': total, 'expiry': expiry,
                            'enable': enable, 'limit': limit}
        self.records[email] = {'email': email, 'totalGB': total,
                               'expiryTime': expiry, 'enable': enable,
                               'limitIp': limit}
        if attached:
            self.attached.add(email)

    def client_attached_sync(self, email):
        self.calls.append(('attached?', email))
        return self.fail_read or email in self.attached

    def get_client_sync(self, email):
        self.calls.append(('read', email))
        if self.fail_read or email not in self.attached:
            return None
        return dict(self.records[email])

    def get_client_traffic_sync(self, email):
        self.calls.append(('read', email))
        row = self.rows.get(email)
        if self.fail_read or row is None:
            return None
        return {'upload': row['used'], 'download': 0, 'total': row['total'],
                'enable': row['enable'], 'expiry_time': row['expiry']}

    def add_client_sync(self, client, inbound_id=None):
        email = client['email']
        self.calls.append(('add', email))
        if self.fail_add:
            return False
        fresh = {'used': 0, 'total': client['totalGB'],
                 'expiry': client['expiryTime'], 'enable': client['enable'],
                 'limit': client['limitIp']}
        if email in self.attached:
            self.calls.append(('delete+re-add', email))
            self.rows[email] = fresh
        elif email not in self.rows:
            self.rows[email] = fresh
        self.attached.add(email)
        self.records[email] = dict(client)
        return True

    def sync_client_settings_sync(self, email, updates):
        self.calls.append(('update', email))
        if self.fail_update or email not in self.attached:
            return False
        row = self.rows[email]
        for key, field in (('totalGB', 'total'), ('expiryTime', 'expiry'),
                           ('enable', 'enable'), ('limitIp', 'limit')):
            if key in updates:
                row[field] = updates[key]
        self.records[email].update(updates)
        return True

    def reset_client_traffic_sync(self, email):
        self.calls.append(('reset', email))
        self.rows[email]['used'] = 0
        return True

    async def sync_user(self, chat_id, client):
        self.calls.append(('sync_user', client['email']))
        return self.add_client_sync(client)

    def works(self, email):
        row = self.rows.get(email)
        return bool(
            email in self.attached and row and row['enable']
            and (not row['expiry'] or row['expiry'] > now_ms())
            and (not row['total'] or row['used'] < row['total']))

    def writes(self, email):
        return [c for c, e in self.calls
                if e == email and c not in ('attached?', 'read')]
