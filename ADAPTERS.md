# External adapter contract — svg-bench/1

An adapter is one persistent process. It reads JSONL requests from stdin, emits JSONL events to stdout, and puts diagnostics on stderr. One process serves all four turns; a native connector may resume one real conversation across native subprocesses. Stable IDs alone are not proof of preserved context.

Config: `{"command":["{python}","{config_dir}/adapter.py"],"deployment":{"model":"exact ID","runtime":"version","quantization":"format"},"options":{}}`. Unknown metadata stays unknown; do not include credentials. No shell interpolation is used.

First request: `type: preflight`, `checkpoint: null`, `protocol: svg-bench/1`, `benchmark_protocol`, fresh host `workspace`, private `state_dir`, candidate path contract and public `options`. Preflight must not perform inference. Reply `type: preflight.completed`, `checkpoint: null`, `status: passed`, `same_session_supported: true`, and truthful `controls`/capability evidence. Metadata-only checks are not tool-negotiation or security verification.

Turn request: `type: turn`, `checkpoint: C0…C3`, `prompt_base64`, `prompt_sha256`, prior `session_id` (null for C0), and `timeout_seconds`. Decode/verify bytes and deliver them unchanged. Preserve actual conversation/tool state. Do not send future prompts early. Relative paths belong to v0.3-portable; original v0.2 requires true candidate-visible /work mapping and cannot be silently rewritten.

Events are correlated with the current checkpoint. Optional types: `turn.started`, `tool.started`, `tool.completed`, `assistant.message`, `usage`, `adapter.event`. Include raw counters with source/scope and call IDs where available; missing is null. Do not extract hidden reasoning. Tool errors are evidence, not automatically poor competence if repaired.

Required success: `{"type":"turn.completed","checkpoint":"C0","status":"completed","session_id":"stable-real-session","usage":null}`. It must follow an actual native/model terminal event, not inferred prose. The adapter must quiesce workspace writes at this boundary until the next request. Recorder/checkpoints are outside the candidate workspace.

Failure: `type: error`, `turn.failed` or `preflight.failed`, checkpoint and message. EOF, malformed output, mismatched checkpoint/session, nonzero exit, logging cap, timeout or interruption prevent complete-trajectory success. Never replace the failed attempt silently.

Finally request `type: shutdown`, `checkpoint: null`; emit `type: run.closed`, `checkpoint: null`, then exit 0. Late errors/output fail clean shutdown. The reference fixture shows the protocol without a model.

Runner owns sequencing, bounded I/O/deadlines, append-only event recording, snapshots/hashes and independent static checks. Adapter owns actual model/session continuity, capability inventory, candidate filesystem/network boundaries, tool logging and descendant containment. Generic execution is not a sandbox. External harness behavior is diagnostic unless controls are separately established.

Current caps: 1 MiB JSONL records, 64 MiB per adapter stdout/stderr stream, 8 MiB captured SVG; built-in model harness has separately recorded request/tool/file budgets. Keep changed caps/tool profiles separate. Standard-library process cleanup is platform-dependent; no complete adversarial containment claim.
