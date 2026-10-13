"""Fresh processes catch import cycles hidden by pytest's collection order."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "drover.server.control_outbox",
        "drover.server.lake.exporter",
        "drover.server.lake.lifecycle",
        "drover.server.harness.events",
        "drover.server.harness.registry",
        "drover.server.harness.recap_prompt",
        "drover.server.harness.recap_worker",
        "drover.server.harness.structured.claude",
        "drover.server.harness.structured.driver",
    ],
)
def test_control_outbox_import_order(module):
    env = dict(os.environ)
    source = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join([str(source), env.get("PYTHONPATH", "")])
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0, result.stderr
