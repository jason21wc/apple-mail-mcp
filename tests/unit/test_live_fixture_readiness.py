"""Exercise fixture synchronization without Mail, network access, or real sleeps."""

from unittest.mock import Mock, call

import pytest

from tests.integration import test_mail_integration as live_tests


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(live_tests.time, "monotonic", lambda: now[0])
    sleeper = Mock(side_effect=sleep)
    monkeypatch.setattr(live_tests.time, "sleep", sleeper)
    return now, sleeper


def wait_for_fixture(connector, timeout=1.0):
    live_tests.TestBulkRfcIdDoesNotFreezeMail._wait_for_imap_fixture(
        connector, "owned@example.invalid", "TestAccount", "INBOX", timeout_s=timeout
    )


def test_already_visible_needs_no_wait(clock):
    connector = Mock()
    connector._locate_via_imap.return_value = {"uid": 123}
    wait_for_fixture(connector)
    connector._locate_via_imap.assert_called_once_with(
        "owned@example.invalid", account="TestAccount", source_mailbox="INBOX"
    )
    clock[1].assert_not_called()
    connector.update_message.assert_not_called()


def test_empty_presence_is_retried_in_exact_scope(clock):
    connector = Mock()
    connector._locate_via_imap.side_effect = [None, None, {"uid": 123}]
    wait_for_fixture(connector, timeout=2.0)
    assert connector._locate_via_imap.call_args_list == [
        call("owned@example.invalid", account="TestAccount", source_mailbox="INBOX")
    ] * 3
    assert 0 < clock[0][0] < 2.0
    connector.update_message.assert_not_called()


def test_missing_fixture_fails_at_polling_deadline(clock):
    connector = Mock()
    connector._locate_via_imap.return_value = None
    with pytest.raises(pytest.fail.Exception, match="Fixture setup incomplete.*IMAP"):
        wait_for_fixture(connector, timeout=0.75)
    assert clock[0][0] == 0.75
    connector.update_message.assert_not_called()


@pytest.mark.parametrize("error", [OSError("offline"), RuntimeError("authentication rejected")])
def test_lookup_errors_propagate_without_retry(clock, error):
    connector = Mock()
    connector._locate_via_imap.side_effect = error
    with pytest.raises(type(error)) as raised:
        wait_for_fixture(connector)
    assert raised.value is error
    connector._locate_via_imap.assert_called_once()
    clock[1].assert_not_called()
    connector.update_message.assert_not_called()
