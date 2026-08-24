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
- a separate local field-recipe collection loads Markdown dropped into `docs/field-recipes/` and
  exposes only the read-only `search_field_recipes` tool in HIGH RISK;
- field recipes are filtered by the connected board when available and are never fetched over HTTP.

Remaining only when the source can support it honestly:

- source-provided RouterOS major/document version metadata per page;
- a refresh policy and a larger version-specific retrieval evaluation set;
- diagnostics/internet golden paths and additional reviewed field cards.
