# Agent Orchestration — Jev Canvas Base

Minimal base version of a visual workflow canvas and Jev-routed runtime.

Phase 1A now defines a versioned canonical flow schema and validates node types, unique IDs, start/end nodes, edge references, variables, and reachability before a flow can run.

Phase 1B adds a SQLite-backed session runtime. Use `POST /api/sessions`, then `POST /api/sessions/{id}/messages` to execute a flow turn and inspect the persisted trace with `GET /api/sessions/{id}`.

MongoDB persistence is now available for users, workspaces, flow versions, tools, connectors, credential references, sessions, and execution records. Set `MONGODB_URI` to use it; SQLite remains a local fallback when MongoDB is not configured.

For local MongoDB, make sure the MongoDB service is running before registering:

```bash
mongod --dbpath /path/to/mongodb-data
```

Or use a MongoDB Atlas connection string in `MONGODB_URI`. If MongoDB is unreachable, the API now returns a clear `503` response instead of an opaque `500`.

Authentication endpoints:

```text
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
GET  /api/auth/me
```

Agent nodes use OpenRouter when `OPENROUTER_API_KEY` is configured. Set `OPENROUTER_MODEL` to any model available to your OpenRouter account. Without a key, an agent node uses its configured static reply as a safe local fallback.

Jev makes typed decisions about which outgoing edge to take. It does not generate the agent's reply. A separate executor owns the node action/reply, so an LLM agent, tool call, human approval, or deterministic action can be added later.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
uvicorn backend.app:app --reload --port 8000
```

In another terminal:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal. The frontend proxies `/api` to `http://localhost:8000`.

## Environment

Required for live Jev routing:

```env
JEV_API_KEY=jev_live_...
JEV_BASE_URL=https://api.typesafe.ai/v1
JEV_MODEL=jev-latest
```

The runtime also accepts `TYPESAFE_API_KEY` (the name used by the source Voice-Agent) and `SYSTEM_ONE_API_KEY` (the name used in the current System One documentation).

For the first canvas test, `JEV_MOCK=true` lets the runtime use a deterministic first-route choice without making a paid request.
