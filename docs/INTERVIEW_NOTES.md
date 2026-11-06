# OpsPilot: interview notes

## 30-second pitch
"I built OpsPilot, an LLM agent that does an on-call SRE's first response on Kubernetes: it
detects that something is wrong, finds the root-cause service, and applies a minimal fix.
It only uses free-tier models. Every command goes through a guardrail I wrote, and a cost-aware
cascade sends routine steps to a small model and calls the large one only when the small model
struggles. I evaluate it the way Microsoft's AIOpsLab does: inject a fault, let the agent work,
then score detection, localization and mitigation automatically, on a simulator and on a real
kind cluster."

## Design decisions you should be able to defend

| Decision | Why | Trade-off |
|---|---|---|
| Allow-list guardrail, no shell (`argv` lists, filters reimplemented in Python) | An LLM can't be prompt-injected into `; rm -rf`, `curl`, or `$(...)` when there's no shell to interpret them | Agent can't use `jq`/`awk`; accepted, kubectl + grep is enough |
| Read-only tasks are enforced in code, not by prompt | Prompts are suggestions; least privilege must be a hard control | Needs a task-type signal (AIOpsLab problem ids provide it) |
| Writes need an explicit `-n <app-namespace>`, single named object, no `--all`, no scale-to-zero, no delete except pods | Caps the blast radius of a wrong action | Some legitimate fixes (e.g. recreating a Service) need a human |
| Cascade router with *behavioural* triggers (format error, blocked action, repeated command, submitting without a fix, step budget) | Cheap signals that the small model is out of its depth; no extra model or training needed | Escalation is one-way per episode; a learned router (RouteLLM) could be finer-grained |
| Pre-submit verification gate for mitigation | Mirrors how SREs close incidents: check pods Ready and endpoints populated before resolving | Costs one extra step; ablated in `cascade-noverify` |
| Faults injected with ordinary kubectl mutations | The same scenario file drives the simulator and a real cluster; the fix space is realistic | The simulator only models the kubectl subset agents actually use |
| Simulator *and* real kind backend | Simulator: deterministic, sub-second, runs in CI without Docker. kind: shows it's not a toy | Slim busybox app instead of the full DeathStarBench images (8 GB laptop) |
| AIOpsLab adapter keeps guardrail + router inside `get_action` | Unsafe/malformed actions are retried locally and never reach the benchmark cluster | AIOpsLab's own scoring is used as-is |
| Recover rejected native tool calls (`tool_use_failed`) | gpt-oss sometimes emits a function call even when none is declared; translating it saves a wasted step | Provider-specific error format (Groq) |

## Likely questions
- **How do you know the scorer is right?** A scripted expert (`--configs oracle`) solves 16/16
  episodes on the simulator and on real kind, and tests check that each fault breaks health and
  each standard fix restores it, while a wrong fix (restart) does not.
- **Why not let the LLM run bash?** Same reason you don't give a new on-call engineer root on
  day one. The guardrail tests (`tests/test_guardrails.py`) show `;`, `&&`, `>`, `$( )`, `curl`,
  `exec`, `drain`, namespace deletion and bulk deletes all being blocked.
- **What fails?** Read the traces in `results/main/report.html`. Typical failures: blaming a
  caller instead of the root cause (localization), or stopping at the first symptom.
- **What would you do next?** Run the full AIOpsLab suite on a bigger machine; add metrics and
  traces tools (Prometheus/Jaeger); replace heuristic escalation with a learned router; add a
  human-approval step for writes in a Slack bot.
