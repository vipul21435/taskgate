"""A small GitHub REST client on the standard library, for pull-request reporting.

It does three things, each with the fewest calls the API allows:

- :meth:`GitHubClient.pull_request_files` lists the files a pull request
  changes (``GET /pulls/{n}/files``, 100 per page, following the ``Link``
  header), after checking with ``GET /pulls/{n}`` that the list is complete (the
  API returns at most 3000 files);
- :meth:`GitHubClient.upsert_comment` keeps one summary comment per pull request:
  it finds the comment whose body starts with :data:`MARKER` (a hidden HTML
  comment), updates it when the text changed, leaves it alone when it did not,
  and creates it otherwise;
- :meth:`GitHubClient.create_check_run` creates a completed check run and sends
  its annotations in batches of :data:`ANNOTATION_BATCH` (50, the API's limit per
  request): the first batch with the create call, the rest with updates.

The base URL comes from ``TASKGATE_GITHUB_API`` (default
``https://api.github.com``; the tests and ``make demo`` point it at
:mod:`taskgate.fakegithub`) and the token from ``GITHUB_TOKEN``. The token is
sent only to that base URL: HTTP redirects are never followed (a 3xx answer is
an error naming the ``Location``), pagination links to another host are
refused, and error messages never include the token.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from taskgate import __version__

API_ENV = "TASKGATE_GITHUB_API"
TOKEN_ENV = "GITHUB_TOKEN"
DEFAULT_API = "https://api.github.com"
API_VERSION = "2022-11-28"
MARKER = "<!-- taskgate:summary -->"
"""Starts the body of the one summary comment TaskGate keeps on a pull request."""

ANNOTATION_BATCH = 50
PER_PAGE = 100
MAX_LISTED_FILES = 3000
MAX_BODY = 65536
"""GitHub's limit on a comment body (characters)."""

MAX_SUMMARY_BYTES = 65535
"""GitHub's limit on a check run's ``output.summary`` and ``output.text`` (bytes)."""

MAX_TITLE = 255
TRUNCATED = "\n\n(truncated: the full report is in report.md and report.json)\n"
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class GitHubError(RuntimeError):
    """The GitHub API could not be reached or refused a request."""


@dataclass(frozen=True, slots=True)
class PullFile:
    """One file a pull request changes."""

    path: str
    status: str
    """``added``, ``modified``, ``removed``, ``renamed``, ``copied``, ``changed``, ..."""

    previous_path: str | None = None
    """The old path of a renamed file."""


@dataclass(frozen=True, slots=True)
class Annotation:
    """A check-run annotation on one line of one file."""

    path: str
    level: str
    """``failure``, ``warning`` or ``notice``."""

    title: str
    message: str
    details: str = ""
    line: int = 1

    def to_api(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "path": self.path,
            "start_line": self.line,
            "end_line": self.line,
            "annotation_level": self.level,
            "title": self.title[:MAX_TITLE],
            "message": fit(self.message),
        }
        if self.details:
            body["raw_details"] = fit(self.details)
        return body


@dataclass(frozen=True, slots=True)
class CommentResult:
    action: str
    """``created``, ``updated`` or ``unchanged``."""

    id: int
    url: str


@dataclass(frozen=True, slots=True)
class CheckRunResult:
    id: int
    url: str
    annotations: int
    requests: int
    """How many API calls sent the check run (one create plus one update per extra batch)."""


def fit(text: str, limit: int = MAX_BODY) -> str:
    """``text`` cut to ``limit`` characters, with a note saying so when it was cut."""
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED)] + TRUNCATED


def fit_bytes(text: str, limit: int = MAX_SUMMARY_BYTES) -> str:
    """``text`` cut to ``limit`` UTF-8 bytes (never inside a character), with a note."""
    if len(text.encode("utf-8")) <= limit:
        return text
    room = limit - len(TRUNCATED.encode("utf-8"))
    return text.encode("utf-8")[:room].decode("utf-8", errors="ignore") + TRUNCATED


def comment_body(markdown: str) -> str:
    """The summary comment's body: the hidden marker, then the report (cut to fit)."""
    return fit(f"{MARKER}\n{markdown}")


def batches(items: Sequence[Annotation], size: int = ANNOTATION_BATCH) -> list[list[Annotation]]:
    """``items`` in consecutive groups of at most ``size``; one empty group for no items."""
    return [list(items[start : start + size]) for start in range(0, len(items), size)] or [[]]


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: following one would send the token to another URL."""

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        raise GitHubError(
            f"HTTP {code}: redirect to {newurl} not followed (the token is sent only to {API_ENV})"
        )


_opener = urllib.request.build_opener(_NoRedirects())


class GitHubClient:
    """Calls to one repository's REST API with one token."""

    def __init__(
        self,
        repo: str,
        *,
        token: str | None = None,
        api: str = DEFAULT_API,
        timeout: float = 30.0,
    ) -> None:
        if not REPO.match(repo):
            raise GitHubError(f"repository must look like OWNER/NAME, not {repo!r}")
        parsed = urllib.parse.urlsplit(api)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise GitHubError(f"{API_ENV} must be an http(s) URL, not {api!r}")
        self.repo = repo
        self.token = token
        self.api = api.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls, repo: str, env: Mapping[str, str] | None = None) -> GitHubClient:
        """A client with the base URL from ``TASKGATE_GITHUB_API`` and the token from
        ``GITHUB_TOKEN`` (either may be unset)."""
        env = os.environ if env is None else env
        return cls(repo, token=env.get(TOKEN_ENV) or None, api=env.get(API_ENV) or DEFAULT_API)

    def _url(self, path: str, query: Mapping[str, str | int] | None = None) -> str:
        url = f"{self.api}/repos/{self.repo}{path}"
        return f"{url}?{urllib.parse.urlencode(query)}" if query else url

    def _send(self, method: str, url: str, body: object = None) -> tuple[Any, str | None]:
        """One request; its decoded JSON and its ``Link`` header."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"taskgate/{__version__}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        where = f"{method} {url.removeprefix(self.api)}"
        try:
            with _opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
                link = response.headers.get("Link")
        except GitHubError as exc:
            raise GitHubError(f"{where}: {exc}") from None
        except urllib.error.HTTPError as exc:
            raise GitHubError(f"{where}: HTTP {exc.code}: {_message(exc)}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise GitHubError(f"{where}: {reason}") from exc
        try:
            return (json.loads(raw) if raw else None), link
        except ValueError as exc:
            raise GitHubError(f"{where}: the response is not JSON") from exc

    def _pages(self, path: str) -> Iterator[Any]:
        """Every item of a paginated list, following ``rel="next"`` links on this API only."""
        url: str | None = self._url(path, {"per_page": PER_PAGE})
        while url is not None:
            page, link = self._send("GET", url)
            if not isinstance(page, list):
                raise GitHubError(f"GET {url.removeprefix(self.api)}: expected a JSON list")
            yield from page
            found = LINK_NEXT.search(link or "")
            url = found.group(1) if found else None
            if url is not None and not url.startswith(f"{self.api}/"):
                raise GitHubError(f"refusing to follow a pagination link to another host: {url}")

    def pull_request_files(self, number: int) -> list[PullFile]:
        """Every file the pull request changes, in the API's order."""
        pull, _ = self._send("GET", self._url(f"/pulls/{number}"))
        expected = int(pull.get("changed_files", 0)) if isinstance(pull, dict) else 0
        files = [
            PullFile(item["filename"], item["status"], item.get("previous_filename"))
            for item in self._pages(f"/pulls/{number}/files")
        ]
        if len(files) < expected:
            raise GitHubError(
                f"pull request #{number} changes {expected} files but the API listed "
                f"{len(files)} (it lists at most {MAX_LISTED_FILES}); leave out --pr to "
                "take the changed files from git instead"
            )
        return files

    def upsert_comment(self, number: int, markdown: str) -> CommentResult:
        """Create or update the pull request's one TaskGate summary comment."""
        body = comment_body(markdown)
        for comment in self._pages(f"/issues/{number}/comments"):
            if str(comment.get("body", "")).startswith(MARKER):
                comment_id = int(comment["id"])
                if comment.get("body") == body:
                    return CommentResult("unchanged", comment_id, str(comment.get("html_url")))
                updated, _ = self._send(
                    "PATCH", self._url(f"/issues/comments/{comment_id}"), {"body": body}
                )
                return CommentResult("updated", comment_id, str(updated.get("html_url")))
        created, _ = self._send("POST", self._url(f"/issues/{number}/comments"), {"body": body})
        return CommentResult("created", int(created["id"]), str(created.get("html_url")))

    def create_check_run(
        self,
        *,
        name: str,
        head_sha: str,
        conclusion: str,
        title: str,
        summary: str,
        annotations: Sequence[Annotation] = (),
    ) -> CheckRunResult:
        """Create a completed check run with every annotation, 50 per request."""
        groups = batches(annotations)

        def output(group: list[Annotation]) -> dict[str, Any]:
            return {
                "title": title[:MAX_TITLE],
                "summary": fit_bytes(summary),
                "annotations": [annotation.to_api() for annotation in group],
            }

        created, _ = self._send(
            "POST",
            self._url("/check-runs"),
            {
                "name": name,
                "head_sha": head_sha,
                "status": "completed",
                "conclusion": conclusion,
                "output": output(groups[0]),
            },
        )
        run_id = int(created["id"])
        for group in groups[1:]:
            self._send("PATCH", self._url(f"/check-runs/{run_id}"), {"output": output(group)})
        return CheckRunResult(run_id, str(created.get("html_url")), len(annotations), len(groups))


def _message(error: urllib.error.HTTPError) -> str:
    """GitHub's ``message`` from an error response, else the HTTP reason."""
    try:
        data = json.loads(error.read() or b"{}")
    except ValueError:
        data = {}
    message = data.get("message") if isinstance(data, dict) else None
    return str(message or error.reason)
