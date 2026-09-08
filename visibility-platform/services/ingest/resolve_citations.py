"""One-off diagnostic: resolve every Gemini grounding-redirect citation URL
from a run to its real underlying domain, so we can see where the AI is
actually pulling information from (own site vs competitor vs third party)
-- not part of the CLI, same spirit as convert_docx_to_pdf.py. Feeds
add_citation_source_analysis.py, which attaches the result to a PDF report.

Usage: python resolve_citations.py <run_id>
Writes resolved_citations.json (redirect URL -> real URL, or "ERROR: ...").
"""
import asyncio
import json
import sys

import httpx

from ingest import db
from ingest.config import load_config


async def resolve(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore, results: dict[str, str]) -> None:
    async with sem:
        try:
            r = await client.head(url, follow_redirects=True, timeout=15)
            results[url] = str(r.url)
        except Exception as exc:
            results[url] = f"ERROR: {exc}"


async def main(run_id: str) -> None:
    config = load_config()
    conn = db.connect(config.database_url)
    cur = conn.cursor()
    cur.execute(
        "select citations from visibility_answers where run_id = %s and citations is not null",
        (run_id,),
    )
    urls = sorted({u for row in cur.fetchall() for u in (row["citations"] or [])})
    conn.close()
    print(f"{len(urls)} unique citation URLs to resolve", flush=True)

    results: dict[str, str] = {}
    sem = asyncio.Semaphore(20)
    async with httpx.AsyncClient() as client:
        tasks = [resolve(client, u, sem, results) for u in urls]
        done = 0
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            if done % 100 == 0:
                print(f"{done}/{len(urls)}", flush=True)

    with open("resolved_citations.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=0)
    print("done -- wrote resolved_citations.json")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python resolve_citations.py <run_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
