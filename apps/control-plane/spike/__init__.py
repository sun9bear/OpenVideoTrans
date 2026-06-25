"""T2.0 — D1-claim concurrency spike (M2 hard gate).

Proves the optimistic-lock job claim is exactly-once under concurrency on BOTH local SQLite and a
real Cloudflare D1 remote (local SQLite alone can't vouch for D1's distributed transaction
semantics). ``claim`` holds the portable SQL; ``local_spike`` the threaded SQLite harness;
``remote_d1`` the D1-REST runner (token from env). Not a shipped library — a validation tool whose
SQL carries forward to the T2.1 control-plane Worker.
"""
