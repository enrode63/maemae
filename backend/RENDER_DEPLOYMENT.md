# Render deployment readiness

This repository is not currently safe or network-compatible for a public Render Web
Service. The HTTP runtime deliberately accepts only `localhost` or `127.0.0.1`, and
its API has no authentication. Do not deploy it publicly and do not change the host
to `0.0.0.0` merely to make a Render health check pass.

No Render deployment was performed as part of this preparation.

## Runtime and dependencies

The application runtime uses only the Python standard library. There is no runtime
dependency installation step and no `requirements.txt` is needed. Use Python 3.10 or
newer (Python 3.11 is suitable on Render).

After a safe authentication and public-bind design has been implemented and reviewed,
configure a Render Web Service with:

- **Root Directory:** the directory containing this file
- **Build Command:** `true` (no package installation is required)
- **Start Command:** `PYTHONPATH=src python -m local_runtime --host 127.0.0.1 --port "$PORT" --state-dir "$LOCAL_RUNTIME_STATE_DIR"`
- **Health Check Path:** `/health`

This command records the current host policy accurately, but a Render proxy cannot
reach a process bound to loopback. It is a readiness reference, not a deployable
public-service configuration. A future public deployment must first add authentication
and a deliberately reviewed external bind policy; only then should its start command
use that new safe policy.

## Environment variables

| Variable | Purpose | Render guidance |
| --- | --- | --- |
| `LOCAL_RUNTIME_HOST` | Bind host | Keep `127.0.0.1` under the current policy. `0.0.0.0` is rejected. |
| `LOCAL_RUNTIME_PORT` | Listening port | Locally defaults to `8765`. On Render, prefer injected `PORT` in the command line above. |
| `LOCAL_RUNTIME_STATE_DIR` | Runtime state and audit files | Set to a disk path such as `/var/data/local-runtime`; otherwise state is ephemeral. |
| `LOCAL_RUNTIME_ALLOWED_ORIGINS` | Comma-separated exact CORS origins | Set only trusted `https://` UI origins; no wildcards or paths. CORS is not authentication. |

`LOCAL_RUNTIME_INTERVAL_SECONDS` is optional and defaults to `1.0`.

The health endpoint is `GET /health`; a healthy local response is HTTP 200 with
`{"ok": true, "scope": "localhost-demo-only"}`. Its scope is an additional reminder
that the current server is not intended for public traffic.

## Persistent disk warning

Render's normal filesystem is ephemeral. To retain state across restarts or deploys,
attach a persistent disk and point `LOCAL_RUNTIME_STATE_DIR` at its mount path. A disk
is tied to one service instance and constrains scaling/failover; this file-backed
runtime is not designed for multiple instances writing the same logical state.
Backups and retention remain the operator's responsibility.

## Required work before public deployment

1. Add and test authentication for every non-health endpoint, with authorization for
   state-changing routes.
2. Add an explicit, reviewed production bind mode instead of weakening localhost
   validation. Default behavior must remain loopback-only.
3. Add proxy-aware transport/security handling, request limits, secret management,
   and production logging as appropriate.
4. Re-run tests and verify `/health` through Render's proxy in a non-public environment
   before enabling public access.
