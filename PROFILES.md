# Profiles and meaningful comparisons

A profile is a versioned JSON file selected in the launcher or through `--profile FILE`. The model/server selection remains a run choice. `profiles/baseline.json` and `profiles/example-careful.json` demonstrate a fixed baseline and one instruction-change experiment; neither is a claimed optimal profile.

Fields: `format: "svg-bench-profile/1"`, `name`, `harness` (`direct`, `codex`, `opencode`), `protocol: "v0.3-portable"`, `settings`, optional `agents_md` UTF-8 text or `agents_md_file`, `external_requirements`, and `note`.

Supported direct settings: `temperature`, `top_p`, `seed`, `max_tokens`, `max_requests`, `max_tool_steps`, `effort` (sent as `reasoning_effort`), and `request_options` for runtime-specific nonreserved request parameters. Invalid/unsupported API parameters cause a retained error; they are never silently retried with a different profile. A server may still ignore accepted parameters: sent payload is not runtime attestation.

Native connectors currently apply `effort` through Codex reasoning effort or OpenCode variant. Configure runtime sampling/providers/plugins in the harness normally; record them as external requirements. The benchmark never installs plugins, changes a global config or restarts your model server behind your back.

AGENTS.md is copied into the fresh workspace. The built-in harness reads it into its initial system message and prevents its file tools from modifying it. Native harnesses load it under their own instruction rules; their preflight cannot independently prove that all instructions were honored. Exact profile/instruction hashes are preserved.

Examples of `external_requirements` keys: `jinja_template_sha256`, `runtime_version`, `quantization`, `plugin_versions`, `agent_policy`, `context_limit`, `server_arguments`. These are **unverified external requirements**, not applied settings. Changing a server-owned Jinja template usually requires configuring the running server; the profile does not pretend otherwise. Freeze its actual bytes/settings and retain runtime evidence for reproducible comparisons.

Each run retains the profile definition, source hash, instruction hash, requested settings and a `profile-receipt.json`. Effective runtime settings remain unverified unless supported by adapter/runtime evidence. Do not call a config field an enforced control or a declared plugin an observed plugin call.

Keep a locked baseline and separately labeled experimental profiles. Agent/plugin-enabled experiments differ from the original no-helper/no-plugin primary controls. A comparison measures the whole configured system unless other variables are held fixed.

Use repeated fresh trajectories and publish all attempted outcomes when authorized. Develop profiles on one task set and evaluate on held-out repository work. Otherwise improvement may only be overfitting to this scene, rubric, renderer or judge. Report transfer task completion, regressions, tool behavior and resource cost separately; do not turn ordinal artistic scores into a misleading universal score.

Performance settings for direct profiles: `stream` (default true), `stream_usage` (default true), and `runtime_metrics` (`auto`, `vllm`, `off`). See [measurement definitions and limits](TELEMETRY.md). These settings change observability and belong in the comparison profile.
