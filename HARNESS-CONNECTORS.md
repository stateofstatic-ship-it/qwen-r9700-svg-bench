# Native harness connectors (diagnostic)
Run `python -m qwen_bench.harness_adapter` as the persistent JSONL adapter.
Preflight options: `{"harness":"codex"}` or `{"harness":"opencode"}`;
Optional `model`, `effort` (Codex effort/OpenCode variant), and `executable` are supported.
Use a harness already configured with the desired provider/server and credentials.
No login, auth copy, provider probe, or model inference happens during preflight.
`endpoint` overrides are rejected: configure the native provider yourself.
Codex needs Responses compatibility; OpenCode `--attach` is not a model-server endpoint.
Only v0.3-portable relative-path prompts are supported, passed unchanged via stdin.
The four C0–C3 calls resume the observed native session ID, never `--last`.
Completion requires an observed successful terminal event, stable ID, and exit zero.
Codex IDs are UUIDs; OpenCode IDs are native `ses_…` identifiers, not UUIDs.
Raw native stdout/stderr remain under `state_dir/native-harness/`, including failures.
Usage and visible tool/message events are normalized; reasoning is not extracted.
Timeout/error stops the outer adapter; POSIX process groups are killed on cleanup.
Existing auth/config/plugins remain inherited; this is not primary isolation.
Codex requests workspace-write/no network, no web search/helpers/image generation.
OpenCode disables sharing and requests denial of task/webfetch/websearch/external paths.
These are harness-owned controls, not independently verified enforcement;
custom tools, native config and agent permissions can affect actual behavior.
Preflight checks version/help only; execution fails closed on incompatible event schemas.
OpenCode completion currently requires `step_finish` with `part.reason == "stop"`.
No real CLI inference trial has been performed; tests use executable fake CLIs.
References: [OpenCode CLI](https://opencode.ai/docs/cli/),
[JSON event source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/cli/cmd/run.ts),
[config](https://opencode.ai/docs/config/), [permissions](https://opencode.ai/docs/permissions/).
Codex flags were checked using installed `codex exec --help` / `exec resume --help`.
