# Technical Signals Layer — deploy checklist

Target: `ssh june` → systemd `june.service` behind nginx at
<https://cassianresearch.com>.

Nothing here has been run against the live box. **Not yet deployed.**

---

## 1. Before you push

**Check the timezone first.** All three cron jobs are interpreted in
`APP_TIMEZONE`. `.env.example` sets `Europe/London`, in which case the shipped
defaults already match the scope doc's UK times and **no overrides are needed**:

```bash
ssh june 'grep APP_TIMEZONE /path/to/june/.env'
```

- `Europe/London` → nothing to do. 21:30 = 21:30 London.
- Anything else (`UTC`, unset) → set the three `SIGNALS_*_TIME` values to the
  local-clock equivalents of 21:30 / 21:45 / 18:00 UK, and remember they shift
  with BST.

**Set the one required secret.** The inbound-email endpoint fails closed:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'   # → SIGNALS_INTAKE_WEBHOOK_SECRET
```

Without it, `/api/signals/intake/email` returns 401 for everything. That is the
intended state until the email provider is wired — the rest of the layer works.

Everything else is optional; see the Technical Signals block in `.env.example`.

---

## 2. Deploy

```bash
ssh june
cd <repo>
git pull
uv sync                      # no new dependencies — PyMuPDF/httpx already present
sudo systemctl restart june.service
sudo systemctl status june.service --no-pager
journalctl -u june.service -n 50 --no-pager
```

`init_db()` runs at import and is idempotent, so the SQLite schema and its
migrations apply on restart with no manual step. The DB lands at
`runtime/signals/signals.db` (gitignored).

---

## 3. Verify

```bash
# Engine health — provider, counts, staleness, TD sign-off state
curl -s https://cassianresearch.com/api/signals/status | jq

# Seed a few names and score them
curl -sX POST 'https://cassianresearch.com/api/signals/watchlist?symbol=SPY&source_tag=OWN'
curl -sX POST https://cassianresearch.com/api/signals/run | jq '.result'

# Dry-run the alert rules — sends nothing
curl -sX POST https://cassianresearch.com/api/signals/alerts/evaluate | jq

# Preview the digest without sending
curl -s https://cassianresearch.com/api/signals/digest | jq -r '.message'
```

All of these are session-authenticated, so run them from a logged-in browser or
with the session cookie.

Then open <https://cassianresearch.com/signals> and confirm: the ranked table
renders, a row expands with a component breakdown and a price chart, and a
lookup on any ticker returns a card.

**Expect an empty first night.** §8.1 alerts only fire on band *transitions*, and
a transition needs two days of scores. Day one is silent by design — that is not
a fault.

---

## 4. Confirm the schedule registered

```bash
journalctl -u june.service | grep -i "signals"
```

Three jobs should exist: `signals_daily`, `signals_digest`,
`signals_intake_digest`.

---

## 5. Rollback

Nothing in this layer mutates existing tables or existing routes — it adds
`/api/signals/*`, `/signals`, a nav link on four pages, and one SQLite file. To
back it out: `git revert` the commit and restart. `runtime/signals/` can be
deleted independently and will be recreated empty.

---

## Deferred to when the provider is chosen

- **Inbound email (§7.2).** Needs an account (Postmark/Mailgun/SendGrid), MX/DNS
  on the intake address, and Fred's forwarding rules. The signature scheme
  implemented is HMAC-SHA256 over the raw body (Mailgun-style); Postmark and
  SendGrid differ, and only `intake.verify_signature` changes.
- **Twelve Data (§3.3).** Provider implemented and unit-tested against stubbed
  responses, never against the live API. Set `TWELVE_DATA_API_KEY` and
  `SIGNALS_OHLCV_PROVIDER=twelvedata`; treat the first run as the integration
  test and watch `journalctl` for parse errors.
