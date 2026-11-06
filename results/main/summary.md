| Config | Overall | Detection | Localization | Mitigation | Avg steps | Tokens / episode | Large-model token share | Guardrail blocks |
|---|---|---|---|---|---|---|---|---|
| `cascade` | **94%** | 100% | 100% | 80% | 7.8 | 12,427 | 36% | 3 |
| `cascade-noverify` | **94%** | 100% | 100% | 80% | 6.5 | 10,076 | 39% | 4 |

run: `main` · backend: `sim` · small model: `openai/gpt-oss-20b` · large model: `openai/gpt-oss-120b` · max steps: `15` · python: `3.11.8`
