# Synthetic extraction cassettes

These fixtures were authored through the real OpenAI SDK + gateway recording transport using
only `httpx2.MockTransport`. The input is a synthetic blank 16-by-16 PNG; the response contains
synthetic invoice observations. They test contracts and control flow, not extraction accuracy.
No provider was called and no baseline metric is implied.

The scenarios cover success, one schema repair, two malformed outputs, and a refusal. Repair
uses a distinct scenario and the `extract@v1+repair@v1` prompt pin. Request/schema hashes bind
the packaged instructions and source representation. Never edit existing fixtures to make a
prompt change pass; introduce a new version/scenario and deliberately record a new fixture.

The `v2/` fixtures replay the same synthetic outcomes against `extract@v2` and
`extract@v2+repair@v1`. They were newly recorded through the SDK and cassette
transport after the character-accurate bank transcription instruction was added.
The historical v1 cassettes remain unchanged for provenance.

The `v3/` fixtures pin `extract@v3` and `extract@v3+repair@v1`, which instruct
the model to transcribe printed line totals even when arithmetic disagrees.
The v1 and v2 fixtures remain unchanged.

The `v4/` fixtures pin `extract@v4` and `extract@v4+repair@v1`, which additionally
instruct the model to transcribe printed tax rates despite inconsistent tax totals.
Earlier fixtures remain unchanged.
