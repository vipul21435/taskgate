.PHONY: install lint fmt typecheck test cov check demo docker

UV ?= uv
IMAGE ?= taskgate:local
SAMPLE ?= examples/sample-repo

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

demo: ## Offline demo on the bundled sample repository
	$(UV) run taskgate version
	$(UV) run taskgate tasks $(SAMPLE)
	$(UV) run taskgate tasks --json $(SAMPLE)

docker: ## Build the image, run the demo in it, prune this project's dangling images
	docker build -t $(IMAGE) .
	docker run --rm $(IMAGE) tasks $(SAMPLE)
	docker image prune -f --filter label=project=taskgate
