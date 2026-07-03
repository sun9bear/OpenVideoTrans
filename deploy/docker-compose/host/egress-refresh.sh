#!/usr/bin/env bash
# DEPLOY (#28) — nftables egress allowlist DNS refresh (the daemon T2.4's prototype deferred).
#
# Resolves the allowlisted hostnames (/etc/ovt/egress-domains.txt) through the PINNED resolver
# (1.1.1.1 — the only DNS destination the egress table permits) and rewrites the
# inet/ovt_egress/@egress_allow{,6} sets so a provider IP rotation is not missed.
#
# Correctness properties (each earned a review finding):
#   * ATOMIC: the flush + repopulate of BOTH sets is a single `nft -f` transaction. Separate
#     flush/add invocations leave a window where the set is empty (all egress dropped) and, worse,
#     a failed add after a committed flush strands the allowlist empty. One transaction = all-or-nothing.
#   * BOTH sets ALWAYS rewritten: v6 is flushed even when AAAA resolution is now empty, so dropping
#     the last IPv6-capable domain does not leave stale IPv6 destinations reachable.
#   * DEDUP: `flush + add` in one file tolerates duplicates fine, but we still unique the lists so a
#     Cloudflare-fronted multi-domain set doesn't bloat.
#   * FAIL-SAFE: if IPv4 resolution yields ZERO addresses overall, ABORT without touching the table —
#     a transient DNS outage must not wipe the allowlist and cut the worker off mid-lease. Established
#     connections survive a refresh regardless (the chain accepts ct state established).
set -euo pipefail

DOMAINS_FILE=${OVT_EGRESS_DOMAINS:-/etc/ovt/egress-domains.txt}
RESOLVER=1.1.1.1

[[ -r "$DOMAINS_FILE" ]] || { echo "egress-refresh: missing $DOMAINS_FILE" >&2; exit 1; }

v4=()
v6=()
while IFS= read -r domain; do
  domain=${domain%%#*}; domain=$(echo "$domain" | tr -d '[:space:]')
  [[ -z "$domain" ]] && continue
  while IFS= read -r ip; do
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && v4+=("$ip")
  done < <(dig +short +time=5 +tries=2 @"$RESOLVER" A "$domain" || true)
  while IFS= read -r ip; do
    [[ "$ip" =~ ^[0-9a-fA-F:]+$ && "$ip" == *:* ]] && v6+=("$ip")
  done < <(dig +short +time=5 +tries=2 @"$RESOLVER" AAAA "$domain" || true)
done < "$DOMAINS_FILE"

if [[ ${#v4[@]} -eq 0 ]]; then
  echo "egress-refresh: resolution produced no IPv4 addresses — keeping existing sets" >&2
  exit 1
fi

# unique + comma-join for nft element syntax.
mapfile -t v4 < <(printf '%s\n' "${v4[@]}" | sort -u)
[[ ${#v6[@]} -gt 0 ]] && mapfile -t v6 < <(printf '%s\n' "${v6[@]}" | sort -u)
join() { local IFS=,; echo "$*"; }

# One transaction: flush BOTH sets then re-add. `add element` on an empty list is a no-op, so the v6
# lines are always emitted (pruning stale v6 when AAAA went empty). If any line fails, nft -f rolls
# back the whole file — the previous allowlist stays intact.
{
  echo "flush set inet ovt_egress egress_allow"
  echo "flush set inet ovt_egress egress_allow6"
  echo "add element inet ovt_egress egress_allow { $(join "${v4[@]}") }"
  [[ ${#v6[@]} -gt 0 ]] && echo "add element inet ovt_egress egress_allow6 { $(join "${v6[@]}") }"
} | nft -f -

echo "egress-refresh: ${#v4[@]} v4 / ${#v6[@]} v6 addresses across $(grep -cvE '^\s*(#|$)' "$DOMAINS_FILE") domains"
