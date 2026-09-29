# TaskGate plan

TaskGate runs review gates on pull requests that add or change benchmark tasks
for AI coding agents: it finds the task directories a pull request touches,
runs a set of gates on each one (schema, hygiene, environment build, reference
solution passes, empty baseline fails, grader determinism, cheat probes), caches
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
  TG4xx solution and baselines, TG5xx determinism, TG6xx cheat probes.
  Severities are `error` (blocking), `warning` and `info`.
- **Exit codes:** 0 when no blocking gate fails, 1 when one does, 2 for usage
  errors (bad path, bad base ref).
- **Offline by default:** tests and `make demo` need no network, no Docker and no
  tokens. Docker is used when available; a local subprocess runner is the
  fallback. GitHub is reached only through a client with a configurable base
  URL, tested against an in-process fake API.
- **Images:** digest-pinned slim bases, non-root user, `LABEL project=taskgate`;
  `make docker` prunes only this project's dangling images.

## Scaffold (done)

- [x] uv project, src layout, strict tooling, MIT license
- [x] Task layout discovery: `taskgate tasks [ROOT] [--json] [--strict]`
- [x] Sample repository under `examples/sample-repo` (one complete task, one draft)
- [x] Makefile (install, lint, fmt, typecheck, test, cov, check, demo, docker),
      pre-commit, digest-pinned Dockerfile, GitHub Actions CI with a docker demo job

## Core (deliverable)

- [ ] **`taskgate check`, smallest end to end.**
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

- [ ] **1. Gate plugin registry and static gates.** A `Gate` protocol and
  registry with stable codes, severities and fix hints; third-party gates via the
  `taskgate.gates` entry-point group; `taskgate gates` lists every code; a
  `taskgate.toml` config to disable gates or override severities. Static gates:
  manifest lint (unknown keys, timeout range, empty instruction), secret scan
  (known token formats, private keys, high-entropy strings with an allowlist),
  file-size and binary-file limits. Each gate tested with passing and failing
  fixtures.
- [ ] **2. Environment build and container runner.** A `Runner` protocol with a
  Docker runner (builds `environment/Dockerfile` under a content-derived tag and
  project label, runs the solution and the grader with `--network none`, CPU,
  memory and time limits, as a non-root user) and automatic fallback to the local
  runner when Docker is unavailable. Gates: environment builds (TG301), every
  `FROM` is digest-pinned (TG302), a no-op stub solution must fail the grader
  (TG403). Unit tests use a fake `docker` executable on PATH; one opt-in
  integration test uses real Docker.
- [ ] **3. Grader determinism and cheat probe.** The determinism gate reruns the
  grader N times (default 5) with a seeded shuffle of test order (a bundled pytest
  plugin) and varied `PYTHONHASHSEED` and random seeds, and fails on any change
  in verdict or per-test outcome (TG501). The cheat probe flags graders that only
  check exit codes or have tests without assertions (TG601), graders that pass a
  solution which writes literal expected output copied from the tests (TG602),
  and tests or solutions that read files they must not (the solution reading
  `tests/`, TG603).
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
