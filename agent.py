import json
import os
import re
from typing import AsyncIterator, Dict, List

from openai import AsyncOpenAI

from tools import TOOL_SCHEMAS, GitRepo, dispatch_tool

MODEL = os.environ.get("MENTAL_MODEL_LLM", "gpt-5.6-sol")
MAX_TURNS = 25
CITATION_RE = re.compile(r"\[commit:([0-9a-fA-F]{4,40})\]")

SYSTEM_PROMPT = """You are Mental Model: an expert investigator of software history.
You answer questions about how and why a repository's code came to be, using git tools.

Repository under investigation:
{repo_summary}

## Investigation protocol
1. ORIENT: if you don't know the layout, use list_tree and search_commits to get bearings.
2. LOCATE: use pickaxe_search to find when specific code appeared or disappeared; use
   blame_lines to attribute current lines to commits.
3. CORROBORATE: use show_commit to read the actual change and its message before drawing
   conclusions. Never infer intent from a commit subject line alone if the diff is available.
4. ANSWER: synthesize findings into a clear narrative.

## Rules of evidence
- Before each tool call, state in one short sentence what hypothesis you are testing.
- Every factual claim about the repository's history in your final answer MUST be backed by
  an inline citation of the form [commit:<short-sha>], e.g. "the retry loop was added to
  handle flaky uploads [commit:a1b2c3d]".
- Cite only commits you have actually seen in tool output during this conversation.
  Fabricating a sha is a critical failure.
- If the history does not contain enough evidence to answer, say so explicitly rather
  than speculating. Clearly separate evidence-backed statements from your interpretation.
- Keep final answers concise: lead with the direct answer, then the supporting timeline.
"""

VERIFICATION_RETRY_PROMPT = """Your previous answer failed citation verification:
{problems}

Rewrite your answer. Every [commit:<sha>] citation must reference a commit that exists in
this repository and that appeared in tool output above. Remove or correct invalid citations;
re-investigate with tools first if needed.
"""


class AgentEvent(dict):
    @staticmethod
    def make(kind: str, **payload) -> "AgentEvent":
        return AgentEvent(kind=kind, **payload)


def verify_citations(repo: GitRepo, answer: str, seen_output: str) -> List[str]:
    problems = []
    shas = set(CITATION_RE.findall(answer))
    if not shas:
        problems.append("the answer contains no [commit:<sha>] citations")
    for sha in shas:
        if not repo.commit_exists(sha):
            problems.append(f"cited commit {sha} does not exist in the repository")
        elif sha.lower()[:7] not in seen_output.lower():
            problems.append(f"cited commit {sha} never appeared in your tool output")
    return problems


class MentalModel:
    def __init__(self, repo: GitRepo):
        self.repo = repo
        self.client = AsyncOpenAI()
        summary = repo.summary()
        repo_desc = (
            f"branch: {summary['branch']}, {summary['commit_count']} commits, "
            f"HEAD: {summary['head']}"
        )
        self.messages: List[Dict] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(repo_summary=repo_desc)}
        ]
        self.seen_tool_output = ""

    async def _complete(self):
        return await self.client.chat.completions.create(
            model=MODEL,
            messages=self.messages,
            tools=TOOL_SCHEMAS,
            tool_choice="auto",
        )

    async def ask(self, question: str) -> AsyncIterator[AgentEvent]:
        self.messages.append({"role": "user", "content": question})
        verification_attempts = 0

        for _ in range(MAX_TURNS):
            response = await self._complete()
            choice = response.choices[0]
            msg = choice.message

            if msg.tool_calls:
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content,
                        "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                    }
                )
                if msg.content:
                    yield AgentEvent.make("thought", text=msg.content)
                for tc in msg.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    yield AgentEvent.make("tool_call", name=tc.function.name, args=args)
                    result = dispatch_tool(self.repo, tc.function.name, args)
                    self.seen_tool_output += "\n" + result
                    yield AgentEvent.make(
                        "tool_result", name=tc.function.name, result=result
                    )
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": result,
                        }
                    )
                continue

            answer = msg.content or ""
            problems = verify_citations(self.repo, answer, self.seen_tool_output)
            if problems and verification_attempts < 2:
                verification_attempts += 1
                yield AgentEvent.make("verification_failed", problems=problems)
                self.messages.append({"role": "assistant", "content": answer})
                self.messages.append(
                    {
                        "role": "user",
                        "content": VERIFICATION_RETRY_PROMPT.format(
                            problems="\n".join(f"- {p}" for p in problems)
                        ),
                    }
                )
                continue

            self.messages.append({"role": "assistant", "content": answer})
            yield AgentEvent.make(
                "answer",
                text=answer,
                verified=not problems,
                citations=sorted(set(CITATION_RE.findall(answer))),
            )
            return

        yield AgentEvent.make(
            "error", text="investigation exceeded the maximum number of steps"
        )
