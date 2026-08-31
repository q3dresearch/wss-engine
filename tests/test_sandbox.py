"""The sandbox is part of the contract: planted ground truth must be recovered."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_recovers_planted_truth(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ENGINE_ROOT / "sandbox" / "run.py"), "--days", "110", "--out", str(tmp_path / "out")],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "SANDBOX OK" in result.stdout
