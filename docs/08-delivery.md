# 08 — Delivery (Gmail send, gated)

`scholar send <run_id>` sends every `APPROVED` draft for a run via the user's
own Gmail account. The whole path — OAuth, MIME construction, daily cap, send
loop — is built and tested. **By default it does nothing.** `SEND_ENABLED=false`
in `.env` is the gate; `send_approved()` short-circuits into a "log only, never
call Gmail" branch and raises `SendingDisabled`.

Implementation: [scholarapp/modules/delivery.py](../scholarapp/modules/delivery.py).

```
                ┌─────────────────────────────────┐
   approved ───▶│        send_approved(run_id)    │
   drafts       │                                 │
                │  ┌──────────────────────────┐   │     SendLog(send_disabled)
                │  │  if not SEND_ENABLED ────┼───┼───▶ per draft, raise
                │  └──────────────────────────┘   │     SendingDisabled
                │                                 │
                │  ┌──────────────────────────┐   │
                │  │  if cap reached  ────────┼───┼───▶ raise DeliveryError
                │  └──────────────────────────┘   │     (no Gmail call)
                │                                 │
                │  ┌──────────────────────────┐   │
                │  │  for each draft:         │   │     SendLog(sent | error)
                │  │    build MIME → send ────┼───┼───▶ Draft.status = sent
                │  │    log result            │   │     (on success)
                │  └──────────────────────────┘   │
                └─────────────────────────────────┘
```

## One-time OAuth setup

You need to do this once before sending will ever work. None of these steps
cost money or change anything that doesn't belong to you.

### 1. Create a Google Cloud project

- Go to https://console.cloud.google.com/projectcreate
- Project name: anything (e.g., "scholarapp")
- Organization: leave as "No organization" unless you have one

### 2. Enable the Gmail API

- In the project: APIs & Services → Library → search "Gmail API" → **Enable**

### 3. Configure the OAuth consent screen

- APIs & Services → OAuth consent screen
- User Type: **External**
- App information:
  - App name: "Scholarapp" (or anything)
  - User support email: your email
  - Developer contact: your email
- Scopes: leave default (we declare the narrow `gmail.send` scope at runtime)
- Test users: **add your own Gmail address** as a test user. While in
  Testing mode, only listed test users can authorize.
- Status: **Testing**. Production verification is only needed if you ship this
  to other people.

### 4. Create OAuth client credentials

- APIs & Services → Credentials → Create Credentials → OAuth client ID
- Application type: **Desktop app**
- Name: anything
- Click **Download JSON** on the new entry

### 5. Place client_secret.json

Move the downloaded JSON to:

```bash
mv ~/Downloads/client_secret_*.json ~/.scholarapp/client_secret.json
```

### 6. Authorize

```bash
scholar init
```

If `~/.scholarapp/client_secret.json` is present and credentials aren't, this
runs the OAuth flow: opens your browser, you click "Allow", the browser
redirects to a local callback, and a refresh token gets saved at
`~/.scholarapp/credentials.json` (chmod 600).

If you re-run `scholar init` later, it sees `credentials.json` exists and
skips the flow.

## OAuth scope: why `gmail.send`

We request exactly one scope:

```python
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
```

This is **write-only outgoing mail.** With this token Scholarapp can:

- Send emails as you.

It **cannot**:

- Read your inbox.
- Read/modify/delete any other mail.
- Access drafts in Gmail itself (we store our drafts in our own DB).
- Touch any other Google service (Drive, Calendar, etc).

Compare to common over-asks:

- `gmail.modify` — read + write all mail. Not needed.
- `gmail.compose` — manage drafts inside Gmail. Not needed; our drafts live in
  our own DB.
- `mail.google.com` — full access. Massively overreaching.

Narrow scope = minimum blast radius if the token is compromised.

## MIME construction

`_build_mime(draft, to_addr, from_addr, resume_bytes=None)` produces an RFC 822
email and returns its base64url-encoded raw string (what Gmail's API takes).

- **Headers:** `To`, `From`, `Subject`.
- **Body:** `draft.body` as plain text. Line breaks preserved.
- **Encoding:** UTF-8.
- **Attachments:** off by default. When `resume_bytes is None` (the default, and
  the only behavior when the resume toggle is off) the message is plain text —
  byte-identical to before. When `resume_bytes` is supplied the message becomes
  multipart: the text body plus a single `application/pdf` attachment named
  `resume.pdf`. See [Resume attachment](#resume-attachment-default-off).
- **HTML:** not generated. Cold emails are more readable as plain text and
  more likely to land in the inbox than the promotions tab.

The `Date` and `Message-ID` headers are added by Gmail server-side.

## Resume attachment (default off)

Outgoing emails can optionally carry the user's resume PDF as an attachment named
`resume.pdf`. The bytes come from the run's snapshotted `Run.resume_path`, read
**once per run** at the top of `send_approved` (not per draft).

**Off by default, and command-driven** — there is no env var. Flip it with:

```bash
scholar attach-resume on    # attach resume.pdf to outgoing emails
scholar attach-resume off   # plain text only (default)
```

The preference is persisted in `~/.scholarapp/config.toml` under
`[send].attach_resume` and is **sticky across runs**. `load_settings` reads it
back via stdlib `tomllib` into `Settings.send_attach_resume`; a missing or
malformed config is treated as "not set" (→ off). The choice is intentionally
*not* an env var so it survives `.env` churn and reads as a deliberate, persisted
decision.

- **Off:** emails are plain text, exactly as before.
- **On:** the message is multipart (text body + the PDF).

**Reads on the gated path only.** The attachment logic lives entirely inside the
real-send branch — it does nothing unless `SEND_ENABLED=true`. The
[`SEND_ENABLED` gate](#the-send_enabled-gate) is independent of this toggle and
is unchanged.

**A missing/unreadable resume never crashes the run.** When the toggle is on,
the PDF is read once; if that read fails (e.g. the file moved), every draft in
the batch is logged with a per-draft `SendLog` `outcome='error'` and the loop
continues. No email goes out without the attachment it was supposed to carry, and
one bad file doesn't take down the rest of the batch.

> **Deliverability tradeoff — why this defaults off.** Plain-text cold emails
> generally have *higher* deliverability than a multipart message with a PDF
> attached, which is more likely to be flagged as spam. Attaching your resume can
> help a warm recipient but hurts inbox placement at scale. Turn it on
> consciously, ideally only for small, targeted batches.

## Send loop

```
(if attach_resume on: read Run.resume_path once → resume_bytes, else None)
For each approved draft:
    if report.sent >= remaining_budget: stop
    if professor row missing: log error, continue
    if resume requested but unreadable: log error, continue
    build MIME (+ resume_bytes) → users.messages.send
        success → Draft.status = sent, SendLog(sent, gmail_message_id)
        HttpError → SendLog(error), continue
        unexpected → SendLog(error: unexpected), continue
```

**Per-draft errors do not stop the rest of the run.** One bad email address — or
one unreadable resume when the attachment toggle is on — is logged with
`outcome='error'` and the loop continues. The `DeliveryReport` carries the count
+ error messages.

## Daily cap

`SEND_DAILY_CAP=20` (default in [config.py](../scholarapp/config.py)).

The cap counts `SendLog` rows with `outcome='sent'` and `attempted_at` in the
current UTC day. Before any Gmail call, `send_approved` checks the count:

- If `today_sent >= cap` → raise `DeliveryError` **before** loading credentials.
- During the loop, if `report.sent` reaches the remaining budget → stop and
  leave the remaining drafts approved (you can `scholar send` them tomorrow).

Why the cap exists:

- Gmail aggressively flags accounts that send a burst of unsolicited mail to
  new recipients. Spreading sends over multiple days improves deliverability.
- Cold-email best practice for an individual sender is around 10–25/day. 20
  is conservative middle ground.

Override by setting `SEND_DAILY_CAP=50` (or whatever) in `.env`. Don't go wild
— if your account gets flagged, Gmail will throttle or suspend.

## The SEND_ENABLED gate

[`scholarapp/config.py`](../scholarapp/config.py) reads `SEND_ENABLED` from env;
default `false`. [`delivery.send_approved`](../scholarapp/modules/delivery.py)
checks it once, at the top of the function. There is no other code path that
talks to Gmail.

### When false (the default)

- For each approved draft, a `SendLog` row is written with
  `outcome='send_disabled'`.
- **Draft status is NOT changed** — they remain `approved`. You can still send
  them later when the gate flips.
- `SendingDisabled` is raised with the count of drafts that would have been
  sent.
- The CLI catches `SendingDisabled` and exits with code 1, displaying the
  message in yellow (`scholar send` returns non-zero so scripts notice).

### When true (future)

- The branch above is skipped.
- Daily cap is checked.
- Credentials loaded, Gmail service built, profile queried for the From: address.
- Send loop runs.

### Exact steps to flip it on

Do NOT skip these. Once an email goes out it can't be recalled.

1. **Verify all approved drafts look correct in the DB.**
   ```bash
   scholar status <run_id>
   ```
   Confirm the `status` column reads `approved` for everyone you want to send to.

2. **Open at least 3 drafts and read them end-to-end.**
   ```bash
   $EDITOR drafts/<run_id>/*.md
   ```
   Look for: hallucinated facts, wrong professor names, subjects that read like
   spam, awkward sign-offs. Fix anything questionable and re-run
   `scholar approve <run_id>`.

3. **Confirm the From address you'll be sending from.** The OAuth flow above
   binds Scholarapp to one specific Gmail account — the one you authorized
   with. If you authorized with the wrong account, delete
   `~/.scholarapp/credentials.json` and re-run `scholar init`.

4. **Flip the gate.** Edit `.env` (you copied this from `.env.example`):
   ```
   SEND_ENABLED=true
   ```

5. **Send.**
   ```bash
   scholar send <run_id>
   ```
   First run is capped at `SEND_DAILY_CAP`. The remaining drafts stay
   `approved` and you can run again tomorrow.

6. **(Optional) Flip the gate back off.** If you only have one batch to send,
   set `SEND_ENABLED=false` immediately after — it stops any accidental future
   sends until you re-flip.

## Token refresh

`_load_credentials` checks `creds.expired` and calls `creds.refresh(Request())`
if a refresh token is available. The refreshed token gets written back to
`credentials.json`. You should never have to do anything manually.

### If credentials.json is corrupted

```bash
rm ~/.scholarapp/credentials.json
scholar init
```

This re-runs the OAuth flow. Your `client_secret.json` is unchanged.

If `client_secret.json` itself is corrupted or you want to switch Google
accounts:

```bash
rm ~/.scholarapp/client_secret.json ~/.scholarapp/credentials.json
# Re-download client_secret.json from the Google Cloud console
scholar init
```

## Security

- **`credentials.json` contains a refresh token.** That token can be exchanged
  for an access token at any time without prompting you. Treat it like a
  password.
- The init flow sets it to `chmod 600` (owner read/write only). Verify with
  `ls -l ~/.scholarapp/credentials.json`.
- **Never commit** `client_secret.json` or `credentials.json`. They live under
  `~/.scholarapp/` which is outside the repo by design.
- If you suspect either file has leaked: in the Google Cloud Console go to
  Credentials → OAuth 2.0 Client IDs → click your client → **Delete**. Then
  create a new one and re-run `scholar init`.

## Rate limits

Gmail API quota at the time of writing:

- **Per-user rate:** 250 quota units / second
- **`users.messages.send` cost:** 100 units per call

Practical implication: ~2.5 sends/second sustained. Our `SEND_DAILY_CAP=20`
is so far below this that you'll never hit it. If you raise the cap, the loop
sends sequentially without any deliberate pacing — that's fine for caps up
to a few hundred.

## "Before you ship" checklist

If Scholarapp ever becomes a tool for other users (not just you), revisit
these:

- [ ] **Deliverability.** Sending many cold emails from a new account =
      spam-folder destiny. Warm up gradually; SPF/DKIM/DMARC matter; consider
      a dedicated sending domain.
- [ ] **Unsubscribe.** Add a one-click unsubscribe link + honor it in the
      send loop. (Today there's no unsubscribe — Scholarapp is for one-off
      personal outreach.)
- [ ] **Anti-spam compliance.** Different jurisdictions have different rules
      (CAN-SPAM, GDPR, CASL). Cold email to academics for collaboration is
      generally OK but read the rules for your region.
- [ ] **Reply handling.** Today we just send and forget. A real product
      would track threads, log replies, surface them in the UI.
- [ ] **Multi-user.** The OAuth flow assumes one account per Scholarapp
      install. Multi-user would need per-user `credentials.json` and proper
      session isolation.
- [ ] **Production OAuth verification.** Testing mode caps you at 100 users
      and prompts an "unverified app" warning. Going public requires Google's
      verification process (security review).
- [ ] **Bounce handling.** A bounced send today logs as `error`. Better
      would be to detect bounces (Gmail surfaces them in inbox) and pause
      that recipient automatically.

None of this matters for the single-user CLI use case. It matters a lot if
Scholarapp becomes a product.
