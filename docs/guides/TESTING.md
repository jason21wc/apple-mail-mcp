# Testing Guide

## Test Levels

| Level | Command | Purpose |
|-------|---------|---------|
| Unit | `make test` | Python logic with mocked I/O — **in CI** |
| Quick smoke | `make smoke` | Every registered MCP tool through FastMCP dispatch, with mail I/O mocked and temporary local storage |
| Integration | `make test-integration` | Real Mail.app operations — local only |
| E2E | `make test-e2e` | FastMCP dispatch layer — local only |
| Benchmarks | `make benchmark` | Performance regression detection (opt-in) — see [BENCHMARKING.md](BENCHMARKING.md) |
| Blind agent eval | (see below) | Whether models can use the tools from descriptions alone — see [`evals/agent_tool_usability/`](../../evals/agent_tool_usability/) |

**CI currently runs unit tests only.** Integration and benchmark tests require real Mail.app
and, for some paths, IMAP credentials. The smoke suite does not require either. The stdio
E2E check also avoids mail access but requires `ai-governance-proxy` on PATH; it skips
explicitly if that executable is unavailable. See **Manual e2e policy** below.

### Quick functional check

Run `make smoke` after setup or changes. It asserts that every registered tool has a
successful invocation case, checks selected typed failures, and verifies that missing
confirmation cannot delete a message or template. Template save/read/render/delete
uses real files under pytest's temporary directory. All Mail connector calls are mocked;
the suite does not read credentials, send mail, or change Mail.app data.

This checks API wiring and selected behavior, not every input combination or live provider
behavior. For transport startup, also run `make test-e2e`. For IMAP/AppleScript correctness,
use reviewed, operation-specific integration fixtures with an explicit dedicated account;
do not interpret a passing smoke suite as permission to run the full integration suite on
a personal mailbox. Mutation fixtures must verify cleanup as well as the operation.

### E2E Scope

`make test-e2e` covers two layers:

1. **In-process FastMCP dispatch** ([tests/e2e/test_mcp_tools.py](../../tests/e2e/test_mcp_tools.py)) — tool registration, schemas, and happy-path invocation across the tool set, with the connector mocked. (Elicitation-gated tools are run with a patched accept so the happy-path exercises dispatch, not the confirmation flow.)
2. **Real stdio transport** ([tests/e2e/test_stdio_transport.py](../../tests/e2e/test_stdio_transport.py)) — spawns the server as a subprocess, completes the MCP handshake over stdio, and asserts `list_tools` returns the full tool set. Catches startup errors, banner/stdout contamination, and JSON-RPC framing bugs that the in-process layer cannot surface.

### Manual e2e policy

CI does not currently run e2e. **If your PR touches IMAP or AppleScript code paths** —
`imap_connector.py`, `mail_connector.py` AppleScript bodies/wrappers, or any tool gated by
`_elicit_confirmation` — run `make test-e2e` (and, where relevant, `make test-integration`) **before
pushing**. A stale e2e failure on `main` is only caught by someone running it locally. `make
test-e2e` is also a mandatory pre-release gate.

## Running Tests

```bash
# All unit tests (default)
make test

# With coverage report
make coverage

# Integration tests (requires Mail.app)
MAIL_TEST_ACCOUNT="Gmail" make test-integration

# Specific test file
uv run pytest tests/unit/test_utils.py -v
```

**Safety gate.** Integration/e2e runs that go through `server.py` tools set `MAIL_TEST_MODE=true`
(the `make` targets do this) and `MAIL_TEST_ACCOUNT=<account>`. The gate (`check_test_mode_safety` in
`security.py`) blocks destructive operations on any account other than the designated test account
and blocks sends to non-reserved recipient domains (must be `@example.com`, `.test`, `.invalid`,
`.localhost`, …). Point `MAIL_TEST_ACCOUNT` at an account you don't mind being mutated — integration
benchmarks move real messages into an isolated bench mailbox and drain them back.

## Writing Tests

### Unit Tests

Mock at the `_run_applescript()` boundary:

```python
from unittest.mock import patch, MagicMock
from apple_mail_fast_mcp.mail_connector import AppleMailConnector

class TestMyFeature:
    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector(timeout=30)

    @patch.object(AppleMailConnector, "_run_applescript")
    def test_my_method(self, mock_run, connector):
        mock_run.return_value = "expected|output"
        result = connector.my_method("param")
        assert result == expected
```

### Integration Tests

```python
pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default."
)

def test_real_operation(self, connector):
    result = connector.list_mailboxes("Gmail")
    assert isinstance(result, list)
```

### Test Organization

Each test file follows:
1. Fixtures (connector instance)
2. Happy path tests
3. Filter/parameter tests
4. Error handling tests
5. Edge cases
6. Security tests (dedicated class per feature)

## Coverage

- Target: 90%
- Enforced: `fail_under = 90` in pyproject.toml
- Run `make coverage` for the current report.
