.PHONY: install lint fmt typecheck test cov check demo demo-docker test-docker docker clean-images

UV ?= uv
IMAGE ?= taskgate:local
SAMPLE ?= examples/sample-repo
DEMO_DIR ?= .taskgate/demo

install: ## Install the locked environment and the pre-commit hook
	$(UV) sync --locked
	$(UV) run pre-commit install

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Apply ruff fixes and formatting
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck: ## mypy --strict on src/
	$(UV) run mypy

test: ## Run the test suite
	$(UV) run pytest

cov: ## Run the tests with the coverage gate (fail_under in pyproject.toml)
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

check: lint typecheck cov ## Everything CI runs except Docker

demo: ## Offline end to end: build a sample repo with three task pull requests and check each
	$(UV) run taskgate tasks $(SAMPLE)
	$(UV) run sh examples/demo.sh $(DEMO_DIR)

demo-docker: ## The demo with the Docker runner: task images built, solutions run offline with limits
	TASKGATE_RUNNER=docker $(UV) run sh examples/demo.sh $(DEMO_DIR)-docker
	docker image prune -f --filter label=project=taskgate

test-docker: ## The opt-in tests against a real Docker daemon
	TASKGATE_DOCKER_TESTS=1 $(UV) run pytest -m docker
	docker image prune -f --filter label=project=taskgate

clean-images: ## Remove the task images the Docker runner built (taskgate-env:*)
	@ids="$$(docker image ls -q --filter reference='taskgate-env')"; \
	if [ -n "$$ids" ]; then docker image rm -f $$ids; else echo "no taskgate-env images"; fi

docker: ## Build the image, run the demo in it, prune this project's dangling images
	docker build -t $(IMAGE) .
	docker run --rm --entrypoint sh $(IMAGE) examples/demo.sh /tmp/taskgate-demo
	docker image prune -f --filter label=project=taskgate
