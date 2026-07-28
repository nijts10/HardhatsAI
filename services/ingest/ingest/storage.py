"""Stage 1: object storage. Supabase Storage bucket `supplier-documents`."""
from __future__ import annotations

import hashlib

from supabase import Client, create_client

BUCKET = "supplier-documents"


def make_client(supabase_url: str, service_role_key: str) -> Client:
    return create_client(supabase_url, service_role_key)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def storage_path(supplier_slug: str, sha256: str, ext: str = "pdf") -> str:
    # {supplier_slug}/{sha256[:2]}/{sha256}.pdf — see docs/INGESTION.md.
    return f"{supplier_slug}/{sha256[:2]}/{sha256}.{ext}"


def upload(client: Client, path: str, data: bytes, mime_type: str = "application/pdf") -> None:
    client.storage.from_(BUCKET).upload(
        path, data, file_options={"content-type": mime_type, "upsert": "true"}
    )


def download(client: Client, path: str) -> bytes:
    return client.storage.from_(BUCKET).download(path)
