# Portable RAG packs

`mth rag` activates the local documentation pack. If the selected directory is empty, mth reads
the official MikroTik `llms.txt` index, downloads every linked Markdown page, builds a SQLite FTS5
index beside the sources, writes SHA-256 checksums to `manifest.json`, validates the result, and
then promotes the completed temporary directory. It never replaces a non-empty directory.
Transient timeouts, connection drops, HTTP 429, and HTTP 5xx responses receive four bounded
attempts with a short backoff. A failed all-or-nothing build removes its temporary directory.

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

## Agent use

HIGH RISK loads an existing valid pack once when the model is selected; it never downloads during
a chat turn. The local `search_routeros_docs` tool accepts a short English RouterOS menu-path query
and returns at most five source-linked excerpts; their URLs are shown in the transcript. Per-hit and total context limits prevent a copied pack from exhausting
the model window. Results include the connected RouterOS version but keep applicability explicitly
unknown: documentation is evidence, not live router state, an instruction, or authority to act.

PLAN and READY do not label this official manual as their operational RAG. Their future RAG1 corpus
must contain reviewed project conventions and cleaned golden paths; until that corpus exists, the
typed tool schemas and runbook definitions remain the deterministic READY guidance.
