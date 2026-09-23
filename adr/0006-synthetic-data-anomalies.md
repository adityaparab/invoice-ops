# ADR 0006: Synthetic data with injected anomalies and published prevalences

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Architecture (this repo)

## Context

Real vendor invoices, banking details, and ERP extracts can never enter a portfolio
repo. But eval credibility requires a ground-truth backend (for 3-way match) and a
labeled anomaly distribution (for detection metrics). "Too clean" synthetic data would
inflate results and look naive to reviewers.

## Decision

- **All data is synthetic.** Real-document variety comes from the licensed Voxel51
  corpus (for extraction only) plus a Faker-generated ERP with ground truth.
- Anomalies are **scripted and seeded** per a 10-code catalog (DUP_EXACT … STALE_PO)
  with prevalence weights mirroring real AP queues; the weights are published as
  assumptions, not hidden.
- Hard negatives (rotation, skew, stamps, faint print, mixed currencies) inject
  realistic degradation; results are reported per quality tier.
- Generators are versioned and seed-pinned: same seed ⇒ byte-identical datasets.

## Consequences

- Every metric is computed against known ground truth; nothing is hand-labeled ad hoc.
- Reviewers can reproduce the dataset exactly; prevalence assumptions are arguable in
  the open.
- Document-quality realism is bounded by the corpus + augmentation (a stated
  limitation, mitigated by tier reporting).
