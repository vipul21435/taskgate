# TaskGate plan

TaskGate runs review gates on pull requests that add or change benchmark tasks
for AI coding agents: it finds the task directories a pull request touches,
runs a set of gates on each one (schema, hygiene, environment build, reference
solution passes, empty or stub baseline fails, grader determinism), caches
results by task content, and reports to the pull request. It mirrors the
submission-pipeline side of benchmark-task work at an AI-data company: every task
passes the same review gates before it is accepted.

## Decisions

- **Fresh repository, not a fork.** On 2026-09-30 `gh search repos` for
  benchmark-task validators, pull-request gate actions and changed-directory
  detection found nothing small, permissively licensed and close enough to
  build on, so the project starts from scratch under MIT.
- **Stack:** Python 3.12 via uv (uv.lock committed), src layout, Typer CLI,
  hatchling build, ruff lint and format, mypy --strict on src/, pytest with a
  90% branch-coverage gate (`fail_under` in pyproject.toml), pre-commit hooks
  that run ruff and mypy through uv.
- **Task layout:** documented in [docs/task-layout.md](docs/task-layout.md).
  A task is any directory holding `task.toml`; a complete task also has
  `instruction.md`, `environment/`, `solution/` and `tests/`. The grader runs
  `python -m pytest` from the task workspace after `solution/solve.sh`.
- **Gate codes:** `TG` plus three digits, grouped by hundreds and never reused:
  TG1xx layout and manifest, TG2xx hygiene (secrets, sizes), TG3xx environment,
  TG4xx solution and baselines, TG5xx determinism, TG6xx reserved.
  Severities are `error` (blocking), `warning` and `info`.
- **Exit codes:** 0 when no blocking gate fails, 1 when one does, 2 for usage
  errors (bad path, bad base ref).
- **Offline by default:** tests and `make demo` need no network, no Docker and no
  tokens. Docker is used when available; a local subprocess runner is the
  fallback. GitHub is reached only through a client with a configurable base
  URL, tested against an in-process fake API.
- **Images:** digest-pinned slim bases, non-root user, `LABEL project=taskgate`;
  `make docker` prunes only this project's dangling images. The image installs
  `git` from Debian (not version-pinned) because `taskgate check` diffs with it.
- **Core decisions (2026-09-30):**
  - Changed tasks come from `git diff --no-renames <merge-base> HEAD`; task roots
    at each ref come from `git ls-tree`, with the same outermost-`task.toml` rule
    as the disk scan. A rename is a removed task plus an added task.
  - The local runner copies workspace, solution and tests into a temp dir, runs
    `sh solve.sh`, then the grader with TaskGate's own interpreter and an empty
    `pytest.ini`; `pytest` moved from a dev to a runtime dependency for this.
  - TG401/TG402 require TG101 (skipped otherwise); TG102 failures do not skip
    them so authors see every problem in one run. pytest exit 5 (no tests) fails
    TG402.
  - `--out` is opt-in (no surprise writes); `--format` picks what goes to stdout.
  - Demo pull requests live as overlays in `examples/pull-requests/<n-name>/`;
    `examples/build_sample_repo.py` commits them with fixed metadata so hashes and
    reports are identical on every machine (checked: macOS and the Linux image
    produce byte-identical `report.md` and `report.json`).
  - Writing the Markdown to `$GITHUB_STEP_SUMMARY` is done by the CI workflow,
    not by TaskGate, until the GitHub slice.

- **Slice 1 decisions (2026-09-30):**
  - `Gate` is a `typing.Protocol` (read-only properties plus `check(ctx)`), so a
    frozen dataclass, a class instance or the `@gate` decorator's `FunctionGate`
    all qualify. Gates run in code order; `requires` must name a lower code.
  - Third-party gates must use TG7xx-TG9xx, so a later built-in code can never
    collide with an installed plugin. A plugin that fails to load or breaks the
    contract is a usage error (exit 2) listing every problem; a gate that raises
    at check time is reported as a failure and the other gates still run.
  - `taskgate.toml` is read from the base ref in diff mode (`git show
    <base>:taskgate.toml`), never from the pull request, so a pull request cannot
    disable the gates that check it. `--all` reads `<root>/taskgate.toml`;
    `--config` overrides both. Disabled gates are not reported; a gate whose
    requirement is disabled is skipped with "(disabled)" in the message.

- **Spec change (2026-09-30):** the cheat-probe gates (TG6xx) were dropped from
  the spec by the run orchestrator, so slice 3 is now grader determinism only and
  TG6xx is kept as a reserved, unused range (plugins still start at TG7xx so the
  plugin contract does not change).
- **Slice 1 progress (2026-09-30):** the registry, `taskgate gates`, entry-point
  plugins and `taskgate.toml` (disable, severity) are committed; the static gates
  (manifest lint, secret scan, file-size limits) are still to do. An interrupted
  agent's untested draft of them was stashed locally ("wip from interrupted
  agent"), not committed.
- **Static gates (2026-09-30, slice 1 done):**
  - The stashed draft was reviewed, not applied blindly. Kept: `unknown_keys`,
    `ManifestCheck.parsed`/`declared_timeout_sec`, the lint gates, the scanner's
    known-format list and the typed `[section]` readers. Changed: hints name the
    qualified key (`task.timeout_sec`, `[environment]`) and point a known key in
    the wrong table to where it belongs; `[secrets] allow` is stored as strings
    (validated at parse time, compiled once per scan) so the config stays plain
    data for the future cache key; the scan streams files line by line instead of
    reading them whole; TG105 was renamed `instruction-not-empty` and reads
    `utf-8-sig`. The stash is now superseded and can be dropped.
  - The draft claimed its class-transition rule kept CamelCase identifiers out;
    it did not (`PyUnicode_AsLatin1String` was flagged, 49 hits in the installed
    site-packages outside RECORD files, 2809 with them). Added a word rule
    (fewer than 15% of neighbouring pairs among the 50 commonest English
    bigrams) and blanking of `sha256=`/`sha512-` digests: 0 findings on 689,539
    lines, at a recall cost of 89% / 97% / 97% / 99.75% on seeded 24/32/40/64
    character base64 tokens (`examples/secret_survey.py`).
  - Severities: TG103 and TG104 warn (a typo or a long timeout is suspicious,
    not wrong); TG105, TG201, TG202 and TG203 block. Binary files block unless
    `[files] binary_allow` lists them, and since the config is read from the
    base ref, accepting a binary is a maintainer decision, not the author's.
  - Globs use `fnmatch.fnmatchcase` on task-relative POSIX paths (`*` crosses
    `/`), for both `[secrets] exclude` and `[files] binary_allow`.
  - Reports list every option that differs from its default
    (`ConfigSummary.options`, JSON `config.options`), so a relaxed limit is
    visible in the pull-request comment.
  - Test fixtures assemble fake tokens at runtime so no token literal is pushed;
    the demo's planted key is a random string with no known format.

- **Slice 2 decisions (2026-09-30):**
  - The `Runner` protocol has `name`, `build(task_dir)` and
    `run(task_dir, solution=..., timeout_sec=...)`, where the solution is
    `"reference"`, `"none"` or a `Stub`; `TaskContext` memoizes the build and
    each distinct run, so TG401 and TG403 share one reference run.
  - "No-op stub" (TG403) is read as: a generated `solve.sh` that creates every
    file the reference solution created, empty, and does nothing else. A literal
    `exit 0` stub would be the TG402 baseline again; the empty-output stub
    catches graders that check only that an output exists (the third demo pull
    request). Files the reference edits in place are left alone, so for
    fix-the-code tasks the stub is a no-op.
  - The Docker runner uses the image's own workdir (seeded by the Dockerfile's
    `COPY workspace/`), not a copy TaskGate makes, because that is what an agent
    sees; `solution/` and `tests/` are streamed in as a tar on stdin into a tmpfs
    at `/taskgate`, so no bind mounts and no uid mapping are needed.
  - Non-root: runs use the image's `USER`, or `65534:65534` when that would be
    root, and the driver refuses uid 0. TG301's static checks require an explicit
    non-root `USER` in the final stage (the base image's user is not visible
    without Docker, and an explicit line is what review wants anyway).
  - TG301 is static checks everywhere plus the build on the Docker runner, so both
    runners report Dockerfile problems. TG401/TG402 skip (not fail) when the
    build failed; TG301 and TG302 skip themselves when `environment/` or the
    Dockerfile is missing instead of requiring TG101, so a task with a missing
    `tests/` still gets its Dockerfile reviewed.
  - `--runner auto|docker|local` (env `TASKGATE_RUNNER`) is a CLI choice, not a
    `taskgate.toml` key: which isolation a CI job has is the CI owner's call;
    `[runner]` in `taskgate.toml` (base ref) only sets limits. `make demo` pins
    the local runner so its reports stay byte-identical with the image's;
    `make demo-docker` is the Docker variant.
  - Build failures are reported with the last build-log line, with BuildKit's
    per-build `ref <id>::<id>` blanked so reports stay stable.

- **Review fixes (2026-09-30):**
  - TG201 took quadratic time on long lines (673 s for one 977 KiB line): the
    unanchored URL pattern was retried at every letter of a run. URL and JSON web
    token patterns are now anchored at the start of their run with a lookbehind
    (a JWT right after a `-` is no longer matched as a JWT; the entropy detector
    still sees it), and TG201 reads at most `[files] max_file_bytes` of each file,
    with bounded `readline` calls, noting a partial scan in its message. The cap
    reuses TG202's limit instead of a new key: a larger file fails TG202 anyway.
  - The hygiene gates dropped committed files named like tool caches. In diff mode
    they now take the task's file list from `git ls-tree` at `HEAD` (no name
    filter); `--all` keeps the disk walk that leaves caches out, since a working
    tree collects them. The local runner's copy now leaves out exactly the names
    the disk walk leaves out (it used to copy `.mypy_cache/`, `.ruff_cache/` and
    `.DS_Store`), so a run never uses a file the `--all` gates did not see.

## Scaffold (done)

- [x] uv project, src layout, strict tooling, MIT license
- [x] Task layout discovery: `taskgate tasks [ROOT] [--json] [--strict]`
- [x] Sample repository under `examples/sample-repo` (one complete task, one draft)
- [x] Makefile (install, lint, fmt, typecheck, test, cov, check, demo, docker),
      pre-commit, digest-pinned Dockerfile, GitHub Actions CI with a docker demo job

## Core (deliverable)

- [x] **`taskgate check`, smallest end to end.** Done 2026-09-30: 89 tests,
  100% branch coverage, CI green including the docker demo job.
  1. Changed-task discovery: `git diff --name-only base...HEAD` against
     `--base` (default `origin/main`, falling back to `main`), each path mapped to
     its enclosing task directory; deleted tasks are listed as removed and skipped.
  2. A `GateResult` model (code, severity, task, message, fix hint) and the
     first gates: layout complete (TG101), manifest parses with the required
     keys and an id equal to the directory name (TG102).
  3. A local runner that seeds a temporary workspace from
     `environment/workspace/`, runs `solution/solve.sh`, then the grader:
     reference solution must pass (TG401), the untouched workspace must fail
     (TG402).
  4. Reports: a Markdown summary and a JSON file under `--out`; exit 1 when a
     blocking gate fails.
  5. `examples/build_sample_repo.py` builds a throwaway git repository with a
     `main` branch and two pull-request branches (one good task, one bad task
     whose grader passes an empty workspace); `make demo` checks both and shows
     one pass and one blocking failure. The Docker CI job runs the same demo.
  6. README rewritten with real output, architecture diagram and measured numbers.

## Slices

- [x] **1. Gate plugin registry and static gates.** Done 2026-09-30: 10 built-in
  gates, 216 tests, 100% branch coverage, CI green. A `Gate` protocol and
  registry with stable codes, severities and fix hints; third-party gates via the
  `taskgate.gates` entry-point group; `taskgate gates` lists every code; a
  `taskgate.toml` config to disable gates or override severities. Static gates:
  manifest lint (unknown keys, timeout range, empty instruction), secret scan
  (known token formats, private keys, high-entropy strings with an allowlist),
  file-size and binary-file limits. Each gate tested with passing and failing
  fixtures.
- [x] **2. Environment build and container runner.** Done 2026-09-30: 13 built-in
  gates, 292 tests (plus 4 opt-in real-Docker tests, green locally and in CI),
  100% branch coverage. A `Runner` protocol with a
  Docker runner (builds `environment/Dockerfile` under a content-derived tag and
  project label, runs the solution and the grader with `--network none`, CPU,
  memory and time limits, as a non-root user) and automatic fallback to the local
  runner when Docker is unavailable. Gates: environment builds (TG301), every
  `FROM` is digest-pinned (TG302), a no-op stub solution must fail the grader
  (TG403). Unit tests use a fake `docker` executable on PATH; one opt-in
  integration test uses real Docker.
- [ ] **3. Grader determinism.** The determinism gate (TG501) reruns the grader
  N times (default 5, `[determinism] runs` in `taskgate.toml`) on the reference
  solution's output, each run with a seeded shuffle of test order (a small pytest
  plugin bundled with TaskGate), a different `PYTHONHASHSEED` and a different
  `TASKGATE_SEED`, and compares the verdict and every per-test outcome (read from
  pytest's JUnit XML) across runs. A difference fails the gate and the report
  lists each flaky test with the runs and seeds where it flipped, so the author
  can reproduce it with one command. Tests use fixture tasks with a stable
  grader, an order-dependent grader and a hash-seed-dependent grader.
- [ ] **4. Content-hash result cache.** A canonical task hash (sorted relative
  POSIX paths, file bytes and executable bits, ignored files excluded, plus the
  TaskGate version and gate config) keys results stored under `.taskgate/cache`
  with atomic writes and a lock file; unchanged tasks are skipped and reported as
  cached; `--no-cache`, `taskgate cache stats` and `taskgate cache prune`. Tests
  prove the hash ignores mtimes and walk order and changes on any byte or mode
  change.
- [ ] **5. GitHub reporting, fake API and composite action.** A stdlib GitHub
  client (base URL from `TASKGATE_GITHUB_API`, token from `GITHUB_TOKEN`) that
  lists pull-request files, upserts a single summary comment by hidden marker and
  creates a check run with annotations in batches of 50; a JUnit XML report and
  workflow-command annotations; an in-process fake GitHub API that records
  requests, used by the tests and `make demo`; a composite `action.yml` and a CI
  job that runs TaskGate on its own sample tasks through `uses: ./`.
