import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional

MAX_OUTPUT_CHARS = 8000
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")
REF_RE = re.compile(r"^[\w./~^@{}-]+$")


class GitToolError(Exception):
    pass


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    omitted = text.count("\n", limit)
    return text[:limit] + f"\n... [truncated, {omitted} more lines omitted]"


def _validate_ref(ref: str) -> str:
    if not REF_RE.match(ref) or ref.startswith("-"):
        raise GitToolError(f"invalid ref: {ref!r}")
    return ref


class GitRepo:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        if not os.path.isdir(os.path.join(self.root, ".git")) and not os.path.isfile(
            os.path.join(self.root, ".git")
        ):
            raise GitToolError(f"not a git repository: {self.root}")

    def _run(self, args: List[str], timeout: int = 30) -> str:
        result = subprocess.run(
            ["git", "-C", self.root, "--no-pager"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        if result.returncode != 0:
            raise GitToolError(result.stderr.strip() or f"git {args[0]} failed")
        return result.stdout

    def summary(self) -> Dict[str, str]:
        head = self._run(["log", "-1", "--format=%h %s (%an, %ad)", "--date=short"]).strip()
        branch = self._run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        count = self._run(["rev-list", "--count", "HEAD"]).strip()
        return {"head": head, "branch": branch, "commit_count": count}

    def remote_web_url(self) -> Optional[str]:
        try:
            url = self._run(["config", "--get", "remote.origin.url"]).strip()
        except GitToolError:
            return None
        match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url)
        if match:
            return f"https://{match.group(1)}/{match.group(2)}"
        if url.startswith("http"):
            return url[:-4] if url.endswith(".git") else url
        return None

    def commit_exists(self, sha: str) -> bool:
        if not SHA_RE.match(sha):
            return False
        try:
            return self._run(["cat-file", "-t", sha]).strip() == "commit"
        except (GitToolError, subprocess.TimeoutExpired):
            return False

    def search_commits(
        self,
        grep: Optional[str] = None,
        author: Optional[str] = None,
        path: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        max_count: int = 30,
    ) -> str:
        args = [
            "log",
            f"--max-count={min(int(max_count), 100)}",
            "--date=short",
            "--format=%h | %ad | %an | %s",
        ]
        if grep:
            args += [f"--grep={grep}", "-i"]
        if author:
            args += [f"--author={author}"]
        if since:
            args += [f"--since={since}"]
        if until:
            args += [f"--until={until}"]
        if path:
            args += ["--", path]
        out = self._run(args)
        return _truncate(out) if out.strip() else "no commits matched"

    def pickaxe_search(
        self,
        text: str,
        regex: bool = False,
        path: Optional[str] = None,
        max_count: int = 20,
    ) -> str:
        flag = "-G" if regex else "-S"
        args = [
            "log",
            f"--max-count={min(int(max_count), 50)}",
            "--date=short",
            "--format=%h | %ad | %an | %s",
            flag + text,
        ]
        if path:
            args += ["--", path]
        out = self._run(args, timeout=60)
        return _truncate(out) if out.strip() else "no commits matched"

    def show_commit(self, sha: str, path: Optional[str] = None) -> str:
        _validate_ref(sha)
        args = ["show", "--date=iso", "--stat", "--patch", sha]
        if path:
            args += ["--", path]
        return _truncate(self._run(args))

    def blame_lines(self, path: str, start_line: int, end_line: int, ref: str = "HEAD") -> str:
        _validate_ref(ref)
        start, end = int(start_line), int(end_line)
        if end < start or end - start > 200:
            raise GitToolError("line range must be ascending and at most 200 lines")
        out = self._run(
            ["blame", "--date=short", "-L", f"{start},{end}", ref, "--", path],
            timeout=60,
        )
        return _truncate(out)

    def read_file_at_ref(
        self,
        path: str,
        ref: str = "HEAD",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> str:
        _validate_ref(ref)
        out = self._run(["show", f"{ref}:{path}"])
        lines = out.splitlines()
        start = int(start_line) if start_line else 1
        end = min(int(end_line) if end_line else start + 199, start + 399)
        selected = lines[start - 1 : end]
        numbered = "\n".join(f"{start + i:6}| {line}" for i, line in enumerate(selected))
        suffix = "" if end >= len(lines) else f"\n... [{len(lines) - end} more lines]"
        return _truncate(numbered + suffix)

    def list_tree(self, path: str = ".", ref: str = "HEAD") -> str:
        _validate_ref(ref)
        target = f"{ref}:{path}" if path not in (".", "") else ref
        out = self._run(["ls-tree", "--format=%(objecttype) %(path)", target])
        return _truncate(out)


def prepare_repo(source: str) -> GitRepo:
    source = source.strip()
    if re.match(r"^(https?://|git@)", source):
        dest = tempfile.mkdtemp(prefix="mental-model-")
        result = subprocess.run(
            ["git", "clone", source, dest],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitToolError(f"clone failed: {result.stderr.strip()[:500]}")
        return GitRepo(dest)
    expanded = os.path.expanduser(source)
    if not os.path.isdir(expanded):
        raise GitToolError(f"no such directory: {source}")
    return GitRepo(expanded)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_commits",
            "description": "Search the commit log. Filter by message text (grep), author, file path, or date range. Returns one line per commit: short sha | date | author | subject.",
            "parameters": {
                "type": "object",
                "properties": {
                    "grep": {"type": "string", "description": "Case-insensitive text to match in commit messages"},
                    "author": {"type": "string"},
                    "path": {"type": "string", "description": "Limit to commits touching this file or directory"},
                    "since": {"type": "string", "description": "e.g. 2023-01-01"},
                    "until": {"type": "string"},
                    "max_count": {"type": "integer", "default": 30},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pickaxe_search",
            "description": "Find commits that added or removed a specific string (or regex) anywhere in the code. This is the best way to answer 'when was X introduced/removed'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Exact code string (or regex if regex=true) to trace"},
                    "regex": {"type": "boolean", "default": False},
                    "path": {"type": "string"},
                    "max_count": {"type": "integer", "default": 20},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_commit",
            "description": "Show a commit's full message, author, date, stats and diff. Optionally restrict the diff to one path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sha": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["sha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blame_lines",
            "description": "Show which commit last touched each line in a file range (max 200 lines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                    "ref": {"type": "string", "default": "HEAD"},
                },
                "required": ["path", "start_line", "end_line"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_at_ref",
            "description": "Read a file's contents (with line numbers) as it existed at a given commit or ref.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "ref": {"type": "string", "default": "HEAD"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tree",
            "description": "List files and directories at a path within the repo at a given ref. Use to orient yourself.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "ref": {"type": "string", "default": "HEAD"},
                },
            },
        },
    },
]


def dispatch_tool(repo: GitRepo, name: str, args: Dict) -> str:
    handlers = {
        "search_commits": repo.search_commits,
        "pickaxe_search": repo.pickaxe_search,
        "show_commit": repo.show_commit,
        "blame_lines": repo.blame_lines,
        "read_file_at_ref": repo.read_file_at_ref,
        "list_tree": repo.list_tree,
    }
    if name not in handlers:
        return f"error: unknown tool {name}"
    try:
        return handlers[name](**args)
    except GitToolError as exc:
        return f"error: {exc}"
    except subprocess.TimeoutExpired:
        return "error: git command timed out"
    except TypeError as exc:
        return f"error: bad arguments: {exc}"
