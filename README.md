# TaskGate

[![CI](https://github.com/vipul21435/taskgate/actions/workflows/ci.yml/badge.svg)](https://github.com/vipul21435/taskgate/actions/workflows/ci.yml)

Review gates for pull requests that add or change benchmark tasks for AI coding
agents. TaskGate finds the task directories a pull request touches, runs each
one through a set of gates with stable codes (layout, manifest schema and lint,
secrets, file sizes and binaries, the environment builds from a digest-pinned
base, the reference solution must pass, an untouched workspace and an
empty-output stub must fail), and reports the result as terminal text, a
Markdown pull-request summary and JSON, with a non-zero exit when a blocking
gate fails. Solutions and graders run in the task's own Docker image with no
network and resource limits, or in a local temporary directory when Docker is
not available.

It mirrors the submission side of benchmark-task work at an AI-data company:
every task has to clear the same review gates before it is accepted, and a
grader that passes when the agent did nothing (or wrote an empty file where the
answer should be) is the most common way a task goes wrong.

## What works today

- **`taskgate check`** diffs the checked-out branch against its merge base with
  `--base` (default `origin/main`, falling back to `main`), maps every changed
  path to the task directory that owns it, labels each task `added`, `modified`
  or `removed`, and runs the gates on every task that still exists. `--all`
  checks every task under a directory instead (no git needed).
- **Thirteen gates**, run in code order; a gate whose prerequisite did not pass,
  or whose input is missing, is reported as `skip` rather than as a second failure:

  | Code | Name | Default | Passes when |
  | --- | --- | --- | --- |
  | TG101 | layout-complete | error | `task.toml`, `instruction.md`, `environment/`, `solution/solve.sh` and a `tests/test_*.py` exist |
  | TG102 | manifest-valid | error | `task.toml` parses; `id` is kebab-case and equals the directory name; `difficulty`, `timeout_sec` (1..3600), `workdir` are valid; every problem is listed at once |
  | TG103 | manifest-known-keys | warning | no table or key outside the schema; each unknown one gets a did-you-mean hint with the qualified name |
  | TG104 | timeout-in-range | warning | `timeout_sec` is inside the recommended range (10..1800 s unless `[manifest]` says otherwise) |
  | TG105 | instruction-not-empty | error | `instruction.md` is UTF-8 and has words beyond headings (ATX and setext), HTML comments (an unclosed `<!--` hides the rest) and bare Markdown markup (empty bullets, rules, fences, `>`) |
  | TG201 | no-secrets | error | no text file holds a known token format (cloud key ids, source-host, chat, payment and `sk-` API keys, JSON web tokens), a private key header or a high-entropy string |
  | TG202 | file-size-limits | error | every file is under 1 MiB and the task under 10 MiB (`[files]`) |
  | TG203 | no-binary-files | error | every binary file (NUL in the first 8000 bytes) matches a `[files] binary_allow` glob |
  | TG301 | environment-builds | error | the Dockerfile passes static checks (known instructions, `FROM` first, every `COPY` source present, non-root `USER`, `workspace/` copied in) and, on the Docker runner, builds |
  | TG302 | base-images-pinned | error | every `FROM` and `COPY --from` image ends in `@sha256:<digest>` after `ARG` defaults are substituted (`scratch` and build stages exempt) |
  | TG401 | solution-passes | error | `solve.sh` exits 0 and the pytest grader then passes (needs TG101 and a built environment) |
  | TG402 | baseline-fails | error | the grader fails on the untouched workspace and collects at least one test (needs TG101 and a built environment) |
  | TG403 | stub-solution-fails | error | the grader fails after a stub `solve.sh` that creates every file the reference solution created, empty (needs TG401) |

  Only `error` failures block. The full task layout, runtime contract and gate
  rules are in [docs/task-layout.md](docs/task-layout.md).
- **A secret scanner** (`secretscan.py`) that streams every text file line by
  line and never prints more than the first four characters of a match. Its
  high-entropy rule needs mixed case and digits, at least 4.0 bits per
  character, frequent character-class changes, and fewer than 15% common
  English bigrams, the last of which keeps long identifiers such as
  `PyUnicode_AsLatin1String` out. URLs and `sha256=`/`sha512-` digests are
  blanked first. A `taskgate: allow-secret` comment, `[secrets] allow` regexes
  and `exclude` globs handle false positives. Every pattern is linear in the
  line length (patterns that start with a run of characters are anchored at
  the run's start), and TG201 reads at most `[files] max_file_bytes` of a file,
  so a 1,000,000-character one-line input scans in well under a second.
- **Hygiene gates see what the pull request commits.** In diff mode TG201-TG203
  check every file git tracks in the task at `HEAD`, whatever its name, so a
  committed `__pycache__/` file, `*.pyc`, `.DS_Store` or `.mypy_cache/` entry
  cannot slip past them. `--all` walks the directory on disk and leaves those
  tool caches out, and the local runner leaves out the same names when it copies
  a task.
- **A Docker runner** (`--runner docker`, or `auto` when `docker version`
  answers). It builds `environment/Dockerfile` under a tag derived from the build
  context's content (`taskgate-env:<16 hex>`, labelled `project=taskgate`) and
  reuses an image whose label matches, so an unchanged environment is built once.
  Every run is a `docker run --rm` with `--network none`, `--cpus 1`,
  `--memory 1024m` with no extra swap, `--pids-limit 256`, all capabilities
  dropped, `no-new-privileges`, and the image's non-root `USER` (`65534:65534`
  if the image would run as root; the in-container driver refuses uid 0). The
  solution and tests arrive as a tar on stdin into a tmpfs outside the
  workspace, and the task's `timeout_sec` kills the container. Limits are set
  under `[runner]` in `taskgate.toml`.
- **A local runner** (`--runner local`, and the automatic fallback with a note on
  stderr when Docker does not answer) that copies `environment/workspace/`,
  `solution/` and `tests/` into a fresh temporary directory, runs `sh solve.sh`
  and then `python -m pytest` there under the task's `timeout_sec` budget
  (killing the whole process group on timeout), and never writes into the task's
  source tree. On this runner TG301 does the static checks only and says the
  image was not built.
- **A Dockerfile reader** (`dockerfile.py`) that handles parser directives
  (`# escape=`), continuations across comment lines, heredocs and global `ARG`
  substitution (`$V`, `${V}`, `${V:-x}`, `${V:+x}`), used by TG301 and TG302.
- **Reports**: text on stdout (or `--format markdown|json`), plus `report.md` and
  `report.json` under `--out DIR`. Exit codes: 0 no blocking failure, 1 blocking
  failure, 2 usage error (not a git repository, unknown base ref, invalid
  `taskgate.toml`).
- **A sample repository builder** (`examples/build_sample_repo.py`) that creates
  a git repository with a `main` branch and three pull-request branches: one
  good task and two flawed ones. Commits use fixed authors and dates, so the
  merge base (`b26918e`) and every report are identical on macOS and in the
  Linux container, and the content-derived image tags are the same on macOS and
  on the GitHub Actions runner.
- **A gate registry.** Every gate satisfies one `Gate` protocol (code, name,
  severity, summary, fix hint, `requires`, `check(ctx)`); `taskgate gates`
  lists every code grouped by hundreds. Third-party gates register through the
  `taskgate.gates` entry-point group and must use TG7xx-TG9xx;
  [examples/plugin](examples/plugin) is a working example (TG701). A plugin that
  fails to load or breaks the contract stops the run with exit code 2, and a gate
  that raises is reported as a failure instead of hiding the other results.
- **`taskgate.toml`** turns gates off (`[gates] disable = [...]`), changes their
  severity (`[gates.severity] TG402 = "warning"`), tunes the static gates and
  sets the Docker runner's limits:

  ```toml
  [manifest]                  # TG104
  min_timeout_sec = 10
  max_timeout_sec = 1800

  [secrets]                   # TG201
  allow = ["^EXAMPLE"]        # regexes on the matched text
  exclude = ["tests/data/*"]  # task-relative path globs, not scanned
  min_length = 24
  entropy_threshold = 4.0

  [files]                     # TG202, TG203
  max_file_bytes = 1048576
  max_task_bytes = 10485760
  binary_allow = ["environment/workspace/*.png"]

  [runner]                    # the Docker runner
  cpus = 1.0
  memory_mb = 1024            # --memory and --memory-swap
  pids_limit = 256
  build_timeout_sec = 900
  ```

  Unknown sections, keys, gate codes, bad values and invalid regexes are errors
  (exit 2), all listed at once. In diff mode the file is read from the base ref
  (`git show main:taskgate.toml`), so a pull request cannot relax the gates that
  check it; `--config PATH` overrides that. Reports name the config source, every
  override and every option that differs from its default (`config: taskgate.toml;
  options manifest.max_timeout_sec=60, files.max_file_bytes=1024`).
- `taskgate tasks [ROOT]` lists task directories and the layout parts each lacks.
- A digest-pinned Docker image (non-root user, git included) that runs the CLI
  and the demo (on the local runner: the image has no Docker CLI), and GitHub
  Actions CI that runs lint, mypy, the tests with a coverage gate and the demo;
  a second job builds the image, runs the demo inside it, then runs the demo on
  the Docker runner and the opt-in real-Docker tests on the runner's daemon.

## Quickstart

```sh
git clone https://github.com/vipul21435/taskgate && cd taskgate
uv sync --locked
make demo
uv run taskgate check --all examples/sample-repo
```

Verified from a fresh clone. `make demo` needs no network, Docker or tokens (it
passes `--runner local` through `TASKGATE_RUNNER`) and took 1.95 to 2.00 s over three runs
(`time make demo`, 8 GB M-series Mac). The last command exits 1 on purpose: the
bundled draft task is incomplete. With Docker running, `make demo-docker` runs
the same three pull requests on the Docker runner (3.6 to 3.8 s with the task
images already built).

## Usage

```
taskgate check [REPO] [--base REF] [--all] [--out DIR] [--format text|markdown|json] [--config FILE]
               [--runner auto|docker|local]
taskgate gates [ROOT] [--json] [--config FILE]
taskgate tasks [ROOT] [--json] [--strict]
taskgate version
```

`make demo` builds the sample repository, checks out each pull-request branch
and runs `taskgate check <repo> --base main --out <dir>`. On the good branch:

```
taskgate 0.1.0: diff against main (merge base b26918e), 1 changed task, 1 other file, local runner
tasks/integer-determinant  added  PASS
  pass  TG101  layout-complete        layout complete
  pass  TG102  manifest-valid         manifest valid
  pass  TG103  manifest-known-keys    no unknown keys
  pass  TG104  timeout-in-range       task.timeout_sec 120 is within 10..1800
  pass  TG105  instruction-not-empty  instruction.md has 88 words
  pass  TG201  no-secrets             no secrets in 6 text files
  pass  TG202  file-size-limits       6 files, 3.5 KiB in total
  pass  TG203  no-binary-files        no binary files
  pass  TG301  environment-builds     Dockerfile passes the static checks; not built: the local runner does not use Docker
  pass  TG302  base-images-pinned     1 image pinned by sha256 digest
  pass  TG401  solution-passes        reference solution passes the grader (3 passed)
  pass  TG402  baseline-fails         an untouched workspace fails the grader (3 failed)
  pass  TG403  stub-solution-fails    a stub that writes output/determinants.txt empty fails the grader (2 failed, 1 passed)
result: PASS, 0 blocking failures
```

On the second branch, whose grader calls `pytest.skip` when the output file is
missing, whose manifest says `difficulty = "trivial"`, and whose `solve.sh`
exports a leftover API key (a planted random string, exit code 1):

```
taskgate 0.1.0: diff against main (merge base b26918e), 1 changed task, local runner
tasks/word-count  added  FAIL
  pass  TG101  layout-complete        layout complete
  FAIL  TG102  manifest-valid         task.difficulty 'trivial' is not one of easy, medium, hard
        fix: Fix task.toml to match the manifest schema in docs/task-layout.md.
  pass  TG103  manifest-known-keys    no unknown keys
  pass  TG104  timeout-in-range       task.timeout_sec 60 is within 10..1800
  pass  TG105  instruction-not-empty  instruction.md has 67 words
  FAIL  TG201  no-secrets             1 likely secret: solution/solve.sh:7 high-entropy string 'huG8...' (32 chars)
        fix: Remove the credential and rotate it, since it has been pushed. For a false positive, add 'taskgate: allow-secret' to the line, or an allow regex or exclude glob under [secrets] in taskgate.toml.
  pass  TG202  file-size-limits       6 files, 2.4 KiB in total
  pass  TG203  no-binary-files        no binary files
  pass  TG301  environment-builds     Dockerfile passes the static checks; not built: the local runner does not use Docker
  pass  TG302  base-images-pinned     1 image pinned by sha256 digest
  pass  TG401  solution-passes        reference solution passes the grader (2 passed)
  FAIL  TG402  baseline-fails         the grader passes an untouched workspace (2 skipped)
        fix: Make the grader assert on the output the instruction asks for, so a workspace where nothing was done cannot pass (no skips or early returns).
  pass  TG403  stub-solution-fails    a stub that writes output/counts.txt empty fails the grader (2 failed)
result: FAIL, 3 blocking failures
```

The third branch adds a task whose grader does fail an untouched workspace (it
asserts that `output/gcds.txt` exists), so TG402 passes, but compares lines with
a non-strict `zip()`, so an empty file passes every comparison; its Dockerfile
also names the base image by tag only. This run used the Docker runner
(`make demo-docker`), so TG301 built the image:

```
taskgate 0.1.0: diff against main (merge base b26918e), 1 changed task, docker runner
tasks/gcd-pairs  added  FAIL
  pass  TG101  layout-complete        layout complete
  pass  TG102  manifest-valid         manifest valid
  pass  TG103  manifest-known-keys    no unknown keys
  pass  TG104  timeout-in-range       task.timeout_sec 60 is within 10..1800
  pass  TG105  instruction-not-empty  instruction.md has 70 words
  pass  TG201  no-secrets             no secrets in 6 text files
  pass  TG202  file-size-limits       6 files, 2.6 KiB in total
  pass  TG203  no-binary-files        no binary files
  pass  TG301  environment-builds     environment builds: image taskgate-env:d98b9b87a032cdee
  FAIL  TG302  base-images-pinned     1 image is not pinned by digest: line 3: FROM python:3.12-slim
        fix: Pin each image by digest, e.g. FROM python:3.12-slim@sha256:<digest> (docker buildx imagetools inspect python:3.12-slim prints it); scratch and earlier build stages need no digest.
  pass  TG401  solution-passes        reference solution passes the grader (2 passed)
  pass  TG402  baseline-fails         an untouched workspace fails the grader (2 failed)
  FAIL  TG403  stub-solution-fails    the grader passes a stub that writes output/gcds.txt empty (2 passed)
        fix: Make the grader check what each output contains, not only that it exists: compare exact content or line counts (zip(..., strict=True)), so empty placeholder files cannot pass.
result: FAIL, 2 blocking failures
```

The `report.md` written for that branch on the local runner, ready to post as a
pull-request comment:

```markdown
## TaskGate: FAIL

2 blocking failures; diff against main (merge base b26918e), 1 changed task, local runner.

| Task | Change | Verdict | Blocking gates |
| --- | --- | --- | --- |
| `tasks/gcd-pairs` | added | **fail** | TG302, TG403 |

### `tasks/gcd-pairs`

| Gate | Status | Message |
| --- | --- | --- |
| TG101 layout-complete | pass | layout complete |
| TG102 manifest-valid | pass | manifest valid |
| TG103 manifest-known-keys | pass | no unknown keys |
| TG104 timeout-in-range | pass | task.timeout_sec 60 is within 10..1800 |
| TG105 instruction-not-empty | pass | instruction.md has 70 words |
| TG201 no-secrets | pass | no secrets in 6 text files |
| TG202 file-size-limits | pass | 6 files, 2.6 KiB in total |
| TG203 no-binary-files | pass | no binary files |
| TG301 environment-builds | pass | Dockerfile passes the static checks; not built: the local runner does not use Docker |
| TG302 base-images-pinned | **fail** (error) | 1 image is not pinned by digest: line 3: FROM python:3.12-slim |
| TG401 solution-passes | pass | reference solution passes the grader (2 passed) |
| TG402 baseline-fails | pass | an untouched workspace fails the grader (2 failed) |
| TG403 stub-solution-fails | **fail** (error) | the grader passes a stub that writes output/gcds.txt empty (2 passed) |

How to fix:

- **TG302**: Pin each image by digest, e.g. FROM python:3.12-slim@sha256:<digest> (docker buildx imagetools inspect python:3.12-slim prints it); scratch and earlier build stages need no digest.
- **TG403**: Make the grader check what each output contains, not only that it exists: compare exact content or line counts (zip(..., strict=True)), so empty placeholder files cannot pass.

<sub>taskgate 0.1.0</sub>
```

A copy of the accepted sample task with a broken environment (no `USER`, and a
`RUN pip install` step that exits 1), checked with
`taskgate check --all DIR --runner docker`. The static problem and the build
failure are reported together, and the runtime gates skip instead of failing a
second time:

```
taskgate 0.1.0: all tasks, 1 task, docker runner
  FAIL  TG301  environment-builds     the final stage has no USER instruction, so the image runs as root; docker build failed (exit 1): ERROR: failed to build: failed to solve: process "/bin/sh -c pip install --no-cache-dir pytest==9.1.2" did not complete successfully: exit code: 1
  skip  TG401  solution-passes        skipped: the environment did not build (see TG301)
  skip  TG402  baseline-fails         skipped: the environment did not build (see TG301)
  skip  TG403  stub-solution-fails    skipped: requires TG401 to pass
result: FAIL, 1 blocking failure
```

Manifest lint on a copy of the accepted sample task with two typos
(`timout_sec`, `[enviroment]`), checked with `taskgate check --all DIR`. TG102
reports what is missing, TG103 names the probable typo, TG104 skips because the
timeout is unusable, and the warning does not add to the blocking count:

```
modular-inverse  FAIL
  pass  TG101  layout-complete        layout complete
  FAIL  TG102  manifest-valid         missing [environment] table; missing task.timeout_sec; missing environment.dockerfile; missing environment.workdir
        fix: Fix task.toml to match the manifest schema in docs/task-layout.md.
  fail  TG103  manifest-known-keys    unknown in task.toml: task.timout_sec (did you mean task.timeout_sec?), [enviroment] (did you mean [environment]?)
        fix: Rename or remove the unknown keys; every key task.toml may hold is listed in docs/task-layout.md.
  skip  TG104  timeout-in-range       task.timeout_sec is missing or invalid (see TG102)
  pass  TG105  instruction-not-empty  instruction.md has 100 words
  ...
result: FAIL, 1 blocking failure
```

`taskgate gates` lists every code with its effective severity:

```
TG1xx  layout and manifest
  TG101  error    layout-complete        task.toml, instruction.md, environment/, solution/solve.sh and tests/test_*.py
  TG102  error    manifest-valid         task.toml parses and has every required key with a valid value
  TG103  warning  manifest-known-keys    task.toml has no unknown tables or keys (likely typos)
  TG104  warning  timeout-in-range       task.timeout_sec is inside the recommended range (default 10..1800 s)
  TG105  error    instruction-not-empty  instruction.md is UTF-8 and has text beyond headings and comments
TG2xx  hygiene
  TG201  error    no-secrets             no credentials: known token formats, private keys or high-entropy strings
  TG202  error    file-size-limits       every file and the task as a whole stay under the size limits (1 MiB, 10 MiB)
  TG203  error    no-binary-files        no binary files unless [files] binary_allow lists them
TG3xx  environment
  TG301  error    environment-builds     environment/Dockerfile passes the static checks and builds (Docker runner)
  TG302  error    base-images-pinned     every FROM (and COPY --from) image is pinned by sha256 digest
TG4xx  solution and baselines
  TG401  error    solution-passes        the reference solution passes the grader in a fresh workspace
  TG402  error    baseline-fails         the grader fails when no solution has run (untouched workspace)
  TG403  error    stub-solution-fails    a stub that writes the reference solution's new files, empty, fails the grader
13 gates: 13 built-in, 0 from plugins
```

`report.json` carries the same data plus the runner, the full merge-base hash,
each task's changed files and a `blocking` flag per gate. CI appends the demo
reports of both runners to the job summaries.

## Architecture

```mermaid
flowchart LR
    PR["checked-out PR branch"] --> CH["changes.py<br/>git merge-base + diff,<br/>ls-tree task roots"]
    CH --> TASKS["changed tasks<br/>added / modified / removed"]
    TASKS --> GATES["gates/<br/>core, lint, hygiene,<br/>environment<br/>(requires -> skip)"]
    GATES --> MAN["manifest.py<br/>schema, unknown keys"]
    GATES --> SEC["secretscan.py<br/>formats, keys, entropy"]
    GATES --> DF["dockerfile.py<br/>parse, static checks,<br/>pulled images"]
    GATES --> RUN["Runner: build + run<br/>reference / none / stub"]
    RUN --> DOCK["docker_runner.py<br/>content-tagged image,<br/>docker run --network none"]
    RUN --> LOC["runner.py LocalRunner<br/>temp workspace (fallback)"]
    GATES --> RES["results.py<br/>GateResult, TaskReport,<br/>CheckReport"]
    RES --> REP["report.py<br/>text / Markdown / JSON"]
    REP --> EXIT["exit 0 / 1 / 2"]
```

| Module | Role |
| --- | --- |
| `layout.py` | task discovery on disk (`task.toml` marks a task; outermost wins; hidden and tool dirs skipped) |
| `changes.py` | changed-task discovery from git, using the same task rules on `git ls-tree` of both refs |
| `manifest.py` | `task.toml` parsing, schema validation that collects every problem, unknown keys with did-you-mean hints |
| `runner.py` | the `Runner` protocol (`build`, `run` with a reference, no or stub solution), `RunResult`, the local subprocess runner |
| `docker_runner.py` | the Docker runner (content-derived tags, locked-down `docker run`, in-container driver), `docker_status` and the auto/docker/local choice |
| `dockerfile.py` | Dockerfile parsing, the static checks behind TG301, the pulled-image list behind TG302 |
| `files.py` | which task paths are content (caches and `.DS_Store` are not) and a sorted walk |
| `gates/` | the `Gate` protocol, `TaskContext` and `@gate` (`base.py`); layout, manifest and runtime gates (`core.py`); manifest and instruction lint (`lint.py`); secrets, sizes and binaries (`hygiene.py`); build and digest pins (`environment.py`) |
| `secretscan.py` | the credential detectors behind TG201, with redacted findings |
| `config.py` | `taskgate.toml` parsing and validation (every problem at once): gates, `[manifest]`, `[secrets]`, `[files]`, `[runner]` |
| `registry.py` | built-in plus entry-point gates, validated and sorted by code |
| `engine.py` | `run_gates`: runs gates in code order, skips unmet `requires`, contains gate crashes |
| `report.py` | pure renderers from a `CheckReport` to text, Markdown and JSON |
| `cli.py` | Typer commands `check` (with `--runner`), `gates`, `tasks`, `version` |

## Measured

| What | Command | Result |
| --- | --- | --- |
| Tests and coverage | `make cov` | 292 passed, 4 skipped (the opt-in real-Docker tests); 100% line and branch coverage of `src/` (1935 statements, 524 branches); gate is 90% |
| Real-Docker tests | `time make test-docker` | 4 passed in 7.2 to 8.1 s (two runs, task image already built); also green on the GitHub Actions runner |
| Demo wall time, local runner | `time make demo` | 1.95 to 2.00 s over three runs |
| Demo wall time, Docker runner | `time make demo-docker` | 3.62 to 3.79 s over three runs with the three task images built; 5.43 s after `make clean-images` (BuildKit layer cache still warm) |
| Demo in the TaskGate image | `time docker run --rm --entrypoint sh taskgate:local examples/demo.sh /tmp/taskgate-demo` | 2.03 to 2.21 s over three runs |
| Image sizes | `docker image ls taskgate`, `docker image ls taskgate-env` | TaskGate image 472 MB (python:3.12-slim plus git); each sample task image 235 MB |
| Reports, macOS vs image | `cmp` of each demo `report.md` and `report.json` (three pull requests) | all six byte-identical |
| Image tags, macOS vs CI | TG301 messages of `make demo-docker` locally and in the CI log | the same three tags (`taskgate-env:8c498462f77c4f11`, `...13e3d22e858b2e52`, `...d98b9b87a032cdee`) |
| Secret-scan false positives | `uv run python examples/secret_survey.py scan .venv/lib/python3.12/site-packages` | 0 findings in 2523 text files, 689,539 lines of the locked dependencies (macOS arm64), 4.3 s |
| Secret-scan recall | `uv run python examples/secret_survey.py recall` | 2000 seeded random base64 tokens per length: 24 chars 0.8905, 32 chars 0.9665, 40 chars 0.9720, 64 chars 0.9975 |
| Secret scan on the samples | `uv run python examples/secret_survey.py scan examples` | 1 finding: the key planted in the bad demo pull request |
| Secret scan, one long line | `uv run python examples/secret_survey.py timing` | 0.002 s at 20,000 characters, 0.009 s at 80,000 and 0.109 s at 1,000,000, the same for a run of one letter, random `a`/`b`, hex and DNA; before the URL pattern was anchored, 20,000 and 40,000 letters took 0.275 s and 1.094 s (quadratic) |

## Design decisions

- **Three-dot diff.** Changes are taken from the merge base of the base ref and
  `HEAD`, so commits that land on `main` after the branch forked are never
  attributed to the pull request. Renames are split (`--no-renames`) into a
  removed task and an added task, so the new location is always checked.
- **One task rule everywhere.** A task is the outermost directory holding
  `task.toml`, outside hidden and tool directories. The disk scan and the git
  scan (`git ls-tree` on both refs) apply the same rule, so `tasks` and `check`
  never disagree about what a task is.
- **Skip, do not cascade.** Runtime gates require TG101; when the layout is
  broken they report `skip` with the reason instead of a confusing crash.
  Manifest problems (TG102) do not skip the runtime gates, so an author sees
  every real problem in one run. The lint gates skip themselves when the file
  they lint is missing or does not parse (TG101 and TG102 already say so), and
  a warning-level lint failure never blocks.
- **Secrets are never echoed.** TG201 findings carry the path, line, detector
  and only the first four characters, so a report posted to a pull request does
  not republish the credential. Test fixtures build their fake tokens at runtime
  so no token literal sits in this repository; the one planted in the demo is a
  random string with no known format.
- **Baseline means untouched.** TG402 runs the grader with no solution at all.
  pytest exit code 5 (no tests collected) fails the gate too, since a grader
  with no tests passes nothing and rejects nothing.
- **A stub, not only a blank.** TG403 learns which files the reference solution
  creates (a workspace listing before and after `solve.sh`, on either runner)
  and reruns the grader after a generated `solve.sh` that creates exactly those
  files, empty. It catches the grader that fails an untouched workspace but
  never looks inside the output; files the reference edits in place are left
  as they were, so for fix-the-code tasks the stub is a no-op.
- **Content-addressed environments.** The image tag is a SHA-256 over the build
  context (paths, bytes, executable bits, symlink targets; caches excluded) and
  the image carries it as a label, so an unchanged environment is never rebuilt,
  two tasks with identical environments share one image, and the tag is the
  same on every machine.
- **Locked-down runs, the image's own workdir.** Containers get no network, a
  CPU, memory and process cap, no capabilities and a non-root user; the
  solution and tests are streamed in on stdin (no bind mounts, so it also works
  when TaskGate itself runs in a container that talks to a remote daemon) and
  land in a tmpfs, while the workspace is whatever the Dockerfile put in the
  workdir, which is what an agent would see.
- **One contract, two runners.** Docker is used whenever it answers; otherwise
  the local runner runs the same three kinds of run and TG301 falls back to its
  static checks, with a note on stderr and the runner named in every report.
  `--runner docker` turns the fallback into an error for CI that must not
  silently lose isolation. TG401 and TG402 skip when the environment did not
  build, so a broken Dockerfile is reported once, by TG301.
- **Docker-free unit tests of the Docker runner.** A fake `docker` executable on
  PATH (`tests/fake_docker_cli.py`) records every argv, emulates builds and image
  inspection, and runs the real in-container driver script on the host, so the
  flags, the tar stream, the step markers, timeouts and the created-file listing
  are all covered without a daemon. Four opt-in tests (`make test-docker`)
  check the same paths against real Docker, including that a container sees no
  network, a non-root uid, `memory.max` and `pids.max`.
- **Hermetic local runs.** The runner works on copies in a temporary directory,
  writes an empty `pytest.ini` there so the surrounding repository's pytest
  config cannot leak in, drops `PYTEST_*` and coverage variables, sets
  `PYTHONHASHSEED=0`, and runs the grader with TaskGate's own interpreter
  (pytest is a runtime dependency for that reason).
- **Byte-stable reports.** Reports hold no timings (pytest durations are stripped
  from summaries) and no absolute paths, and the demo repository has fixed
  commit metadata; a test checks that two independent builds produce identical
  output.
- **Fresh repository, MIT.** No existing permissively licensed project was small
  and close enough to build on (see [PLAN.md](PLAN.md)).

## Known issues

- The local runner is not a sandbox: `solve.sh` and the grader run as the
  current user with network and filesystem access, and the grader uses
  TaskGate's own Python 3.12 and pytest, so a task that needs other packages only
  passes on the Docker runner.
- On both runners the tests are present (in a separate directory) while
  `solve.sh` runs, so a reference solution could read them; the grader is not
  hidden from the solution.
- The Docker runner needs the image to provide `sh`, `tar`, `find` and
  `python` (or `python3`) with pytest, and a workdir the image's user can write;
  the samples do this in five lines, and a missing piece shows up as a TG401
  failure with the container's output.
- TG301's static checks cannot see a `USER` inherited from the base image, so a
  Dockerfile must set `USER` in its final stage even when the base already does.
- Task images accumulate under `taskgate-env:*`; nothing prunes them
  automatically (`make clean-images` removes them all).
- The TaskGate image has no Docker CLI, so `taskgate check` inside it always
  uses the local runner.
- The diff is taken from committed `HEAD`, while gates read the working tree;
  uncommitted edits are checked only if they sit inside a task the commits
  already touch.
- Gates run sequentially and nothing is cached yet; every run re-executes every
  changed task.
- The high-entropy detector trades recall for precision: it misses about 11% of
  random 24-character tokens and 3% of 32- to 40-character ones (see Measured),
  never flags a token without both letter cases and a digit, and does not look
  inside URLs, so a random token in a query string is found only when it has a
  known format.
- TG103 knows only the keys in the manifest schema; a task format that needs
  extra keys has to disable it or live with warnings until the schema grows.
- Timeouts use POSIX process groups, so the runner does not support Windows.

## Roadmap

Planned in [PLAN.md](PLAN.md). Slices 1 (the gate registry, plugins,
`taskgate.toml` and the static gates) and 2 (the Docker runner, TG301, TG302 and
TG403) are built (see above); the rest is not built yet:

1. Grader determinism over N reruns with shuffled test order and varied seeds
   (TG501), with the flaky tests and the seeds that flip them in the report.
2. A content-hash result cache so unchanged tasks are skipped on re-runs.
3. GitHub reporting: pull-request comment upsert, check-run annotations, JUnit
   XML, a fake GitHub API for tests and the demo, and a composite `action.yml`.

## Development

| Command | What it runs |
| --- | --- |
| `make install` | `uv sync --locked` and the pre-commit hook |
| `make lint` | `ruff check` and `ruff format --check` |
| `make typecheck` | `mypy --strict` on `src/` |
| `make test` / `make cov` | pytest, and pytest with the 90% branch-coverage gate |
| `make demo` | the offline end-to-end demo described above (local runner) |
| `make demo-docker` | the same demo on the Docker runner, then prune this project's dangling images |
| `make test-docker` | the opt-in tests against a real Docker daemon (`TASKGATE_DOCKER_TESTS=1 uv run pytest -m docker`) |
| `make clean-images` | remove the `taskgate-env:*` task images the Docker runner built |
| `make docker` | build the image, run the demo in it, prune this project's dangling images |
| `uv run python examples/secret_survey.py scan DIR` / `recall` / `timing` | the secret-scan false-positive, recall and long-line timing measurements above |

## License

MIT, see [LICENSE](LICENSE).
