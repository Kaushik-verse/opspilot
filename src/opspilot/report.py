"""Turns ``results.jsonl`` + traces into a Markdown summary and a self-contained HTML report."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from pathlib import Path

TASK_ORDER = ("detection", "localization", "mitigation")
CONFIG_ORDER = ("small", "cascade", "cascade-noverify", "large", "oracle")


def load_rows(run_dir: Path) -> list[dict]:
    f = run_dir / "results.jsonl"
    rows: dict[tuple, dict] = {}
    for line in f.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            rows[(r["config"], r["episode"])] = r  # last write wins (re-runs)
    return list(rows.values())


def aggregate(rows: list[dict]) -> dict[str, dict]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["config"]].append(r)
    out = {}
    for cfg, rs in by.items():
        n = len(rs)
        agg = {"episodes": n, "success": sum(r["success"] for r in rs) / n}
        for t in TASK_ORDER:
            tr = [r for r in rs if r["task"] == t]
            agg[f"{t}_n"] = len(tr)
            agg[t] = (sum(r["success"] for r in tr) / len(tr)) if tr else None
        tot_small = sum(r["small_tokens"] for r in rs)
        tot_large = sum(r["large_tokens"] for r in rs)
        agg.update(
            avg_steps=sum(r["steps"] for r in rs) / n,
            tokens_per_episode=(tot_small + tot_large) / n,
            large_tokens=tot_large, small_tokens=tot_small,
            large_share=(tot_large / (tot_small + tot_large)) if (tot_small + tot_large) else 0.0,
            escalation_rate=sum(r["escalated"] for r in rs) / n,
            blocked=sum(r["blocked_actions"] for r in rs),
            errors=sum(1 for r in rs if r.get("error")),
            avg_wall_s=sum(r["wall_s"] for r in rs) / n,
        )
        out[cfg] = agg
    return dict(sorted(out.items(), key=lambda kv: CONFIG_ORDER.index(kv[0])
                       if kv[0] in CONFIG_ORDER else 99))


def _pct(x) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def markdown_summary(agg: dict[str, dict], meta: dict | None = None) -> str:
    lines = ["| Config | Overall | Detection | Localization | Mitigation | Avg steps | Tokens / episode | Large-model token share | Guardrail blocks |",
             "|---|---|---|---|---|---|---|---|---|"]
    for cfg, a in agg.items():
        lines.append(f"| `{cfg}` | **{_pct(a['success'])}** | {_pct(a['detection'])} | {_pct(a['localization'])} | "
                     f"{_pct(a['mitigation'])} | {a['avg_steps']:.1f} | {a['tokens_per_episode']:,.0f} | "
                     f"{_pct(a['large_share'])} | {a['blocked']} |")
    if meta:
        lines.append("")
        lines.append(" · ".join(f"{k}: `{v}`" for k, v in meta.items()))
    return "\n".join(lines)


def _bars_svg(agg: dict[str, dict]) -> str:
    cfgs = list(agg)
    groups = list(TASK_ORDER) + ["overall"]
    w, h, pad_l, pad_b, top = 760, 280, 44, 40, 16
    gw = (w - pad_l - 10) / len(groups)
    bw = min(26, (gw - 16) / max(1, len(cfgs)))
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Success rate by task and configuration" class="chart">']
    for frac in (0, .25, .5, .75, 1):
        y = top + (h - pad_b - top) * (1 - frac)
        parts.append(f'<line x1="{pad_l}" x2="{w - 10}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{pad_l - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{int(frac * 100)}%</text>')
    for gi, g in enumerate(groups):
        gx = pad_l + gi * gw + (gw - bw * len(cfgs)) / 2
        for ci, c in enumerate(cfgs):
            v = agg[c]["success"] if g == "overall" else agg[c][g]
            if v is None:
                continue
            bh = (h - pad_b - top) * v
            x, y = gx + ci * bw, h - pad_b - bh
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 4:.1f}" height="{bh:.1f}" rx="2" '
                         f'class="s{ci}"><title>{c} · {g}: {_pct(v)}</title></rect>')
        parts.append(f'<text x="{pad_l + gi * gw + gw / 2:.1f}" y="{h - 14}" class="lbl" text-anchor="middle">{g}</text>')
    parts.append("</svg>")
    legend = "".join(f'<span class="key"><i class="s{ci}"></i>{html.escape(c)}</span>' for ci, c in enumerate(cfgs))
    return "".join(parts) + f'<div class="legend">{legend}</div>'


def html_report(run_dir: Path, rows: list[dict], agg: dict[str, dict], meta: dict) -> str:
    traces_dir = run_dir / "traces"
    ep_rows = []
    for r in sorted(rows, key=lambda r: (CONFIG_ORDER.index(r["config"]) if r["config"] in CONFIG_ORDER else 9,
                                          r["episode"])):
        tf = traces_dir / f"{r['config']}__{r['episode']}.json"
        steps_html = ""
        if tf.exists():
            tr = json.loads(tf.read_text())["trace"]
            for s in tr:
                cmd = s.get("command") or (f"submit({s['answer']!r})" if s.get("action") == "submit" else s.get("action"))
                guard = s.get("guardrail", "")
                steps_html += (
                    f'<div class="step"><div class="sh"><b>#{s["step"]}</b> <span class="tier {s["tier"]}">{s["tier"]}</span> '
                    f'<code>{html.escape(str(cmd))}</code> '
                    f'{"<span class=blk>" + html.escape(guard) + "</span>" if guard.startswith("BLOCKED") else ""}</div>'
                    f'<details><summary>reasoning &amp; output</summary><pre>{html.escape(s.get("response", ""))}</pre>'
                    f'<pre class="obs">{html.escape(str(s.get("observation", ""))[:2500])}</pre></details></div>')
        badge = "pass" if r["success"] else "fail"
        ep_rows.append(
            f'<details class="ep"><summary><span class="badge {badge}">{"PASS" if r["success"] else "FAIL"}</span> '
            f'<b>{html.escape(r["config"])}</b> · {html.escape(r["episode"])} · {r["steps"]} steps · '
            f'{r["small_tokens"] + r["large_tokens"]:,} tokens'
            f'{" · escalated (" + html.escape(r["escalation_reason"]) + ")" if r["escalated"] else ""}'
            f'<span class="det">{html.escape(r["detail"] or r.get("error", ""))}</span></summary>{steps_html}</details>')
    table = "".join(
        f"<tr><td><code>{html.escape(c)}</code></td><td><b>{_pct(a['success'])}</b></td><td>{_pct(a['detection'])}</td>"
        f"<td>{_pct(a['localization'])}</td><td>{_pct(a['mitigation'])}</td><td>{a['avg_steps']:.1f}</td>"
        f"<td>{a['tokens_per_episode']:,.0f}</td><td>{_pct(a['large_share'])}</td><td>{a['blocked']}</td></tr>"
        for c, a in agg.items())
    meta_html = " · ".join(f"{html.escape(k)}: <code>{html.escape(str(v))}</code>" for k, v in meta.items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>OpsPilot Benchmark</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1f2328;--mut:#5d6470;--card:#fff;--line:#e3e1db;--c0:#8a6fd1;--c1:#2f9e8f;--c2:#d98a2b;--c3:#3b82c4;--c4:#9aa0a6;--ok:#1a7f4b;--bad:#b42318}}
@media (prefers-color-scheme:dark){{:root{{--bg:#15171a;--fg:#e8e6e1;--mut:#9aa0a6;--card:#1d2024;--line:#30343a;--ok:#4cc38a;--bad:#f97066}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:1040px;margin:0 auto;padding:28px 16px 60px}} h1{{margin:0 0 4px;font-size:26px}} .sub{{color:var(--mut);margin:0 0 22px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 18px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}} th{{color:var(--mut);font-weight:600}}
.chart{{width:100%;height:auto}} .grid{{stroke:var(--line)}} .tick,.lbl{{fill:var(--mut);font-size:12px}}
.s0{{fill:var(--c0);background:var(--c0)}} .s1{{fill:var(--c1);background:var(--c1)}} .s2{{fill:var(--c2);background:var(--c2)}} .s3{{fill:var(--c3);background:var(--c3)}} .s4{{fill:var(--c4);background:var(--c4)}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--mut)}} .key i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}}
.ep{{border-top:1px solid var(--line);padding:8px 0}} .ep>summary{{cursor:pointer}} .det{{color:var(--mut);margin-left:8px;font-size:13px}}
.badge{{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px;color:#fff}} .pass{{background:var(--ok)}} .fail{{background:var(--bad)}}
.step{{margin:8px 0 0 18px}} .tier{{font-size:11px;padding:1px 5px;border-radius:4px;border:1px solid var(--line)}} .tier.large{{border-color:var(--c2);color:var(--c2)}}
.blk{{color:var(--bad);font-size:12px;margin-left:6px}} code{{font-size:12.5px}} pre{{white-space:pre-wrap;word-break:break-word;font-size:12px;background:var(--bg);padding:8px;border-radius:6px;border:1px solid var(--line);max-height:320px;overflow:auto}} .obs{{color:var(--mut)}}
</style></head><body><main>
<h1>OpsPilot benchmark</h1><p class="sub">{meta_html}</p>
<div class="card"><table><thead><tr><th>Config</th><th>Overall</th><th>Detection</th><th>Localization</th><th>Mitigation</th><th>Avg steps</th><th>Tokens / episode</th><th>Large-model share</th><th>Guardrail blocks</th></tr></thead><tbody>{table}</tbody></table></div>
<div class="card">{_bars_svg(agg)}</div>
<div class="card"><h2 style="margin-top:0;font-size:18px">Episodes &amp; agent traces</h2>{"".join(ep_rows)}</div>
</main></body></html>"""


def build_report(run_dir: Path) -> tuple[Path, str]:
    rows = load_rows(run_dir)
    agg = aggregate(rows)
    meta_f = run_dir / "meta.json"
    meta = json.loads(meta_f.read_text()) if meta_f.exists() else {}
    md = markdown_summary(agg, meta)
    (run_dir / "summary.md").write_text(md + "\n")
    out = run_dir / "report.html"
    out.write_text(html_report(run_dir, rows, agg, meta))
    return out, md
