"""The stdlib GitHub client against the in-process fake API."""

from __future__ import annotations

import io
import json
import runpy
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from email.message import Message
from typing import Any

import pytest

from taskgate import fakegithub, github
from taskgate.fakegithub import DEFAULT_TOKEN, FakeGitHub, main
from taskgate.github import (
    ANNOTATION_BATCH,
    MARKER,
    MAX_BODY,
    TRUNCATED,
    Annotation,
    GitHubClient,
    GitHubError,
    PullFile,
    batches,
    comment_body,
    fit,
)

REPO = "sample/tasks"


@pytest.fixture
def fake() -> Iterator[FakeGitHub]:
    with FakeGitHub(REPO) as server:
        yield server


def client(fake: FakeGitHub, token: str | None = DEFAULT_TOKEN) -> GitHubClient:
    return GitHubClient(REPO, token=token, api=fake.url)


def calls(fake: FakeGitHub) -> list[tuple[str, str, int]]:
    return [(r.method, r.path, r.status) for r in fake.requests]


def test_from_env_reads_the_base_url_and_token() -> None:
    env = {"TASKGATE_GITHUB_API": "http://127.0.0.1:9/", "GITHUB_TOKEN": "t0k"}
    made = GitHubClient.from_env("a/b", env)
    assert (made.api, made.token, made.repo) == ("http://127.0.0.1:9", "t0k", "a/b")
    default = GitHubClient.from_env("a/b", {"GITHUB_TOKEN": ""})
    assert (default.api, default.token) == ("https://api.github.com", None)


@pytest.mark.parametrize(
    ("repo", "api", "problem"),
    [
        ("no-slash", "https://api.github.com", "repository must look like OWNER/NAME"),
        ("a/b/c", "https://api.github.com", "repository must look like OWNER/NAME"),
        ("a/b", "file:///etc", "TASKGATE_GITHUB_API must be an http(s) URL"),
        ("a/b", "https://", "TASKGATE_GITHUB_API must be an http(s) URL"),
    ],
)
def test_bad_settings_are_rejected(repo: str, api: str, problem: str) -> None:
    with pytest.raises(GitHubError) as caught:
        GitHubClient(repo, api=api)
    assert str(caught.value).startswith(problem)


def test_pull_request_files_follow_the_link_header(fake: FakeGitHub) -> None:
    files = [(f"tasks/t{n:03}/task.toml", "added") for n in range(230)]
    fake.add_pull(7, [*files, ("tasks/new/solve.sh", "renamed", "tasks/old/solve.sh")])
    listed = client(fake).pull_request_files(7)
    assert len(listed) == 231
    assert listed[0] == PullFile("tasks/t000/task.toml", "added")
    assert listed[-1] == PullFile("tasks/new/solve.sh", "renamed", "tasks/old/solve.sh")
    assert calls(fake) == [
        ("GET", f"/repos/{REPO}/pulls/7", 200),
        ("GET", f"/repos/{REPO}/pulls/7/files", 200),
        ("GET", f"/repos/{REPO}/pulls/7/files", 200),
        ("GET", f"/repos/{REPO}/pulls/7/files", 200),
    ]
    assert [r.query.get("page", "1") for r in fake.requests[1:]] == ["1", "2", "3"]
    assert all(r.authorized for r in fake.requests)


def test_a_truncated_file_list_is_an_error(fake: FakeGitHub) -> None:
    fake.add_pull(8, [("a.txt", "added")], changed_files=3001)
    with pytest.raises(GitHubError) as caught:
        client(fake).pull_request_files(8)
    assert str(caught.value) == (
        "pull request #8 changes 3001 files but the API listed 1 (it lists at most 3000); "
        "leave out --pr to take the changed files from git instead"
    )


def test_the_summary_comment_is_created_updated_and_left_alone(fake: FakeGitHub) -> None:
    fake.add_comment(3, "Looks good to me")
    for n in range(120):
        fake.add_comment(3, f"chatter {n}")
    api = client(fake)
    created = api.upsert_comment(3, "## TaskGate: FAIL\n")
    assert created.action == "created"
    assert created.url == f"https://github.example/{REPO}/pull/3#issuecomment-{created.id}"
    assert api.upsert_comment(3, "## TaskGate: FAIL\n").action == "unchanged"
    updated = api.upsert_comment(3, "## TaskGate: PASS\n")
    assert (updated.action, updated.id) == ("updated", created.id)
    bodies = [comment["body"] for comment in fake.comments(3)]
    assert bodies[0] == "Looks good to me"
    assert [body for body in bodies if body.startswith(MARKER)] == [
        f"{MARKER}\n## TaskGate: PASS\n"
    ]
    methods = [method for method, _, _ in calls(fake)]
    assert methods == ["GET", "GET", "POST", "GET", "GET", "GET", "GET", "PATCH"]


def test_check_runs_send_annotations_fifty_at_a_time(fake: FakeGitHub) -> None:
    notes = [
        Annotation(f"tasks/t{n}/task.toml", "failure", f"TG401 n{n}", "failed", "fix: it")
        for n in range(120)
    ]
    run = client(fake).create_check_run(
        name="TaskGate",
        head_sha="abc123",
        conclusion="failure",
        title="TaskGate: FAIL, 120 blocking failures",
        summary="## TaskGate: FAIL\n",
        annotations=notes,
    )
    assert (run.annotations, run.requests) == (120, 3)
    assert calls(fake) == [
        ("POST", f"/repos/{REPO}/check-runs", 201),
        ("PATCH", f"/repos/{REPO}/check-runs/{run.id}", 200),
        ("PATCH", f"/repos/{REPO}/check-runs/{run.id}", 200),
    ]
    sent = [len(r.body["output"]["annotations"]) for r in fake.requests]
    assert sent == [50, 50, 20]
    (stored,) = fake.check_runs()
    assert stored["conclusion"] == "failure"
    assert stored["status"] == "completed"
    assert [a["title"] for a in stored["output"]["annotations"]] == [n.title for n in notes]
    assert stored["output"]["annotations"][0] == {
        "path": "tasks/t0/task.toml",
        "start_line": 1,
        "end_line": 1,
        "annotation_level": "failure",
        "title": "TG401 n0",
        "message": "failed",
        "raw_details": "fix: it",
    }


def test_a_check_run_without_annotations_is_one_request(fake: FakeGitHub) -> None:
    run = client(fake).create_check_run(
        name="TaskGate", head_sha="abc", conclusion="success", title="ok", summary="ok"
    )
    assert (run.annotations, run.requests) == (0, 1)
    assert Annotation("x", "notice", "t", "m").to_api().get("raw_details") is None


def test_the_fake_enforces_the_annotation_limit(fake: FakeGitHub) -> None:
    """GitHub answers 422 to 51 annotations in one request; the fake must too, or the
    batching test above would prove nothing."""
    api = client(fake)
    notes = [Annotation("a", "failure", "t", "m").to_api() for _ in range(ANNOTATION_BATCH + 1)]
    output = {"title": "t", "summary": "s", "annotations": notes}
    body = {"name": "n", "head_sha": "s", "status": "completed", "conclusion": "failure"}
    with pytest.raises(GitHubError) as caught:
        api._send("POST", api._url("/check-runs"), {**body, "output": output})
    assert str(caught.value) == (
        "POST /repos/sample/tasks/check-runs: HTTP 422: "
        "Invalid request. Only 50 annotations are allowed per request."
    )


def test_a_wrong_token_is_refused_without_echoing_it(fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError) as caught:
        client(fake, token="s3cret-value").upsert_comment(1, "x")
    assert str(caught.value) == (
        "GET /repos/sample/tasks/issues/1/comments?per_page=100: HTTP 401: Bad credentials"
    )
    assert "s3cret" not in str(caught.value)
    assert fake.requests[0].authorized is False


def test_an_unreachable_api_is_an_error() -> None:
    fake = FakeGitHub(REPO).start()
    url = fake.url
    fake.stop()
    with pytest.raises(GitHubError, match=r"^GET /repos/sample/tasks/pulls/1: "):
        GitHubClient(REPO, api=url, timeout=5).pull_request_files(1)


class Canned:
    """A stand-in for ``urlopen``'s response."""

    def __init__(self, body: bytes, link: str | None = None) -> None:
        self.body = body
        self.headers = Message()
        if link:
            self.headers["Link"] = link

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> Canned:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def serve(monkeypatch: pytest.MonkeyPatch, *responses: Canned | Exception) -> None:
    queue = list(responses)

    def urlopen(request: urllib.request.Request, timeout: float) -> Canned:
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_odd_responses_are_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GitHubClient(REPO, api="https://api.example")
    serve(monkeypatch, Canned(b"<html>"))
    with pytest.raises(GitHubError, match="the response is not JSON"):
        api.upsert_comment(1, "x")
    serve(monkeypatch, Canned(b"{}"))
    with pytest.raises(GitHubError, match="expected a JSON list"):
        api.upsert_comment(1, "x")
    serve(monkeypatch, Canned(b"[]", '<https://elsewhere.example/page2>; rel="next"'))
    with pytest.raises(GitHubError, match="refusing to follow a pagination link to another host"):
        api.upsert_comment(1, "x")
    error = urllib.error.HTTPError(
        "https://api.example/x", 502, "Bad Gateway", Message(), io.BytesIO(b"<html>")
    )
    serve(monkeypatch, error)
    with pytest.raises(GitHubError, match=r"HTTP 502: Bad Gateway$"):
        api.upsert_comment(1, "x")
    listed = urllib.error.HTTPError(
        "https://api.example/x", 500, "Server Error", Message(), io.BytesIO(b"[1]")
    )
    serve(monkeypatch, listed)
    with pytest.raises(GitHubError, match=r"HTTP 500: Server Error$"):
        api.upsert_comment(1, "x")
    serve(monkeypatch, Canned(b""), Canned(b"[]"))
    assert api.pull_request_files(1) == []


def test_long_bodies_are_cut_with_a_note() -> None:
    assert fit("short") == "short"
    long = fit("x" * (MAX_BODY + 10))
    assert len(long) == MAX_BODY
    assert long.endswith(TRUNCATED)
    assert comment_body("## TaskGate").startswith(f"{MARKER}\n## TaskGate")
    assert batches([]) == [[]]
    title = Annotation("a", "failure", "t" * 300, "m").to_api()["title"]
    assert len(title) == github.MAX_TITLE


# The fake itself.


def request(fake: FakeGitHub, method: str, path: str, body: bytes | None = None) -> tuple[int, Any]:
    headers = {"Authorization": f"Bearer {DEFAULT_TOKEN}"}
    made = urllib.request.Request(fake.url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(made, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_the_fake_answers_like_github_for_what_it_does_not_know(fake: FakeGitHub) -> None:
    prefix = f"/repos/{REPO}"
    assert request(fake, "GET", "/repos/other/repo/pulls/1")[0] == 404
    assert request(fake, "GET", f"{prefix}/pulls/1")[0] == 404
    assert request(fake, "GET", f"{prefix}/pulls/1/files")[0] == 404
    assert request(fake, "GET", f"{prefix}/nothing")[0] == 404
    assert request(fake, "PATCH", f"{prefix}/issues/1/comments", b'{"body": "x"}')[0] == 404
    assert request(fake, "PATCH", f"{prefix}/issues/comments/99", b'{"body": "x"}')[0] == 404
    assert request(fake, "PATCH", f"{prefix}/check-runs/99", b'{"output": {}}')[0] == 404
    assert request(fake, "POST", f"{prefix}/check-runs", b"[]") == (
        422,
        {"message": "Invalid request."},
    )
    assert request(fake, "POST", f"{prefix}/check-runs", b'{"name": "n"}')[0] == 422
    assert request(fake, "POST", f"{prefix}/issues/1/comments", b"{nope") == (
        400,
        {"message": "Problems parsing JSON"},
    )
    assert request(fake, "POST", f"{prefix}/issues/1/comments", b'{"body": 1}')[0] == 422
    status, state = request(fake, "GET", "/_fake/state")
    assert status == 200
    assert [r["status"] for r in state["requests"]] == [404] * 7 + [422, 422, 400, 422]
    assert state["comments"] == {}
    assert state["check_runs"] == []


def test_the_fake_serves_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[FakeGitHub] = []
    monkeypatch.setattr(FakeGitHub, "serve_forever", lambda self: served.append(self))
    main(["--port", "0", "--repo", "a/b", "--token", "t"])
    (fake,) = served
    assert (fake.repo, fake.token) == ("a/b", "t")
    assert capsys.readouterr().out == f"fake GitHub API for a/b on {fake.url}\n"
    monkeypatch.undo()
    thread = threading.Thread(target=fake.serve_forever)
    thread.start()
    status, _ = request(fake, "GET", "/_fake/state")
    fake.shutdown()
    thread.join(timeout=10)
    assert status == 200
    assert not thread.is_alive()


def test_the_fake_updates_the_right_comment_and_stops_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with FakeGitHub(REPO) as fake:
        fake.add_comment(1, "first")
        fake.add_comment(2, "other pull request")
        second = fake.add_comment(2, "second")
        status, body = request(
            fake, "PATCH", f"/repos/{REPO}/issues/comments/{second}", b'{"body": "edited"}'
        )
        assert (status, body["body"]) == (200, "edited")
        assert [c["body"] for c in fake.comments(2)] == ["other pull request", "edited"]
        gone = request(fake, "PATCH", f"/repos/{REPO}/issues/comments/99", b'{"body": "x"}')
        assert gone == (404, {"message": "Not Found"})
    FakeGitHub(REPO).stop()
    monkeypatch.setattr(sys, "argv", ["fakegithub", "--help"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(fakegithub.__file__, run_name="__main__")
    assert exited.value.code == 0
    assert "--port" in capsys.readouterr().out
