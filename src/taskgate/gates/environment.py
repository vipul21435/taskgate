"""Environment gates: the Dockerfile is sound and builds (TG301), and every image
it pulls is pinned by digest (TG302).

TG301 always runs the static checks in :func:`taskgate.dockerfile.static_problems`.
With the Docker runner it also builds the image (tagged by the build context's
content, so an unchanged environment is built once); with the local runner the
static checks are all it can do, and its message says the image was not built.
"""

from __future__ import annotations

from taskgate.dockerfile import external_images, listed, read, static_problems
from taskgate.gates.base import Check, TaskContext, gate
from taskgate.results import Severity


@gate(
    "TG301",
    "environment-builds",
    severity=Severity.ERROR,
    summary="environment/Dockerfile passes the static checks and builds (Docker runner)",
    fix_hint=(
        "Make docker build environment/ succeed: every COPY source must exist in "
        "environment/, environment/workspace/ must be copied into the image, and the "
        "final stage needs a non-root USER."
    ),
)
def environment_builds(ctx: TaskContext) -> Check:
    env_dir = ctx.task_dir / "environment"
    if not env_dir.is_dir():
        return Check.skip("skipped: environment/ is missing (see TG101)")
    problems = static_problems(env_dir, ctx.manifest.dockerfile)
    built = ctx.build()
    if not built.ok and built.message not in problems:
        problems.append(built.message)
    if problems:
        return Check.fail(listed(problems))
    if built.image is not None:
        return Check.ok(f"environment builds: {built.message}")
    return Check.ok(f"Dockerfile passes the static checks; {built.message}")


@gate(
    "TG302",
    "base-images-pinned",
    severity=Severity.ERROR,
    summary="every FROM (and COPY --from) image is pinned by sha256 digest",
    fix_hint=(
        "Pin each image by digest, e.g. FROM python:3.12-slim@sha256:<digest> "
        "(docker buildx imagetools inspect python:3.12-slim prints it); scratch and "
        "earlier build stages need no digest."
    ),
)
def base_images_pinned(ctx: TaskContext) -> Check:
    instructions = read(ctx.task_dir / "environment", ctx.manifest.dockerfile)
    if isinstance(instructions, str):
        return Check.skip(f"skipped: {instructions} (see TG301)")
    refs = external_images(instructions)
    unpinned = [ref.describe() for ref in refs if not ref.pinned]
    if unpinned:
        count = len(unpinned)
        noun = "image is" if count == 1 else "images are"
        return Check.fail(f"{count} {noun} not pinned by digest: {listed(unpinned)}")
    if not refs:
        return Check.ok("no external images (scratch or build stages only)")
    noun = "image" if len(refs) == 1 else "images"
    return Check.ok(f"{len(refs)} {noun} pinned by sha256 digest")


ENVIRONMENT_GATES = (environment_builds, base_images_pinned)
