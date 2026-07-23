# Mental Model

An LLM agent that explains a repository's purpose, architecture, behavior, and history
using evidence from source files, commits, and GitHub pull requests.

Point it at any repo (local path or GitHub URL) and ask questions like:

- "When was shell completion introduced, and what motivated it?"
- "Why does `core.py` import `errno`?"
- "What did the error handling in this file look like before the 2023 refactor?"

## How it works

The agent (default model: `gpt-5.4`) classifies each question as current-state,
historical, intent, or mixed, then follows the appropriate evidence path. It
investigates using eight read-only tools:

| Tool | Backing command |
|---|---|
| `search_commits` | `git log` with grep/author/path/date filters |
| `pickaxe_search` | `git log -S/-G` — when was a string introduced or removed |
| `show_commit` | `git show` — full message, diff and stats |
| `blame_lines` | `git blame -L` |
| `read_file_at_ref` | `git show ref:path` |
| `search_code` | `git grep` at any ref |
| `list_tree` | `git ls-tree` |
| `get_pull_request_context` | GitHub PR description, discussion, and reviews |

Every tool call streams to the browser over SSE and renders in a live investigation
trace panel next to the chat.

### Typed, quote-backed evidence

Every repository-specific claim carries an inline evidence ID linked to a structured
ledger. Each record identifies a commit, file, or PR; labels the claim as direct
evidence or inference; and includes an exact quote from the retrieved source. Before an
answer is shown, a deterministic verification pass checks that the source was retrieved
and that the quote occurs in it. Failures force the agent to re-investigate and retry.
Evidence chips expand to show the claim, quote, source, and direct/inferred status. The
trace renders a per-claim report for inline citation, label, source retrieval, commit
existence where applicable, and exact quote matching; failed attempts remain visible
beside the retry reason.

### Interface and introspection

- Investigations are grouped by question with tool, retry, evidence, type, and duration
  summaries. The history is searchable and older investigations can remain collapsed.
- The trace panel can be hidden with the header control or `Option+T`, and resized by
  dragging its left edge. Width and visibility persist across refreshes.
- The Meta tab exposes all three workspace states, live repository context, question
  routing, tool availability, evidence contract, latest per-claim verification, model,
  token usage, and run statistics.
- Up to three repositories can remain open as isolated workspaces in a dedicated
  top tab strip. Each tab shows its branch, connection state, active state, external
  link, and removal control; each workspace has its own agent session, chat,
  investigation trace, and Meta statistics.
- Browser persistence retains 50 compact investigations and 100 messages per
  repository. Full tool results are reduced to previews when stored to stay within
  localStorage limits.
- Removing a workspace closes its backend session and deletes temporary clones. After
  a server restart, transcripts remain available while each agent session reconnects
  explicitly with a visible context-reset boundary.
- Running investigations show elapsed time and can be stopped from the ask bar.
  Cancellation rolls the server-side agent back to its pre-question state.

Repository files, commits, pull requests, and tool results are treated as untrusted
evidence rather than instructions. The system prompt prevents repository content from
altering agent policy, tool contracts, or verification rules.

## Evaluation

The deterministic eval runner scores expected evidence sources against the agent's
verified evidence ledger:

```bash
.venv/bin/python evals.py --repo /path/to/click
```

The included Click question bank contains five current-state, documentation, design,
testing, and historical-intent cases. Use `--limit 1` for a smoke test and
`--output result.json` to save the full report. The validated baseline currently passes
all five cases with `gpt-5.4`.

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

## Deploy to Render

The root `render.yaml` defines one Render web service. It installs Python dependencies,
builds the React frontend with npm, serves the resulting assets through FastAPI, and
checks `/api/health`.

1. Push the repository, including `render.yaml`.
2. In Render, create a new Blueprint and select the repository.
3. Provide `OPENAI_API_KEY` and `APP_PASSWORD` when prompted.
4. Optionally provide `GITHUB_TOKEN` for private repos and higher API limits.
5. Apply the Blueprint and open the generated `onrender.com` URL.

The Blueprint uses Render's free plan. Free services can spin down when idle, so warm
the URL before the interview or change `plan` to `starter`. Agent sessions and cloned
repositories use Render's ephemeral filesystem and process memory; redeploys and
restarts preserve browser transcripts but require repository reconnection.

## Configuration

Configuration is read from environment variables, or from a `.env` file in the project
root (copy `.env.example` to get started).

| Env var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | required |
| `MENTAL_MODEL_LLM` | `gpt-5.4` | override the model |
| `APP_PASSWORD` | empty (gate disabled) | password required to access the app |
| `GITHUB_TOKEN` | empty | optional; raises GitHub API limits and enables private repos |

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Layout

```
tools.py    git and GitHub tool implementations + OpenAI tool schemas
agent.py    adaptive agent loop, evidence ledger, deterministic verification
server.py   FastAPI app: repo sessions, SSE ask endpoint, static frontend
frontend/   React (Vite) chat + investigation trace UI
tests/      evidence-verification and tool tests
```
