# Synthetic extraction cassettes

These fixtures were authored through the real OpenAI SDK + gateway recording transport using
only `httpx2.MockTransport`. The input is a synthetic blank 16-by-16 PNG; the response contains
synthetic invoice observations. They test contracts and control flow, not extraction accuracy.
No provider was called and no baseline metric is implied.

The scenarios cover success, one schema repair, two malformed outputs, and a refusal. Repair
uses a distinct scenario and the `extract@v1+repair@v1` prompt pin. Request/schema hashes bind
the packaged instructions and source representation. Never edit existing fixtures to make a
prompt change pass; introduce a new version/scenario and deliberately record a new fixture.
