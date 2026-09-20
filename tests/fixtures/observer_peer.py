"""Deterministic herdr read surface for end-to-end observer CLI tests."""

import json
import os
from pathlib import Path
import sys

config_path = Path(os.environ["ORCH_OBSERVER_FIXTURE"])
config = json.loads(config_path.read_text())
with config_path.with_suffix(".calls").open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] != ["--session", "user-session"]:
    sys.exit(2)
if args[2:] == ["agent", "get", "w1:p2"]:
    print(json.dumps({"result": {"agent": {"name": config.get("name", "child"),
        "pane_id": "w1:p2", "agent_status": config["state"]}}}))
elif args[2:] == ["pane", "read", "w1:p2", "--source", "detection", "--lines", "80"]:
    print(config["screen"])
else:
    # The test fails if the watcher attempts input, approval, startup or cleanup.
    sys.exit(2)
