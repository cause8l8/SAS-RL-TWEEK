"""Conservative network actions; no TCP, MTU, IPv6, or DNS-server tweaks."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sas_booster.models import StateSnapshot
from sas_booster.utils.windows import is_windows, run_command


class NetworkOptimizer:
    def __init__(self) -> None:
        self.log = logging.getLogger("sas_booster")

    def apply(
        self,
        snapshot: StateSnapshot,
        options: dict[str, object],
        persist: Callable[[], None],
        status: Callable[[str], None],
    ) -> None:
        if not bool(options.get("flush_dns_troubleshooting", False)):
            return
        try:
            if is_windows():
                run_command(["ipconfig", "/flushdns"], timeout=15, check=True)
            status("DNS resolver cache flushed (troubleshooting only)")
            self.log.info("Action applied | action=flush-dns | scope=troubleshooting")
        except (OSError, RuntimeError, TimeoutError) as exc:
            snapshot.warnings.append(f"DNS troubleshooting skipped: {type(exc).__name__}")
            persist()
            status("DNS troubleshooting action could not be completed")
            self.log.warning("Action failed | action=flush-dns | error=%s", type(exc).__name__)


# Compatibility alias for callers from the previous release.
NetworkService = NetworkOptimizer
