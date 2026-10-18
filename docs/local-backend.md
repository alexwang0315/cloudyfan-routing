# Local open-source first-answer adapter

`LocalBackend(http, root, endpoint=..., model_version=..., quantization=...,
engine=..., execution_location=..., no_model_fee_evidence=...)` is injected as
`Core(..., local_backend=backend)`. Only the `open_source` profile may use
`backend: local`; its pinned model version, quantization, engine and execution
location must exactly match the backend descriptor. The profile uses
`effort: none`, `external_score: null`, and the same `accepted_models` contract as
cloud configurations. Local profiles do not need an OpenRouter provider.

The explicit subset is loopback HTTP `POST /v1/chat/completions`, nonstreaming
text system/user/assistant messages, exact model identifier, and `max_tokens`.
Request tools, media, reasoning-effort settings, streaming, nontext responses,
multiple choices and generated tool calls are unsupported and fail closed.
Redirects are disabled. The endpoint must be localhost, 127.0.0.1 or ::1.
No claim of real llama.cpp/Ollama interoperability is made by fixture tests.

Location is operator configuration `home:<site-id>` or `company:<site-id>`;
a loopback HTTP endpoint does not independently attest where a tunneled process
or its upstream runs. Operators must provide trusted engine/location provenance.
`execution_policy` owns any stricter egress guarantees. Engine and quantization
are deployment attestations, not fields the text-completions protocol verifies.
A non-null external score requires a separate approved
`quantization_qualification` object with matching `model_version`, `quantization`,
`engine`, and an `evidence` reference. Base-model scores cannot silently qualify a
quantized model. These checks do not grant real-model qualification.

The local backend uses its own private receipt root. Every intent and original
response is saved before parsing; run/call IDs, configuration and elapsed time
are preserved. A completed request is reused, and interrupted or malformed
requests are never automatically resent. Local receipts have
`execution_kind: local`, a `local_call_id`, and **no OpenRouter generation ID or
provider**. They are never reconciled against OpenRouter. `ledger.summarize` can
read the local root independently; add its rows to the complete workflow ledger,
not merely completed results. Unknown/incomplete local execution still stops
resume; it is not silently promoted to a failed model answer.

`no_model_fee_evidence` is a required operator attestation identifying a locally
owned no-charge execution contract. Only that contract establishes zero model
API fee; it says nothing about electricity, host depreciation, labor or usage.
Missing token usage stays unknown even with known zero API fee. Cloud-hosted open
weights remain billed normally; the existing hypothetical zero-model-fee scenario
stays separate. Cloud unknown-charge, provider and generation-identity guards
remain unchanged. A public local answer failure enters the existing binary
low/mid, then optional mid/high decision path and one final generation stage.

All checks in `tests/test_local_backend.py` use synthetic `httpx.MockTransport`
responses through `Core.run`. No model was downloaded, no engine started, and no
hardware capacity, local quality or physical privacy performance was measured.
