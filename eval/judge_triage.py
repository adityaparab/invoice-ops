"""Run the versioned triage rubric through the direct LiteLLM gateway."""

import argparse
import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from eval.runners.schema import PipelineReport, RunRecord
from invoiceops_agent.agents.eval_judge import EvalJudgeAgent, JudgeGateway
from invoiceops_agent.agents.eval_judge_settings import LiteLLMJudgeSettings
from invoiceops_agent.artifacts import write_new_artifact
from invoiceops_agent.gateway_client import GatewayClient, GatewayError
from invoiceops_agent.schemas.eval_judge import TriageJudgeRequest, TriageJudgeResult
from invoiceops_agent.schemas.exceptions import ExceptionCode
from invoiceops_agent.schemas.triage import TriageDraft, TriageEvidence

logger = logging.getLogger(__name__)
CODE_LIST = TypeAdapter(tuple[ExceptionCode, ...])


class TriageJudgeReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["triage-judge-report@v1"] = "triage-judge-report@v1"
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scored_at: AwareDatetime
    eligible_count: int = Field(ge=0, le=500)
    results: tuple[TriageJudgeResult, ...]


def triage_requests(pipeline: PipelineReport) -> tuple[TriageJudgeRequest, ...]:
    output = []
    for record in pipeline.samples:
        request = _triage_request(record)
        if request is not None:
            output.append(request)
    return tuple(output)


def _triage_request(record: RunRecord) -> TriageJudgeRequest | None:
    payload = record.detail.evidence.get("triage.prepared")
    if payload is None:
        return None
    raw_triage = payload.get("triage")
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_triage, dict) or not isinstance(raw_evidence, dict):
        raise ValueError("Audited triage evidence is malformed")
    if raw_triage.get("status") != "DRAFT":
        return None
    codes = payload.get("codes")
    if not isinstance(codes, list):
        raise ValueError("Audited triage codes are malformed")
    observed = CODE_LIST.validate_python(codes)
    trace_id = hashlib.sha256(f"{record.upload.run_id}:eval-judge".encode()).hexdigest()[:32]
    return TriageJudgeRequest(
        sample_id=record.sample_id,
        run_id=record.upload.run_id,
        trace_id=trace_id,
        evidence=TriageEvidence.model_validate(raw_evidence),
        draft=TriageDraft.model_validate(raw_triage.get("draft")),
        expected_codes=record.expected_codes,
        observed_codes=observed,
    )


async def judge_pipeline(
    pipeline: PipelineReport,
    source_report_sha256: str,
    gateway: JudgeGateway,
    *,
    limit: int | None = None,
) -> TriageJudgeReport:
    requests = triage_requests(pipeline)
    eligible_count = len(requests)
    if limit is not None:
        if not 1 <= limit <= 500:
            raise ValueError("Judge limit must be from one to 500")
        requests = requests[:limit]
    agent = EvalJudgeAgent(gateway)
    semaphore = asyncio.Semaphore(4)

    async def bounded(request: TriageJudgeRequest) -> TriageJudgeResult:
        async with semaphore:
            return await agent.judge(request)

    results = await asyncio.gather(*(bounded(request) for request in requests))
    return TriageJudgeReport(
        manifest_sha256=pipeline.manifest_sha256,
        source_report_sha256=source_report_sha256,
        scored_at=datetime.now(UTC),
        eligible_count=eligible_count,
        results=tuple(results),
    )


async def _run(input_path: Path, output_path: Path, limit: int | None) -> None:
    raw = await asyncio.to_thread(input_path.read_bytes)
    pipeline = PipelineReport.model_validate_json(raw)
    settings = await asyncio.to_thread(LiteLLMJudgeSettings)
    async with GatewayClient(settings.gateway_settings()) as gateway:
        report = await judge_pipeline(
            pipeline,
            hashlib.sha256(raw).hexdigest(),
            gateway,
            limit=limit,
        )
    await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(
        write_new_artifact, output_path, (report.model_dump_json(indent=2) + "\n").encode()
    )
    logger.info("triage_judge_written output=%s scored=%d", output_path, len(report.results))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        asyncio.run(_run(args.input, args.output, args.limit))
    except (GatewayError, ValidationError, ValueError, OSError) as error:
        logger.error("triage_judge_failed error_type=%s", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
