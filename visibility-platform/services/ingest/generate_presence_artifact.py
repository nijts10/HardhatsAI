"""One-off generator: builds the presence-rate Q&A review artifact HTML
from openai_qa_dump.json. Not part of the ingest pipeline -- a reporting
aid, same spirit as convert_docx_to_pdf.py."""
import html
import json

INTENT_LABELS = {
    "spec_constrained": "Spec-constrained",
    "application_driven": "Application-driven",
    "comparative": "Comparative",
    "compliance": "Compliance",
    "sustainability": "Sustainability",
    "brand_direct": "Brand-direct",
    "problem_driven": "Problem-driven",
}

with open(r"C:\Users\stijn\HardhatsAI\visibility-platform\docs\hd\reports\openai_qa_dump.json", encoding="utf-8") as f:
    rows = json.load(f)

# Group by intent, then by prompt text (each prompt has 3 replicates)
by_intent: dict[str, dict[str, list[dict]]] = {}
for row in rows:
    by_intent.setdefault(row["intent"], {}).setdefault(row["prompt"], []).append(row)

total = len(rows)
total_mentioned = sum(1 for r in rows if r["mentioned"])

def esc(s: str) -> str:
    return html.escape(s, quote=True)

sections = []
intent_order = ["brand_direct", "spec_constrained", "comparative", "application_driven",
                 "sustainability", "compliance", "problem_driven"]
for intent in intent_order:
    prompts = by_intent.get(intent)
    if not prompts:
        continue
    intent_rows = [r for plist in prompts.values() for r in plist]
    n_mentioned = sum(1 for r in intent_rows if r["mentioned"])
    n_total = len(intent_rows)

    prompt_blocks = []
    for prompt_text, replicates in prompts.items():
        replicates = sorted(replicates, key=lambda r: r["replicate"])
        rep_items = []
        for r in replicates:
            pill_class = "pill-yes" if r["mentioned"] else "pill-no"
            pill_text = "Genoemd" if r["mentioned"] else "Niet genoemd"
            rep_items.append(f'''
            <details class="answer">
              <summary>
                <span class="rep">Replicate {r["replicate"] + 1}</span>
                <span class="pill {pill_class}">{pill_text}</span>
              </summary>
              <p class="answer-text">{esc(r["answer"])}</p>
            </details>''')
        prompt_blocks.append(f'''
        <div class="prompt-block">
          <p class="prompt-text">&ldquo;{esc(prompt_text)}&rdquo;</p>
          <div class="replicates">{"".join(rep_items)}</div>
        </div>''')

    sections.append(f'''
    <section class="intent-section">
      <div class="intent-head">
        <h2>{INTENT_LABELS.get(intent, intent)}</h2>
        <span class="intent-stat">{n_mentioned} / {n_total} genoemd</span>
      </div>
      {"".join(prompt_blocks)}
    </section>''')

html_out = f'''<title>Presence Rate — Hunter Douglas × OpenAI</title>
<style>
:root {{
  --bg: #f1f2f0;
  --surface: #ffffff;
  --ink: #1c2128;
  --muted: #5b6470;
  --line: #dcdfe2;
  --accent: #b5652a;
  --accent-soft: #f3e2d3;
  --absent-soft: #eceef0;
  --absent-ink: #6b7280;
  --font-display: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --font-body: Seravek, "Segoe UI", system-ui, -apple-system, sans-serif;
  --font-mono: ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #1a1d21;
    --surface: #23272c;
    --ink: #ecebe7;
    --muted: #9aa3ad;
    --line: #363b41;
    --accent: #d98a4f;
    --accent-soft: #3a2c22;
    --absent-soft: #2c3036;
    --absent-ink: #9aa3ad;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #1a1d21;
  --surface: #23272c;
  --ink: #ecebe7;
  --muted: #9aa3ad;
  --line: #363b41;
  --accent: #d98a4f;
  --accent-soft: #3a2c22;
  --absent-soft: #2c3036;
  --absent-ink: #9aa3ad;
}}
* {{ box-sizing: border-box; }}
body {{
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font-body);
  line-height: 1.55;
  padding: 0 0 5rem;
}}
.wrap {{
  max-width: 780px;
  margin: 0 auto;
  padding: 3rem 1.5rem 0;
}}
header.page {{
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  margin-bottom: 3rem;
}}
.eyebrow {{
  font-family: var(--font-mono);
  font-size: 0.75rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}}
h1 {{
  font-family: var(--font-display);
  font-size: 2.1rem;
  font-weight: 600;
  margin: 0;
  text-wrap: balance;
}}
.subtitle {{
  color: var(--muted);
  font-size: 1rem;
  max-width: 60ch;
}}
.statline {{
  display: flex;
  align-items: baseline;
  gap: 1rem;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 1.25rem 1.5rem;
}}
.statline .big {{
  font-family: var(--font-display);
  font-size: 2.6rem;
  font-weight: 600;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}}
.statline .desc {{
  color: var(--muted);
  font-size: 0.92rem;
  max-width: 42ch;
}}
.method {{
  font-size: 0.88rem;
  color: var(--muted);
  border-left: 2px solid var(--line);
  padding-left: 1rem;
}}
.method code {{
  font-family: var(--font-mono);
  background: var(--absent-soft);
  padding: 0.1em 0.35em;
  border-radius: 4px;
  font-size: 0.85em;
}}
.intent-section {{
  margin-bottom: 2.75rem;
}}
.intent-head {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.5rem;
  margin-bottom: 1.25rem;
}}
.intent-head h2 {{
  font-family: var(--font-display);
  font-size: 1.3rem;
  font-weight: 600;
  margin: 0;
}}
.intent-stat {{
  font-family: var(--font-mono);
  font-size: 0.82rem;
  color: var(--muted);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}}
.prompt-block {{
  margin-bottom: 1.5rem;
}}
.prompt-text {{
  font-family: var(--font-display);
  font-style: italic;
  font-size: 1.02rem;
  margin: 0 0 0.6rem;
  color: var(--ink);
}}
.replicates {{
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}}
details.answer {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
}}
details.answer summary {{
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.55rem 0.9rem;
  cursor: pointer;
  list-style: none;
  font-size: 0.88rem;
}}
details.answer summary::-webkit-details-marker {{ display: none; }}
details.answer summary::before {{
  content: "▸";
  color: var(--muted);
  font-size: 0.75rem;
  transition: transform 0.15s ease;
}}
details.answer[open] summary::before {{
  transform: rotate(90deg);
}}
details.answer summary:focus-visible {{
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}}
.rep {{
  font-family: var(--font-mono);
  color: var(--muted);
  font-size: 0.78rem;
}}
.pill {{
  font-family: var(--font-mono);
  font-size: 0.72rem;
  letter-spacing: 0.02em;
  padding: 0.15em 0.6em;
  border-radius: 99px;
  margin-left: auto;
}}
.pill-yes {{
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}}
.pill-no {{
  background: var(--absent-soft);
  color: var(--absent-ink);
}}
.answer-text {{
  margin: 0;
  padding: 0.9rem 1.1rem 1.1rem;
  border-top: 1px solid var(--line);
  font-size: 0.92rem;
  color: var(--ink);
  white-space: pre-wrap;
}}
footer.page {{
  max-width: 780px;
  margin: 0 auto;
  padding: 2rem 1.5rem 0;
  color: var(--muted);
  font-size: 0.82rem;
  border-top: 1px solid var(--line);
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ transition: none !important; }}
}}
</style>
<div class="wrap">
  <header class="page">
    <span class="eyebrow">AI Visibility Audit · Hunter Douglas · OpenAI (gpt-4o)</span>
    <h1>Presence rate — every question, every answer</h1>
    <p class="subtitle">50 prompts × 3 replicates = 150 real web-search-grounded GPT-4o answers, run 2026-08-11. Each one checked for whether &ldquo;Hunter Douglas&rdquo; appears anywhere in the raw text.</p>
    <div class="statline">
      <span class="big">{total_mentioned} / {total}</span>
      <span class="desc">answers mentioned Hunter Douglas by name &mdash; {round(100*total_mentioned/total)}% overall presence rate</span>
    </div>
    <p class="method">Method: case-insensitive substring match for <code>"hunter douglas"</code> in the raw response text. No fuzzy matching, no synonyms &mdash; a literal text search, same as the number in the PDF report. Click any answer to read it in full.</p>
  </header>

  {"".join(sections)}

  <footer class="page">
    Source: <code>visibility_runs</code> / <code>visibility_answers</code>, run <code>00708c69-8adc-48e4-8664-f82decac860d</code>. Generated for internal review, not a client-facing document.
  </footer>
</div>
'''

out_path = r"C:\Users\stijn\HardhatsAI\visibility-platform\docs\hd\reports\presence-rate-openai.html"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(html_out)
print("wrote", out_path, len(html_out), "chars")
