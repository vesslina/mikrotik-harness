# Portable RAG packs

`mth rag` activates the local documentation pack. If the selected directory is empty, mth reads
the official MikroTik `llms.txt` index, downloads every linked Markdown page, builds a SQLite FTS5
index beside the sources, writes SHA-256 checksums to `manifest.json`, validates the result, and
then promotes the completed temporary directory. It never replaces a non-empty directory.

If the directory already contains a valid pack, loading and searching perform no network request.
Copy the complete folder to an offline machine and select it with either:

```powershell
$env:MTH_RAG_HOME = "E:\routeros-rag"
mth rag --query "ip firewall nat"
```

or:

```powershell
mth rag --rag-dir E:\routeros-rag --query "ip firewall nat"
```

The default directory is `.mth/rag`. A pack contains:

```text
manifest.json
content.sqlite3
sources/
  index.txt
  0001-....md
  ...
```

The corpus is built locally and is not included in project releases. The manifest keeps source
URLs and retrieval time for attribution and refresh decisions. A checksum mismatch, missing file,
unsupported schema, or path escaping the pack directory is a hard failure; mth does not silently
redownload over a damaged portable copy.

SQLite FTS5 is the only retrieval dependency in the first pass. It is part of Python's SQLite
build on supported distributions and does not require an LLM, embedding model, Chroma server, or
background process. Dense embeddings remain an optional future layer if retrieval evaluations
demonstrate queries that lexical search cannot serve reliably.
