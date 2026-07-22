# Fork Notice — AlphaGo Live

This repository is a fork of [marketcalls/openalgo](https://github.com/marketcalls/openalgo)
maintained by AlphaQuark for the **Alpha Live Trading** hosting product (`*.hostingsol.alphaquark.in`).

## License

The original OpenAlgo project is licensed under **AGPL-3.0**. This fork preserves that license
in full. See [`License.md`](./License.md) for the complete text.

Per AGPL Section 13, the complete source code of this fork is and will remain publicly
available at: <https://github.com/alpha112233/alphago_live>

## Fork base

- **Upstream:** `github.com/marketcalls/openalgo`
- **Forked at tag:** `openalgo-remote-mcp` (GitHub release `v2.0.1.0`, published 2026-05-03)
- **Fork created:** 2026-05-12

## Why a fork

OpenAlgo upstream is intentionally designed for **single-user, single-broker** personal use:

- The active broker is derived from a regex on `REDIRECT_URL` env var.
- `BROKER_API_KEY` is read from `os.getenv()` and cached at module-import time.
- The broker selection UI disables all brokers except the one matching `REDIRECT_URL`.

These design choices are correct for OpenAlgo's audience but conflict with our hosting
use case, where one client should be able to add multiple broker accounts and switch
between them from a UI without a container restart.

This fork makes the **minimum** modifications necessary to support multi-broker management
per user. We keep the diff small to make periodic rebasing from upstream straightforward
(target: rebase every 3-6 months in <1 day of work).

## Differences from upstream

> The list below is updated as patches land. Pre-patch, this fork is byte-identical to
> upstream at the fork base above.

| Patch | Files | Purpose |
|---|---|---|
| (pending) | `blueprints/auth.py`, `templates/broker.html` | Decouple active broker from `REDIRECT_URL`; read from per-user `active_broker` field. |
| (pending) | `database/broker_creds_db.py` (new) | Per-user encrypted broker credential storage (SQLite). |
| (pending) | `blueprints/manage_brokers.py` (new), `templates/manage_brokers/*.html` (new) | `/manage/brokers` UI for add/list/activate/remove. |
| (pending) | `blueprints/brlogin.py` | Move `BROKER_API_KEY` read from module-level to function-level. |
| (pending) | `templates/base.html`, `utils/config.py` | Parameterize brand strings (`BRAND_NAME`, `BRAND_TAGLINE`). |

## Upstream relationship

- We do **not** plan to PR these changes upstream. The two projects serve different audiences.
- We pull bug fixes, broker plugins, and new feature work from upstream as appropriate via
  `git fetch upstream && git rebase upstream/main` (or merge, depending on conflict shape).
- Issues specific to OpenAlgo's core functionality should be filed upstream.
  Issues specific to multi-broker hosting / Alpha Live Trading should be filed in this repo.

## Maintainer

AlphaQuark (`pratik@alphaquark.in`)

## Consent gate (Phase 3b) — hosting-agreement enforcement

Fork-only addition. Before placing a LIVE order, the container checks whether its
AlphaQuark hosting agreement is signed (`utils/consent.py::is_consent_blocked`),
using a read-only, subdomain-scoped token from hostingsol (env
`AQ_CONSENT_STATUS_URL` + `AQ_CONSENT_STATUS_TOKEN`, injected at provision time).

- Enforced at the two live-entry service funnels: `services/place_order_service.py`
  (`place_order_with_auth`) and `services/place_smart_order_service.py`
  (`place_smart_order_with_auth`), right after the analyze-mode branch.
- **Exits are never gated** (close-position is not blocked).
- **Fail-open:** blocks ONLY on a definitive `{"signed": false}`; any error /
  timeout / non-200 / unconfigured → allow. A hostingsol outage never freezes trading.
- Config getters: `utils/config.py::get_consent_status_url/token`. Env in `.sample.env`.
