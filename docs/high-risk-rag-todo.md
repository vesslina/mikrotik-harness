# HIGH RISK RouterOS CLI RAG — integration TODO

The portable Markdown + SQLite FTS5 pack is implemented. This file tracks the remaining live-agent
integration; RAG is not a hidden dependency of HIGH RISK mode.

- Inject only official MikroTik RouterOS documentation and CLI reference pages into HIGH RISK.
- Keep a separate corpus from future READY-mode operational guidance.
- Record `ros_major`, topic, relevant commands, source URL, retrieved date and documentation
  version for each chunk.
- Retrieve deterministically from task keywords and the RouterOS version known for the connected
  router; do not claim a retrieved document applies if its version/topic is unknown.
- Supply retrieved context as evidence, never as a command execution authority. The HIGH RISK
  seven-step prompt still requires inspection and post-change verification.
- Add source refresh policy and version-specific retrieval evaluation before enabling it for live
  agent prompts. Portable/offline validation is already covered by pack tests.
