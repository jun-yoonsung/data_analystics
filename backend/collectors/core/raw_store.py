"""원문(raw) 저장·로드.

같은 소스·문서 종류·외부 키에서 내용(sha256)이 같은 원문은 한 번만 저장한다.
본문이 1KB 를 넘으면 gzip 으로 압축해 저장한다 (sha256 은 압축 전 기준).
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass

from sqlalchemy import Connection, text

from collectors.core.interface import RawDocument

COMPRESS_THRESHOLD = 1024


@dataclass(frozen=True)
class StoredRaw:
    id: int
    is_new: bool


def save_raw(conn: Connection, *, source_id: int, ingest_run_id: int | None, doc: RawDocument,
             parser_version: int) -> StoredRaw:
    body, encoding = doc.body, "identity"
    if len(body) > COMPRESS_THRESHOLD:
        body, encoding = gzip.compress(body), "gzip"
    row = conn.execute(text("""
        INSERT INTO ingest.raw_payload
            (source_id, ingest_run_id, document_type, external_key, request_url, request_params, http_status,
             content_type, body, body_encoding, sha256, size_bytes, fetched_at, parser_version)
        VALUES (:source_id, :run, :document_type, :external_key, :url, CAST(:params AS jsonb), :status,
                :content_type, :body, :encoding, :sha, :size, :fetched_at, :pv)
        ON CONFLICT ON CONSTRAINT uq_raw_payload_content DO NOTHING
        RETURNING id
    """), {
        "source_id": source_id, "run": ingest_run_id, "document_type": doc.document_type,
        "external_key": doc.external_key, "url": doc.request_url,
        "params": None if doc.request_params is None else json.dumps(doc.request_params),
        "status": doc.http_status, "content_type": doc.content_type, "body": body, "encoding": encoding,
        "sha": doc.sha256, "size": len(doc.body), "fetched_at": doc.fetched_at, "pv": parser_version,
    }).first()
    if row is not None:
        return StoredRaw(row.id, True)
    existing = conn.execute(text("""
        SELECT id FROM ingest.raw_payload
        WHERE source_id = :s AND document_type = :t AND external_key = :k AND sha256 = :h
    """), {"s": source_id, "t": doc.document_type, "k": doc.external_key, "h": doc.sha256}).scalar_one()
    return StoredRaw(existing, False)


def mark_parsed(conn: Connection, raw_id: int, parser_version: int, error: str | None = None) -> None:
    conn.execute(text("""
        UPDATE ingest.raw_payload
        SET parse_status = :st, parse_error = :err, parser_version = :pv, parsed_at = now()
        WHERE id = :id
    """), {"id": raw_id, "st": "failed" if error else "parsed", "err": error, "pv": parser_version})


def load_raw(row) -> RawDocument:
    """raw_payload 행 → RawDocument (재처리용)."""
    body = bytes(row.body)
    if row.body_encoding == "gzip":
        body = gzip.decompress(body)
    elif row.body_encoding != "identity":
        raise ValueError(f"지원하지 않는 인코딩: {row.body_encoding}")
    return RawDocument(document_type=row.document_type, external_key=row.external_key,
                       request_url=row.request_url, body=body, fetched_at=row.fetched_at,
                       content_type=row.content_type, http_status=row.http_status,
                       request_params=row.request_params)
