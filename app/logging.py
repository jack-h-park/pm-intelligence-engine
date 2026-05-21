import json
import sys
from datetime import datetime, timezone
from typing import Optional


def emit_event(
    stage: str,
    action: str,
    run_id: str,
    detail: Optional[dict] = None,
) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "action": action,
        "run_id": run_id,
        "detail": detail or {},
    }
    print(json.dumps(event), file=sys.stdout, flush=True)
