# Draft validation — 2026-09-16

**Verified locally on Linux:** 36 no-inference tests; JavaScript syntax check; source launcher demo; actual in-app-browser demo from Run through the four-section report. No real server or native harness trial was performed for this portable package.

Tests cover direct HTTP client/agent history using a fake localhost endpoint, native Codex/OpenCode continuation using fake executables, exact prompts, frozen profiles and instruction protection, artifact checks, errors/incomplete generation, cancellation without another dispatch, retained-pipe cleanup, requested/effective-setting distinctions and launcher action-token checks.

Reproduce: `python -m unittest discover -s tests -v` and `python benchmark.py --demo --no-open`.

**Not run:** real local Qwen or other-model inference, real native connector execution, Windows/macOS execution, packaged standalone binaries, representative performance/grade calibration or predictive validation. CI is configured for three operating systems, but configuration is not evidence of successful execution. POSIX fake-native and escaped-process tests skip on other platforms; symlink checks may skip without privileges.

**Scope limits:** built-in profile is text-only/restricted file tools; metadata preflight cannot prove tool-parser compatibility; native harness controls are requested but not independently certified. Server/plugin/template requirements are not automatically applied. Source-only packaging excludes generated runs and credentials, but user-created run folders and arbitrary profile metadata are not sanitized exports.

The earlier hosted calibration and all private artifacts remain outside this repository. Portable v0.3-path/tool profiles must not be pooled with those v0.2 experiments as identical conditions.
