"""Unit tests for the CLI module's Windows event-loop shim.

`scholarapp.cli._silence_windows_proactor_shutdown_noise` selects the asyncio
selector event-loop policy on Windows to avoid a spurious "Event loop is closed"
traceback at shutdown. It is a no-op on every other platform.

These tests run on the Linux CI runner by monkeypatching `sys.platform` and
spying on `asyncio.set_event_loop_policy`, so the real global policy is never
mutated and no state leaks between tests.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from scholarapp.cli import _silence_windows_proactor_shutdown_noise


def test_shim_is_noop_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Windows the shim must not touch the asyncio event-loop policy."""
    monkeypatch.setattr(sys, "platform", "linux")

    calls: list[object] = []
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: calls.append(policy))

    _silence_windows_proactor_shutdown_noise()

    assert calls == []


@pytest.mark.skipif(
    not hasattr(asyncio, "WindowsSelectorEventLoopPolicy"),
    reason="WindowsSelectorEventLoopPolicy not available on this interpreter",
)
def test_shim_sets_selector_policy_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Windows the shim must install a WindowsSelectorEventLoopPolicy."""
    monkeypatch.setattr(sys, "platform", "win32")

    captured: list[object] = []
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: captured.append(policy))

    _silence_windows_proactor_shutdown_noise()

    assert len(captured) == 1
    assert isinstance(captured[0], asyncio.WindowsSelectorEventLoopPolicy)
