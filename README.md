# Agent Orchestration — Jev Canvas Base

Minimal base version of a visual workflow canvas and Jev-routed runtime.

Phase 1A now defines a versioned canonical flow schema and validates node types, unique IDs, start/end nodes, edge references, variables, and reachability before a flow can run.

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
