"""Explicit foreground worker, no OS service, autostart entry or hidden scheduler."""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.workspace import WorkspaceService
from workspace_api import workspace_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    service = WorkspaceService(workspace_config())
    print(json.dumps({"worker": "document-intake", "status": "ready"}), flush=True)
    try:
        while True:
            worked = service.run_once()
            if args.once:
                return
            if not worked:
                time.sleep(0.8)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
