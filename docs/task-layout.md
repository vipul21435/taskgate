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

The working directory is the task's workspace: `/workspace` in the container
(seeded from `environment/workspace/`), or a fresh temporary directory seeded
the same way when Docker is not available.

1. `solution/solve.sh` runs with `sh`, with the workspace as its current
   directory, and writes its output there.
2. The grader then runs `python -m pytest <tests dir>` from the same
   directory. Tests read only the workspace, never the solution.

The local runner (implemented) copies `environment/workspace/`, `solution/` and
`tests/` into a fresh temporary directory, so a run never writes into the task's
source tree, and gives pytest an empty config file there so settings from the
surrounding repository do not leak in. `solve.sh` and the grader share the
`task.timeout_sec` budget; on timeout the whole process group is killed. The
baseline run (TG402) skips step 1 and runs the grader on the untouched workspace.

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
UTF-8 or has no text once HTML comments and Markdown headings are removed (a
template left with only `# Title` and `<!-- TODO -->` is empty).

## Gates (implemented)

| Code | Name | Blocks | Passes when |
| --- | --- | --- | --- |
| TG101 | layout-complete | yes | the parts above exist, including `solution/solve.sh` and a `tests/test_*.py` (or `*_test.py`) file |
| TG102 | manifest-valid | yes | `task.toml` matches the schema above |
| TG103 | manifest-known-keys | no (warning) | `task.toml` has no tables or keys outside the schema (skipped when it does not parse) |
| TG104 | timeout-in-range | no (warning) | `timeout_sec` is inside the recommended range (skipped when it is invalid) |
| TG105 | instruction-not-empty | yes | `instruction.md` is UTF-8 and has text beyond headings and comments (skipped when missing) |
| TG401 | solution-passes | yes | `solve.sh` exits 0 and the grader then passes (requires TG101) |
| TG402 | baseline-fails | yes | the grader fails on the untouched workspace, and collects at least one test (requires TG101) |

A gate whose requirement did not pass is reported as `skip`, not as a second
failure. `taskgate gates` prints the current list with each gate's effective
severity; `taskgate.toml` can disable a gate or change its severity.
