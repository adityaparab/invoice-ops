# Gateway fixtures

These files contain synthetic HTTP contract examples recorded through `CassetteTransport` with
`httpx2.MockTransport`; no provider was contacted. They do not measure extraction quality.

The cassette key is alias + scenario + prompt version. Its hash pins the guarded HTTP request and
caller response schema, excluding credentials and run/trace identifiers. Replay has no network
fallback. Recording is explicit and create-only. Introduce a new scenario/prompt version when the
contract changes; never overwrite a fixture to hide prompt drift. Only synthetic response content
is permitted here. See [the gateway guide](../../../docs/GATEWAY_CLIENT.md).

New recordings use format version 2 with one ordered outcome sequence per logical gateway call.
Retries consume successive outcomes; every new replay call starts at the first outcome. Existing
single-response fixtures remain valid without edits. Recording publishes the complete sequence
atomically and reserves its identity before upstream I/O, including concurrent recorders.
