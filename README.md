# TaskGate

[![CI](https://github.com/vipul21435/taskgate/actions/workflows/ci.yml/badge.svg)](https://github.com/vipul21435/taskgate/actions/workflows/ci.yml)

Review gates for pull requests that add or change benchmark tasks for AI coding
agents, shipped as a CLI (and, per the plan, a composite GitHub Action).

**Status: scaffold.** Today TaskGate discovers task directories and reports which
parts of the [task layout](docs/task-layout.md) each one lacks. The gates
themselves (reference solution passes, empty baseline fails, determinism, cheat
probes, caching, pull-request reporting) are planned in [PLAN.md](PLAN.md) and
are not built yet.

## What works today

- `taskgate tasks [ROOT]` finds every directory holding a `task.toml` and lists
  the required parts it lacks (`instruction.md`, `environment/`, `solution/`,
  `tests/`). `--json` prints machine-readable output; `--strict` exits 1 when any
  task is incomplete.
- A sample repository in `examples/sample-repo` with one complete task
  (modular inverses, with a byte-exact pytest grader) and one draft task.
- A digest-pinned Docker image that runs the CLI as a non-root user.

## Quickstart

```sh
git clone https://github.com/vipul21435/taskgate && cd taskgate
uv sync --locked
make demo
```

`uv run taskgate tasks examples/sample-repo` prints:

```
ok       tasks/modular-inverse
missing  tasks/word-count-draft  (environment/, solution/, tests/)
2 task(s), 1 complete
```

## Development

| Command | What it runs |
| --- | --- |
| `make install` | `uv sync --locked` and the pre-commit hook |
| `make lint` | `ruff check` and `ruff format --check` |
| `make typecheck` | `mypy --strict` on `src/` |
| `make test` / `make cov` | pytest, and pytest with the 90% branch-coverage gate |
| `make demo` | the CLI on the bundled sample repository (offline) |
| `make docker` | build the image, run the demo in it, prune this project's dangling images |

Measured with `make cov` at the scaffold commit: 18 tests pass, 100% branch
coverage of `src/`.

## Known issues

- Only the presence of layout parts is checked; `task.toml` is not parsed or
  validated yet.

## Roadmap

See [PLAN.md](PLAN.md): the core `taskgate check` command (changed-task
discovery from a git diff, reference-solution and empty-baseline gates, Markdown
and JSON reports), then a gate plugin registry with static gates, a Docker runner,
grader determinism and cheat probes, a content-hash result cache, and GitHub
reporting with a fake API and a composite action.

## License

MIT, see [LICENSE](LICENSE).
