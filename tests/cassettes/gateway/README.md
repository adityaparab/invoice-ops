# Gateway fixtures

These files contain synthetic HTTP contract examples recorded through `CassetteTransport` with
`httpx2.MockTransport`; no provider was contacted. They do not measure extraction quality.

The cassette key is alias + scenario + prompt version. Its hash pins the guarded HTTP request and
caller response schema, excluding credentials and run/trace identifiers. Replay has no network
fallback. Recording is explicit and create-only. Introduce a new scenario/prompt version when the
contract changes; never overwrite a fixture to hide prompt drift. Only synthetic response content
is permitted here. See [the gateway guide](../../../docs/GATEWAY_CLIENT.md).
