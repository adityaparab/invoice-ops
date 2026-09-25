# ADR 0012: Seed role accounts and use revocable login sessions

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** Project owner
- **Supersedes:** Browser entry of static persona and upload tokens

## Context

The operator configured role and service tokens, but each user had to paste a
token into the UI. The selected persona was a browser choice rather than an
identity established by login. The same manual step appeared again for intake.

## Decision

Compose's one-shot migration service seeds one environment-owned account for
each of Analyst, Manager, Auditor, and Platform. Email addresses and passwords
come from `.env`; passwords are stored as salted scrypt hashes. Repeating the
seed preserves unchanged hashes and sessions. Changing a role email or password
updates that account and revokes its sessions. The restricted runtime database
role can read accounts and update only failed-login counters and lockout state;
it cannot rewrite account identity or password hashes.

The login API checks the password and issues an eight-hour, role-bound bearer
session. It derives the token from a server secret and the request's idempotency
key, stores only a SHA-256 token digest, and supports idempotent successful
login. Logout deletes the session. Five failed password attempts lock an account
for 15 minutes. Rotating the session secret invalidates existing sessions. The
UI gets its role from the authenticated response, restores the session within
the tab, and clears protected query data on sign-out or expired credentials.

The upload service token remains available for automated clients. An Analyst
session can upload through the UI. Existing persona tokens remain accepted by
the API for existing automation during migration, but the UI never asks for or
stores them.

## Consequences

Compose refuses to start without the four role email/password pairs and a
session secret. A local deployment can use `.env.example` as a starting point,
then set unique credentials in its ignored `.env`. The migration service must
complete before the API starts. Operators can apply credential changes with
`docker compose run --rm migrate`.

Browser sessions use `sessionStorage`, so refreshes within the tab preserve
login and closing the tab removes its local token. The API remains the role
authority. Session tokens are sensitive bearer credentials and require HTTPS
for any deployment exposed beyond loopback. This change does not affect
LiteLLM configuration or model traffic.
