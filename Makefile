.PHONY: help install dev test test-unit test-integration test-e2e test-verbose lint format typecheck complexity audit check-all coverage clean smoke install-hooks

help:
	@echo "Available targets:"
	@echo "  make install          - Install dependencies"
	@echo "  make dev              - Install with dev dependencies"
	@echo "  make test             - Run unit tests"
	@echo "  make test-unit        - Run unit tests only"
	@echo "  make test-integration - Run integration tests (requires Mail.app)"
	@echo "  make test-e2e         - Run end-to-end tests"
	@echo "  make test-verbose     - Run tests with verbose output"
	@echo "  make lint             - Run ruff linter"
	@echo "  make format           - Run ruff formatter"
	@echo "  make typecheck        - Run mypy type checker"
	@echo "  make complexity       - Check cyclomatic complexity"
	@echo "  make audit            - Run all audit scripts"
	@echo "  make check-all        - Run all checks"
	@echo "  make coverage         - Run tests with coverage report"
	@echo "  make clean            - Remove cache and build artifacts"

install:
	uv sync

dev:
	uv sync --dev

test:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" -q

test-unit:
	uv run pytest tests/unit/ -q

test-integration:
	MAIL_TEST_MODE=true uv run pytest tests/integration/ --run-integration -v

test-e2e:
	MAIL_TEST_MODE=true uv run pytest tests/e2e/ -v

# Real-execution smoke suite: runs osascript against the local Mail.app (and
# IMAP against the real account) to catch the AppleScript-runtime bug class
# that mocked unit tests structurally cannot. Self-skips if Mail.app/account
# aren't available, so it's safe to run anywhere. Wired into the pre-push hook.
smoke:
	uv run pytest tests/integration/test_smoke.py --run-integration -q

# Install repo-tracked git hooks into .git/hooks (symlink so they stay in sync).
install-hooks:
	@ln -sf ../../scripts/hooks/pre-push .git/hooks/pre-push
	@chmod +x scripts/hooks/pre-push
	@echo "Installed pre-push hook -> scripts/hooks/pre-push"

benchmark:
	MAIL_TEST_MODE=true uv run pytest tests/benchmarks/ --run-benchmark -v -s

benchmark-baseline:
	@echo "Re-capturing baselines into tests/benchmarks/baseline.json..."
	MAIL_TEST_MODE=true uv run pytest tests/benchmarks/ --run-benchmark --capture-baseline -v -s

test-verbose:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" -v --tb=long

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

typecheck:
	uv run mypy src/

complexity:
	@./scripts/check_complexity.sh

audit:
	@./scripts/check_dependencies.sh
	@./scripts/check_applescript_safety.sh
	@./scripts/check_readme_claims.sh

coverage:
	uv run pytest tests/ -m "not integration and not e2e and not benchmark" --cov=apple_mail_mcp --cov-report=term-missing -q

check-all: lint typecheck test complexity
	@./scripts/check_version_sync.sh
	@./scripts/check_client_server_parity.sh
	@echo ""
	@echo "All checks passed."

clean:
	rm -rf __pycache__ .pytest_cache .coverage htmlcov/ .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
