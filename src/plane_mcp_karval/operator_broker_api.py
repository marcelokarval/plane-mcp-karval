"""Narrow production entry point for the operator broker.

Core state-machine and test-store symbols intentionally live in the internal
module and are not exported here. Same-process Python is not a security
boundary; deployment must still isolate this process externally.
"""

from typing import Any, Callable

__all__ = ("create_production_broker",)


def create_production_broker(
    config: Any,
    verifier: Any,
    provider: Any,
    clock: Callable[[], float],
) -> Any:
    from .operator_broker import create_production_broker as _create

    return _create(config, verifier, provider, clock)
