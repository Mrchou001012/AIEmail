import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "records_requests.js"


def test_records_request_coordinator_aborts_and_rejects_stale_responses() -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute the records request helper")
    script = r"""
const {createRequestCoordinator} = require(process.argv[1]);
const coordinator = createRequestCoordinator();
const first = coordinator.begin({tab: "handoffs", status: "OPEN", offset: 0});
const second = coordinator.begin({tab: "outbox", status: "CLAIMED", offset: 100});
console.log(JSON.stringify({
  firstAborted: first.signal.aborted,
  firstCurrent: coordinator.isCurrent(first),
  secondCurrent: coordinator.isCurrent(second),
  snapshot: second.snapshot,
}));
"""
    completed = subprocess.run(
        ["node", "-e", script, str(MODULE_PATH)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "firstAborted": True,
        "firstCurrent": False,
        "secondCurrent": True,
        "snapshot": {"tab": "outbox", "status": "CLAIMED", "offset": 100},
    }
