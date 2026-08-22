# HIGH RISK RouterOS CLI RAG — TODO

This is deliberately the next milestone, not a hidden dependency of HIGH RISK mode.

- Source only official MikroTik RouterOS documentation and CLI reference pages.
- Keep a separate corpus from future READY-mode operational guidance.
- Record `ros_major`, topic, relevant commands, source URL, retrieved date and documentation
  version for each chunk.
- Retrieve deterministically from task keywords and the RouterOS version known for the connected
  router; do not claim a retrieved document applies if its version/topic is unknown.
- Supply retrieved context as evidence, never as a command execution authority. The HIGH RISK
  seven-step prompt still requires inspection and post-change verification.
- Add source refresh, licensing/attribution handling, offline tests and version-specific retrieval
  evaluation before enabling it for live agent prompts.
