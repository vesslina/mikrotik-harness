"""A small, copyable Markdown + SQLite FTS5 retrieval pack."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from mth.core.mcp_client.runtime import project_root

SCHEMA_VERSION = 1
PACK_ENV = "MTH_RAG_HOME"
DEFAULT_MAX_CHUNK_CHARS = 2_400
DEFAULT_MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024

Fetcher = Callable[[str], bytes | str]


class PackError(RuntimeError):
    """The pack cannot be built or trusted."""


@dataclass(frozen=True, slots=True)
class RagHit:
    text: str
    heading: str
    source_url: str
    source_path: str
    score: float


@dataclass(frozen=True, slots=True)
class RagPack:
    path: Path
    manifest: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path | None = None) -> RagPack:
        pack_path = resolve_pack_dir(path)
        manifest = _validate(pack_path)
        return cls(pack_path, manifest)

    def search(self, query: str, *, limit: int = 5) -> tuple[RagHit, ...]:
        if limit < 1:
            return ()
        words = re.findall(r"[^\W_]+(?:[-./][^\W_]+)*", query, flags=re.UNICODE)
        if not words:
            return ()
        match = " OR ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words)
        database = self.path / str(self.manifest["database"]["path"])
        with _readonly_connection(database) as connection:
            rows = connection.execute(
                """
                SELECT c.text, c.heading, d.source_url, d.local_path,
                       bm25(chunks_fts, 4.0, 1.0) AS rank
                FROM chunks_fts
                JOIN chunks AS c ON c.id = chunks_fts.rowid
                JOIN documents AS d ON d.id = c.document_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match, limit),
            ).fetchall()
        return tuple(
            RagHit(
                text=str(row[0]),
                heading=str(row[1]),
                source_url=str(row[2]),
                source_path=str(row[3]),
                score=-float(row[4]),
            )
            for row in rows
        )


def resolve_pack_dir(path: str | Path | None = None) -> Path:
    """Resolve an explicit path, MTH_RAG_HOME, or the default user-local directory."""
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get(PACK_ENV)
    return (
        Path(configured).expanduser().resolve()
        if configured
        else project_root() / ".mth" / "rag"
    )


def load_or_build(
    path: str | Path | None = None,
    *,
    index_url: str | None = None,
    fetcher: Fetcher | None = None,
    source_name: str = "RouterOS Manual",
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> RagPack:
    """Load without network when populated; build only when the directory is empty."""
    pack_path = resolve_pack_dir(path)
    if pack_path.exists() and any(pack_path.iterdir()):
        return RagPack.load(pack_path)
    if not index_url:
        raise PackError(f"RAG pack is empty at {pack_path}; an index URL is required to build it")
    return build_pack(
        pack_path,
        index_url=index_url,
        fetcher=fetcher or fetch_url,
        source_name=source_name,
        max_chunk_chars=max_chunk_chars,
    )


def fetch_url(url: str) -> bytes:
    """Fetch one bounded source using only the standard library."""
    request = Request(url, headers={"User-Agent": "mikrotik-harness-rag/0.1"})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - caller controls source URL
        payload = bytes(response.read(DEFAULT_MAX_DOWNLOAD_BYTES + 1))
    if len(payload) > DEFAULT_MAX_DOWNLOAD_BYTES:
        raise PackError(f"RAG source exceeds {DEFAULT_MAX_DOWNLOAD_BYTES} bytes: {url}")
    return payload


def build_pack(
    path: str | Path,
    *,
    index_url: str,
    fetcher: Fetcher,
    source_name: str = "RouterOS Manual",
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> RagPack:
    """Build beside the destination, validate it, then promote the complete directory."""
    if max_chunk_chars < 256:
        raise ValueError("max_chunk_chars must be at least 256")
    pack_path = resolve_pack_dir(path)
    if pack_path.exists() and any(pack_path.iterdir()):
        raise PackError(f"refusing to replace non-empty RAG pack: {pack_path}")
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{pack_path.name}.building-", dir=pack_path.parent))
    try:
        _write_pack(
            temporary,
            index_url=index_url,
            fetcher=fetcher,
            source_name=source_name,
            max_chunk_chars=max_chunk_chars,
        )
        _validate(temporary)
        if pack_path.exists():
            if any(pack_path.iterdir()):
                raise PackError(f"RAG pack changed while it was being built: {pack_path}")
            pack_path.rmdir()
        temporary.replace(pack_path)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return RagPack.load(pack_path)


def _write_pack(
    path: Path,
    *,
    index_url: str,
    fetcher: Fetcher,
    source_name: str,
    max_chunk_chars: int,
) -> None:
    source_dir = path / "sources"
    source_dir.mkdir(parents=True)
    index_bytes = _as_bytes(fetcher(index_url))
    index_path = source_dir / "index.txt"
    index_path.write_bytes(index_bytes)
    pages = _index_pages(index_bytes.decode("utf-8", errors="replace"), index_url)
    if not pages:
        raise PackError(f"no Markdown page links found in RAG index: {index_url}")

    database_path = path / "content.sqlite3"
    documents: list[dict[str, str]] = []
    chunk_count = 0
    with closing(sqlite3.connect(database_path)) as connection:
        _create_schema(connection)
        for ordinal, (label, url) in enumerate(pages, start=1):
            payload = _as_bytes(fetcher(url))
            text = payload.decode("utf-8", errors="replace")
            filename = f"{ordinal:04d}-{_safe_name(url)}.md"
            local_path = f"sources/{filename}"
            (path / local_path).write_bytes(payload)
            digest = _sha256_bytes(payload)
            title = _document_title(text) or label or filename.removesuffix(".md")
            cursor = connection.execute(
                """
                INSERT INTO documents(source_url, local_path, title, sha256)
                VALUES (?, ?, ?, ?)
                """,
                (url, local_path, title, digest),
            )
            document_id = int(cursor.lastrowid or 0)
            for chunk_ordinal, (heading, chunk) in enumerate(
                _chunk_markdown(text, max_chunk_chars=max_chunk_chars), start=1
            ):
                chunk_cursor = connection.execute(
                    """
                    INSERT INTO chunks(document_id, ordinal, heading, text)
                    VALUES (?, ?, ?, ?)
                    """,
                    (document_id, chunk_ordinal, heading, chunk),
                )
                connection.execute(
                    "INSERT INTO chunks_fts(rowid, heading, text) VALUES (?, ?, ?)",
                    (chunk_cursor.lastrowid, heading, chunk),
                )
                chunk_count += 1
            documents.append(
                {"url": url, "path": local_path, "sha256": digest, "title": title}
            )
        connection.commit()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "source": {
            "name": source_name,
            "index_url": index_url,
            "index_path": "sources/index.txt",
            "index_sha256": _sha256_bytes(index_bytes),
        },
        "database": {
            "path": "content.sqlite3",
            "sha256": _sha256_file(database_path),
        },
        "document_count": len(documents),
        "chunk_count": chunk_count,
        "documents": documents,
    }
    (path / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode = DELETE;
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            source_url TEXT NOT NULL UNIQUE,
            local_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            ordinal INTEGER NOT NULL,
            heading TEXT NOT NULL,
            text TEXT NOT NULL,
            UNIQUE(document_id, ordinal)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            heading,
            text,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )


def _validate(path: Path) -> dict[str, Any]:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        raise PackError(f"missing RAG pack manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
            raise PackError("unsupported RAG pack schema")
        database = manifest["database"]
        documents = manifest["documents"]
        source = manifest["source"]
        if not isinstance(database, dict) or not isinstance(documents, list):
            raise PackError("invalid RAG pack manifest structure")
        database_path = _safe_member(path, str(database["path"]))
        if _sha256_file(database_path) != database["sha256"]:
            raise PackError("RAG pack database checksum mismatch")
        index_path = _safe_member(path, str(source["index_path"]))
        if _sha256_file(index_path) != source["index_sha256"]:
            raise PackError("RAG pack index checksum mismatch")
        for document in documents:
            source_path = _safe_member(path, str(document["path"]))
            if _sha256_file(source_path) != document["sha256"]:
                raise PackError(f"RAG source checksum mismatch: {document['path']}")
        with _readonly_connection(database_path) as connection:
            document_count = connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            chunk_count = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if document_count != manifest["document_count"] or chunk_count != manifest["chunk_count"]:
            raise PackError("RAG pack row counts do not match its manifest")
    except PackError:
        raise
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PackError(f"invalid RAG pack at {path}: {exc}") from exc
    return manifest


def _safe_member(root: Path, relative: str) -> Path:
    member = (root / relative).resolve()
    try:
        member.relative_to(root.resolve())
    except ValueError as exc:
        raise PackError(f"RAG pack path escapes its directory: {relative}") from exc
    if not member.is_file():
        raise PackError(f"missing RAG pack file: {relative}")
    return member


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _index_pages(text: str, index_url: str) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    pages: list[tuple[str, str]] = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
        label, link = match.groups()
        url = urljoin(index_url, link.strip())
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.path.lower().endswith(".md"):
            continue
        if url not in seen:
            seen.add(url)
            pages.append((label.strip(), url))
    return tuple(pages)


def _chunk_markdown(text: str, *, max_chunk_chars: int) -> Iterable[tuple[str, str]]:
    heading = ""
    section: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            yield from _split_section(heading, "\n".join(section).strip(), max_chunk_chars)
            heading = match.group(1).strip()
            section = []
        else:
            section.append(line)
    yield from _split_section(heading, "\n".join(section).strip(), max_chunk_chars)


def _split_section(heading: str, text: str, max_chars: int) -> Iterable[tuple[str, str]]:
    if not text:
        return
    remaining = text
    while remaining:
        if len(remaining) <= max_chars:
            yield heading, remaining
            return
        cut = remaining.rfind("\n\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = remaining.rfind(" ", 0, max_chars + 1)
        if cut < 1:
            cut = max_chars
        yield heading, remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip()


def _document_title(text: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _safe_name(url: str) -> str:
    stem = Path(urlparse(url).path).stem or "page"
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-.")
    return (safe or "page")[:80]


def _as_bytes(value: bytes | str) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
