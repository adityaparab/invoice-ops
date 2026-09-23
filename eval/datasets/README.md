# Pinned Voxel51 development subset

Step 1.8 prepares 32 annotated synthetic invoice images for the first extraction baseline. It does
not run models, produce extraction scores, or define the later held-out golden dataset.

```bash
uv sync --locked
uv run python -m eval.datasets.prepare_voxel51 --output eval/data/voxel51-v1
```

The downloader uses public HTTPS URLs without credentials. Each download has a byte bound, a
30-second read timeout, and a 60-second elapsed budget checked between chunks. A stalled final read
can take up to another read timeout before failure. Failed preparation exits nonzero without
publishing a complete manifest. Re-running verifies existing bytes and never replaces different
files; choose another output directory to change settings. Each output is published atomically without overwriting existing files; the complete manifest is
published last. Files already produced by an interrupted run can be reused when their bytes agree.

## Source and integrity

The [upstream dataset card](https://huggingface.co/datasets/Voxel51/high-quality-invoice-images-for-ocr)
describes synthetic invoices and declares an ODbL license. It credits Osama Hosam Abdellatif's
[original Kaggle corpus](https://www.kaggle.com/datasets/osamahosamabdellatif/high-quality-invoice-images-for-ocr)
and Harpreet Sahota's FiftyOne port. Preserve attribution with any redistributed dataset artifacts.
Raw images, normalized images, and annotations remain in ignored `eval/data/`; Git contains only
code, tests, and the small [preparation report](../reports/voxel51-v1.json).

The exact source revision is `d21f03cfeea2b330e15a229883c66d7ebece8e69`; `samples.json` must match
SHA-256 `79cb36846bf28f41636d795d2f85fd8b7ef99a8e9807f6ddc8e6f0d7c883ae7d` before selection.
That revision contains 8,106 records and 1,414 annotated records, rather than the card's advertised
8,181 / 1,489. One image path appears twice with identical annotations: stable identity ordering
keeps one copy, leaving 1,413 unique annotated source images. Conflicting labels or duplicate record
identities fail preparation.

Selection sorts the unique annotated records by SHA-256 of `seed:sample_id`, using seed 20260827.
This avoids dependence on input order or random-library internals. Counts and seed are configurable;
the default report records the exact 32 identities, source hashes, prepared hashes, and quality
measurements. Source paths cannot escape the pinned repository. All future held-out splits must
exclude these baseline-development source paths and hashes to prevent overlap.

## Normalization and quality tiers

Preparation validates a JPEG signature through Pillow's decoder, limits images to 10 MB and
30 million pixels, applies EXIF orientation, converts to RGB, and downsizes the longest edge to at
most 2,400 pixels with Lanczos. Fresh PNGs retain no source EXIF or comments. The manifest records
the pipeline version, settings, pinned Pillow version, zlib version, source/output checksums, and
original annotations. Reproduction uses the recorded toolchain; byte differences fail existing-file
verification rather than silently replacing artifacts.

`quality-heuristic-v1` assesses the oriented source before resizing. A grayscale thumbnail fits
inside 512 × 512 pixels. Contrast is the difference between the 0.5th and 99.5th percentile grayscale
levels. Mean absolute horizontal/vertical neighbor difference, multiplied by 100 and truncated to an
integer, is the gradient proxy. Tier A requires a short edge of at least 1,200 pixels, contrast 128,
and gradient 300. Tier B requires 700 pixels, contrast 64, and gradient 100; other images are C.
These reproducible proxies are not human readability labels or measured OCR accuracy.

All 32 selected source images are tier A. B and C coverage is zero; a baseline must report those
metrics as unavailable. Versioned hard-negative augmentation belongs to the later golden-set step.

Upstream annotation strings are retained verbatim in structure. Missing currency, unit-price, IBAN,
or other ground truth is not inferred. In particular, upstream `subtotal.total` and line
`total_price` retain their source names: a baseline must state its mapping and eligible fields, and
must not assume unlabeled fields are negative examples. Label curation remains separate from model
quality measurement.

Offline tests cover seeded selection, duplicate/conflicting sources, checksum failure, path bounds,
size/time limits, orientation/metadata removal, deterministic outputs, quality tiers, and preservation
of existing files. No unit test downloads data or invokes a model.
