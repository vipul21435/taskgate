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
  errors (bad path, bad base ref), 3 for a crash (added 2026-09-30 so a crash
  is never read as a verdict).
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
  - TG105 passed unfilled templates. It now counts words (tokens with a letter or
    digit) after removing comments (an unclosed `<!--` hides the rest, as in
    CommonMark), ATX and setext headings, list/quote/task-box markers and fence
    lines, without a full Markdown parser: a line after a list item that a `---`
    underlines is treated as a setext heading, which can undercount a few words
    but only fails a file with no other text. Comment removal uses `str.find`,
    so repeated unclosed `<!--` is linear. Sample word counts dropped (90 to 88,
    109 to 100) because bullets and list numbers no longer count.

- **Slice 3 decisions (2026-09-30):**
  - TG501 grades one solution output repeatedly (the spec says "on the
    reference solution's output"): `Runner.regrade` runs `solve.sh` once more,
    snapshots the workspace and restores it before every rerun after the first,
    so reruns differ only by their seed and a grader that writes into the
    workspace cannot leak state into the next one. On Docker the reruns share one
    container; the driver keeps a tar of the workdir in the tmpfs and restores it
    with `find -mindepth 1 -delete` plus `tar -x` (one container keeps the cost
    to a single start; the snapshot needs the tmpfs's 256 MB).
  - One seed per rerun drives all three knobs: `s = seed + i - 1` is the
    shuffle seed, `PYTHONHASHSEED` and `TASKGATE_SEED`, with `[determinism]
    seed` (default 1) and `runs` (default 5, 2..100) in `taskgate.toml`. Seeds
    start at 1 because TG401 already ran with `PYTHONHASHSEED=0` in file order.
  - The shuffle plugin (`src/taskgate/shuffle_plugin.py`) is copied into each
    rerun as `taskgate_shuffle.py` and loaded by `-p` through `PYTHONPATH`, on
    both runners, since in a task image TaskGate is not installed. It only uses
    long-stable hooks, shuffles `trylast`, and also seeds `random` in
    `pytest_configure` so a rerun is reproducible from its seed (a grader that
    draws unseeded random numbers still varies between seeds).
  - Outcomes come from JUnit XML with `-o junit_family=xunit1`, whose `file`
    attribute lets the report rebuild pytest's node ids. A test "flips" when its
    outcome set across the usable reruns has more than one member, or is all
    failed/error (it passed TG401). Missing tests count as `not run`. Timed-out
    reruns and reruns without readable XML are reported as rerun problems, not
    as flips of every test.
  - Gate results gained `details` (one line each), rendered in text, Markdown
    (a "Details" list, reproduce commands as code) and JSON, instead of packing
    every flipped test into the one-line message.
  - The reproduce command is a new CLI command, `taskgate grade TASK --seed S`,
    rather than a raw pytest line, because the author needs the solution's
    output first; it uses the task path as reports show it and the runner the
    report used. `TaskContext.label` carries that path to the gate.
  - A fourth demo pull request (`log-levels`) has a grader whose tests share a
    module-level cache; only TG501 blocks it, and its flips are identical on
    macOS, in the image and on the CI runner.

- **Review fixes, Dockerfile reader (2026-09-30):**
  - TG302 now lists the `from=` of every `RUN --mount` (CSV fields, any mount
    type; stage names and stage numbers exempt, like `COPY --from`), since
    BuildKit resolves it like a base image. A `# syntax=` frontend and `ONBUILD`
    pulls are still not listed (README, Known issues).
  - A global `ARG` default is expanded with the `ARG`s before it, in order, as
    Docker does for meta args; a default that uses a variable with no value has
    no value itself, so the existing "a variable in it has no default" rule
    still applies.
  - Lines are split the way Docker's parser does: a leading BOM is dropped and
    only `\n` ends a line (one `\r` before it is dropped); `str.splitlines()`
    broke at form feeds and U+2028. The keyword is split off at
    `[\t\v\f\r ]+` like Docker's `splitCommand`.
  - A logical line with nothing but continuations becomes an instruction with
    an empty keyword: TG301 reports "a line continuation with no instruction
    after it" (Docker rejects the file too), and TG302 still checks the pins
    instead of skipping, so the author sees both problems in one run.

- **Review fixes, runner (2026-09-30):**
  - `execute()` ignores `ESRCH` and `EPERM` from the `killpg` after the timeout
    hook: when the process exits during `docker kill`, its group is gone or holds
    only a zombie, and macOS answers `EPERM`, which used to surface as "cannot
    run docker: [Errno 1]". A run that passed its deadline stays a timeout even
    if it exited during the hook.
  - Tests now pin a task's own `environment.workdir` reaching `docker run
    --workdir` (and the `/workspace` fallback for a relative value), and the
    Docker runner's created-file listing leaving out cache files; both
    mutations from the review now fail the suite.

- **Slice 4 decisions (2026-09-30):**
  - The key is two hashes. `task_digest` covers the task alone: every
    directory, regular file and symlink by sorted relative POSIX path, with file
    bytes and the full permission bits (a superset of "executable bits": a
    read-only input changes what `solve.sh` can do on both runners), symlink
    targets, and never mtimes, owners or walk order. `cache_key` adds the
    TaskGate build, the gates, the effective config, the runner and the task's
    label and directory name.
  - "TaskGate version" is `version+<12 hex of its own *.py files>`: the version
    string stays 0.1.0 across commits, so a version-only key would replay
    results of older code in a working checkout. Plugin gates add a digest of
    their module's source for the same reason.
  - Ignored files are the ones `files.is_ignored` names, now including
    `.taskgate/` itself (a task at the repository root would otherwise hash its
    own cache). In diff mode the tracked path list is hashed as well, with the
    content of tracked files whatever their names, because the hygiene gates
    read exactly that list; so diff mode and `--all` never share an entry.
  - The runner's name, Python, pytest and platform are in the key, since the
    local runner grades with TaskGate's own interpreter; the Docker image is
    already covered by the task content (its Dockerfile and pinned bases).
  - Only results with no blocking failure are stored: a failing task is
    re-checked every run, so a transient failure (Docker hiccup, timeout under
    load) is never replayed, and a failing pull request has to change anyway.
  - A symlink that leaves the task (or loops) makes the task uncacheable, with a
    note on stderr, instead of hashing content outside it.
  - Location: `.taskgate/cache` under the repository root (diff mode) or the
    `--all` directory, `--cache-dir` / `TASKGATE_CACHE_DIR` to move it. The
    directory gets a `.gitignore` of `*` and a `CACHEDIR.TAG`, as pytest does,
    so `git status` stays clean in a checked repository.
  - Entries are one JSON file per key, written with mkstemp + fsync +
    `os.replace` under an exclusive `flock` on `<cache>/lock` (10 s timeout);
    lookups take no lock. `stats.json` sums hits, misses, stores and
    not-stored across runs under the same lock. A hit bumps the entry's mtime,
    which `prune` reads as last use.
  - `prune` by default removes entries of another build or cache format,
    unreadable ones and all but the most recently used entry per (task, runner);
    `--older-than DAYS`, `--all` and `--dry-run` extend it.
  - Any cache failure (unwritable directory, lock timeout) is a stderr note and
    turns the cache off for the rest of that run; it never changes the exit
    code. The tests point `TASKGATE_CACHE_DIR` at each test's temp directory so
    no test writes into the source tree.

- **Review fixes, TG501 restore and JUnit cap (2026-09-30):**
  - The Docker driver's tar snapshot failed on a root-owned `mkdir -m 777`
    workdir (tar could not reset `.`'s times and mode) and lost mode bits and
    sub-second mtimes (no `-p`, gnu format); the local runner's `copytree` split
    hard links, could not copy named pipes or `chmod 000` files, and put absolute
    temp paths into messages. Both runners now use one stdlib module,
    `snapshot.py` (Python 3.8+, copied into the container as
    `taskgate_snapshot.py`, the local runner imports it). It records every entry
    with its inode and change time and copies file contents once per inode;
    restore recreates only entries whose inode or ctime moved (ctime cannot be set
    by hand, so an unchanged ctime proves an untouched inode) and never replaces
    the workdir itself. Untouched entries keep their inode and owner, which is
    closer to rerun 1 than any copy. Metadata a non-owner cannot set is skipped
    only for entries the run user does not own; errors name workspace-relative
    paths. A directory the run user can neither list nor `chmod` is a save error
    (known issue), since its content could not be checked.
  - `snapshot.py` uses `os` functions, with a per-file ruff `PTH` exemption,
    because `Path.readlink` and `Path.hardlink_to` do not exist on Python 3.8.
  - The 8 MiB JUnit cap now holds on Docker too: the driver prints the file only
    when `wc -c` says it fits, else marks `junit-over-limit`, and both runners
    carry `GraderRun.junit_problem`, so TG501's detail says "JUnit XML over the
    8 MiB limit (not read)" instead of "no JUnit XML". The message never states
    the size, since JUnit durations make it vary between runs.
  - `taskgate grade` checks for `solution/solve.sh` and `tests/` and exits 2
    (usage error) instead of a traceback.

- **Slice 5 decisions (2026-09-30):**
  - The GitHub client is `urllib` only (no HTTP dependency): three operations,
    each in the fewest calls the API allows. The pull-request file list follows
    the `Link` header at 100 per page and first reads the pull request's
    `changed_files`, so a list the API truncated at 3000 files is an error
    rather than a silently partial check. Pagination links to another host are
    refused and the token is sent only to `TASKGATE_GITHUB_API`.
  - One summary comment per pull request, found by a hidden HTML marker at the
    start of its body: updated when the Markdown changed, left alone when not
    (no write), created otherwise. Byte-stable reports make "unchanged" a plain
    string comparison. A check run is created per `publish` (GitHub shows the
    latest per name and commit); its summary is the Markdown report and its
    annotations go 50 per request (create, then update calls), which the fake
    enforces with a 422 like GitHub does.
  - Annotations land on line 1 of each task's `task.toml` for every gate:
    results carry no locations, and inventing them from message text would be
    guesswork. Listed as a known issue and the first roadmap item.
  - `publish` is a separate command from `check` (reads `report.json`, needs
    the token), so the action can check without a token and a report can be
    posted again later. `report.json` therefore reads back into the same
    `CheckReport` (`from_json`, round trip tested), and `taskgate report`
    renders a saved report in any format, which is how the action prints
    annotations. `--out` now also writes `junit.xml` (no `time` attributes,
    blocking failures as `failure`, warnings in `system-out`).
  - `check --pr N` replaces `git diff` with the pull request's file list (both
    sides of a rename) and reports `files of pull request #N` in the header
    and `pull_request` in JSON; task roots and the merge base still come from
    git, so it does not remove the need for history. Kept optional (`pr-files`
    off by default in the action).
  - The fake (`src/taskgate/fakegithub.py`) is part of the package, stdlib
    only and Python 3.9+, so `make demo` in the image and a CI step with the
    system Python can start it (`python -m taskgate.fakegithub --port N`,
    `GET /_fake/state` for inspection). It serves from a thread on
    `127.0.0.1:0` in tests; `html_url` values use a fixed host so demo output
    does not depend on the port. An `RLock` guards its state because the
    handler calls `snapshot()` under the lock.
  - The composite action installs from the lock file (`uv sync --locked
    --no-dev --project $GITHUB_ACTION_PATH`) and passes every input through
    `env:` rather than interpolating `${{ inputs.* }}` into scripts. The check
    step turns exit 1 into outputs (usage errors still fail it) and a final
    step fails the job unless `fail-on-blocking: "false"`. A test parses
    `action.yml` and runs its `run:` steps in bash against the fake, so the
    scripts are checked before CI; the CI `action` job runs `uses: ./` twice
    on the sample tasks against a fake started in the job and asserts one
    comment (updated, not duplicated), two check runs and the annotation path.
  - Docker-runner and local-runner demo reports plus the `--pr 4` run are 15
    files, byte-identical between macOS and the image.

- **Review fixes, recovered from an interrupted agent (2026-09-30):** a
  resumed run found uncommitted review fixes in the working tree. They were
  checked (lint, mypy, the full suite) and committed as four units rather than
  stashed, because each was complete and tested:
  - `changes.py`: a non-UTF-8 path in git's output is a `GitError` (exit 2),
    not an uncaught `UnicodeDecodeError`.
  - `dockerfile.py`: flag words are read like Docker's `extractBuilderFlags`
    (quotes and backslash escapes removed, `--` ends the flags), so TG302 sees
    a quoted `RUN --mount="...,from=x"` or `COPY --from="x"`.
  - `github.py`: redirects are refused (urllib would forward the token to the
    `Location` host), and check-run summaries are cut at 65535 UTF-8 bytes on
    a character boundary; the fake answers 422 to both oversize bodies.
  - `cache.py` (cache format 2): a replay must carry exactly the enabled
    gates' codes; a cache directory the checked repository tracks is not used;
    `stats` and `prune` act only on a directory with TaskGate's `CACHEDIR.TAG`
    and only on key-named regular files in a real `entries/` directory, and a
    dry run writes nothing, not even the lock file; plugin gates add their
    entry point, distribution version, object parameters and one hop of
    imported source and constants to the key. The resuming run added the tests
    that bring `cache.py` back to 100% branch coverage, routed both git calls
    through one helper, and fixed a `ValueError` when the cache path reaches
    the work tree through a symlink (`/var` versus `/private/var`).
  - The older stash `wip from interrupted agent` (slice 1 static gates) is
    superseded by the committed gates and is kept only because stashes are
    never dropped by these runs.

- **Refresh (2026-09-30):** `make lint`, `make typecheck` and `make cov`
  green (544 passed, 9 skipped, 100% line and branch coverage of 3913
  statements and 1076 branches); `make test-docker` 8 passed; the README
  quickstart re-run from a fresh clone (`make demo` exits 0 in 9.53 to 9.93 s;
  `check --all examples/sample-repo` exits 1 on the draft task as documented);
  CI green on the pushed head. README and delivery numbers updated to these
  runs. The slice-1 stash `wip from interrupted agent` was compared with the
  committed `config.py` and `manifest.py` once more, found fully superseded,
  and dropped (the refresh task allows dropping a stash that is not useful).

- **Review fixes, composite action (2026-09-30):** two findings from the late
  slice-5 review, re-confirmed on `4534bc8`, fixed with regression tests that
  fail on that commit.
  - A crash exited 1 and the action took any code up to 1 as a finished
    check, then published whatever `report.json` sat in `out` (stale, or
    committed by the pull request) and echoed its raw `blocking_failures`
    into `GITHUB_OUTPUT`. Now `main()` maps an unexpected exception to exit
    3; `check --out` writes each report to a new file and renames it over the
    name (a committed symlink is replaced, not followed; an unwritable
    directory is exit 2); the action deletes the three reports before the
    check and reads the count from the report parsed by TaskGate, which must
    agree with the exit code. Decision: the default `out` stays
    `.taskgate/out` (changing it would break workflows that upload it); the
    remaining case, a pull request that commits the directory itself as a
    symlink, is under Known issues.
  - A fork's `pull_request` run has a read-only token, so publish's 403 failed
    the job. Decision: publish is still attempted (a private repository can
    send write tokens to forks), and exit 1 on a fork's `pull_request` event
    becomes a warning; `fail-on-blocking` then decides the job. Other
    read-only tokens (Dependabot) still fail the step, under Known issues.
  - `make check`: 554 passed, 9 skipped, 100% line and branch coverage of
    3936 statements and 1076 branches.

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
- [x] **3. Grader determinism.** Done 2026-09-30: 14 built-in gates, 389 tests
  (plus 5 opt-in real-Docker tests, green locally and in CI), 100% branch
  coverage; `taskgate grade` reproduces a rerun. The determinism gate (TG501)
  reruns the grader N times (default 5, `[determinism] runs` in `taskgate.toml`) on the reference
  solution's output, each run with a seeded shuffle of test order (a small pytest
  plugin bundled with TaskGate), a different `PYTHONHASHSEED` and a different
  `TASKGATE_SEED`, and compares the verdict and every per-test outcome (read from
  pytest's JUnit XML) across runs. A difference fails the gate and the report
  lists each flaky test with the runs and seeds where it flipped, so the author
  can reproduce it with one command. Tests use fixture tasks with a stable
  grader, an order-dependent grader and a hash-seed-dependent grader.
- [x] **4. Content-hash result cache.** Done 2026-09-30: 463 tests (plus 5
  opt-in real-Docker tests, green locally and in CI), 100% branch coverage; a cached
  check of one task takes 0.18 s against 1.13 to 1.18 s (local runner) and
  0.21 s against 1.89 to 1.95 s (Docker runner). A canonical task hash (sorted relative
  POSIX paths, file bytes and executable bits, ignored files excluded, plus the
  TaskGate version and gate config) keys results stored under `.taskgate/cache`
  with atomic writes and a lock file; unchanged tasks are skipped and reported as
  cached; `--no-cache`, `taskgate cache stats` and `taskgate cache prune`. Tests
  prove the hash ignores mtimes and walk order and changes on any byte or mode
  change.
- [x] **5. GitHub reporting, fake API and composite action.** Done 2026-09-30:
  524 tests (plus 8 opt-in real-Docker tests), 100% branch coverage; `make demo`
  8.08 to 8.38 s including the GitHub part. A stdlib GitHub
  client (base URL from `TASKGATE_GITHUB_API`, token from `GITHUB_TOKEN`) that
  lists pull-request files, upserts a single summary comment by hidden marker and
  creates a check run with annotations in batches of 50; a JUnit XML report and
  workflow-command annotations; an in-process fake GitHub API that records
  requests, used by the tests and `make demo`; a composite `action.yml` and a CI
  job that runs TaskGate on its own sample tasks through `uses: ./`.
