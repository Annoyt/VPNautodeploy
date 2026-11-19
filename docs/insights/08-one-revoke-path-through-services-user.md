# §8. One revoke path through `services/user_lifecycle.py`

`/reject`, `/ban`, the Reject and Revoke inline callbacks, plus the dashboard `reject`/`ban` actions all used to set state and only sometimes remove the x-ui client / clear `uuid+email`. The single helper `revoke_user_key(user, xui, db)` does both. `/unban` clears leftover `uuid+email` defensively so legacy rows can't re-issue. `/reset` and the `_reset_approval` callback now also zero `reject_count`, otherwise a user who hit `MAX_REJECT_RETRIES` stayed locked out forever after admin reset.
