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

## DeepSeek Harness

Select **DeepSeek Harness → model server** in the browser launcher, discover the loaded model, and select a saved DeepSeek profile. Or:

```bash
python benchmark.py --harness dsh --server http://127.0.0.1:8000/v1 --profile profiles/dsh-gestalt-standard.json
```

The connector currently targets installed `@deepseek-ai/dsh` **0.1.5-rc.2** and rejects unverified versions. It uses that runtime’s native agent loop and tools. A small Cordis runner retains one real native agent/session through all four exact prompts; it does not invoke the stock one-shot headless runner four times. Settings, profile and native session state are isolated under the new run's private adapter directory. No package installation or global settings change is performed.

The supplied Gestalt standard profile requests temperature 0.8, top-p 0.95, top-k 20, min-p 0, presence penalty 0, repetition penalty 1, thinking/history preservation, xhigh effort and 65,536 output tokens per request. It does not install the custom xhigh-verify Jinja template: configure that on your server, confirm its rendered instructions, and retain its hash as runtime evidence. A filename containing xhigh alone does not prove the effort setting.

An authenticated loopback streaming proxy applies and records these explicit sampling overrides while preserving native messages and tools. It forwards streamed bytes immediately; it does not replace the native harness with the built-in tool loop. Per-request timings describe proxy-to-upstream observations; harness orchestration/tool time remains in section wall time. See [telemetry definitions](TELEMETRY.md).

Benchmark web/helper/title-generation plugins are disabled. Native workspace-write and approval-never policies are requested; they are not complete read/network confinement. Keep this diagnostic native-tool condition separate from the built-in restricted text-tool profile. Raw native sessions and request traces may contain private information and are not uploaded automatically.
