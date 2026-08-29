"""A small, copyable Markdown + SQLite FTS5 retrieval pack."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from mth.core.mcp_client.runtime import project_root

SCHEMA_VERSION = 1
PACK_ENV = "MTH_RAG_HOME"
CHECKSUM_ENV = "MTH_RAG_CHECKSUM"
DEFAULT_MAX_CHUNK_CHARS = 2_400
DEFAULT_MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_DOCUMENTS = 4_000
DEFAULT_MAX_TOTAL_DOWNLOAD_BYTES = 256 * 1024 * 1024
DOWNLOAD_ATTEMPTS = 4

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
    def load(
        cls,
        path: str | Path | None = None,
        *,
        checksum_path: str | Path | None = None,
    ) -> RagPack:
        pack_path = resolve_pack_dir(path)
        _verify_external_checksum(pack_path, checksum_path)
        manifest = _validate(pack_path)
        return cls(pack_path, manifest)

    def search(self, query: str, *, limit: int = 5) -> tuple[RagHit, ...]:
        if limit < 1:
            return ()
        words = re.findall(r"[^\W_]+(?:[-./][^\W_]+)*", query, flags=re.UNICODE)
        if not words:
            return ()
        query_terms = _search_terms(query)
        terms = [f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words]
        database = self.path / str(self.manifest["database"]["path"])
        try:
            with _readonly_connection(database) as connection:
                rows = []
                for operator in (" AND ", " OR "):
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
                        (operator.join(terms), 100),
                    ).fetchall()
                    if rows:
                        break
        except sqlite3.Error as error:
            raise PackError(f"RAG index search failed: {error}") from error
        ranked: list[tuple[float, tuple[Any, ...]]] = []
        for row in rows:
            heading_terms = _search_terms(str(row[1]))
            source_terms = _search_terms(str(row[2]))
            # ponytail: lightweight lexical rerank; add embeddings only if the eval set outgrows
            # heading/URL disambiguation.
            score = (
                -float(row[4])
                + 4 * len(query_terms.intersection(heading_terms))
                + 2 * len(query_terms.intersection(source_terms))
            )
            ranked.append((score, row))
        ranked.sort(key=lambda item: item[0], reverse=True)
        hits: list[RagHit] = []
        seen: set[tuple[str, str]] = set()
        for score, row in ranked:
            key = (str(row[2]), str(row[1]))
            if key in seen:
                continue
            seen.add(key)
            hits.append(
                RagHit(
                    text=str(row[0]),
                    heading=str(row[1]),
                    source_url=str(row[2]),
                    source_path=str(row[3]),
                    score=score,
                )
            )
            if len(hits) == limit:
                break
        return tuple(hits)


def _search_terms(value: str) -> set[str]:
    return {
        word.casefold()
        for word in re.findall(r"[^\W_]+", value, flags=re.UNICODE)
    }


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
    checksum_path: str | Path | None = None,
) -> RagPack:
    """Load without network when populated; build only when the directory is empty."""
    pack_path = resolve_pack_dir(path)
    if pack_path.exists() and any(pack_path.iterdir()):
        return RagPack.load(pack_path, checksum_path=checksum_path)
    if not index_url:
        raise PackError(f"RAG pack is empty at {pack_path}; an index URL is required to build it")
    return build_pack(
        pack_path,
        index_url=index_url,
        fetcher=fetcher or fetch_url,
        source_name=source_name,
        max_chunk_chars=max_chunk_chars,
        checksum_path=checksum_path,
    )


def fetch_url(url: str) -> bytes:
    """Fetch one bounded source using only the standard library."""
    _validate_source_url(url, url, resolve_host=True)
    request = Request(url, headers={"User-Agent": "mikrotik-harness-rag/0.1"})
    failure: OSError | HTTPException
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with urlopen(  # noqa: S310 - caller controls source URL
                request, timeout=30
            ) as response:
                final_url = getattr(response, "geturl", lambda: url)()
                _validate_source_url(str(final_url), url, resolve_host=True)
                payload = bytes(response.read(DEFAULT_MAX_DOWNLOAD_BYTES + 1))
            break
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise PackError(f"HTTP {error.code} while fetching RAG source: {url}") from error
            failure = error
        except (OSError, HTTPException) as error:
            failure = error
        if attempt + 1 == DOWNLOAD_ATTEMPTS:
            raise PackError(
                f"failed to fetch RAG source after {DOWNLOAD_ATTEMPTS} attempts: {url}: "
                f"{failure}"
            ) from failure
        time.sleep(2**attempt)
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
    checksum_path: str | Path | None = None,
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
    return RagPack.load(pack_path, checksum_path=checksum_path)


def write_external_checksum(
    pack_path: str | Path,
    checksum_path: str | Path | None = None,
) -> Path:
    """Write a release-side checksum for the pack manifest.

    The manifest already covers the database, index, and every source file. Keeping this
    checksum outside the pack gives a release operator an independent integrity anchor.
    """

    resolved_pack = resolve_pack_dir(pack_path)
    _validate(resolved_pack)
    manifest = resolved_pack / "manifest.json"
    target = _resolve_checksum_path(resolved_pack, checksum_path, for_write=True)
    assert target is not None
    _ensure_external_checksum_path(resolved_pack, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{_sha256_file(manifest)}  manifest.json\n", encoding="ascii")
    return target


def _resolve_checksum_path(
    pack_path: Path,
    requested: str | Path | None,
    *,
    for_write: bool = False,
) -> Path | None:
    if requested is not None:
        return Path(requested).expanduser().resolve()
    configured = os.environ.get(CHECKSUM_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    sibling = pack_path.with_name(f"{pack_path.name}.sha256")
    return sibling.resolve() if for_write or sibling.is_file() else None


def _verify_external_checksum(pack_path: Path, requested: str | Path | None) -> None:
    checksum_path = _resolve_checksum_path(pack_path, requested)
    if checksum_path is None:
        return
    _ensure_external_checksum_path(pack_path, checksum_path)
    try:
        line = checksum_path.read_text(encoding="ascii").strip()
        match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(manifest\.json)", line)
        if match is None:
            raise PackError(f"invalid external RAG checksum file: {checksum_path}")
        manifest = _safe_member(pack_path, match.group(2))
        if _sha256_file(manifest) != match.group(1).lower():
            raise PackError("external RAG manifest checksum mismatch")
    except PackError:
        raise
    except (OSError, UnicodeError) as error:
        raise PackError(f"cannot read external RAG checksum: {checksum_path}") from error


def _ensure_external_checksum_path(pack_path: Path, checksum_path: Path) -> None:
    try:
        checksum_path.resolve().relative_to(pack_path.resolve())
    except ValueError:
        return
    raise PackError("external RAG checksum must be stored outside the pack directory")


def _write_pack(
    path: Path,
    *,
    index_url: str,
    fetcher: Fetcher,
    source_name: str,
    max_chunk_chars: int,
) -> None:
    _validate_source_url(index_url, index_url)
    source_dir = path / "sources"
    source_dir.mkdir(parents=True)
    index_bytes = _as_bytes(fetcher(index_url))
    index_path = source_dir / "index.txt"
    index_path.write_bytes(index_bytes)
    pages = _index_pages(index_bytes.decode("utf-8", errors="replace"), index_url)
    if not pages:
        raise PackError(f"no Markdown page links found in RAG index: {index_url}")
    if len(pages) > DEFAULT_MAX_DOCUMENTS:
        raise PackError(
            f"RAG index contains {len(pages)} pages; maximum is {DEFAULT_MAX_DOCUMENTS}"
        )

    database_path = path / "content.sqlite3"
    documents: list[dict[str, str]] = []
    chunk_count = 0
    total_download_bytes = len(index_bytes)
    with closing(sqlite3.connect(database_path)) as connection:
        _create_schema(connection)
        for ordinal, (label, url) in enumerate(pages, start=1):
            payload = _as_bytes(fetcher(url))
            total_download_bytes += len(payload)
            if total_download_bytes > DEFAULT_MAX_TOTAL_DOWNLOAD_BYTES:
                raise PackError(
                    "RAG corpus exceeds "
                    f"{DEFAULT_MAX_TOTAL_DOWNLOAD_BYTES} total downloaded bytes"
                )
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
        if not parsed.path.lower().endswith(".md"):
            continue
        try:
            _validate_source_url(url, index_url)
        except PackError:
            continue
        if url not in seen:
            seen.add(url)
            pages.append((label.strip(), url))
    return tuple(pages)


def _validate_source_url(
    url: str,
    origin_url: str,
    *,
    resolve_host: bool = False,
) -> None:
    try:
        parsed = urlparse(url)
        origin = urlparse(origin_url)
        hostname = parsed.hostname
        origin_hostname = origin.hostname
    except ValueError as error:
        raise PackError(f"invalid RAG source URL: {url}") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PackError(f"RAG source URL is not a safe HTTP(S) URL: {url}")
    if origin_hostname is None or _origin(parsed) != _origin(origin):
        raise PackError(f"RAG source must stay on the index origin: {url}")
    loopback = _is_loopback_host(hostname)
    if parsed.scheme == "http" and not loopback:
        raise PackError("plain HTTP RAG sources are allowed only on loopback")
    if _is_private_literal(hostname) and not loopback:
        raise PackError(f"private or local RAG source is blocked: {url}")
    if resolve_host:
        try:
            addresses = {
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
            }
        except (OSError, ValueError) as error:
            raise PackError(f"could not resolve RAG source host: {hostname}") from error
        if not addresses or (
            any(not address.is_loopback for address in addresses)
            if loopback
            else any(not address.is_global for address in addresses)
        ):
            raise PackError(f"RAG source host resolves to a private or local address: {hostname}")


def _origin(parsed: Any) -> tuple[str, str, int]:
    scheme = str(parsed.scheme).casefold()
    hostname = str(parsed.hostname).casefold()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, hostname, port


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_private_literal(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    )


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
