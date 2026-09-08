"""One-off: attach a CitationSourceSummary (built from resolved_citations.json,
produced by running resolve_citations.py <run_id> first) to a report's data
and regenerate the PDF. Not part of the CLI -- citation resolution is a
slow, network-heavy step that doesn't belong in the normal report path
(see CitationSourceSummary's docstring in pdf_report.py). Same "one-off
reporting aid" precedent as generate_presence_artifact.py / reextract_batch.py.

Usage: python add_citation_source_analysis.py <run_id> <output_pdf_path>
"""
import json
import sys
from urllib.parse import urlparse

from ingest import db, pdf_report
from ingest.config import load_config

# Substring match against the resolved domain -- good enough for a one-off
# analysis of a single brand's known web presence, not meant to generalise.
OWN_DOMAIN_KEYWORDS = ["hunterdouglasarchitectural", "luxalon.nl", "hunterdouglasblog"]


def is_own_domain(domain: str) -> bool:
    return any(kw in domain for kw in OWN_DOMAIN_KEYWORDS)


def main(run_id: str, output_path: str) -> int:
    with open("resolved_citations.json", encoding="utf-8") as f:
        resolved: dict[str, str] = json.load(f)

    def domain_of(url: str) -> str | None:
        r = resolved.get(url, "")
        return None if r.startswith("ERROR") else urlparse(r).netloc

    config = load_config()
    conn = db.connect(config.database_url)

    cur = conn.cursor()
    cur.execute(
        "select citations from visibility_answers where run_id = %s and citations is not null",
        (run_id,),
    )
    all_urls = [u for row in cur.fetchall() for u in (row["citations"] or [])]
    domain_counts: dict[str, int] = {}
    for u in all_urls:
        d = domain_of(u)
        if d:
            domain_counts[d] = domain_counts.get(d, 0) + 1
    total_citations = sum(domain_counts.values())
    own_domain_citations = sum(c for d, c in domain_counts.items() if is_own_domain(d))
    top_domains = [
        (d, c, is_own_domain(d))
        for d, c in sorted(domain_counts.items(), key=lambda kv: -kv[1])
    ]

    cur.execute(
        """
        select ac.product_name, sa.name_nl as spec_name, ac.source_quote, va.citations
          from answer_claims ac
          join visibility_answers va on va.id = ac.answer_id
          join spec_attributes sa on sa.id = ac.spec_attribute_id
         where va.run_id = %s and not ac.is_competitor and ac.verdict = 'unverifiable_spec'
        """,
        (run_id,),
    )
    gap_sources = []
    for row in cur.fetchall():
        doms = {domain_of(u) for u in (row["citations"] or [])}
        doms.discard(None)
        if any(is_own_domain(d) for d in doms):
            bucket = "own_site"
        elif doms:
            bucket = "third_party"
        else:
            bucket = "no_citation"
        gap_sources.append({
            "product_name": row["product_name"], "spec_name": row["spec_name"],
            "source_quote": row["source_quote"], "bucket": bucket,
        })

    summary = pdf_report.CitationSourceSummary(
        total_citations=total_citations, own_domain_citations=own_domain_citations,
        top_domains=top_domains, gap_sources=gap_sources,
    )

    data = pdf_report.gather_report_data(conn, run_id=run_id)
    data.citation_source_summary = summary
    pdf_report.render_pdf(data, output_path)
    conn.close()

    print(f"report regenerated with citation source analysis: {output_path}")
    print(f"own-domain share: {own_domain_citations}/{total_citations} ({own_domain_citations/total_citations:.1%})")
    print(f"gap sources: {sum(1 for g in gap_sources if g['bucket']=='own_site')} own_site, "
          f"{sum(1 for g in gap_sources if g['bucket']=='third_party')} third_party, "
          f"{sum(1 for g in gap_sources if g['bucket']=='no_citation')} no_citation")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python add_citation_source_analysis.py <run_id> <output_pdf_path>")
        sys.exit(1)
    sys.exit(main(sys.argv[1], sys.argv[2]))
