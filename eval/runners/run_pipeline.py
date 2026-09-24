"""Drive the real Compose API and durable worker over the golden dataset."""

import argparse
import hashlib
import json
import logging
import subprocess
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from eval.golden.schema import GoldenManifest, GoldenSample
from eval.runners.schema import ModelClass, PipelineReport, RunRecord, utc_now
from invoiceops_agent.agents.near_duplicate_settings import LiteLLMWorkflowSettings
from invoiceops_agent.api.schemas.invoice_read import InvoiceDetail
from invoiceops_agent.api.schemas.provenance import InvoiceProvenancePage
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.tools.ingestion_schemas import IngestionResult

logger = logging.getLogger(__name__)
COMMITTED_MANIFEST = Path("eval/golden/v1.0.0/manifest.json")
DATASET = Path("eval/data/golden/v1.0.0")
RECORDED_SAMPLE_ID = "SYN-CLEAN-0034"
RECORDED_DOCUMENT = Path("eval/cassettes/smoke/SYN-CLEAN-0034.png")
MAX_DOCUMENT_BYTES = 10_000_000


class PipelineRunError(Exception):
    """Preflight, Compose, or API evidence failed a bounded evaluation run."""


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    api_base_url: str = Field(default="http://127.0.0.1:8000", validation_alias="EVAL_API_BASE_URL")
    service_token: SecretStr = Field(
        default=SecretStr("invoiceops-local-upload-token"),
        validation_alias="INVOICEOPS_SERVICE_TOKEN",
    )
    analyst_token: SecretStr = Field(
        default=SecretStr("invoiceops-local-analyst-token"),
        validation_alias="INVOICEOPS_ANALYST_TOKEN",
    )
    auditor_token: SecretStr = Field(
        default=SecretStr("invoiceops-local-auditor-token"),
        validation_alias="INVOICEOPS_AUDITOR_TOKEN",
    )

    @field_validator("api_base_url")
    @classmethod
    def local_api_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Evaluation runner requires a local Compose API")
        if parsed.username or parsed.password or parsed.path not in {"", "/"}:
            raise ValueError("Evaluation API base URL cannot contain credentials or a path")
        return value.rstrip("/")


class PipelineAPI(Protocol):
    def ready(self) -> None: ...

    def upload(self, sample: GoldenSample, body: bytes) -> IngestionResult: ...

    def detail(self, invoice_id: UUID) -> InvoiceDetail: ...

    def provenance(self, invoice_id: UUID) -> InvoiceProvenancePage: ...


class Worker(Protocol):
    def process(
        self, run_ids: tuple[UUID, ...], *, recorded: bool
    ) -> dict[UUID, dict[str, str]]: ...


class Compose:
    def __init__(
        self, *, project_name: str | None = None, timeout_seconds: int = 18000, workers: int = 1
    ) -> None:
        if not 1 <= workers <= 16:
            raise ValueError("Compose worker count must be between one and 16")
        self._prefix = ["docker", "compose"]
        if project_name:
            self._prefix.extend(("--project-name", project_name))
        self._timeout_seconds = timeout_seconds
        self._workers = workers

    def _run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
        allow_worker_failure: bool = False,
    ) -> str:
        try:
            result = subprocess.run(
                [*self._prefix, *args],
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PipelineRunError(f"Compose command failed: {type(error).__name__}") from None
        if result.returncode and not (allow_worker_failure and result.returncode == 1):
            raise PipelineRunError(f"Compose command exited with status {result.returncode}")
        return result.stdout

    def start(self) -> None:
        self._run(("up", "-d", "--build", "--wait", "--wait-timeout", "120", "api"))

    def seed(self) -> None:
        self._run(
            (
                "--profile",
                "workflow",
                "run",
                "--rm",
                "-T",
                "invoice-worker",
                "invoiceops-golden-seed",
            )
        )

    def _process_chunk(
        self, run_ids: tuple[UUID, ...], *, recorded: bool
    ) -> dict[UUID, dict[str, str]]:
        args = ["--profile", "workflow", "run", "--rm", "-T"]
        if recorded:
            args.extend(("-e", "LITELLM_EMBED_MODEL=recorded-embed-model"))
        args.extend(("invoice-worker", "invoiceops-invoice-batch"))
        if recorded:
            args.extend(("--cassette-dir", "/app/eval/cassettes/smoke"))
        output = self._run(
            args,
            input_text="".join(f"{run_id}\n" for run_id in run_ids),
            allow_worker_failure=True,
        )
        try:
            rows = [json.loads(line) for line in output.splitlines()]
            by_id = {UUID(row["run_id"]): row for row in rows}
        except (KeyError, ValueError, TypeError):
            raise PipelineRunError("Worker emitted invalid result JSON") from None
        if len(rows) != len(run_ids) or set(by_id) != set(run_ids):
            raise PipelineRunError("Worker result IDs differ from submitted run IDs")
        return by_id

    def process(self, run_ids: tuple[UUID, ...], *, recorded: bool) -> dict[UUID, dict[str, str]]:
        if not run_ids:
            return {}
        count = min(self._workers, len(run_ids))
        chunks = tuple(run_ids[index::count] for index in range(count))
        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = tuple(
                pool.submit(self._process_chunk, chunk, recorded=recorded) for chunk in chunks
            )
            batches = tuple(future.result() for future in futures)
        combined = {run_id: row for batch in batches for run_id, row in batch.items()}
        if len(combined) != len(run_ids) or set(combined) != set(run_ids):
            raise PipelineRunError("Parallel worker results differ from submitted run IDs")
        return combined


class HTTPPipelineAPI:
    def __init__(self, settings: EvalSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(base_url=settings.api_base_url, timeout=30)

    def __enter__(self) -> "HTTPPipelineAPI":
        return self

    def __exit__(self, *args: object) -> None:
        self._client.close()

    def ready(self) -> None:
        response = self._client.get("/readyz")
        response.raise_for_status()

    def upload(self, sample: GoldenSample, body: bytes) -> IngestionResult:
        response = self._client.post(
            "/v1/invoices",
            headers={
                "Authorization": f"Bearer {self._settings.service_token.get_secret_value()}",
                "Idempotency-Key": f"golden-v1.0.0-{sample.sample_id}",
            },
            files={"file": (f"{sample.sample_id}.png", body, "image/png")},
        )
        response.raise_for_status()
        if response.status_code not in {200, 201}:
            raise PipelineRunError("Upload returned an unexpected success status")
        upload = IngestionResult.model_validate(response.json())
        if upload.duplicate != (response.status_code == 200):
            raise PipelineRunError("Upload duplicate signal disagrees with HTTP status")
        return upload

    def detail(self, invoice_id: UUID) -> InvoiceDetail:
        response = self._client.get(
            f"/v1/invoices/{invoice_id}",
            headers={"Authorization": f"Bearer {self._settings.analyst_token.get_secret_value()}"},
        )
        response.raise_for_status()
        return InvoiceDetail.model_validate(response.json())

    def provenance(self, invoice_id: UUID) -> InvoiceProvenancePage:
        response = self._client.get(
            f"/v1/invoices/{invoice_id}/provenance",
            headers={"Authorization": f"Bearer {self._settings.auditor_token.get_secret_value()}"},
        )
        response.raise_for_status()
        page = InvoiceProvenancePage.model_validate(response.json())
        if page.next_cursor is not None:
            raise PipelineRunError("Invoice provenance requires pagination")
        return page


def load_manifest(path: Path = COMMITTED_MANIFEST) -> tuple[GoldenManifest, str]:
    raw = path.read_bytes()
    return GoldenManifest.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def select_samples(
    manifest: GoldenManifest,
    *,
    split: Literal["all", "development", "held_out"] = "all",
    limit: int | None = None,
    recorded: bool = False,
) -> tuple[GoldenSample, ...]:
    if recorded:
        selected = tuple(
            sample for sample in manifest.samples if sample.sample_id == RECORDED_SAMPLE_ID
        )
        if len(selected) != 1 or selected[0].split != "development":
            raise PipelineRunError("Recorded smoke sample is absent from development")
        return selected
    selected = tuple(
        sample for sample in manifest.samples if split == "all" or sample.split == split
    )
    if limit is not None:
        if not 1 <= limit <= 500:
            raise PipelineRunError("Limit must be between one and 500")
        selected = selected[:limit]
    ids = {sample.sample_id for sample in selected}
    if any(sample.parent_id not in ids for sample in selected if sample.parent_id):
        raise PipelineRunError("Selected duplicate lacks its parent in the same run")
    return selected


def _read_document(dataset: Path, sample: GoldenSample, *, recorded: bool) -> bytes:
    if recorded:
        body = RECORDED_DOCUMENT.read_bytes()
    else:
        relative = Path(sample.document_path)
        if relative.is_absolute() or len(relative.parts) != 2 or relative.parts[0] != "documents":
            raise PipelineRunError("Document path escapes the golden dataset")
        root = dataset.resolve()
        path = (dataset / relative).resolve()
        if not path.is_relative_to(root):
            raise PipelineRunError("Document path escapes the golden dataset")
        body = path.read_bytes()
    if (
        not 0 < len(body) <= MAX_DOCUMENT_BYTES
        or not body.startswith(b"\x89PNG\r\n\x1a\n")
        or hashlib.sha256(body).hexdigest() != sample.document_sha256
    ):
        raise PipelineRunError("Golden document bytes differ from their committed checksum")
    return body


def preflight_documents(
    dataset: Path, selected: tuple[GoldenSample, ...], *, recorded: bool
) -> dict[str, bytes]:
    if not recorded and (dataset / "manifest.json").read_bytes() != COMMITTED_MANIFEST.read_bytes():
        raise PipelineRunError("Generated golden manifest differs from the committed manifest")
    return {
        sample.sample_id: _read_document(dataset, sample, recorded=recorded) for sample in selected
    }


def schedule_run_waves(
    uploads: Sequence[tuple[GoldenSample, IngestionResult, float]],
) -> tuple[tuple[UUID, ...], ...]:
    """Finish parent invoices before their near-duplicate children are compared."""
    completed = {sample.sample_id for sample, upload, _ in uploads if upload.duplicate}
    pending = {
        sample.sample_id: (sample, upload) for sample, upload, _ in uploads if not upload.duplicate
    }
    waves: list[tuple[UUID, ...]] = []
    while pending:
        ready = tuple(
            (sample_id, upload.run_id)
            for sample_id, (sample, upload) in pending.items()
            if sample.parent_id is None or sample.parent_id in completed
        )
        if not ready or len({run_id for _, run_id in ready}) != len(ready):
            raise PipelineRunError("Golden run dependencies are cyclic or share a run ID")
        waves.append(tuple(run_id for _, run_id in ready))
        completed.update(sample_id for sample_id, _ in ready)
        for sample_id, _ in ready:
            del pending[sample_id]
    return tuple(waves)


def run_pipeline(
    manifest: GoldenManifest,
    manifest_sha256: str,
    selected: tuple[GoldenSample, ...],
    documents: Mapping[str, bytes],
    api: PipelineAPI,
    worker: Worker,
    *,
    recorded: bool,
    model_class: ModelClass | None = None,
) -> PipelineReport:
    if recorded != (model_class is None):
        raise PipelineRunError("Live runs require a model class; recorded smoke cannot claim one")
    started = utc_now()
    api.ready()
    uploads: list[tuple[GoldenSample, IngestionResult, float]] = []
    for sample in selected:
        upload_started = perf_counter()
        upload = api.upload(sample, documents[sample.sample_id])
        upload_duration_ms = (perf_counter() - upload_started) * 1000
        if "DUP_EXACT" in sample.anomaly_codes and not upload.duplicate:
            raise PipelineRunError("An exact-duplicate gold case was accepted as new")
        if "DUP_EXACT" not in sample.anomaly_codes and upload.duplicate:
            raise PipelineRunError("A non-duplicate gold case matched existing content")
        uploads.append((sample, upload, upload_duration_ms))
    waves = schedule_run_waves(uploads)
    run_ids = tuple(run_id for wave in waves for run_id in wave)
    already_processed = {
        upload.run_id: api.detail(upload.invoice_id).invoice.run_status != "QUEUED"
        for _, upload, _ in uploads
        if not upload.duplicate
    }
    worker_results: dict[UUID, dict[str, str]] = {}
    for wave in waves:
        worker_results.update(worker.process(wave, recorded=recorded))
    if set(worker_results) != set(run_ids):
        raise PipelineRunError("Worker result set differs from uploaded run IDs")
    records: list[RunRecord] = []
    for sample, upload, upload_duration_ms in uploads:
        worker_result = worker_results.get(upload.run_id, {})
        detail = api.detail(upload.invoice_id)
        provenance = api.provenance(upload.invoice_id)
        if detail.invoice.id != upload.invoice_id or provenance.invoice_id != upload.invoice_id:
            raise PipelineRunError("API evidence differs from the uploaded invoice")
        records.append(
            RunRecord(
                sample_id=sample.sample_id,
                split=sample.split,
                expected_codes=sample.anomaly_codes,
                document_sha256=sample.document_sha256,
                upload=upload,
                upload_duration_ms=upload_duration_ms,
                worker_duration_ms=(
                    float(worker_result["duration_ms"])
                    if not upload.duplicate and "duration_ms" in worker_result
                    else None
                ),
                replayed_before_worker=already_processed.get(upload.run_id, False),
                route="REJECT" if upload.duplicate else worker_result.get("route", "NONE"),
                worker_error_type=worker_result.get("error_type"),
                detail=detail,
                provenance=provenance,
            )
        )
    return PipelineReport(
        mode="recorded" if recorded else "live",
        model_class=model_class,
        manifest_sha256=manifest_sha256,
        started_at=started,
        completed_at=utc_now(),
        samples=tuple(records),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--split", choices=("all", "development", "held_out"), default="all")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--recorded", action="store_true")
    parser.add_argument("--model-class", choices=("local-dev", "openai-prod"))
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--project-name")
    parser.add_argument("--worker-timeout", type=int, default=18000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.recorded and args.model_class is not None:
            raise PipelineRunError("Recorded smoke cannot claim a model class")
        if not args.recorded and args.model_class is None:
            raise PipelineRunError("Live evaluation requires --model-class")
        settings = EvalSettings()
        try:
            if args.recorded:
                LiteLLMWorkflowSettings(embed_model="recorded-embed-model")
            else:
                LiteLLMWorkflowSettings()
        except ValidationError:
            raise PipelineRunError(
                "Workflow requires direct LITELLM_API_BASE, LITELLM_MASTER_KEY, "
                "LITELLM_MODEL, and LITELLM_EMBED_MODEL (live only)"
            ) from None
        manifest, manifest_sha = load_manifest()
        selected = select_samples(
            manifest, split=args.split, limit=args.limit, recorded=args.recorded
        )
        documents = preflight_documents(args.dataset, selected, recorded=args.recorded)
        compose = Compose(
            project_name=args.project_name,
            timeout_seconds=args.worker_timeout,
            workers=args.workers,
        )
        if not args.no_start:
            compose.start()
        compose.seed()
        with HTTPPipelineAPI(settings) as api:
            report = run_pipeline(
                manifest,
                manifest_sha,
                selected,
                documents,
                api,
                compose,
                recorded=args.recorded,
                model_class=args.model_class,
            )
        output = args.output or Path(
            "eval/data/runs/golden-v1.0.0-"
            f"{report.model_class or 'recorded'}-"
            f"{report.started_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        write_new_artifact(output, (report.model_dump_json(indent=2) + "\n").encode())
    except (
        PipelineRunError,
        ValidationError,
        httpx.HTTPError,
        OSError,
        ValueError,
    ) as error:
        logger.error("pipeline_eval_failed error_type=%s", type(error).__name__)
        return 1
    logger.info(
        "pipeline_eval_completed mode=%s samples=%d elapsed_seconds=%.3f output=%s",
        report.mode,
        len(report.samples),
        report.elapsed_seconds,
        output,
    )
    return (
        1
        if any(sample.worker_error_type for sample in report.samples)
        or not args.recorded
        and any(sample.replayed_before_worker for sample in report.samples)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
