# Cafe OS Intelligence Agent — MVP

LangGraph-backed POS intelligence layer with FastAPI, SQLite, and OpenRouter.

## Local Development

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/ -v
uvicorn cafe_os.main:app --reload
```

The server initializes the schema and demo seed data on startup. To seed a
database file manually (e.g. before deploying), run:

```bash
python3 scripts/seed_db.py --db path/to/cafe_os.db
```

Order state is persisted via a SQLite LangGraph checkpointer (in the same DB
file), so in-flight order graphs survive restarts.

## Payments

- `POST /api/v1/orders/{id}/pay` — `{"payment_method": "cash|card|mobile", "shift_id"?}`.
  Marks the order paid, accrues loyalty points (1 pt per whole currency unit),
  and adds cash totals to the open shift's `expected_cash` (the given
  `shift_id`, or the only open shift). Paying twice → 400.

## Shifts

- `POST /api/v1/shifts` — `{"cashier_id": "...", "opening_cash"?}` → opens a
  shift; `expected_cash` starts at the opening float (201).
- `GET /api/v1/shifts` — list shifts newest-first; `?status=open|closed`.
- `POST /api/v1/shifts/{id}/reconcile` — counts the drawer, records
  `actual_cash`/`cash_difference`, and closes the shift.

## Kitchen Display (KDS)

- `GET /api/v1/kds/orders` — list active tickets (`?status=`, `?include_completed=true`)
- `WS /ws/kds` — live channel. On connect the server sends a `snapshot` of
  active tickets; each new order dispatch pushes `order_dispatched`. Clients
  send `{"type": "status_update", "kds_id": ..., "status": "acknowledged|in_progress|completed"}`
  to move a ticket; updates are rebroadcast as `status_changed`. `{"type": "ping"}`
  returns `{"type": "pong"}`.

## Deployment

### Option A: Render (Recommended — Free Tier)

1. Push this repo to GitHub
2. Go to https://render.com and sign up
3. Click **New +** → **Web Service**
4. Connect your GitHub repo
5. Render auto-detects `render.yaml`
6. Click **Create Web Service**
7. Your public URL will be: `https://cafe-os.onrender.com`

### Option B: Railway (Free Tier)

1. Push this repo to GitHub
2. Go to https://railway.app and sign up
3. Click **New Project** → **Deploy from GitHub repo**
4. Select your repo
5. Railway auto-detects `railway.json`
6. Your public URL will be: `https://cafe-os-production.up.railway.app`

### Option C: Docker (Any Provider)

```bash
docker build -t cafe-os .
docker run -p 8000:8000 cafe-os
```

### Option D: Public URL without an account (cloudflared quick tunnel)

Runs the API locally and publishes it on a temporary `https://*.trycloudflare.com`
URL. No account, no credit card — the URL changes whenever the tunnel restarts.

```bash
pip install -e ".[dev]"
API_KEY=$(openssl rand -hex 16) ./scripts/run_public_tunnel.sh
```

On a bare container (no Python, no venv, no cloudflared) use the bootstrap
script instead — it installs everything and then starts the deployment:

```bash
./scripts/bootstrap_deploy.sh
```

The script seeds the database on first run, starts uvicorn and the tunnel with
restart supervisors, and prints the public API / docs / KDS WebSocket URLs.
Requires the `cloudflared` binary
(<https://developers.cloudflare.com/cloudflare-one/connections/download-cloudflared>).

The active URL is written to `.deploy/public-url.txt` (updated whenever the
tunnel reconnects — a reconnect takes ~1 minute while Cloudflare provisions a
new hostname). The generated API key is written to `.deploy/api-key.txt`.
Read both with:

```bash
./scripts/public_url.sh
```

## Authentication

Set `API_KEY` to protect the API. When it is set, every `/api/v1` route and the
KDS WebSocket require the key, supplied as the `X-API-Key` header or an
`?api_key=` query parameter (browsers cannot set headers on a WebSocket
handshake). `/healthz`, `/docs` and `/openapi.json` stay public.

```bash
curl -H "X-API-Key: $API_KEY" https://<url>/api/v1/menu
```

Leaving `API_KEY` unset disables authentication — fine for local development,
not for a public deployment.

## Health Check

`GET /healthz` returns `{"status": "ok", "service": "cafe-os"}` without
authentication and is used as the healthcheck path in `railway.json` and
`render.yaml`.

## Environment Variables

See `.env.example` for required variables.

## API Documentation

Once deployed, visit `https://your-url.com/docs` for interactive API docs.

## Test

```bash
python3 -m pytest tests/ -v
```
