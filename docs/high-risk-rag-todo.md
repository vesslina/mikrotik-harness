# HIGH RISK RouterOS CLI RAG — status

The portable Markdown + SQLite FTS5 pack and live local search tool are implemented. RAG remains
optional: HIGH RISK has no hidden online, embedding-model, or vector-database dependency.

Implemented:

- official Markdown pages stay separate from future READY operational guidance;
- the model searches in English through `search_routeros_docs`, which solves Russian task to
  English corpus retrieval without a translation or embedding model;
- excerpts have per-hit and total context limits, source URLs, retrieval time, connected RouterOS
  version and `applicability=unknown`;
- structured results and the system prompt mark documentation as untrusted evidence;
- AND-first lexical retrieval falls back to OR only when no exact multi-term result exists.

Remaining only when the source can support it honestly:

- source-provided RouterOS major/document version metadata per page;
- a refresh policy and a larger version-specific retrieval evaluation set.
