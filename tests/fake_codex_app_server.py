"""Deterministic fake for the Codex App Server used by simulation tests."""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    mode = os.environ.get("FAKE_CODEX_MODE", "normal")
    if mode == "hang":
        time.sleep(60)
        return
    if mode == "stderr":
        print("simulated app-server diagnostic", file=sys.stderr, flush=True)

    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        request_id = message.get("id")
        if request_id == 1:
            print(json.dumps({"id": 1, "result": {"ok": True}}), flush=True)
        elif request_id == 2:
            if mode in {"error", "stderr"}:
                print(json.dumps({"id": 2, "error": {"message": "simulated app-server failure"}}), flush=True)
            else:
                used = float(os.environ.get("FAKE_PRIMARY_USED", "25"))
                weekly = float(os.environ.get("FAKE_SECONDARY_USED", "40"))
                print(json.dumps({"id": 2, "result": {"rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": used, "windowDurationMins": 300, "resetsAt": 2000000000},
                    "secondary": {"usedPercent": weekly, "windowDurationMins": 10080, "resetsAt": 2000600000},
                    "rateLimitReachedType": None,
                }}}), flush=True)
            return


if __name__ == "__main__":
    main()

