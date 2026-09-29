# Task layout

TaskGate reviews benchmark tasks that follow this small layout. A task is a
directory that holds a `task.toml` manifest; it may live anywhere in the
repository (TaskGate's own samples use `tasks/<id>/`).

```
tasks/<id>/
  task.toml          manifest (see below)
  instruction.md     the prompt the agent receives
  environment/       Dockerfile for the agent's container
    Dockerfile
    workspace/       optional: files that seed the working directory
  solution/          reference solution; entry point solve.sh
    solve.sh
  tests/             pytest grader, run after the solution finishes
    test_*.py
```

## Discovery rules (implemented)

`taskgate tasks <root>` and `taskgate.layout.find_tasks` apply these rules:

- a task directory is any directory that contains `task.toml`;
- a complete task also has `instruction.md` (file) and `environment/`,
  `solution/` and `tests/` (directories); anything absent is reported as missing;
- hidden directories (`.git`, `.venv`, ...) and `node_modules`, `__pycache__`
  and `venv` are not scanned, and symlinks are not followed;
- a `task.toml` nested inside another task (for example a grader fixture) does
  not start a second task.

## Runtime contract

The working directory is the task's workspace: `environment.workdir`
(`/workspace`) in the task's image, which the Dockerfile seeds by copying
`environment/workspace/` into it, or a fresh temporary directory seeded from
`environment/workspace/` when the local runner is used.

1. `solution/solve.sh` runs with `sh`, with the workspace as its current
   directory, and writes its output there.
2. The grader then runs `python -m pytest <tests dir>` from the same
   directory. Tests read only the workspace, never the solution.

`solve.sh` and the grader share the `task.timeout_sec` budget. Three kinds of
run use this contract: the reference solution (TG401), no solution at all, so
the grader sees the untouched workspace (TG402), and a stub `solve.sh` that
creates every file the reference solution created, empty, and nothing else
(TG403). "Created" means a regular file that exists in the workspace after the
reference `solve.sh` and did not before it; files it changed in place are left
alone by the stub, and cache files (`__pycache__/`, `*.pyc`) do not count.

### Runners

`taskgate check --runner auto|docker|local` (or `TASKGATE_RUNNER`) picks where
runs happen. `auto`, the default, uses Docker when `docker version` answers and
otherwise the local runner, with a note on stderr; `docker` exits 2 without a
daemon. The report header names the runner that was used.

**Docker runner.** The image is built from `environment/<dockerfile>` with
`environment/` as the build context, tagged `taskgate-env:<16 hex>` from a
SHA-256 over the context (sorted paths, content, executable bits and symlink
targets; caches left out), and labelled `project=taskgate` and
`taskgate.context=<sha256>`. An image whose label matches is reused, so an
unchanged environment is built once. Each run is a `docker run --rm` of that
image with:

- `--network none`, `--cpus 1`, `--memory 1024m --memory-swap 1024m` (no swap on
  top), `--pids-limit 256`, `--cap-drop ALL`, `--security-opt no-new-privileges`;
- the image's `USER`, or `65534:65534` when the image would run as root, and a
  driver that refuses to run anything as uid 0;
- `solution/` (or the stub) and `tests/` streamed in as a tar archive on stdin
  and unpacked into a tmpfs at `/taskgate`, outside the workspace;
- the workdir from `environment.workdir`, as the image provides it.

When the budget runs out the container is killed (`docker kill`) and removed.
The limits come from `[runner]` in `taskgate.toml` (`cpus`, `memory_mb`,
`pids_limit`, `build_timeout_sec`, default 900 s for a build).

**Local runner** (the fallback). It copies `environment/workspace/`, `solution/`
(or the stub) and `tests/` into a fresh temporary directory, so a run never
writes into the task's source tree, and gives pytest an empty config file there
so settings from the surrounding repository do not leak in. On timeout the whole
process group is killed. It is not a sandbox: it has no network or resource
isolation and runs as the current user. The grader runs with TaskGate's own
interpreter, so a task that needs packages beyond Python 3.12 and pytest only
passes on the Docker runner.

## Environment

`environment/Dockerfile` (the name comes from `environment.dockerfile`) must stay
inside `environment/`. Gate TG301 checks it without Docker on either runner:

- every instruction is a known Dockerfile instruction, and `FROM` comes first
  (only `ARG` may precede it);
- every local `COPY`/`ADD` source exists in `environment/` (globs must match
  something; heredocs, variables and URLs are not checked; `../` is refused);
- the final stage sets a `USER` that is not `root` or `0`;
- when `environment/workspace/` exists, some `COPY`/`ADD` copies it (or `.`) in.

On the Docker runner TG301 also builds the image, and a failed build is reported
with the last line of the build log. Gate TG302 lists every image the build
pulls, `FROM` and `COPY --from=`, after substituting global `ARG` defaults, and
requires each to end in `@sha256:<64 hex>`; `scratch` and earlier build stages
need no digest, and a variable with no default counts as unpinned.

## Manifest

```toml
[task]
id = "modular-inverse"   # kebab-case, equal to the directory name
title = "Modular inverses of integer pairs"
difficulty = "easy"      # easy | medium | hard
timeout_sec = 120        # wall-clock budget for solution plus grader

[environment]
dockerfile = "Dockerfile"   # relative to environment/
workdir = "/workspace"
```

Gate TG102 validates the manifest: both tables present, `id` kebab-case and equal
to the directory name, a non-empty `title`, `difficulty` one of the three values,
`timeout_sec` an integer in 1..3600, and `workdir` an absolute path. Every problem
is reported at once.

The keys above are the whole schema. Gate TG103 flags any other table or key as a
likely typo and names the closest known one (`task.timout_sec (did you mean
task.timeout_sec?)`, `[enviroment] (did you mean [environment]?)`); a known key
in the wrong place (`timeout_sec` above `[task]`) is pointed at where it belongs.
Gate TG104 warns when `timeout_sec` is valid but outside the recommended range,
10..1800 s by default (`[manifest]` in `taskgate.toml`).

## Instruction

`instruction.md` is the prompt the agent receives. Gate TG105 fails when it is not
UTF-8 or has no words once what a reader would not see as text is removed:

- HTML comments, including everything after an unclosed `<!--` (CommonMark hides
  it too);
- headings, both ATX (`# Title`) and setext (a paragraph underlined with `===` or
  `---`);
- bare Markdown markup: list bullets and numbers, `>` quote markers, `[ ]` task
  boxes, thematic breaks (`---`, `* * *`), code-fence lines and table rules.

A word is a whitespace-separated token with at least one letter or digit, so a
template left with `# Title`, `<!-- TODO -->` and an empty bullet has no text.
Lines inside a fenced code block do count: they are shown to the agent.

## Hygiene

Three gates check every file in the task; the limits live in `taskgate.toml`.
In diff mode "every file" means every file git tracks in the task at `HEAD`,
whatever its name, so a committed `__pycache__/` file, `*.pyc`, `.DS_Store` or
`.mypy_cache/` entry is checked like any other. With `--all` the directory is
walked on disk instead, and the tool caches a working tree collects
(`__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `*.pyc`,
`.DS_Store`) are left out; the local runner leaves out the same names when it
copies a task, so a run never uses a file those gates did not see. Symlinks are
left out in both modes.

- **TG201** scans every text file for credentials: known token formats (cloud
  access key ids, source-host, chat, payment and `sk-` style API keys, JSON web
  tokens), private key headers, and high-entropy strings (see
  `src/taskgate/secretscan.py` for the exact rule). Messages show only the first
  four characters of a match. A line containing `taskgate: allow-secret` is
  skipped; `[secrets] allow` (regexes on the matched text) and `exclude` (path
  globs) cover the rest. Every pattern runs in time linear in the line length,
  and at most `[files] max_file_bytes` of each file is read (a bigger file
  fails TG202, and the message says it was scanned in part), so one long line or
  one huge file cannot stall the run.
- **TG202** fails a file over `[files] max_file_bytes` (default 1 MiB) or a task
  over `max_task_bytes` (default 10 MiB) in total.
- **TG203** fails a binary file (a NUL byte in its first 8000 bytes, git's rule)
  unless a `[files] binary_allow` glob matches its task-relative path.

Globs are matched with `fnmatch` on task-relative POSIX paths, so `*` also
matches `/` (`tests/data/*` covers every file below `tests/data/`).

## Gates (implemented)

| Code | Name | Blocks | Passes when |
| --- | --- | --- | --- |
| TG101 | layout-complete | yes | the parts above exist, including `solution/solve.sh` and a `tests/test_*.py` (or `*_test.py`) file |
| TG102 | manifest-valid | yes | `task.toml` matches the schema above |
| TG103 | manifest-known-keys | no (warning) | `task.toml` has no tables or keys outside the schema (skipped when it does not parse) |
| TG104 | timeout-in-range | no (warning) | `timeout_sec` is inside the recommended range (skipped when it is invalid) |
| TG105 | instruction-not-empty | yes | `instruction.md` is UTF-8 and has words beyond headings, comments and bare Markdown markup (skipped when missing) |
| TG201 | no-secrets | yes | no text file holds a known token format, a private key or a high-entropy string |
| TG202 | file-size-limits | yes | every file and the whole task are under the size limits |
| TG203 | no-binary-files | yes | every binary file matches a `[files] binary_allow` glob |
| TG301 | environment-builds | yes | the Dockerfile passes the checks under "Environment", and builds on the Docker runner (skipped without `environment/`) |
| TG302 | base-images-pinned | yes | every `FROM` and `COPY --from` image is pinned by sha256 digest (skipped without a Dockerfile) |
| TG401 | solution-passes | yes | `solve.sh` exits 0 and the grader then passes (requires TG101) |
| TG402 | baseline-fails | yes | the grader fails on the untouched workspace, and collects at least one test (requires TG101) |
| TG403 | stub-solution-fails | yes | the grader fails after a stub that creates the reference solution's new files, empty (requires TG401) |

A gate whose requirement did not pass is reported as `skip`, not as a second
failure. TG401 and TG402 also skip when the runner could not build the
environment (TG301 says why). `taskgate gates` prints the current list with each gate's effective
severity; `taskgate.toml` can disable a gate or change its severity.
