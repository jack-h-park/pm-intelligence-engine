from typing import Optional, Protocol, runtime_checkable


@runtime_checkable
class PMWorkflowStore(Protocol):
    # --- Signal ---
    def save_signal(
        self,
        product_id: str,
        title: str,
        raw_content: str,
        source_url: Optional[str] = None,
        category: str = "other",
        source_type: str = "manual",
    ) -> str: ...

    def get_signal(self, signal_id: str) -> Optional[dict]: ...

    def update_signal_status(self, signal_id: str, status: str) -> None: ...

    def list_signals(
        self,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]: ...

    # --- WorkflowRun ---
    def create_run(self, product_id: str, signal_id: str) -> str: ...

    def get_run(self, run_id: str) -> Optional[dict]: ...

    def update_run(self, run_id: str, **kwargs) -> None: ...

    def list_runs(
        self,
        product_id: Optional[str] = None,
        status: Optional[str] = None,
        routing: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]: ...

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

    # --- Artifact ---
    def save_artifact(
        self,
        run_id: str,
        artifact_type: str,
        content_md: str,
        content_json: str,
        source_stage: Optional[str] = None,
    ) -> str: ...

    def list_artifacts(
        self,
        run_id: str,
        artifact_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]: ...
