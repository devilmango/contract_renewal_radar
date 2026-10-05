from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

from .documents import extract_pdf_text
from .extractor import get_extractor
from .store import Store

MAX_PDF_BYTES = 25 * 1024 * 1024


def ingest_pdf_bytes(
    store: Store,
    content: bytes,
    filename: str,
    actor: str,
    tenant_id: str,
    *,
    source: tuple[str, str, str, str, str | None] | None = None,
) -> tuple[str, bool]:
    """Extract and store a PDF. Source tuple is provider, source key, item ID, revision, URL."""
    if not content:
        raise ValueError("PDF is empty")
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("PDF must be 25 MB or smaller")
    safe_filename = Path(filename or "contract.pdf").name
    if not safe_filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF files are supported")
    if source:
        provider, source_key, external_id, revision, _source_url = source
        if store.external_document_exists(tenant_id, provider, source_key, external_id, revision):
            return "", False

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp:
            temp.write(content)
            temp_path = Path(temp.name)
        text, used_ocr = extract_pdf_text(temp_path)
    finally:
        if temp_path:
            temp_path.unlink(missing_ok=True)

    extractor = get_extractor()
    extracted = extractor.extract(text)
    contract_id = str(uuid4())
    provider_name = extractor.name + ("+ocr" if used_ocr else "")
    if source:
        source_provider, source_key, external_id, revision, source_url = source
        saved_id, created = store.save_external_contract(
            contract_id, safe_filename, provider_name, extracted, text, tenant_id,
            source_provider, source_key, external_id, revision, source_url,
        )
        return saved_id, created
    store.save_contract(contract_id, safe_filename, provider_name, extracted, text, actor, tenant_id)
    return contract_id, True
