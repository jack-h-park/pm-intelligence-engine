import json
import sys
from datetime import UTC, datetime
from typing import Any


def emit_event(
    stage: str,
    action: str,
    run_id: str,
    detail: dict[str, Any] | None = None,
) -> None:
    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "stage": stage,
        "action": action,
        "run_id": run_id,
        "detail": detail or {},
    }
    print(json.dumps(event), file=sys.stdout, flush=True)
