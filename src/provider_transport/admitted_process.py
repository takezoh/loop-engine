"""Launch and end one provider process through its spawn admission.

Every wire protocol client starts its process here, so the spawn fence, the
admitted cwd, cell membership and the reservation's release hold the same
way for each.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any, Mapping

from .cwd_authority import SpawnAdmission, SpawnReceipt


EXECUTION_ROLE_VARIABLE = "LOOP_ENGINE_EXECUTION_ROLE"
PRODUCER_ROLE = "producer"


def producer_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Every provider the transport starts is a producer.

    The MCP servers a provider starts inherit this role; the unit entry
    refuses to open for it, so a producer cannot start or control any unit.
    """
    return {**base, EXECUTION_ROLE_VARIABLE: PRODUCER_ROLE}


def launch_admitted(
    command: tuple[str, ...], *, spawn_admission: SpawnAdmission,
    host_cell_id: str, options: Mapping[str, Any],
) -> tuple[Any, SpawnReceipt]:
    """Spawn the provider as a producer, in the admitted cwd and cell."""
    options = {**options, "env": producer_environment(options.get("env", os.environ))}
    return spawn_admission.spawn_process(
        command, expected_cell_id=host_cell_id, options=options
    )


def reap(process: Any, *, term_grace: float, kill_reap_timeout: float) -> None:
    """Terminate the process if it still runs, then kill it after the grace."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=kill_reap_timeout)


def release(
    spawn_admission: SpawnAdmission | None, receipt: SpawnReceipt | None, process: Any
) -> None:
    """Return the process's reservation, certain only when the process is gone."""
    if spawn_admission is not None and receipt is not None:
        spawn_admission.reservation.release_live(receipt, certain=process.poll() is not None)
