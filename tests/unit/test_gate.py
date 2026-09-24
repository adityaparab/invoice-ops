"""Versioned composite score, policy override, and abstention boundary."""

from decimal import Decimal, localcontext

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.unit.policy_support import policy_request

from invoiceops_agent.schemas.gate import (
    CompositeGateConfig,
    CompositeGateResult,
    GateConfig,
    GateOutcome,
)
from invoiceops_agent.tools.gate import (
    evaluate_composite_gate,
    evaluate_provisional_gate,
    normalized_match_delta,
)
from invoiceops_agent.tools.policy import evaluate_policy

pytestmark = pytest.mark.unit


def test_equal_threshold_auto_approves_and_preserves_all_evidence() -> None:
    request = policy_request()
    policy = evaluate_policy(request)
    result = evaluate_composite_gate(request, policy, CompositeGateConfig(threshold=Decimal(1)))
    assert result.score == Decimal(1)
    assert result.minimum_field_confidence == Decimal(1)
    assert result.normalized_match_delta == Decimal(0)
    assert result.policy_severity_term == Decimal(1)
    assert result.route == "AUTO_APPROVE"
    assert result.reason == "ELIGIBLE"
    assert CompositeGateResult.model_validate(result.model_dump(mode="json")) == result
    assert TypeAdapter(GateOutcome).validate_python(result.model_dump(mode="json")) == result


def test_below_threshold_abstains_even_when_policy_is_eligible() -> None:
    source = policy_request()
    extraction = source.extraction.model_copy(
        update={
            "vendor_name": source.extraction.vendor_name.model_copy(
                update={"confidence": Decimal("0.9")}
            )
        }
    )
    request = policy_request(extraction=extraction)
    policy = evaluate_policy(request)
    assert policy.status == "AUTO_APPROVE_ELIGIBLE"
    boundary = evaluate_composite_gate(
        request, policy, CompositeGateConfig(threshold=Decimal("0.95"))
    )
    assert boundary.score == Decimal("0.95")
    assert boundary.route == "AUTO_APPROVE"
    abstained = evaluate_composite_gate(
        request, policy, CompositeGateConfig(threshold=Decimal("0.950001"))
    )
    assert abstained.route == "REVIEW"
    assert abstained.reason == "BELOW_THRESHOLD"
    just_below = source.extraction.model_copy(
        update={
            "vendor_name": source.extraction.vendor_name.model_copy(
                update={"confidence": Decimal("0.899999")}
            )
        }
    )
    narrow_request = policy_request(extraction=just_below)
    narrow = evaluate_composite_gate(narrow_request, evaluate_policy(narrow_request))
    assert narrow.score == Decimal("0.9499995")
    assert narrow.route == "REVIEW"


def test_policy_block_and_operator_disable_override_high_score() -> None:
    blocked_request = policy_request(exact_duplicate=True)
    blocked = evaluate_composite_gate(blocked_request, evaluate_policy(blocked_request))
    assert blocked.reason == "POLICY"
    assert blocked.route == "REVIEW"
    eligible_request = policy_request()
    disabled = evaluate_composite_gate(
        eligible_request,
        evaluate_policy(eligible_request),
        CompositeGateConfig(auto_approval_enabled=False),
    )
    assert disabled.score == Decimal(1)
    assert disabled.reason == "AUTO_DISABLED"
    assert disabled.route == "REVIEW"


def test_match_delta_is_bounded_and_unknown_comparison_abstains() -> None:
    request = policy_request()
    check = request.match.numeric_checks[0]
    changed = check.model_copy(
        update={
            "expected": Decimal(100),
            "actual": Decimal(90),
            "difference": Decimal(-10),
            "status": "MISMATCH",
        }
    )
    match = request.match.model_copy(update={"numeric_checks": (changed,)})
    assert normalized_match_delta(match, Decimal(1)) == Decimal("0.1")
    tiny = changed.model_copy(
        update={
            "expected": Decimal(10_000_000),
            "actual": Decimal(9_999_999),
            "difference": Decimal(-1),
        }
    )
    assert normalized_match_delta(
        match.model_copy(update={"numeric_checks": (tiny,)}), Decimal(1)
    ) == Decimal("0.0000001")
    unknown = changed.model_copy(update={"actual": None, "difference": None, "status": "UNKNOWN"})
    assert (
        normalized_match_delta(match.model_copy(update={"numeric_checks": (unknown,)}), Decimal(1))
        == 1
    )
    assert (
        normalized_match_delta(
            match.model_copy(update={"snapshot_found": False, "numeric_checks": ()}), Decimal(1)
        )
        == 1
    )


def test_versioned_weights_reject_floats_and_bad_sum() -> None:
    with pytest.raises(ValidationError, match="sum to one"):
        CompositeGateConfig(weight_field=Decimal("0.4"))
    with pytest.raises(ValidationError, match="floats"):
        CompositeGateConfig.model_validate({"threshold": 0.95})


def test_legacy_provisional_gate_evidence_remains_readable() -> None:
    request = policy_request()
    legacy = evaluate_provisional_gate(
        request.extraction, evaluate_policy(request), GateConfig(auto_approval_enabled=True)
    )
    assert TypeAdapter(GateOutcome).validate_python(legacy.model_dump(mode="json")) == legacy


def test_gate_rejects_policy_from_different_inputs_and_tampered_route() -> None:
    request = policy_request()
    policy = evaluate_policy(request)
    with pytest.raises(ValueError, match="Policy evidence"):
        evaluate_composite_gate(policy_request(exact_duplicate=True), policy)
    result = evaluate_composite_gate(request, policy)
    with pytest.raises(ValidationError, match="route"):
        CompositeGateResult.model_validate({**result.model_dump(mode="json"), "route": "REVIEW"})


def test_score_is_independent_of_process_decimal_precision() -> None:
    request = policy_request()
    policy = evaluate_policy(request)
    expected = evaluate_composite_gate(request, policy)
    with localcontext() as context:
        context.prec = 6
        actual = evaluate_composite_gate(request, policy)
    assert actual == expected
