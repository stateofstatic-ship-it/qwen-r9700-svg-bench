# Small command-line interface

Open the local launcher: `python benchmark.py`

No-model demo: `python benchmark.py --demo --no-open`

Direct server: `python benchmark.py --endpoint http://127.0.0.1:8000/v1 --model YOUR_SERVED_ID`

Configured harness: `python benchmark.py --harness codex` or `python benchmark.py --harness opencode`

Profile: add `--profile profiles/example-careful.json` to a direct-server run. This is an example, not a proven improvement. Use a profile whose harness matches the selected connector.

Other adapter: `python benchmark.py --adapter path/to/adapter.json`; command is a JSON argv list, never a shell string. `{python}`, `{kit}` and `{config_dir}` are supported path substitutions.

Fallback without an integration: `python benchmark.py --manual --label "my model + harness"`. Open the fresh workspace in your existing agent, paste the four prompts as prompted and save `output/scene.svg` at each step. Same-conversation and prompt delivery are user-confirmed, not independently observed. Time includes human handling.

Use `--runs-dir DIRECTORY` to choose storage; each attempt gets an exclusive new directory. `--turn-seconds 900` sets each automated section's wall deadline; manual timing is observational. `--no-open` suppresses browser opening.

API credentials: set `SVG_BENCH_API_KEY` in the launcher's environment if the direct server requires a key. Never put credentials in profiles, adapter JSON, endpoint URLs or command arguments. Native harnesses keep their own authentication configuration; the kit does not copy credentials.

A failed section stops the automated trajectory and retains logs/artifacts. A missing/invalid SVG still gets diagnostic checks. Four completed calls are distinct from four passing artifacts. Unknown usage stays unavailable. Read `status.json`, `events.jsonl`, each `checkpoints/C*/result.json` and `manifest.json` for analysis.

The default local web UI binds only to 127.0.0.1 and requires a per-launch token for actions. Leave the launcher running while a benchmark is active; Stop requests cleanup and preserves partial results. External harness descendant containment is harness/platform-owned, not a security certification.
