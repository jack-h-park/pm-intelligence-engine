from datetime import datetime
from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class PMWorkflowStore(Protocol):
    # --- Signal ---
    def save_signal(
        self,
        title: str,
        raw_content: str,
        original_product_id: Optional[str] = None,
        source_url: Optional[str] = None,
        category: str = "other",
        source_type: str = "manual",
        source_ref: Optional[str] = None,
    ) -> str: ...

    def get_signal(self, signal_id: str) -> Optional[dict]: ...

    def update_signal_status(self, signal_id: str, status: str) -> None: ...

    def update_signal_content(
        self,
        signal_id: str,
        raw_content: str,
        category: Optional[str] = None,
    ) -> Optional[dict]: ...

    def list_signals(
        self,
        original_product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]: ...

    # --- WorkflowRun ---
    def create_decision_request_run(self, case) -> dict: ...

    def create_idempotent_decision_request(
        self, actor: str, idempotency_key: str, request_hash: str, case
    ) -> tuple[dict, int]: ...

    def save_decision_case(self, run_id: str, case) -> None: ...

    def get_decision_case(self, run_id: str): ...

    def create_run(
        self,
        product_id: str,
        signal_id: str,
        batch_id: Optional[str] = None,
        origin: str = "start",
    ) -> str: ...

    def get_run(self, run_id: str) -> Optional[dict]: ...

    def update_run(self, run_id: str, **kwargs) -> None: ...

    # State transitions (US-55): the canonical (lifecycle, position, outcome,
    # reason) columns are the authoritative run state, written only here.
    def advance(self, run_id: str, position: str, **extra) -> None: ...

    def pause(self, run_id: str, position: str, **extra) -> None: ...

    def finish(
        self, run_id: str, outcome: str, position: str | None = None,
        reason: str | None = None, **extra,
    ) -> None: ...

    def list_runs(
        self,
        product_id: Optional[str] = None,
        routing: Optional[str] = None,
        event: Optional[str] = None,
        since: Optional[datetime] = None,
        batch_id: Optional[str] = None,
        signal_id: Optional[str] = None,
        lifecycle: Optional[str] = None,
        position: Optional[str] = None,
        outcome: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]: ...

    # --- RunBatch (US-49) ---
    def create_batch(self, signal_id: str, triage: Optional[list] = None) -> str: ...

    def get_batch(self, batch_id: str) -> Optional[dict]: ...

    def close_batch_membership(self, batch_id: str) -> None: ...

    # --- PortfolioSynthesis (US-49, Variant 2) ---
    def save_portfolio_synthesis(
        self,
        batch_id: str,
        signal_id: str,
        content_md: str,
        content_json: str,
        run_ids_json: str,
    ) -> bool: ...

    def get_portfolio_synthesis(self, batch_id: str) -> Optional[dict]: ...

    # --- StageOutput ---
    def save_stage_output(
        self,
        run_id: str,
        stage: str,
        output_json: str,
        version: int = 1,
    ) -> str: ...

    def get_stage_output(
        self,
        run_id: str,
        stage: str,
        version: Optional[int] = None,
    ) -> Optional[dict]: ...

    def get_all_stage_outputs(self, run_id: str) -> list[dict]: ...

    # --- ApprovalEvent ---
    def record_approval(
        self,
        run_id: str,
        stage: str,
        action: str,
        feedback_text: Optional[str] = None,
    ) -> str: ...

    def get_approval_events(self, run_id: str) -> list[dict]: ...

    # --- Artifact ---
    def save_artifact(
        self,
        run_id: str,
        artifact_type: str,
        content_md: str,
        source_stage: Optional[str] = None,
    ) -> str: ...

    def list_artifacts(
        self,
        run_id: str,
        artifact_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]: ...
