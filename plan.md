Runtime: Python 3.12 or 3.13 on cPanel Passenger (WSGI). No background workers, no long-running processes, no Docker.
Framework: Flask. Database: MySQL via PyMySQL. No ORM required, but if used, keep it lightweight.
The receiver must never call external APIs. It validates, writes one row, and returns 204 in under 100 ms.
All outbound calls happen in the forwarder, run by cron every minute.
Multi-tenant on every table. Every query filters by tenant_id.
Admin UI: Jinja templates plus HTMX, with IBM Carbon styles and web components. No Node build step on the server. Compiled Carbon CSS and JS are committed as static assets.
Secrets encrypted at rest with a key from an environment variable. Never committed.
License Apache 2.0. Every source file gets an SPDX header. Commits need a DCO sign-off.
Private add-ons load through the tracker.adapters entry point. The core must never import them.
Phases for the agent

Phase 0: Host validation. Deploy the minimal receiver and health route. Add a /diag/outbound admin-only route that tests HTTPS to graph.facebook.com. Acceptance: 204 on POST, JSON on health, diagnostic reports status.

Phase 1: Schema and ingestion. Create the migrations for tenants, tenant_keys, tenant_origins, events, deliveries, users, audit_log, with the indexes listed in the plan. Build the receiver: resolve write key to tenant, check origin allowlist, enforce body size and rate limits, dedupe on (tenant_id, event_id), hash client IP with a salt, store the row. Acceptance: unit tests cover valid events, unknown key, bad origin, oversize body, duplicate event_id, and a p95 under 100 ms against a local MySQL.

Phase 2: Browser SDK. A small vanilla JS file, under 10 KB minified. Generates event_id, collects page view, add to cart, checkout, and purchase, and sends the fields Meta needs for matching (fbp, fbc, user agent, hashed email and phone when provided). Uses navigator.sendBeacon with a fetch fallback. Acceptance: test page sends events that land in the database with correct tenant resolution.

Phase 3: Forwarder and adapters. Adapter base class with map, send, and classify_error methods. Build GA4, Meta CAPI, and TikTok adapters. The forwarder picks due deliveries, applies exponential backoff, moves to dead letter after N attempts, and is safe to run concurrently (use row locking or a status claim). Acceptance: fake HTTP server tests for success, 5xx retry, 4xx dead letter, and duplicate-run safety.

Phase 4: Admin WebUI. Login with argon2 or bcrypt, CSRF on all forms, login rate limiting. Screens: setup wizard, overview, events list and detail (PII masked), destinations CRUD with test-event button, dead-letter queue with retry, tenant settings (origins, keys), audit log. Carbon look, HTMX for partial updates.

Phase 5: Hardening and packaging. Security review checklist, consent enforcement check, retention purge job, backup docs, installer README for cPanel, CONTRIBUTING, SECURITY, NOTICE, and LICENSE.

Rules to keep the agent honest
Each phase ends with passing tests and a short CHANGELOG entry.
If a phase needs a decision the plan doesn't cover, the agent writes it to DECISIONS.md and asks you before continuing.
Never mark a phase done without running the cPanel smoke test from Phase 0 against the real host.