"""An in-process fake of the GitHub REST endpoints TaskGate uses, for tests and demos.

:class:`FakeGitHub` serves on ``127.0.0.1`` from a background thread and records
every request (method, path, query, JSON body and whether the token matched).
It implements only what :mod:`taskgate.github` calls, with GitHub's shapes and
limits where TaskGate depends on them:

- ``GET /repos/{repo}/pulls/{n}`` (``changed_files``) and
  ``GET /repos/{repo}/pulls/{n}/files`` (paginated with a ``Link`` header);
- ``GET``/``POST /repos/{repo}/issues/{n}/comments`` (paginated) and
  ``PATCH /repos/{repo}/issues/comments/{id}``;
- ``POST /repos/{repo}/check-runs`` and ``PATCH /repos/{repo}/check-runs/{id}``,
  which answer 422 to more than 50 annotations in one request, as GitHub does.

Requests without ``Authorization: Bearer <token>`` get 401 when the fake has a
token; unknown routes get 404. ``GET /_fake/state`` returns the recorded
requests, comments and check runs as JSON, so a process that did not start the
fake (a CI step running the composite action) can inspect it.

Run ``python -m taskgate.fakegithub --port 8765`` to serve until interrupted. The
module imports nothing outside the standard library and runs on Python 3.9 and
later, so ``PYTHONPATH=src`` with a system Python is enough to start it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import threading
import urllib.parse
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HTML_BASE = "https://github.example"
"""Host used in ``html_url`` values, so recorded output does not depend on the port."""

DEFAULT_TOKEN = "fake-token"
ANNOTATION_LIMIT = 50
MAX_PER_PAGE = 100


@dataclass(frozen=True)
class Recorded:
    """One request the fake received."""

    method: str
    path: str
    query: dict[str, str]
    body: Any
    authorized: bool
    status: int


@dataclass
class _State:
    pulls: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    changed_files: dict[int, int] = field(default_factory=dict)
    comments: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    check_runs: dict[int, dict[str, Any]] = field(default_factory=dict)
    requests: list[Recorded] = field(default_factory=list)
    next_id: int = 1

    def new_id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value


class _Reply(Exception):  # noqa: N818 (a reply, raised to leave the router early)
    def __init__(self, status: int, body: Any, link: str | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.body = body
        self.link = link


def _error(status: int, message: str) -> _Reply:
    return _Reply(status, {"message": message})


class FakeGitHub:
    """A recording fake of part of the GitHub REST API, served from a thread."""

    def __init__(
        self, repo: str = "sample/tasks", token: str | None = DEFAULT_TOKEN, port: int = 0
    ) -> None:
        self.repo = repo
        self.token = token
        self.state = _State()
        self._lock = threading.RLock()
        self._server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        """The base URL to put in ``TASKGATE_GITHUB_API``."""
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    def start(self) -> FakeGitHub:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve on the calling thread until interrupted (or until :meth:`shutdown`)."""
        with contextlib.suppress(KeyboardInterrupt):
            self._server.serve_forever()
        self._server.server_close()

    def shutdown(self) -> None:
        """Make :meth:`serve_forever` return (call it from another thread)."""
        self._server.shutdown()

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join()
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> FakeGitHub:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # Seeding and inspection.

    def add_pull(
        self,
        number: int,
        files: Sequence[tuple[str, str] | tuple[str, str, str]],
        changed_files: int | None = None,
    ) -> None:
        """Serve ``files`` as pull request ``number``'s files: ``(path, status)``, or
        ``(path, "renamed", previous_path)``. ``changed_files`` overrides the count the
        pull request reports (GitHub lists at most 3000 files)."""
        with self._lock:
            listed: list[dict[str, Any]] = []
            for path, status, *previous in files:
                item = {"filename": path, "status": status}
                if previous:
                    item["previous_filename"] = previous[0]
                listed.append(item)
            self.state.pulls[number] = listed
            self.state.changed_files[number] = (
                len(listed) if changed_files is None else changed_files
            )

    def add_comment(self, number: int, body: str, user: str = "someone") -> int:
        with self._lock:
            return self._store_comment(number, body, user)["id"]  # type: ignore[no-any-return]

    @property
    def requests(self) -> list[Recorded]:
        with self._lock:
            return list(self.state.requests)

    def comments(self, number: int) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(comment) for comment in self.state.comments.get(number, [])]

    def check_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(run) for _, run in sorted(self.state.check_runs.items())]

    def snapshot(self) -> dict[str, Any]:
        """Everything recorded, as plain data (what ``GET /_fake/state`` returns)."""
        with self._lock:
            return {
                "requests": [asdict(request) for request in self.state.requests],
                "comments": {str(n): list(c) for n, c in self.state.comments.items()},
                "check_runs": [run for _, run in sorted(self.state.check_runs.items())],
            }

    # Request handling.

    def _store_comment(self, number: int, body: str, user: str) -> dict[str, Any]:
        comment_id = self.state.new_id()
        comment = {
            "id": comment_id,
            "body": body,
            "user": {"login": user},
            "html_url": f"{HTML_BASE}/{self.repo}/pull/{number}#issuecomment-{comment_id}",
        }
        self.state.comments.setdefault(number, []).append(comment)
        return comment

    def _page(self, items: list[Any], path: str, query: dict[str, str]) -> _Reply:
        per_page = min(int(query.get("per_page", "30")), MAX_PER_PAGE)
        page = int(query.get("page", "1"))
        start = (page - 1) * per_page
        link = None
        if start + per_page < len(items):
            next_query = urllib.parse.urlencode({"per_page": per_page, "page": page + 1})
            link = f'<{self.url}{path}?{next_query}>; rel="next"'
        return _Reply(200, items[start : start + per_page], link)

    def _route(self, method: str, path: str, query: dict[str, str], body: Any) -> _Reply:
        """The reply to one request; errors are raised as :class:`_Reply`."""
        if method == "GET" and path == "/_fake/state":
            return _Reply(200, self.snapshot())
        prefix = f"/repos/{self.repo}"
        if not path.startswith(prefix + "/"):
            raise _error(404, "Not Found")
        route = path.removeprefix(prefix)
        if match := re.fullmatch(r"/pulls/(\d+)", route):
            files = self.state.pulls.get(int(match[1]))
            if method != "GET" or files is None:
                raise _error(404, "Not Found")
            count = self.state.changed_files[int(match[1])]
            return _Reply(200, {"number": int(match[1]), "changed_files": count})
        if match := re.fullmatch(r"/pulls/(\d+)/files", route):
            files = self.state.pulls.get(int(match[1]))
            if method != "GET" or files is None:
                raise _error(404, "Not Found")
            return self._page(files, path, query)
        if match := re.fullmatch(r"/issues/(\d+)/comments", route):
            number = int(match[1])
            if method == "GET":
                return self._page(self.state.comments.get(number, []), path, query)
            if method == "POST":
                return _Reply(201, self._store_comment(number, _text(body, "body"), "taskgate"))
        if (match := re.fullmatch(r"/issues/comments/(\d+)", route)) and method == "PATCH":
            for comments in self.state.comments.values():
                for comment in comments:
                    if comment["id"] == int(match[1]):
                        comment["body"] = _text(body, "body")
                        return _Reply(200, comment)
            raise _error(404, "Not Found")
        if route == "/check-runs" and method == "POST":
            return _Reply(201, self._create_check_run(body))
        if (match := re.fullmatch(r"/check-runs/(\d+)", route)) and method == "PATCH":
            run = self.state.check_runs.get(int(match[1]))
            if run is None:
                raise _error(404, "Not Found")
            output = _output(body)
            run["output"]["title"] = output["title"]
            run["output"]["summary"] = output["summary"]
            run["output"]["annotations"].extend(output["annotations"])
            return _Reply(200, run)
        raise _error(404, "Not Found")

    def _create_check_run(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise _error(422, "Invalid request.")
        output = _output(body)
        run_id = self.state.new_id()
        run = {
            "id": run_id,
            "name": _text(body, "name"),
            "head_sha": _text(body, "head_sha"),
            "status": _text(body, "status"),
            "conclusion": _text(body, "conclusion"),
            "output": output,
            "html_url": f"{HTML_BASE}/{self.repo}/runs/{run_id}",
        }
        self.state.check_runs[run_id] = run
        return run

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(parsed.query))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                authorized = fake.token is None or (
                    self.headers.get("Authorization") == f"Bearer {fake.token}"
                )
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = None
                with fake._lock:
                    try:
                        if not authorized and parsed.path != "/_fake/state":
                            raise _error(401, "Bad credentials")
                        if raw and body is None:
                            raise _error(400, "Problems parsing JSON")
                        result = fake._route(self.command, parsed.path, query, body)
                    except _Reply as reply:
                        result = reply
                    if parsed.path != "/_fake/state":
                        fake.state.requests.append(
                            Recorded(
                                self.command, parsed.path, query, body, authorized, result.status
                            )
                        )
                data = json.dumps(result.body).encode("utf-8")
                self.send_response(result.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if result.link:
                    self.send_header("Link", result.link)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._serve()

            def do_POST(self) -> None:
                self._serve()

            def do_PATCH(self) -> None:
                self._serve()

            def log_message(self, format: str, *args: Any) -> None:
                """Stay quiet: requests are recorded, not logged."""

        return Handler


def _text(body: Any, key: str) -> str:
    value = body.get(key) if isinstance(body, dict) else None
    if not isinstance(value, str):
        raise _error(422, f"Invalid request. {key} is missing or not a string.")
    return value


def _output(body: Any) -> dict[str, Any]:
    output = body.get("output") if isinstance(body, dict) else None
    if not isinstance(output, dict):
        raise _error(422, "Invalid request. output is missing.")
    annotations = output.get("annotations", [])
    if not isinstance(annotations, list) or len(annotations) > ANNOTATION_LIMIT:
        raise _error(
            422, f"Invalid request. Only {ANNOTATION_LIMIT} annotations are allowed per request."
        )
    return {
        "title": _text(output, "title"),
        "summary": _text(output, "summary"),
        "annotations": list(annotations),
    }


def main(argv: Sequence[str] | None = None) -> None:
    """Serve a fake on ``--port`` until interrupted (for a CI step to post to)."""
    parser = argparse.ArgumentParser(prog="python -m taskgate.fakegithub")
    parser.add_argument("--repo", default="sample/tasks")
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    fake = FakeGitHub(args.repo, args.token, port=args.port)
    print(f"fake GitHub API for {args.repo} on {fake.url}", flush=True)
    fake.serve_forever()


if __name__ == "__main__":
    main()
