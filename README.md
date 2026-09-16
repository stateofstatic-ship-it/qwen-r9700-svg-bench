# Qwen × R9700 SVG benchmark

**Load your model. Start the benchmark. Open the report.** Any model family can take the test; Qwen/R9700 is the illustration subject, not a restriction on the model being tested.

## Start

Download and extract the [source ZIP](https://github.com/stateofstatic-ship-it/qwen-r9700-svg-bench/archive/refs/heads/main.zip), or clone this repository. Open its folder, then:

1. Start your model server as usual (for example vLLM or llama.cpp), or configure Codex/OpenCode as usual.
2. With **Python 3.11 or newer**, run `python benchmark.py` (`python3` on some systems). No pip packages are required. Windows users can double-click `start.bat`.
3. In the local browser page, choose the built-in harness and your server address, find the loaded model, then **Run benchmark**. Or select an installed Codex/OpenCode harness. Optional: select a saved profile.

The benchmark creates a fresh workspace, runs four sections in the same conversation, keeps each SVG and log, and opens an offline full/half-size report. One click launches one trajectory—not a sweep. Stop keeps the partial result.

Try the plumbing without using a model: `python benchmark.py --demo`. Or choose **Demo only** in the launcher. Synthetic output is clearly labeled and is not a model result.

## What works now

- Built-in OpenAI-compatible Chat Completions client with restricted file/structural-check tools; **text-only diagnostic profile**, not the native visual-agent primary track. API metadata discovery is no-inference; it cannot prove tool-calling compatibility. [vLLM setup](https://docs.vllm.ai/en/latest/features/tool_calling/) and [llama.cpp setup](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md) may require model-specific tool templates/parsers.
- Codex CLI / OpenCode connectors use each installed harness's configured provider. They do not automatically make a chat-only endpoint compatible with a different harness API. See [connectors](HARNESS-CONNECTORS.md).
- [Saved profiles](PROFILES.md) freeze instructions, requested settings and external requirements. Confirmed application is separate from requested configuration.
- [Manual fallback](USAGE.md) and an [adapter contract](ADAPTERS.md) for other harnesses. No universal zero-configuration harness claim.

## Read the result

Machine checks cover file/format constraints, not good art. Look at the four full/half previews and the short visual checklist; record uncertain rows as not observed. Tool evidence and usage are logged when available, never invented. Browser previews are not fixed-renderer screenshots or proof of candidate inspection.

Keep model, runtime, harness, profile, protocol and tool differences visible. Five fresh trajectories per fixed configuration are a future comparative plan, not an automatic action. Improvement on this SVG alone does not establish general coding gains—test held-out repository tasks too.

The portable prompt edition is **v0.3-portable**: only the absolute output path changes from v0.2 to `output/scene.svg`. Original v0.2 prompt/rubric bytes are retained separately. Do not pool the editions or different tool tracks as identical tests.

Source prototype: fake-server/fake-harness tests, a Linux browser demo and partial real-server trials; complete real-model trajectories, real native harness execution, and interactive Windows/macOS desktop use are not yet validated. See [validation scope](VALIDATION.md) and [cross-platform CI results](https://github.com/stateofstatic-ship-it/qwen-r9700-svg-bench/actions/workflows/tests.yml). This is a Python launcher, not yet a bundled OS binary. Runs/private state are ignored by Git and are not sanitized sharing bundles. No results are uploaded automatically. Licensed under the [MIT License](LICENSE).
