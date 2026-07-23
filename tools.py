import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

MAX_OUTPUT_CHARS = 8000
SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")
REF_RE = re.compile(r"^[\w./~^@{}-]+$")
GITHUB_PATH_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


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


def canonical_repo_source(source: str, allow_local_repos: bool = True) -> str:
    source = source.strip()
    if "://" in source or source.startswith("git@"):
        parsed = urllib.parse.urlsplit(source)
        match = GITHUB_PATH_RE.fullmatch(parsed.path)
        try:
            port = parsed.port
        except ValueError:
            raise GitToolError("remote repository URL has an invalid port")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not match
        ):
            raise GitToolError(
                "remote repositories must use https://github.com/<owner>/<repo>"
            )
        owner, repo = match.groups()
        return f"https://github.com/{owner}/{repo}.git"
    if not allow_local_repos:
        raise GitToolError("local repository paths are disabled")
    expanded = os.path.realpath(os.path.expanduser(source))
    if not os.path.isdir(expanded):
        raise GitToolError(f"no such directory: {source}")
    return expanded


class GitRepo:
    def __init__(self, root: str, temporary: bool = False):
        self.root = os.path.abspath(root)
        self.temporary = temporary
        if not os.path.isdir(os.path.join(self.root, ".git")) and not os.path.isfile(
            os.path.join(self.root, ".git")
        ):
            raise GitToolError(f"not a git repository: {self.root}")

    def cleanup(self):
        if self.temporary:
            shutil.rmtree(self.root, ignore_errors=True)
            self.temporary = False

    def _run(
        self, args: List[str], timeout: int = 30, allow_no_matches: bool = False
    ) -> str:
        result = subprocess.run(
            ["git", "-C", self.root, "--no-pager"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        if result.returncode != 0 and not (
            allow_no_matches and result.returncode == 1
        ):
            raise GitToolError(result.stderr.strip() or f"git {args[0]} failed")
        return result.stdout

    def summary(self) -> Dict[str, str]:
        head = self._run(["log", "-1", "--format=%h %s (%an, %ad)", "--date=short"]).strip()
        branch = self._run(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        count = self._run(["rev-list", "--count", "HEAD"]).strip()
        return {"head": head, "branch": branch, "commit_count": count}

    def prompt_context(self) -> str:
        summary = self.summary()
        entries = self._run(["ls-tree", "--name-only", "HEAD"]).splitlines()
        top_level = ", ".join(entries[:40])
        if len(entries) > 40:
            top_level += f", ... ({len(entries) - 40} more)"
        remote = self.remote_web_url() or "none"
        return (
            f"branch: {summary['branch']}\n"
            f"commits: {summary['commit_count']}\n"
            f"HEAD: {summary['head']}\n"
            f"origin: {remote}\n"
            f"top-level entries: {top_level or 'none'}"
        )

    def _origin_url(self) -> Optional[str]:
        try:
            return self._run(["config", "--get", "remote.origin.url"]).strip()
        except GitToolError:
            return None

    def remote_web_url(self) -> Optional[str]:
        url = self._origin_url()
        if not url:
            return None
        match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", url)
        if match:
            return f"https://{match.group(1)}/{match.group(2)}"
        if url.startswith("http"):
            return url[:-4] if url.endswith(".git") else url
        return None

    def github_slug(self) -> Optional[str]:
        url = self._origin_url()
        if not url:
            return None
        match = re.match(
            r"^(?:https?://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?$",
            url,
        )
        return match.group(1) if match else None

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

    def search_code(
        self,
        query: str,
        regex: bool = False,
        path: Optional[str] = None,
        ref: str = "HEAD",
        max_count: int = 50,
    ) -> str:
        _validate_ref(ref)
        args = ["grep", "-n", "-I"]
        if regex:
            args.append("-E")
        args += ["-e", query, ref]
        if path:
            args += ["--", path]
        out = self._run(args, timeout=60, allow_no_matches=True)
        lines = out.splitlines()
        limit = min(max(int(max_count), 1), 200)
        selected = lines[:limit]
        suffix = (
            f"\n... [{len(lines) - limit} more matches]"
            if len(lines) > limit
            else ""
        )
        return _truncate("\n".join(selected) + suffix) if selected else "no code matched"

    def list_tree(self, path: str = ".", ref: str = "HEAD") -> str:
        _validate_ref(ref)
        target = f"{ref}:{path}" if path not in (".", "") else ref
        out = self._run(["ls-tree", "--format=%(objecttype) %(path)", target])
        return _truncate(out)

    def _github_get(self, path: str):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "mental-model",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"https://api.github.com{path}", headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8", errors="replace"))
                message = body.get("message", str(exc))
            except (json.JSONDecodeError, UnicodeDecodeError):
                message = str(exc)
            raise GitToolError(f"GitHub API returned {exc.code}: {message}")
        except urllib.error.URLError as exc:
            raise GitToolError(f"GitHub API request failed: {exc.reason}")

    def get_pull_request_context(
        self,
        number: Optional[int] = None,
        commit_sha: Optional[str] = None,
        max_comments: int = 10,
    ) -> str:
        slug = self.github_slug()
        if not slug:
            raise GitToolError("repository origin is not a GitHub repository")
        if number is None and commit_sha is None:
            raise GitToolError("provide either number or commit_sha")
        if commit_sha:
            _validate_ref(commit_sha)
            pulls = self._github_get(f"/repos/{slug}/commits/{commit_sha}/pulls")
            if not pulls:
                return f"no GitHub pull request found for commit {commit_sha}"
            pull = pulls[0]
            number = int(pull["number"])
        if number is None or int(number) < 1:
            raise GitToolError("pull request number must be positive")
        number = int(number)
        pull = self._github_get(f"/repos/{slug}/pulls/{number}")
        limit = min(max(int(max_comments), 0), 30)
        comments = self._github_get(
            f"/repos/{slug}/issues/{number}/comments?per_page={limit}"
        )
        reviews = self._github_get(
            f"/repos/{slug}/pulls/{number}/reviews?per_page={limit}"
        )
        lines = [
            f"SOURCE pr:{number}",
            f"URL: {pull['html_url']}",
            f"Title: {pull['title']}",
            f"Author: {pull['user']['login']}",
            f"State: {pull['state']}",
            f"Merged at: {pull.get('merged_at') or 'not merged'}",
            "Body:",
            pull.get("body") or "(empty)",
        ]
        if comments:
            lines.append("Discussion comments:")
            for comment in comments[:limit]:
                lines.append(
                    f"[{comment['user']['login']}] {comment.get('body') or '(empty)'}"
                )
        review_bodies = [review for review in reviews[:limit] if review.get("body")]
        if review_bodies:
            lines.append("Review summaries:")
            for review in review_bodies:
                lines.append(f"[{review['user']['login']}] {review['body']}")
        return _truncate("\n".join(lines))


def prepare_repo(
    source: str,
    allow_local_repos: bool = True,
    clone_timeout_seconds: int = 120,
) -> GitRepo:
    source = canonical_repo_source(source, allow_local_repos)
    if source.startswith("https://github.com/"):
        dest = tempfile.mkdtemp(prefix="mental-model-")
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        try:
            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--filter=blob:none",
                    "--no-tags",
                    "--single-branch",
                    source,
                    dest,
                ],
                capture_output=True,
                text=True,
                timeout=clone_timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitToolError(
                f"clone timed out after {clone_timeout_seconds} seconds"
            )
        if result.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitToolError(f"clone failed: {result.stderr.strip()[:500]}")
        return GitRepo(dest, temporary=True)
    return GitRepo(source)


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
            "name": "search_code",
            "description": "Search tracked file contents at a git ref. Use this to discover symbols, concepts, configuration, and documentation before reading a specific file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "regex": {"type": "boolean", "default": False},
                    "path": {"type": "string"},
                    "ref": {"type": "string", "default": "HEAD"},
                    "max_count": {"type": "integer", "default": 50},
                },
                "required": ["query"],
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
    {
        "type": "function",
        "function": {
            "name": "get_pull_request_context",
            "description": "Retrieve a GitHub pull request's title, body, discussion comments, and review summaries. Resolve it by PR number or by an associated commit SHA. Use PR evidence for motivation, trade-offs, and design intent that git history alone cannot establish.",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "commit_sha": {"type": "string"},
                    "max_comments": {"type": "integer", "default": 10},
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
        "search_code": repo.search_code,
        "list_tree": repo.list_tree,
        "get_pull_request_context": repo.get_pull_request_context,
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
