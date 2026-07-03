#!/usr/bin/env bash
# DEPLOY (#28) — nftables egress allowlist DNS refresh (the daemon T2.4's prototype deferred).
#
# Resolves the allowlisted hostnames (/etc/ovt/egress-domains.txt) through the PINNED resolver
# (1.1.1.1 — the only DNS destination the egress table permits) and rewrites the
# inet/ovt_egress/@egress_allow{,6} sets so a provider IP rotation is not missed.
#
# Fail-safe posture:
#   * If resolution yields ZERO IPv4 addresses overall, ABORT without touching the sets — a
#     transient DNS outage must not wipe the allowlist and cut the worker off mid-lease.
#   * Established connections survive a refresh (the chain accepts ct state established).
#   * The flush+add window is a few ms; a new connection racing it just retries (the worker's
#     HTTP paths retry transient failures).
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
    [[ "$ip" == *:* ]] && v6+=("$ip")
  done < <(dig +short +time=5 +tries=2 @"$RESOLVER" AAAA "$domain" || true)
done < "$DOMAINS_FILE"

if [[ ${#v4[@]} -eq 0 ]]; then
  echo "egress-refresh: resolution produced no IPv4 addresses — keeping existing sets" >&2
  exit 1
fi

join() { local IFS=,; echo "$*"; }

nft flush set inet ovt_egress egress_allow
nft add element inet ovt_egress egress_allow "{ $(join "${v4[@]}") }"
if [[ ${#v6[@]} -gt 0 ]]; then
  nft flush set inet ovt_egress egress_allow6
  nft add element inet ovt_egress egress_allow6 "{ $(join "${v6[@]}") }"
fi

echo "egress-refresh: ${#v4[@]} v4 / ${#v6[@]} v6 addresses across $(grep -cvE '^\s*(#|$)' "$DOMAINS_FILE") domains"
