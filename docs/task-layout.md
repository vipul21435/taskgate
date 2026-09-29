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

1. `solution/solve.sh` runs with the workspace as its current directory and
   writes its output there.
2. The grader then runs `python -m pytest <tests dir>` from the same
   directory. Tests read only the workspace, never the solution.

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

Today TaskGate checks only that the manifest and the other parts exist. Manifest
schema validation and running the runtime contract are the core deliverable and
the first slices in [PLAN.md](../PLAN.md).
