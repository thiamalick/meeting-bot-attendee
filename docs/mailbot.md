# Mailbot: schedule bots by email

The mailbot gives the deployment an email address, for example `bot@client.fr`. To get a bot in a meeting, a user adds that address as a guest in their calendar, or forwards it an invitation. No account, API key or calendar connection is needed.

- The bot joins at the start of the meeting, records and transcribes it.
- Reschedules and changes of link are applied to the bot, cancellations remove it.
- Recurring meetings are supported, including moved or cancelled occurrences.
- The requester gets an answer: confirmation, or the reason why the bot can't come.

Every component can be self-hosted, and no Google or Microsoft API is involved: the mailbot reads a standard mailbox and parses standard iCalendar invitations (RFC 5545 / 5546), whatever calendar sent them.

## How it works

```
Mailbox (IMAP)  ─┐
                 ├─▶ InboundEmail (raw email in object storage, deduplicated by Message-ID)
MTA (HTTP push) ─┘        │
                          ▼  Celery task
              loop & abuse guards ─▶ sender authorization ─▶ iCalendar parsing
                          │
                          ▼
              CalendarEvent of the project's "email" calendar ─▶ scheduled bot ─▶ reply
```

1. **Reception.** `run_mailbot` reads the mailbox every `MAILBOT_POLL_INTERVAL_SECONDS` and moves each recorded message to the `Processed` folder. An MTA can push messages to `POST /mailbot/inbound` instead. Both are idempotent: an email delivered twice is processed once.
2. **Guards.** Automatic messages (`Auto-Submitted`, out of office, bulk) are ignored and never answered, so mail loops can't happen. Senders are rate limited.
3. **Authorization.** The `From` domain must be in `MAILBOT_ALLOWED_SENDER_DOMAINS`, and its DMARC check must have passed on your own mail server. The authenticated sender is checked rather than the meeting organizer, so an employee can forward an invitation received from an external partner. Unauthorized emails are ignored without an answer, to avoid sending mail to spoofed addresses.
4. **Parsing.** Invitations are read from `text/calendar` parts and `.ics` attachments, including inside forwarded messages. Timezones come from the invitation itself (IANA or Windows names). The meeting link is looked up in the Teams/Google specific properties, then the location, description, and finally the email body.
5. **Scheduling.** Each invitation becomes a `CalendarEvent` of a calendar with the `email` platform, created automatically in `MAILBOT_PROJECT_ID`. Its bot is created like any API bot, linked to the event, and kept in sync with it. Updates with an older `SEQUENCE` than the one already processed are ignored, so out of order deliveries are harmless.
6. **Recurring meetings.** Occurrences are created over a rolling `MAILBOT_RECURRENCE_HORIZON_DAYS` window and extended every hour. `EXDATE` and individually modified or cancelled occurrences (`RECURRENCE-ID`) are respected.

Every received email is kept as an audit record: sender, DMARC result, decision and reason, events and bots. They are listed in the **Mailbot** page of the project dashboard and in the Django admin. The raw email is deleted after `MAILBOT_RAW_RETENTION_DAYS`.

## Setting up a deployment

### 1. The mailbox

Any IMAP mailbox works. Pick one that matches the client's sovereignty requirements:

- **Self-hosted**: [Stalwart](https://stalw.art) (SMTP, IMAP and JMAP in one server) or Postfix with Dovecot and OpenDMARC.
- **European providers**: OVHcloud, Infomaniak, Scaleway...
- **The client's own mail system**, with a dedicated mailbox.

The mailbot needs the mailbox for itself: every message in its inbox is processed and moved.

### 2. Sender authentication

`MAILBOT_REQUIRE_DMARC=true` (the default) relies on the `Authentication-Results` header (RFC 8601) that the receiving mail server adds after checking SPF, DKIM and DMARC. Stalwart does it natively, Postfix does it with OpenDMARC.

- Set `MAILBOT_TRUSTED_AUTHSERV_ID` to the identifier your server writes at the start of that header, e.g. `mx.client.fr` in `Authentication-Results: mx.client.fr; dkim=pass ...; dmarc=pass ...`. Look at the headers of a received email to find it.
- Your server must remove `Authentication-Results` headers that claim its identifier from incoming emails (RFC 8601, section 5). The mailbot only trusts the topmost header with that identifier.
- The sending domains must publish a DMARC record. Without one, DMARC can't pass.

Only disable `MAILBOT_REQUIRE_DMARC` when the mailbox only receives mail from trusted internal servers. Anyone can otherwise send an email with a forged `From`.

### 3. Outgoing email

Replies are sent from `MAILBOT_ADDRESS` through the SMTP server configured with `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`... Make sure the bot domain's SPF and DKIM authorize that server, otherwise replies will land in spam.

### 4. Configuration

```bash
MAILBOT_ENABLED=true
MAILBOT_ADDRESS=bot@client.fr
MAILBOT_PROJECT_ID=proj_xxxxxxxxxxxxxxxx
MAILBOT_IMAP_HOST=imap.client.fr
MAILBOT_IMAP_USER=bot@client.fr
MAILBOT_IMAP_PASSWORD=...
MAILBOT_ALLOWED_SENDER_DOMAINS=client.fr
MAILBOT_TRUSTED_AUTHSERV_ID=mx.client.fr

EMAIL_HOST=smtp.client.fr
EMAIL_HOST_USER=bot@client.fr
EMAIL_HOST_PASSWORD=...
```

All variables are listed in [environment-variables.md](environment-variables.md#mailbot). The bots are created with the project's credentials and the default settings of the API. `MAILBOT_BOT_SETTINGS` adds settings to every bot, e.g. `{"zoom_settings": {"sdk": "web"}}`. Zoom meetings need Zoom credentials in the project, like API bots.

### 5. Running

Run **one** instance of `python manage.py run_mailbot` next to the Celery worker. Processing happens in the Celery worker, which scales horizontally. Set `MAILBOT_HEARTBEAT_FILE` to monitor the poller with a liveness probe: the file is only refreshed after a successful poll.

### Pushing emails over HTTP instead

When the client already runs an MTA, it can deliver straight to the mailbot instead of a mailbox. Set `MAILBOT_HTTP_INGEST_TOKEN` and POST the raw message:

```bash
curl -X POST https://attendee.client.fr/mailbot/inbound \
  -H "Authorization: Bearer $MAILBOT_HTTP_INGEST_TOKEN" \
  -H "Content-Type: message/rfc822" \
  --data-binary @message.eml
```

For Postfix, a pipe transport in `master.cf` does it:

```
mailbot unix - n n - - pipe
  flags=Rq user=nobody argv=/usr/bin/curl -sf -X POST -H Authorization:\ Bearer\ <token> -H Content-Type:\ message/rfc822 --data-binary @- https://attendee.client.fr/mailbot/inbound
```

The `Authentication-Results` header must still be added before the message is pushed.

### Checking a deployment

```bash
python manage.py mailbot_send_test_invitation --from alice@client.fr --meeting-url https://meet.google.com/abc-defg-hij --in-minutes 15
```

The bot should appear as scheduled in the dashboard and the sender should get a confirmation. Send it again with `--uid <uid> --sequence 1 --in-minutes 30` to move the meeting, or with `--cancel` to cancel it.

## Local development

The `mail` profile of `dev.docker-compose.yaml` starts [GreenMail](https://greenmail-mail-test.github.io/greenmail/), a test mail server that accepts any login, and the `run_mailbot` poller. GreenMail doesn't check DMARC, so disable that check locally. Add to `.env`:

```bash
MAILBOT_ENABLED=true
MAILBOT_ADDRESS=bot@client.local
MAILBOT_PROJECT_ID=proj_...   # from the dashboard URL
MAILBOT_IMAP_HOST=mail
MAILBOT_IMAP_PORT=3143
MAILBOT_IMAP_SECURITY=none
MAILBOT_IMAP_USER=bot@client.local
MAILBOT_IMAP_PASSWORD=dev
MAILBOT_ALLOWED_SENDER_DOMAINS=client.local
MAILBOT_REQUIRE_DMARC=false
EMAIL_HOST=mail
EMAIL_PORT=3025
EMAIL_USE_TLS=false
```

Then:

```bash
docker compose -f dev.docker-compose.yaml --profile mail up -d
docker compose -f dev.docker-compose.yaml exec attendee-app-local \
  python manage.py mailbot_send_test_invitation --from alice@client.local --meeting-url https://meet.google.com/abc-defg-hij
```

Replies can be read in the `alice@client.local` mailbox, on IMAP port 3143.

## Limitations and next steps

- The post-meeting summary (transcript and recording link sent to the requester) isn't sent yet.
- One address per deployment, creating bots in a single project.
- Polling adds up to `MAILBOT_POLL_INTERVAL_SECONDS` of delay. IMAP IDLE or JMAP push would make it instant.
- All-day events are ignored: they have no start time to join at.
