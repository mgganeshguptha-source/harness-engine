"""Read ACTUAL AI-credit consumption from GitHub's billing API.

Why this exists
---------------
The per-phase figure printed by _report() is a token-priced ESTIMATE. It has
measured at roughly 90-95% of the real charge, i.e. it reliably runs UNDER.
This module reads the number GitHub itself bills, so a run can report a real
consumed-credits delta alongside the estimate.

What it reads
-------------
The web page github.com/settings/copilot/features ("Included usage: 110 / 1,500
AI credits") is HTML behind a browser session — a PAT cannot read it. The API
equivalent for a PERSONAL Copilot plan is:

    GET /users/{username}/settings/billing/ai_credit/usage

It returns one usageItems[] entry per MODEL for the current billing month. The
figure that matches the settings page is the sum of **grossQuantity** across
those entries.

Which field, and why it matters
-------------------------------
Observed live on a Copilot Pro account (Aug 2026):

    model "Claude Haiku 4.5" gross 149.40931  discount 149.40931  net 0.0
    model "GPT-5 mini"       gross 164.09129  discount 164.09129  net 0.0

    grossQuantity    credits CONSUMED                      <- what we want
    discountQuantity the part covered by the included pool
    netQuantity      credits actually CHARGED (the overage)

While consumption sits inside the monthly allowance, discount == gross and net is
0.0. Summing netQuantity would therefore report "0 credits used" for every run
until the 1,500 pool is exhausted. So this module sums grossQuantity, and reports
netQuantity separately only when a real overage exists.

Token permission
----------------
Requires a fine-grained PAT with **"Plan" user permissions (read)**, under Account
permissions — a DIFFERENT section from Repository permissions, and different from
"Copilot Requests: Read". A token that runs the harness fine returns
403 "Resource not accessible by personal access token" until Plan:Read is added
and the token is REGENERATED (editing an existing token has been observed not to
take effect).

Personal vs organization (BCBSM)
--------------------------------
This reader targets a personal plan. Where a Copilot licence is managed and
billed through an org/enterprise, user-level endpoints return nothing for that
user — the org endpoint must be used instead and it needs org Administration:read
(admin only, not a developer's token).

Note the docs describe different product/sku spellings for personal vs org, but
the live personal response above actually used the "org" spelling
(product "Copilot" / sku "Copilot AI Credits"). Matching on product or sku is
therefore unsafe; this module matches on unitType only, and accepts both
"ai-credits" and "credits".

So credit reporting is expected to be UNAVAILABLE for BCBSM developers running
the harness with their own token. That is a known limitation, not a bug — the
run still completes and simply reports the estimate alone.

Failure policy
--------------
A billing read must NEVER fail a run. Every error path returns None, and the
caller prints an "unable to read" line pointing at GitHub to verify manually.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_API = "https://api.github.com"
_API_VERSION = "2026-03-10"
_TIMEOUT = 20

# Both spellings seen in the billing schema; match case-insensitively so a
# future rename of one of them does not silently zero the total.
_CREDIT_UNITS = {"ai-credits", "credits"}


def _token() -> str | None:
    for var in ("COPILOT_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _get(url: str, token: str):
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", _API_VERSION)
    req.add_header("User-Agent", "harness-engine")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _viewer_login(token: str) -> str | None:
    """Resolve the account the token belongs to, so the caller need not pass it."""
    try:
        return (_get(f"{_API}/user", token) or {}).get("login")
    except Exception:
        return None


def read_credits_used(log=print) -> float | None:
    """Credits consumed in the CURRENT billing month, or None if unreadable.

    Returns a float because partial credits are reported (a cheap call can cost
    a fraction of a credit). The caller subtracts two readings to get the run's
    actual consumption.
    """
    token = _token()
    if not token:
        log("  [credits] no token in env — cannot read AI-credit usage")
        return None

    login = os.environ.get("GITHUB_REPOSITORY_OWNER") or _viewer_login(token)
    if not login:
        log("  [credits] could not resolve the account for this token")
        return None

    url = f"{_API}/users/{login}/settings/billing/ai_credit/usage"
    try:
        data = _get(url, token)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            log("  [credits] 403 — token lacks 'Plan' user permission (read). "
                "Add it, REGENERATE the token, and update the secret.")
        elif e.code == 404:
            log("  [credits] 404 — no personal AI-credit usage for this account. "
                "Expected when the Copilot licence is billed via an org/enterprise.")
        else:
            log(f"  [credits] billing API returned HTTP {e.code}")
        return None
    except Exception as e:
        log(f"  [credits] billing API unreachable ({e.__class__.__name__})")
        return None

    items = (data or {}).get("usageItems") or []
    if not items:
        # A valid empty report — no usage yet this month is a legitimate 0.
        return 0.0

    total = 0.0
    billable = 0.0
    matched = 0
    for it in items:
        if str(it.get("unitType", "")).lower() not in _CREDIT_UNITS:
            continue
        try:
            # grossQuantity is CONSUMPTION — the figure the settings page shows as
            # "N / 1,500 AI credits". netQuantity is what is CHARGED once the
            # included allowance is exhausted, so while usage is covered by the
            # monthly pool discountQuantity == grossQuantity and netQuantity is
            # 0.0. Summing netQuantity would therefore report 0 credits used for
            # every run until the pool runs out — measured against a real account
            # response, gross 313.50 vs net 0.0.
            total += float(it.get("grossQuantity") or 0)
            billable += float(it.get("netQuantity") or 0)
            matched += 1
        except (TypeError, ValueError):
            continue

    if matched == 0:
        # Items came back but none in credit units — the schema moved.
        log("  [credits] usage report had no credit-unit items "
            "(billing schema may have changed)")
        return None
    if billable > 0:
        log(f"  [credits] note: {billable:.2f} credits are beyond the included "
            f"allowance and are actually billed")
    return total
