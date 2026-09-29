.PHONY: install lint fmt typecheck test cov check demo docker

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

demo: ## Offline end to end: build a sample repo with two task pull requests and check both
	$(UV) run taskgate tasks $(SAMPLE)
	$(UV) run sh examples/demo.sh $(DEMO_DIR)

docker: ## Build the image, run the demo in it, prune this project's dangling images
	docker build -t $(IMAGE) .
	docker run --rm --entrypoint sh $(IMAGE) examples/demo.sh /tmp/taskgate-demo
	docker image prune -f --filter label=project=taskgate
