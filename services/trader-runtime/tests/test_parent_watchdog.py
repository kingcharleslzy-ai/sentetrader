from __future__ import annotations

import os

from trader_runtime.sidecar_entry import process_is_alive


def test_parent_watchdog_can_distinguish_the_desktop_parent_from_a_missing_pid():
    assert process_is_alive(os.getpid()) is True
    assert process_is_alive(2_147_483_647) is False
