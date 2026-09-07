"""One-off batch re-extraction: re-runs the fixed extraction pipeline
(pipeline.run) over every document_version that either predates the
Aug-14/Aug-19 extraction fixes, or was never linked/extracted at all
(the 5 Luxalon metal-ceiling documents newly linked to the scoping
product 'Luxalon Metal Ceilings'). Not part of the CLI -- a one-time
reporting/data-quality aid, same spirit as convert_docx_to_pdf.py and
generate_presence_artifact.py."""
import sys
import traceback

from ingest import db, pipeline
from ingest.config import load_config

DOCUMENT_VERSION_IDS = [
    # -- previously extracted with pre-fix code, re-run to pick up the
    #    product-identity / table-scrambling / no-value / range fixes --
    "2b7c6982-1942-47ab-882e-377100034ef2",  # HeartFelt Linear - Colour Card
    "09c51f83-3419-4fa3-8e5f-bebe8b567eb4",  # Technical Specifications - HeartFelt Linear Ceiling
    "3fde74e6-426c-4a61-b6cb-b12d181ee8f9",  # PareauLux Climate Ceilings - Comparison Overview
    "63d1e4be-85d8-4c4c-86b2-8561f9a58a88",  # Cradle to Cradle Bronze certificate
    "c6c03b64-7d9e-413b-bd94-c30302b2acbd",  # Material Health Bronze certificate
    "d01c15d5-2771-4cea-ab4a-e42ac3b63769",  # Indoor Air Comfort Gold certificate
    "4cbcde72-e00e-4e21-a26a-779cdc3b6034",  # SundaHus Miljodata certificate
    "9d057452-1b51-4c07-8326-d54596163961",  # REACH Declaration
    # -- never extracted, just linked to the new Luxalon scoping product --
    "5e980f70-9a17-4cc4-adcb-2a5bd4abaeb6",  # EPD - aluminium ceiling systems
    "b8e9b7b9-3934-409b-9b0a-60394a760401",  # EPD - steel ceiling systems
    "4ada385b-831a-41cf-8f81-e49ec6470278",  # Install instructions - Luxalon Linear 84B-84C-84R
    "5351c291-6134-49b9-ab4d-1bc12ba83338",  # Install instructions - Luxalon Stretch Metal Lay-on
    "daa7c0e0-ca74-4e4e-93fa-4b46b1c22661",  # Technische brochure - Metalen Roosterplafonds (NL/BE)
]


def main() -> int:
    config = load_config()
    failures = []
    for i, dvid in enumerate(DOCUMENT_VERSION_IDS, 1):
        print(f"\n=== [{i}/{len(DOCUMENT_VERSION_IDS)}] {dvid} ===", flush=True)
        conn = db.connect(config.database_url)
        try:
            result = pipeline.run(conn, config, document_version_id=dvid)
            conn.commit()
            print(f"OK: {result}", flush=True)
        except Exception as exc:
            conn.rollback()
            print(f"FAILED: {exc}", flush=True)
            traceback.print_exc()
            failures.append((dvid, str(exc)))
        finally:
            conn.close()

    print("\n=== SUMMARY ===")
    print(f"{len(DOCUMENT_VERSION_IDS) - len(failures)}/{len(DOCUMENT_VERSION_IDS)} succeeded")
    for dvid, err in failures:
        print(f"  FAILED {dvid}: {err}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
