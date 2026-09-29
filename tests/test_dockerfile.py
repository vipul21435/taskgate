"""The Dockerfile parser, the pulled-image list (TG302) and the static checks (TG301)."""

from pathlib import Path

import pytest

from taskgate.dockerfile import (
    ImageRef,
    external_images,
    listed,
    parse,
    static_problems,
)

PIN = "@sha256:" + "a" * 64


def keywords(text: str) -> list[tuple[int, str, str]]:
    return [(ins.line, ins.keyword, ins.args) for ins in parse(text)]


def test_parse_joins_continuations_and_skips_comments_and_heredoc_bodies() -> None:
    text = (
        "# syntax=docker/dockerfile:1\n"
        "from python:3.12 AS base\n"
        "\n"
        "# a comment\n"
        "RUN apt-get update \\\n"
        "# a comment inside the continuation\n"
        "\n"
        "    && apt-get install -y git\n"
        "RUN <<EOF\n"
        "FROM not-an-instruction\n"
        "EOF\n"
        "COPY <<-END /etc/motd\n"
        "\tUSER root\n"
        "\tEND\n"
        "user\tagent\n"
        "WORKDIR\n"
    )
    assert keywords(text) == [
        (2, "FROM", "python:3.12 AS base"),
        (5, "RUN", "apt-get update     && apt-get install -y git"),
        (9, "RUN", "<<EOF"),
        (12, "COPY", "<<-END /etc/motd"),
        (15, "USER", "agent"),
        (16, "WORKDIR", ""),
    ]


def test_parse_honours_the_escape_directive_and_a_trailing_continuation() -> None:
    text = "# escape=`\nFROM alpine\nRUN echo a `\n  b\nRUN echo \\\nRUN last `"
    assert keywords(text) == [
        (2, "FROM", "alpine"),
        (3, "RUN", "echo a   b"),
        (5, "RUN", "echo \\"),
        (6, "RUN", "last"),
    ]


def refs(text: str) -> list[tuple[int, str, str, str | None, bool]]:
    return [
        (r.line, r.instruction, r.written, r.resolved, r.pinned)
        for r in external_images(parse(text))
    ]


def test_external_images_skip_scratch_and_build_stages() -> None:
    text = (
        f"FROM --platform=linux/amd64 golang:1.23{PIN} AS build\n"
        "FROM scratch\n"
        "FROM build AS again\n"
        "FROM alpine:3.20\n"
        "COPY --from=build /out /out\n"
        "COPY --from=0 /a /a\n"
        "COPY --from=busybox:1.36 /bin/sh /bin/sh\n"
        f"ADD --from=nginx{PIN} /x /x\n"
        "COPY --chown=1 src /src\n"
        "FROM\n"
    )
    assert refs(text) == [
        (1, "FROM", f"golang:1.23{PIN}", f"golang:1.23{PIN}", True),
        (4, "FROM", "alpine:3.20", "alpine:3.20", False),
        (7, "COPY --from", "busybox:1.36", "busybox:1.36", False),
        (8, "ADD --from", f"nginx{PIN}", f"nginx{PIN}", True),
    ]


def test_global_arg_defaults_are_substituted() -> None:
    text = (
        f'ARG BASE=python:3.12-slim{PIN} TAG="3.20" EMPTY\n'
        "FROM $BASE\n"
        "FROM ${BASE}\n"
        "FROM alpine:${TAG}\n"
        "FROM alpine:${MISSING:-3.19}\n"
        "FROM alpine${TAG:+:3.18}\n"
        "FROM alpine${EMPTY:+:never}\n"
        "FROM ${EMPTY}\n"
        "ARG LATE=ignored\n"
        "FROM ${LATE}\n"
        "ARG BROKEN='unclosed\n"
    )
    assert refs(text) == [
        (2, "FROM", "$BASE", f"python:3.12-slim{PIN}", True),
        (3, "FROM", "${BASE}", f"python:3.12-slim{PIN}", True),
        (4, "FROM", "alpine:${TAG}", "alpine:3.20", False),
        (5, "FROM", "alpine:${MISSING:-3.19}", "alpine:3.19", False),
        (6, "FROM", "alpine${TAG:+:3.18}", "alpine:3.18", False),
        (7, "FROM", "alpine${EMPTY:+:never}", "alpine", False),
        (8, "FROM", "${EMPTY}", None, False),
        (10, "FROM", "${LATE}", None, False),
    ]


def test_image_refs_describe_themselves() -> None:
    assert ImageRef(3, "FROM", "alpine", "alpine").describe() == "line 3: FROM alpine"
    assert ImageRef(3, "FROM", "$B", "alpine").describe() == "line 3: FROM $B (= alpine)"
    assert ImageRef(3, "FROM", "$B", None).describe() == (
        "line 3: FROM $B (a variable in it has no default)"
    )


def write_env(tmp_path: Path, dockerfile: str, *files: str) -> Path:
    env = tmp_path / "environment"
    env.mkdir()
    (env / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    for name in files:
        (env / name).parent.mkdir(parents=True, exist_ok=True)
        (env / name).write_text("x\n", encoding="utf-8")
    return env


GOOD = f"""\
FROM python:3.12-slim{PIN}
WORKDIR /workspace
COPY --chown=agent workspace/ /workspace/
COPY ["requirements.txt", "/tmp/"]
COPY *.cfg /etc/
USER agent
"""


def test_a_sound_dockerfile_has_no_static_problems(tmp_path: Path) -> None:
    env = write_env(tmp_path, GOOD, "workspace/input.txt", "requirements.txt", "a.cfg")
    assert static_problems(env, "Dockerfile") == []


def test_static_problems_are_all_listed(tmp_path: Path) -> None:
    dockerfile = (
        "ARG X=1\n"
        "RUN true\n"
        "FROM alpine\n"
        "COPY missing.txt /x\n"
        "COPY ../secret /x\n"
        "COPY *.nope /x\n"
        "COPY [broken /x\n"
        "ADD https://example.invalid/a.tgz /x\n"
        "COPY $SRC /x\n"
        "COPY <<EOF /x\n"
        "hello\n"
        "EOF\n"
        "BOGUS thing\n"
        "USER root:root\n"
    )
    env = write_env(tmp_path, dockerfile, "workspace/input.txt")
    assert static_problems(env, "Dockerfile") == [
        "line 13: unknown instruction BOGUS",
        "line 2: RUN comes before the first FROM",
        "line 4: COPY source 'missing.txt' not found in environment/",
        "line 5: COPY source '../secret' is outside the build context",
        "line 6: COPY source '*.nope' matches nothing in environment/",
        "line 7: COPY source '[broken' matches nothing in environment/",
        "line 14: the final stage runs as root (USER root:root)",
        "environment/workspace/ is never copied into the image",
    ]


@pytest.mark.parametrize(
    ("dockerfile", "problem"),
    [
        ("FROM alpine\n", "the final stage has no USER instruction, so the image runs as root"),
        ("FROM alpine\nUSER 0\n", "line 2: the final stage runs as root (USER 0)"),
        ("FROM alpine\nUSER\n", "line 2: USER has no value"),
        (
            "FROM alpine AS build\nUSER agent\nFROM alpine\nCOPY --from=build /a /a\n",
            "the final stage has no USER instruction, so the image runs as root",
        ),
    ],
)
def test_the_final_stage_must_not_run_as_root(
    tmp_path: Path, dockerfile: str, problem: str
) -> None:
    assert static_problems(write_env(tmp_path, dockerfile), "Dockerfile") == [problem]


def test_copying_the_whole_context_counts_as_copying_the_workspace(tmp_path: Path) -> None:
    env = write_env(tmp_path, "FROM alpine\nCOPY . /workspace\nUSER 1000\n", "workspace/in.txt")
    assert static_problems(env, "Dockerfile") == []


def test_unreadable_or_empty_dockerfiles(tmp_path: Path) -> None:
    env = write_env(tmp_path, "# only a comment\nARG X=1\n")
    assert static_problems(env, "Dockerfile") == ["environment/Dockerfile has no FROM instruction"]
    (env / "Dockerfile").write_bytes(b"FROM \xff\n")
    assert static_problems(env, "Dockerfile") == ["environment/Dockerfile is not UTF-8"]
    assert static_problems(env, "Other") == ["environment/Other not found"]


def test_listed_caps_long_lists() -> None:
    assert listed(["a", "b"]) == "a; b"
    assert listed([str(n) for n in range(8)]) == "0; 1; 2; 3; 4; and 3 more"


def test_unterminated_heredocs_and_unbalanced_quotes_do_not_crash() -> None:
    assert keywords("FROM alpine\nRUN <<EOF\necho never closed\n") == [
        (1, "FROM", "alpine"),
        (2, "RUN", "<<EOF"),
    ]
    assert refs("ARG B='x\nFROM alpine:$B\n") == [(2, "FROM", "alpine:$B", "alpine:'x", False)]
