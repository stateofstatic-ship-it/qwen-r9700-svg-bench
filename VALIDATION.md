# Draft validation — 2026-09-16

**Verified locally on Linux:** 42 no-inference tests; JavaScript syntax check; source launcher demo; actual in-app-browser demo from Run through the four-section report. A live vLLM tokenizer also accepts and preserves a synthetic reasoning-history marker without inference.

Tests cover direct HTTP client/agent history using a fake localhost endpoint, provider text-reasoning replay across tool/user continuations without public event disclosure, native Codex/OpenCode continuation using fake executables, exact prompts, frozen profiles and instruction protection, artifact checks, errors/incomplete generation, cancellation without another dispatch, retained-pipe cleanup, denied/failed cleanup preserving the original error and report, requested/effective-setting distinctions, profile budget precedence and launcher action-token checks.

Reproduce: `python -m unittest discover -s tests -v` and `python benchmark.py --demo --no-open`.

**Cross-platform automation:** [GitHub Actions results](https://github.com/stateofstatic-ship-it/qwen-r9700-svg-bench/actions/workflows/tests.yml) show the actual outcome for each commit across Linux, Windows and macOS on Python 3.11/3.12. These are no-inference unit tests and a synthetic CLI demo, not real model trials. POSIX fake-native and escaped-process tests skip on other platforms; symlink checks may skip without privileges.

**Verified for one real deployment:** a fresh public GitHub download completed all four sections through the web UI against an existing Qwen/vLLM endpoint, using the text-only built-in harness with a 32,768-token request ceiling. Exact prompts, frozen source, actual reasoning/tool history, checkpoints, eight report previews and clean shutdown were checked. One malformed tool call was returned to the model and successfully repaired. Earlier incomplete attempts were preserved; no model ranking or universal compatibility claim follows from this trial.

The first real attempts exposed an insufficient 8,192-token budget for that configuration and an omitted provider reasoning-history field. Reasoning replay is now fixed and regression-tested. Tests and live tokenizer checks passed before the complete retest; raw model artifacts and traces remain private, outside this repository.

**Not run:** real native connector execution, interactive Windows/macOS desktop/browser use, packaged standalone binaries, representative performance/grade calibration or predictive validation.

**Scope limits:** built-in profile is text-only/restricted file tools; metadata preflight cannot prove tool-parser compatibility; native harness controls are requested but not independently certified. Server/plugin/template requirements are not automatically applied. Source-only packaging excludes generated runs and credentials, but user-created run folders and arbitrary profile metadata are not sanitized exports.

The earlier hosted calibration and all private artifacts remain outside this repository. Portable v0.3-path/tool profiles must not be pooled with those v0.2 experiments as identical conditions.
