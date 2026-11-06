| Config | Overall | Detection | Localization | Mitigation | Avg steps | Tokens / episode | Large-model token share | Guardrail blocks |
|---|---|---|---|---|---|---|---|---|
| `small` | **94%** | 100% | 100% | 80% | 7.9 | 12,469 | 0% | 6 |
| `large` | **100%** | 100% | 100% | 100% | 7.6 | 14,606 | 100% | 0 |

run: `main-k2` · backend: `sim` · small model: `openai/gpt-oss-20b` · large model: `openai/gpt-oss-120b` · max steps: `15` · python: `3.11.8`
