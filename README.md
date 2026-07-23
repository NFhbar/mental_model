# Mental Model

An LLM agent that answers questions about a git repository's history: why code exists,
when behavior changed, who changed it and what they said about it at the time.

Point it at any repo (local path or GitHub URL) and ask questions like:

- "When was shell completion introduced, and what motivated it?"
- "Why does `core.py` import `errno`?"
- "What did the error handling in this file look like before the 2023 refactor?"

## How it works

The agent (default model: `gpt-5.6-sol`) investigates using six read-only git tools:

| Tool | Backing command |
|---|---|
| `search_commits` | `git log` with grep/author/path/date filters |
| `pickaxe_search` | `git log -S/-G` — when was a string introduced or removed |
| `show_commit` | `git show` — full message, diff and stats |
| `blame_lines` | `git blame -L` |
| `read_file_at_ref` | `git show ref:path` |
| `list_tree` | `git ls-tree` |

Every tool call streams to the browser over SSE and renders in a live investigation
trace panel next to the chat.

### Evidence-cited answers

The system prompt requires every historical claim to carry an inline `[commit:<sha>]`
citation. Before an answer is shown, a verification pass checks that each cited commit
exists in the repository and appeared in the agent's own tool output — fabricated
citations force a retry. Verified citations render as clickable chips that expand the
underlying commit.

## Setup

Requires Python 3.9+, Node 18+, git.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..

cp .env.example .env   # then fill in OPENAI_API_KEY, APP_PASSWORD, etc.
.venv/bin/uvicorn server:app --port 8000
```

Open http://localhost:8000.

For frontend development with hot reload, run `npm run dev` in `frontend/`
(it proxies `/api` to port 8000).

## Configuration

Configuration is read from environment variables, or from a `.env` file in the project
root (copy `.env.example` to get started).

| Env var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | required |
| `MENTAL_MODEL_LLM` | `gpt-5.6-sol` | override the model |
| `APP_PASSWORD` | empty (gate disabled) | password required to access the app |

## Layout

```
tools.py    git tool implementations + OpenAI tool schemas
agent.py    agent loop, system prompt, citation verification
server.py   FastAPI app: repo sessions, SSE ask endpoint, static frontend
frontend/   React (Vite) chat + investigation trace UI
```
