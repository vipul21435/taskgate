# TaskGate

[![CI](https://github.com/vipul21435/taskgate/actions/workflows/ci.yml/badge.svg)](https://github.com/vipul21435/taskgate/actions/workflows/ci.yml)

Review gates for pull requests that add or change benchmark tasks for AI coding
agents. TaskGate finds the task directories a pull request touches, runs each
one through a set of gates with stable codes (layout, manifest schema and lint,
secrets, file sizes and binaries, the environment builds from a digest-pinned
base, the reference solution must pass, an untouched workspace and an
empty-output stub must fail, and the grader must give the same per-test
outcomes when rerun with a shuffled test order and new seeds), and reports the
result as terminal text, a Markdown pull-request summary and JSON, with a
non-zero exit when a blocking gate fails. Solutions and graders run in the task's own Docker image with no
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
- **Fourteen gates**, run in code order; a gate whose prerequisite did not pass,
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
  | TG501 | grader-deterministic | error | on 5 reruns of the grader (`[determinism] runs`) over the reference solution's output, each with a seeded shuffle of the test order, its own `PYTHONHASHSEED` and `TASKGATE_SEED`, every rerun passes and every test has the same outcome (needs TG401) |

  Only `error` failures block. The full task layout, runtime contract and gate
  rules are in [docs/task-layout.md](docs/task-layout.md).
- **Grader determinism (TG501).** The reference solution runs once more, then
  the grader runs 5 times on identical copies of its output (the workspace is
  restored before each rerun). Rerun `i` uses seed `i` (`[determinism] seed`
  moves the start) three ways: a pytest plugin TaskGate bundles
  (`shuffle_plugin.py`, loaded as `-p taskgate_shuffle --taskgate-seed i`)
  shuffles the collected tests with `random.Random(i)` and seeds `random`;
  `PYTHONHASHSEED=i` changes set and hash ordering; `TASKGATE_SEED=i` is there
  for graders that draw random cases. Each rerun writes pytest's JUnit XML, and
  the gate compares the exit code and every test's outcome. It fails when a
  rerun fails, when a test's outcome differs between reruns, or when a test
  fails in every rerun although TG401's run (file order, `PYTHONHASHSEED=0`)
  passed, and lists each flipped test with the runs and seeds of each outcome
  and one command that repeats the first failing rerun.
- **`taskgate grade TASK --seed S`** is that command: it runs the reference
  solution, then the grader exactly as TG501's rerun with seed `S` did, and
  prints pytest's output and each test's outcome in the order the tests ran
  (exit 0 when the grader passes, 1 when it fails).
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
  a git repository with a `main` branch and four pull-request branches: one
  good task and three flawed ones. Commits use fixed authors and dates, so the
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

  [determinism]               # TG501
  runs = 5                    # 2..100 grader reruns
  seed = 1                    # rerun i uses seed + i - 1
  ```

  Unknown sections, keys, gate codes, bad values and invalid regexes are errors
  (exit 2), all listed at once. In diff mode the file is read from the base ref
  (`git show main:taskgate.toml`), so a pull request cannot relax the gates that
  check it; `--config PATH` overrides that. Reports name the config source, every
  override and every option that differs from its default (`config: taskgate.toml;
  options manifest.max_timeout_sec=60, files.max_file_bytes=1024`).
- **Report details.** A gate can attach detail lines to its result: the text
  report prints them under the gate's line, `report.md` lists them under
  "Details" with each reproduce command as code, and `report.json` carries them
  as a `details` list.
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
passes `--runner local` through `TASKGATE_RUNNER`) and took 5.34 to 6.28 s over
three runs (`time make demo`, 8 GB M-series Mac; 1.95 to 2.00 s before TG501
added five grader reruns per task). The last command exits 1 on purpose: the
bundled draft task is incomplete. With Docker running, `make demo-docker` runs
the same four pull requests on the Docker runner (8.4 to 8.7 s with the task
images already built).

## Usage

```
taskgate check [REPO] [--base REF] [--all] [--out DIR] [--format text|markdown|json] [--config FILE]
               [--runner auto|docker|local]
taskgate grade TASK --seed N [--runner auto|docker|local] [--config FILE]
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
  pass  TG501  grader-deterministic   the grader passed all 5 reruns with identical per-test outcomes (3 tests, seeds 1-5, shuffled order)
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
  pass  TG501  grader-deterministic   the grader passed all 5 reruns with identical per-test outcomes (2 tests, seeds 1-5, shuffled order)
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
  pass  TG501  grader-deterministic   the grader passed all 5 reruns with identical per-test outcomes (2 tests, seeds 1-5, shuffled order)
result: FAIL, 2 blocking failures
```

The fourth branch adds a task whose grader parses the output in its first test
and caches it in a module-level dict that the other two tests read. Every other
gate passes, since pytest runs tests in file order; TG501 shuffles the order,
and the two tests that read the cache fail whenever they run before the one
that fills it. This run used the Docker runner too:

```
taskgate 0.1.0: diff against main (merge base b26918e), 1 changed task, docker runner
tasks/log-levels  added  FAIL
  ...
  pass  TG401  solution-passes        reference solution passes the grader (3 passed)
  pass  TG402  baseline-fails         an untouched workspace fails the grader (3 failed)
  pass  TG403  stub-solution-fails    a stub that writes output/counts.txt empty fails the grader (2 failed, 1 passed)
  FAIL  TG501  grader-deterministic   4 of 5 reruns failed; 2 tests flipped: tests/test_outputs.py::test_counts_match, tests/test_outputs.py::test_one_line_per_level
        - tests/test_outputs.py::test_counts_match: failed in runs 1, 2, 3, 4 (seeds 1, 2, 3, 4); passed in run 5 (seed 5); reproduce: taskgate grade tasks/log-levels --seed 1 --runner docker
        - tests/test_outputs.py::test_one_line_per_level: failed in runs 1, 2, 3, 4 (seeds 1, 2, 3, 4); passed in run 5 (seed 5); reproduce: taskgate grade tasks/log-levels --seed 1 --runner docker
        fix: Make each test independent of test order, hash order and chance: no state shared between tests, sort sets and dict keys before comparing or printing, and seed any random generator from TASKGATE_SEED. Run the reproduce command from the directory the task paths are relative to.
result: FAIL, 1 blocking failure
```

The reproduce command, run from the sample repository's root on that branch,
shows the failing order (the two cache readers ran before the test that fills
the cache):

```
$ taskgate grade tasks/log-levels --seed 1 --runner local
taskgate grade tasks/log-levels: reference solution, then the grader with seed 1 (shuffled order, PYTHONHASHSEED=1, TASKGATE_SEED=1), local runner
FF.                                                                      [100%]
...
E       AssertionError: assert {} == {'DEBUG': 2, ... 6, 'WARN': 2}
...
2 failed, 1 passed in 0.02s
failed   tests/test_outputs.py::test_counts_match
failed   tests/test_outputs.py::test_one_line_per_level
passed   tests/test_outputs.py::test_output_parses
result: FAIL (pytest exited 1)
```

With `--seed 5` the same command prints the three tests in file order and
`result: PASS`.

The `report.md` written for that branch on the local runner, ready to post as a
pull-request comment:

```markdown
## TaskGate: FAIL

1 blocking failure; diff against main (merge base b26918e), 1 changed task, local runner.

| Task | Change | Verdict | Blocking gates |
| --- | --- | --- | --- |
| `tasks/log-levels` | added | **fail** | TG501 |

### `tasks/log-levels`

| Gate | Status | Message |
| --- | --- | --- |
| TG101 layout-complete | pass | layout complete |
| TG102 manifest-valid | pass | manifest valid |
| TG103 manifest-known-keys | pass | no unknown keys |
| TG104 timeout-in-range | pass | task.timeout_sec 60 is within 10..1800 |
| TG105 instruction-not-empty | pass | instruction.md has 96 words |
| TG201 no-secrets | pass | no secrets in 6 text files |
| TG202 file-size-limits | pass | 6 files, 3.1 KiB in total |
| TG203 no-binary-files | pass | no binary files |
| TG301 environment-builds | pass | Dockerfile passes the static checks; not built: the local runner does not use Docker |
| TG302 base-images-pinned | pass | 1 image pinned by sha256 digest |
| TG401 solution-passes | pass | reference solution passes the grader (3 passed) |
| TG402 baseline-fails | pass | an untouched workspace fails the grader (3 failed) |
| TG403 stub-solution-fails | pass | a stub that writes output/counts.txt empty fails the grader (2 failed, 1 passed) |
| TG501 grader-deterministic | **fail** (error) | 4 of 5 reruns failed; 2 tests flipped: tests/test_outputs.py::test_counts_match, tests/test_outputs.py::test_one_line_per_level |

Details:

- **TG501**: tests/test_outputs.py::test_counts_match: failed in runs 1, 2, 3, 4 (seeds 1, 2, 3, 4); passed in run 5 (seed 5); reproduce: `taskgate grade tasks/log-levels --seed 1 --runner local`
- **TG501**: tests/test_outputs.py::test_one_line_per_level: failed in runs 1, 2, 3, 4 (seeds 1, 2, 3, 4); passed in run 5 (seed 5); reproduce: `taskgate grade tasks/log-levels --seed 1 --runner local`

How to fix:

- **TG501**: Make each test independent of test order, hash order and chance: no state shared between tests, sort sets and dict keys before comparing or printing, and seed any random generator from TASKGATE_SEED. Run the reproduce command from the directory the task paths are relative to.

<sub>taskgate 0.1.0</sub>
```

A copy of the accepted sample task with a broken environment (no `USER`, and a
`RUN pip install` step that exits 1), checked with
`taskgate check --all DIR --runner docker` (the lines that did not pass). The
static problem and the build failure are reported together, and the runtime
gates skip instead of failing a second time:

```
taskgate 0.1.0: all tasks, 1 task, docker runner
tasks/modular-inverse  FAIL
  FAIL  TG301  environment-builds     the final stage has no USER instruction, so the image runs as root; docker build failed (exit 1): ERROR: failed to build: failed to solve: process "/bin/sh -c pip install --no-cache-dir pytest==9.1.2 && useradd --create-home --uid 1000 agent && mkdir /workspace && chown agent /workspace" did not complete successfully: exit code: 1
        fix: Make docker build environment/ succeed: every COPY source must exist in environment/, environment/workspace/ must be copied into the image, and the final stage needs a non-root USER.
  skip  TG401  solution-passes        skipped: the environment did not build (see TG301)
  skip  TG402  baseline-fails         skipped: the environment did not build (see TG301)
  skip  TG403  stub-solution-fails    skipped: requires TG401 to pass
  skip  TG501  grader-deterministic   skipped: requires TG401 to pass
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
  TG105  error    instruction-not-empty  instruction.md is UTF-8 and has words beyond headings, comments and bare markup
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
TG5xx  determinism
  TG501  error    grader-deterministic   the grader gives the same verdict and per-test outcomes on N reruns with shuffled test order and new seeds
14 gates: 14 built-in, 0 from plugins
```

`report.json` carries the same data plus the runner, the full merge-base hash,
each task's changed files, and a `blocking` flag and a `details` list per gate.
CI appends the demo reports of both runners to the job summaries.

## Architecture

```mermaid
flowchart LR
    PR["checked-out PR branch"] --> CH["changes.py<br/>git merge-base + diff,<br/>ls-tree task roots"]
    CH --> TASKS["changed tasks<br/>added / modified / removed"]
    TASKS --> GATES["gates/<br/>core, lint, hygiene,<br/>environment<br/>(requires -> skip)"]
    GATES --> MAN["manifest.py<br/>schema, unknown keys"]
    GATES --> SEC["secretscan.py<br/>formats, keys, entropy"]
    GATES --> DF["dockerfile.py<br/>parse, static checks,<br/>pulled images"]
    GATES --> DET["determinism.py<br/>JUnit outcomes, flips"]
    GATES --> RUN["Runner: build + run<br/>reference / none / stub,<br/>regrade with seeds"]
    RUN --> SHUF["shuffle_plugin.py<br/>seeded test order"]
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
| `runner.py` | the `Runner` protocol (`build`, `run` with a reference, no or stub solution, `regrade` with seeds), `RunResult`, `Regrade`, the local subprocess runner |
| `docker_runner.py` | the Docker runner (content-derived tags, locked-down `docker run`, in-container driver with a regrade mode), `docker_status` and the auto/docker/local choice |
| `shuffle_plugin.py` | the pytest plugin copied into every TG501 rerun: seeded test order and a seeded `random` |
| `determinism.py` | JUnit XML to pytest test ids and outcomes, flipped tests and how to describe them |
| `dockerfile.py` | Dockerfile parsing, the static checks behind TG301, the pulled-image list behind TG302 |
| `files.py` | which task paths are content (caches and `.DS_Store` are not) and a sorted walk |
| `gates/` | the `Gate` protocol, `TaskContext` and `@gate` (`base.py`); layout, manifest and runtime gates (`core.py`); manifest and instruction lint (`lint.py`); secrets, sizes and binaries (`hygiene.py`); build and digest pins (`environment.py`); grader determinism (`determinism.py`) |
| `secretscan.py` | the credential detectors behind TG201, with redacted findings |
| `config.py` | `taskgate.toml` parsing and validation (every problem at once): gates, `[manifest]`, `[secrets]`, `[files]`, `[runner]`, `[determinism]` |
| `registry.py` | built-in plus entry-point gates, validated and sorted by code |
| `engine.py` | `run_gates`: runs gates in code order, skips unmet `requires`, contains gate crashes |
| `report.py` | pure renderers from a `CheckReport` to text, Markdown and JSON |
| `cli.py` | Typer commands `check` (with `--runner`), `grade`, `gates`, `tasks`, `version` |

## Measured

| What | Command | Result |
| --- | --- | --- |
| Tests and coverage | `make cov` | 388 passed, 5 skipped (the opt-in real-Docker tests); 100% line and branch coverage of `src/` (2415 statements, 666 branches); gate is 90% |
| Real-Docker tests | `time make test-docker` | 5 passed in 10.1 to 10.4 s (two runs, task images already built), including TG501 in a real container finding the same flips as the local runner; also green on the GitHub Actions runner (13.1 s) |
| Demo wall time, local runner | `time make demo` | 5.34 to 6.28 s over three runs (four pull requests; 1.95 to 2.00 s for three before TG501) |
| Demo wall time, Docker runner | `time make demo-docker` | 8.44 to 8.68 s over two runs with the four task images built; 10.84 s on the run that built the new log-levels image |
| Demo in the TaskGate image | `time docker run --rm --entrypoint sh taskgate:local examples/demo.sh /tmp/taskgate-demo` | 6.56 to 6.91 s over three runs |
| TG501 cost | `time taskgate check --all examples/sample-repo --runner local`, with and without `[gates] disable = ["TG501"]` | 1.08 to 1.21 s with it, 0.50 to 0.56 s without (three runs each; one complete task, five grader reruns) |
| Image sizes | `docker image ls taskgate`, `docker image ls taskgate-env` | TaskGate image 473 MB (python:3.12-slim plus git); each sample task image 235 MB |
| Reports, macOS vs image | `cmp` of each demo `report.md` and `report.json` (four pull requests) | all eight byte-identical, TG501's shuffled reruns included |
| Image tags, macOS vs CI | TG301 messages of `make demo-docker` locally and in the CI log | the same four tags (`taskgate-env:8c498462f77c4f11`, `...13e3d22e858b2e52`, `...d98b9b87a032cdee`, `...4c55c370265970c9`), and the same TG501 flips |
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
- **Determinism is checked against identical inputs.** TG501 grades one
  solution output five times rather than rerunning the solution, so a flip is
  the grader's alone, and every rerun starts from a fresh copy of that output
  (the Docker driver keeps a tar of the workdir in its tmpfs), so a grader that
  writes into the workspace cannot change a later rerun. A rerun is fully
  determined by its seed: the plugin shuffles with `random.Random(seed)` and
  seeds `random` before collection, which is why `taskgate grade --seed S`
  reproduces it, and why the demo's flips are the same on macOS, in the image
  and on the CI runner.
- **"Flipped" includes "always fails".** TG401 already ran the grader once in
  file order with `PYTHONHASHSEED=0` and it passed, so a test that fails in all
  five reruns (a grader that iterates a set the solution also iterated, say)
  has flipped against that run and is reported the same way.
- **Test ids from JUnit XML.** Outcomes come from pytest's own JUnit report
  (`xunit1`, which keeps the file), not from parsing console output, and the
  ids are rebuilt as `tests/test_x.py::Class::test_y[param]`, the form pytest
  accepts on its command line.
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
- TG501 roughly doubles the time of the runtime gates on a small task (1.1 s
  against 0.5 s on the sample repository) because it adds a solution run and
  five grader runs; on a slow grader the cost grows with it. The solution run
  and the reruns share `(runs + 1) x timeout_sec`.
- TG501 catches order, hash-seed and seeded-randomness dependence that shows up
  within its reruns. A flake that five runs do not hit (a race, a timing
  threshold, a one-in-a-hundred order) passes; more runs (`[determinism] runs`,
  up to 100) lower that chance but never remove it. The plugin seeds Python's
  `random` only, not NumPy or other generators.
- The shuffle permutes individual tests, so module- and class-scoped fixtures
  are set up more often than in file order; a grader that is only correct under
  its file's fixture order is reported as flaky, which is the point, but the
  first failure can be a setup error rather than an assertion.
- JUnit XML over 8 MiB is not read; such a rerun is reported as leaving no
  readable JUnit XML.
- On the Docker runner the workdir is restored between reruns by deleting its
  contents and unpacking the saved tar as the run user, so the image must
  provide `find -mindepth -delete`, and files the image made read-only to that
  user cannot be restored (the gate then fails with "could not restore the
  workspace").
- `taskgate grade` takes the task path as reports print it, relative to the
  repository root in diff mode, so it must be run from that directory.
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
`taskgate.toml` and the static gates), 2 (the Docker runner, TG301, TG302 and
TG403) and 3 (grader determinism, TG501, and `taskgate grade`) are built (see
above); the rest is not built yet:

1. A content-hash result cache so unchanged tasks are skipped on re-runs.
2. GitHub reporting: pull-request comment upsert, check-run annotations, JUnit
   XML, a fake GitHub API for tests and the demo, and a composite `action.yml`.

## Development

| Command | What it runs |
| --- | --- |
| `make install` | `uv sync --locked` and the pre-commit hook |
| `make lint` | `ruff check` and `ruff format --check` |
| `make typecheck` | `mypy --strict` on `src/` |
| `make test` / `make cov` | pytest, and pytest with the 90% branch-coverage gate |
| `make demo` | the offline end-to-end demo described above (local runner, four pull requests) |
| `make demo-docker` | the same demo on the Docker runner, then prune this project's dangling images |
| `make test-docker` | the opt-in tests against a real Docker daemon (`TASKGATE_DOCKER_TESTS=1 uv run pytest -m docker`) |
| `make clean-images` | remove the `taskgate-env:*` task images the Docker runner built |
| `make docker` | build the image, run the demo in it, prune this project's dangling images |
| `uv run python examples/secret_survey.py scan DIR` / `recall` / `timing` | the secret-scan false-positive, recall and long-line timing measurements above |

## License

MIT, see [LICENSE](LICENSE).
