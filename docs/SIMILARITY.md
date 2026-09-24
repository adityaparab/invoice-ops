# Near-duplicate detection v2

Step 2.4 prepares extracted invoice fields as a stable text summary, excluding
bank and tax identifiers. The near-duplicate agent requests one embedding through
the existing LiteLLM gateway client. It reads only `LITELLM_API_BASE`,
`LITELLM_MASTER_KEY`, and the new `LITELLM_EMBED_MODEL` model-name variable;
it does not load a LiteLLM proxy YAML configuration. The configured route must
return **384 dimensions**. A nonfinite, zero, or incorrectly sized vector fails
before a database write.

The repository compares cosine distance with pgvector's existing
`invoices.embedding` column. The query computes exact distance across candidates
with the same model pin, extracted invoice number, and PO number. Both identities
must be present; similar templates with different identifiers are not duplicate
evidence. The existing HNSW index remains available for future large-corpus
tuning. Its default threshold is cosine similarity **0.95**
under policy `near-duplicate@v2`. A found candidate receives `NEAR_DUPLICATE`;
otherwise the result is `NO_MATCH`. This is review evidence, not an automatic
rejection. The exact-content-hash ingest check remains independent.

The embedding, extracted invoice/PO identifiers, and its model route pin are
stored together. Search considers only vectors with the same model pin, so
different embedding models never share a similarity space. Migration 0003
labels existing vectors `__legacy_unpinned__`;
new model routes cannot compare against those records. A model-scoped transaction
lock serializes the nearest-neighbor search and write, including concurrent
invoices. The ledger's `similarity.completed` event commits in that same
transaction and includes the model, summary, policy, extraction, and vector
fingerprints without raw summary text or vector coordinates. An audit failure
rolls back the embedding write.

The model route is a configured pin, and a LiteLLM alias can change its underlying
model independently. Operators must change `LITELLM_EMBED_MODEL` when that route
changes. The detector is an integration point until the full graph is wired in
step 2.6. Local tests use fixed synthetic vectors and never contact a model.
