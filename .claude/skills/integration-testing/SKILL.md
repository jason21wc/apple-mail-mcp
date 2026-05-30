---
name: integration-testing
description: Use when setting up, running, or debugging integration tests against real Apple Mail. Also use when unit tests pass but behavior seems wrong, when adding new AppleScript operations, or when you need to understand why mocked tests are insufficient for this project.
---

# Apple Mail Integration Testing

## Why Integration Tests Matter

Unit tests mock `_run_applescript()` and test Python logic only. They CANNOT catch:
- AppleScript syntax errors
- Variable naming conflicts in AppleScript
- Mail.app API behavior differences between versions
- Silently-dropped record keys from NSJSONSerialization (e.g., `name`, `id`, `size` selector collisions)
- Gmail-specific behavior differences
- Timeout issues with real mailbox sizes

**The OmniFocus project's story:** A variable naming typo went undetected by 400+ unit tests because they all mocked the AppleScript boundary. Only integration tests against the real app caught it. This lesson applies equally to Apple Mail.

## Mock fixtures must come from reality (anti-echo-chamber)

A mock is only as good as its fidelity to what the real layer actually returns. The most dangerous unit-test failure mode here is the **echo chamber**: you hand-author a fixture shape, write code against it, and the test passes — but the shape is one the real layer *never emits*, so the test confirms your assumption instead of reality.

Real case (2026-05): the IMAP attachment walkers assumed multipart BODYSTRUCTURE children were bare tuple elements `(child1, child2, subtype)`. IMAPClient actually groups them in a **list** at position 0 — `([child1, child2], subtype, ...)`. ~15 unit fixtures encoded the impossible bare-tuple shape, so 92%-coverage unit tests stayed green while every multipart attachment was silently dropped on the real IMAP path.

Rules to prevent it:
- **Derive expected values from the spec or from captured-real output, never from the implementation's observed output.** For this codebase the "spec" of an IMAP/AppleScript response *is* the real server/Mail.app output.
- **Treat hand-authored fixtures as suspect.** When a fixture stands in for an IMAPClient `BodyData`/`Envelope` or an `NSJSONSerialization` record, confirm its shape against the library's real return type (e.g. run one value through `imapclient`'s `BodyData.create`, or capture a real `BODYSTRUCTURE`/AppleScript-JSON once and keep it verbatim — see `_BS_REAL_ICLOUD_MIXED_PDF` in `tests/unit/test_imap_connector.py`).
- **Known AppleScript-JSON reality to encode in fixtures:** an empty/degenerate record `{}` bridges to NSArray `[]` (so a "dict" parse can return a list — guard with `isinstance(..., dict)`); a bare `key:` record key is dropped (use `|key|:`); `missing value` must be coerced to a safe default or the key is absent (bracket access then `KeyError`s).
- When you fix a real-layer bug, add a regression test built from the **captured real** structure, not a reconstructed guess.

## Three-Tier Testing Strategy

| Tier | Speed | What it catches | When to run |
|------|-------|-----------------|-------------|
| Unit (mocked) | ~1s, 99 tests | Python logic, parsing, validation | Every change |
| Integration (real) | ~30s | AppleScript bugs, Mail.app quirks | New AppleScript code |
| E2E (full MCP) | ~30s | Tool registration, parameter passing | New/modified tools |

## Setting Up Integration Tests

### Prerequisites
1. Apple Mail configured with at least one account
2. macOS Automation permission granted to Terminal/IDE

### Test Account Setup
```bash
# Set test account (default: "Gmail")
export MAIL_TEST_ACCOUNT="Gmail"

# Run integration tests
make test-integration
```

### Running Tests
```bash
# Integration tests are opt-in
pytest tests/integration/ --run-integration -v

# Or via Makefile
make test-integration
```

## Writing Integration Tests

```python
import pytest
from apple_mail_mcp.mail_connector import AppleMailConnector

# Skip unless explicitly enabled
pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Integration tests disabled by default."
)

class TestMailIntegration:
    @pytest.fixture
    def connector(self) -> AppleMailConnector:
        return AppleMailConnector()

    @pytest.fixture
    def test_account(self) -> str:
        import os
        return os.getenv("MAIL_TEST_ACCOUNT", "Gmail")

    def test_list_mailboxes(self, connector, test_account):
        """Verify we can list mailboxes from a real account."""
        result = connector.list_mailboxes(test_account)
        assert isinstance(result, list)
        assert len(result) > 0
        # INBOX should always exist
        assert any("INBOX" in mb for mb in result)

    @pytest.mark.skip(reason="Sends real email - enable manually")
    def test_draft_send_now(self, connector):
        """Test sending a draft - enable manually only."""
        ...
```

## Key Patterns

### Never Auto-Run Destructive Tests
```python
# Destructive operations (send, delete, move) should be:
# 1. Skipped by default
# 2. Require explicit --run-integration flag
# 3. Require MAIL_TEST_ACCOUNT environment variable
# 4. Target a specific test account, never "all accounts"
```

### Test Account Configuration
```python
@pytest.fixture
def test_account(self) -> str:
    """Configurable test account - never hardcode."""
    import os
    return os.getenv("MAIL_TEST_ACCOUNT", "Gmail")
```

### Cleanup After Tests
```python
# If test creates data (mailbox, flag), clean up in fixture teardown
@pytest.fixture
def test_mailbox(self, connector, test_account):
    name = f"MCP-Test-{uuid.uuid4()}"
    connector.create_mailbox(test_account, name)
    yield name
    try:
        # Cleanup - don't fail if already deleted
        connector.delete_mailbox(test_account, name)
    except Exception:
        pass
```

## Hard Rule

**If you wrote or modified AppleScript in `mail_connector.py`, integration tests must cover that operation before merge.**

This is not optional. This is not "nice to have." Unit tests with mocked `_run_applescript()` give false confidence about AppleScript correctness.

## Common Integration Test Failures

1. **"Not authorized to send Apple events"** — Grant Automation permission in System Settings > Privacy & Security > Automation
2. **Account not found** — Verify `MAIL_TEST_ACCOUNT` matches an actual configured account name in Mail.app
3. **Timeout** — Large mailboxes may exceed 60s default. Use `AppleMailConnector(timeout=120)`
4. **Gmail behavior** — Gmail move/delete may behave differently than IMAP accounts. Test both if possible.
