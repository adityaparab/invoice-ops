"""Pure schema, net-line arithmetic, subtotal, and per-line tax validation."""

from decimal import ROUND_HALF_UP, Context, Decimal, localcontext

from invoiceops_agent.schemas.extraction import InvoiceExtraction, InvoiceLineItem
from invoiceops_agent.schemas.validation import (
    CurrencyRule,
    IssueCode,
    ValidationConfig,
    ValidationIssue,
    ValidationResult,
    model_digest,
)

# Extraction limits every number to <=18 digits and every invoice to <=500 lines.
# Products need <=36 digits and aggregation adds <=3; this context has ample exact headroom.
ARITHMETIC_PRECISION = 60


def validate_invoice(
    extraction: InvoiceExtraction, config: ValidationConfig | None = None
) -> ValidationResult:
    """Return business failures as evidence; never call infrastructure or retry a decision."""
    policy = config if config is not None else ValidationConfig()
    # A fresh Context also isolates traps/exponent limits from caller configuration.
    with localcontext(Context(prec=ARITHMETIC_PRECISION, rounding=ROUND_HALF_UP)):
        issues = _validate(extraction, policy)
    return ValidationResult(
        status="FAIL" if issues else "PASS",
        input_sha256=model_digest(extraction),
        config_sha256=model_digest(policy),
        config=policy,
        issues=tuple(issues),
    )


def _compare(
    issues: list[ValidationIssue],
    code: IssueCode,
    field: str,
    expected: Decimal,
    actual: Decimal,
    tolerance: Decimal,
    *,
    line_number: int | None = None,
) -> None:
    difference = actual - expected
    if abs(difference) > tolerance:
        issues.append(
            ValidationIssue(
                code=code,
                field=field,
                line_number=line_number,
                expected=expected,
                actual=actual,
                difference=difference,
                tolerance=tolerance,
            )
        )


def _required(
    issues: list[ValidationIssue],
    fields: tuple[tuple[str, object], ...],
    *,
    line_number: int | None = None,
) -> None:
    for field, value in fields:
        if value is None or isinstance(value, str) and not value.strip():
            issues.append(
                ValidationIssue(code="REQUIRED_FIELD", field=field, line_number=line_number)
            )


def _nonnegative(
    issues: list[ValidationIssue],
    field: str,
    value: Decimal | None,
    *,
    line_number: int | None = None,
) -> Decimal | None:
    if value is not None and value < 0:
        issues.append(
            ValidationIssue(
                code="NEGATIVE_VALUE", field=field, line_number=line_number, actual=value
            )
        )
        return None
    return value


def _line_values(
    issues: list[ValidationIssue],
    line: InvoiceLineItem,
    number: int,
    rule: CurrencyRule | None,
) -> tuple[Decimal | None, Decimal | None]:
    _required(
        issues,
        (
            ("description", line.description.value),
            ("quantity", line.quantity.value),
            ("unit_price", line.unit_price.value),
            ("tax_rate", line.tax_rate.value),
            ("line_total", line.line_total.value),
        ),
        line_number=number,
    )
    quantity, rate = line.quantity.value, line.tax_rate.value
    price = _nonnegative(issues, "unit_price", line.unit_price.value, line_number=number)
    net = _nonnegative(issues, "line_total", line.line_total.value, line_number=number)
    if quantity is not None and quantity <= 0:
        issues.append(
            ValidationIssue(
                code="NONPOSITIVE_QUANTITY", field="quantity", line_number=number, actual=quantity
            )
        )
        quantity = None
    if rate is not None and not Decimal(0) <= rate <= Decimal(1):
        issues.append(
            ValidationIssue(
                code="TAX_RATE_OUT_OF_RANGE", field="tax_rate", line_number=number, actual=rate
            )
        )
        rate = None
    if rule is None:
        return net, None
    quantum = Decimal((0, (1,), -rule.decimal_places))
    if quantity is not None and price is not None and net is not None:
        _compare(
            issues,
            "LINE_MATH_MISMATCH",
            "line_total",
            (quantity * price).quantize(quantum),
            net,
            rule.absolute_tolerance,
            line_number=number,
        )
    tax = (net * rate).quantize(quantum) if net is not None and rate is not None else None
    return net, tax


def _complete_sum(values: list[Decimal | None]) -> Decimal | None:
    if not values or any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal(0))


def _validate(extraction: InvoiceExtraction, config: ValidationConfig) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    _required(
        issues,
        (
            ("vendor_name", extraction.vendor_name.value),
            ("invoice_number", extraction.invoice_number.value),
            ("currency", extraction.currency.value),
            ("invoice_date", extraction.invoice_date.value),
            ("subtotal", extraction.subtotal.value),
            ("tax_amount", extraction.tax_amount.value),
            ("total_amount", extraction.total_amount.value),
        ),
    )
    if not extraction.line_items:
        issues.append(ValidationIssue(code="EMPTY_LINES", field="line_items"))
    currency = extraction.currency.value
    rule = next((rule for rule in config.currencies if rule.currency == currency), None)
    if currency is not None and rule is None:
        issues.append(ValidationIssue(code="UNSUPPORTED_CURRENCY", field="currency"))
    subtotal = _nonnegative(issues, "subtotal", extraction.subtotal.value)
    tax = _nonnegative(issues, "tax_amount", extraction.tax_amount.value)
    total = _nonnegative(issues, "total_amount", extraction.total_amount.value)
    values = [
        _line_values(issues, line, number, rule)
        for number, line in enumerate(extraction.line_items, start=1)
    ]
    if rule is None:
        return issues
    line_subtotal = _complete_sum([net for net, _ in values])
    line_tax = _complete_sum([tax for _, tax in values])
    if line_subtotal is not None and subtotal is not None:
        _compare(
            issues,
            "SUBTOTAL_MISMATCH",
            "subtotal",
            line_subtotal,
            subtotal,
            rule.absolute_tolerance,
        )
    if line_tax is not None and tax is not None:
        _compare(issues, "TAX_MISMATCH", "tax_amount", line_tax, tax, rule.absolute_tolerance)
    if subtotal is not None and tax is not None and total is not None:
        _compare(
            issues, "TOTAL_MISMATCH", "total_amount", subtotal + tax, total, rule.absolute_tolerance
        )
    return issues
