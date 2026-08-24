# Render public demo mode

This runtime is a deterministic simulation only. No deployment was performed by this
change. Public binding is fail-closed and requires both an explicit mode flag and a
secret bearer token; localhost remains the default.

## Render Web Service settings

- **Root Directory:** this backend directory
- **Build Command:** `true`
- **Start Command:** `PYTHONPATH=src python -m local_runtime --host 0.0.0.0 --port "$PORT" --state-dir "$LOCAL_RUNTIME_STATE_DIR"`
- **Health Check Path:** `/health`

Configure these environment variables in Render's secret/environment UI:

| Variable | Value/guidance |
| --- | --- |
| `LOCAL_RUNTIME_PUBLIC_MODE` | Exactly `1` |
| `LOCAL_RUNTIME_AUTH_TOKEN` | A strong, non-empty secret; never commit it |
| `LOCAL_RUNTIME_ALLOWED_ORIGINS` | Comma-separated exact trusted UI origins; wildcards are rejected |
| `LOCAL_RUNTIME_STATE_DIR` | `/tmp/local-runtime` for safe ephemeral state, or an attached disk path such as `/var/data/local-runtime` |

Render supplies `PORT`; the runtime also uses it automatically when
`LOCAL_RUNTIME_PORT` is absent. `LOCAL_RUNTIME_PORT` may explicitly override it.
`LOCAL_RUNTIME_INTERVAL_SECONDS` is optional and defaults to `1.0`.

`GET /health` and all `OPTIONS` requests are unauthenticated. Every other endpoint
requires `Authorization: Bearer <LOCAL_RUNTIME_AUTH_TOKEN>`. CORS remains an exact
allowlist and is not authentication. Missing public mode or a blank/missing token makes
`0.0.0.0` startup fail. Enabling public mode with no token also fails even on loopback.

Render's normal filesystem is ephemeral. A persistent disk is tied to one instance;
this file-backed runtime is not designed for multiple writers. Backups and retention
remain the operator's responsibility.
