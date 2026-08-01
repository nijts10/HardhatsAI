"""Phase 2 report generator — data-coverage only.

Deliberately NOT an AI-visibility score report: no prompt has been run
against any AI model yet (that's benchmark-running, a later phase, and
picking which AI systems to actually test against is a product decision,
not an engineering default). What this generates is the thing that's
actually possible right now: for each of a brand's products, how much of
the relevant spec vocabulary is backed by a citation versus missing —
exactly the "coverage" half of the brief's own data_completeness idea from
the HardhatsAI schema, adapted here into an actual persisted report rather
than a single ranking-bonus number.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psycopg


@dataclass
class ProductCoverage:
    product_id: str
    product_name: str
    stated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.stated) + len(self.missing)

    @property
    def completeness(self) -> float:
        return len(self.stated) / self.total if self.total else 0.0


@dataclass
class BrandCoverage:
    brand_id: str
    brand_name: str
    products: list[ProductCoverage] = field(default_factory=list)

    @property
    def completeness(self) -> float:
        totals = [p.total for p in self.products]
        stated = [len(p.stated) for p in self.products]
        return sum(stated) / sum(totals) if sum(totals) else 0.0


def compute_coverage(conn: psycopg.Connection, brand_id: str) -> BrandCoverage:
    with conn.cursor() as cur:
        cur.execute("select name from brands where id = %s", (brand_id,))
        brand_row = cur.fetchone()
        if brand_row is None:
            raise LookupError(f"brand {brand_id} not found")

        cur.execute("select id, name, category_id from products where brand_id = %s order by name", (brand_id,))
        products = cur.fetchall()

    coverage = BrandCoverage(brand_id=brand_id, brand_name=brand_row["name"])

    for product in products:
        with conn.cursor() as cur:
            cur.execute(
                """
                select sa.key, sa.name_nl
                  from spec_attributes sa
                 where sa.is_filterable
                   and (
                     sa.category_codes = '{}'
                     or exists (
                       select 1 from product_categories pc
                        where pc.id = %s and pc.code = any(sa.category_codes)
                     )
                   )
                 order by sa.key
                """,
                (product["category_id"],),
            )
            relevant_attrs = cur.fetchall()

            cur.execute(
                """
                select sa.key
                  from claims c
                  join spec_attributes sa on sa.id = c.spec_attribute_id
                 where c.product_id = %s and c.is_current
                   and c.presence in ('stated', 'derived', 'manual')
                """,
                (product["id"],),
            )
            stated_keys = {row["key"] for row in cur.fetchall()}

            cur.execute("select scheme from certifications where product_id = %s", (product["id"],))
            certifications = [row["scheme"] for row in cur.fetchall()]

        pc = ProductCoverage(product_id=product["id"], product_name=product["name"], certifications=certifications)
        for attr in relevant_attrs:
            (pc.stated if attr["key"] in stated_keys else pc.missing).append(attr["name_nl"])

        coverage.products.append(pc)

    return coverage


def render_sections(coverage: BrandCoverage) -> list[tuple[str, str]]:
    """Returns [(heading, body), ...] ready to insert into report_sections,
    in display order."""
    sections: list[tuple[str, str]] = []

    overview = (
        f"{coverage.brand_name} has {len(coverage.products)} product(s) on file. "
        f"Overall spec coverage: {coverage.completeness:.0%} of applicable claims are backed "
        f"by a citation; the rest are either not yet extracted or genuinely not stated in the "
        f"documents on file."
    )
    sections.append(("Overview", overview))

    if not coverage.products:
        sections.append(("Product coverage", "No products on file yet."))
        return sections

    coverage_lines = []
    for p in sorted(coverage.products, key=lambda p: p.completeness):
        coverage_lines.append(f"- {p.product_name}: {len(p.stated)}/{p.total} specs known ({p.completeness:.0%})")
    sections.append(("Product coverage", "\n".join(coverage_lines)))

    gap_lines = []
    for p in coverage.products:
        if p.missing:
            gap_lines.append(f"{p.product_name}: " + ", ".join(sorted(p.missing)))
    sections.append((
        "Missing data",
        "\n".join(gap_lines) if gap_lines else "No gaps — every applicable spec has a citation.",
    ))

    cert_lines = []
    for p in coverage.products:
        if p.certifications:
            cert_lines.append(f"{p.product_name}: " + ", ".join(sorted(set(p.certifications))))
    sections.append((
        "Certifications found",
        "\n".join(cert_lines) if cert_lines else "No certifications extracted yet.",
    ))

    return sections


def create_report(
    conn: psycopg.Connection, *, brand_id: str, organization_id: str,
    kind: str = "audit", created_by: str | None = None,
) -> str:
    coverage = compute_coverage(conn, brand_id)
    sections = render_sections(coverage)

    with conn.cursor() as cur:
        cur.execute(
            """
            insert into reports (organization_id, brand_id, kind, title, status, summary, created_by)
            values (%s, %s, %s, %s, 'draft', %s, %s)
            returning id
            """,
            (
                organization_id, brand_id, kind,
                f"{coverage.brand_name} — data coverage report",
                f"Overall coverage {coverage.completeness:.0%} across {len(coverage.products)} product(s).",
                created_by,
            ),
        )
        report_id = cur.fetchone()["id"]

        for i, (heading, body) in enumerate(sections):
            cur.execute(
                "insert into report_sections (report_id, section_order, heading, body) values (%s, %s, %s, %s)",
                (report_id, i, heading, body),
            )

    return report_id
