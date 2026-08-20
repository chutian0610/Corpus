"""corpus: LLM-driven wiki builder (CLI-first).

Stage 1 (current): CLI for ingest + wiki build + quality certification.
Stage 2 (planned): wiki health-check & optimization.
Stage 3 (planned): multi-path query & retrieval.

设计原则：
- CLI-first：所有功能通过 `corpus <subcommand>` 调用
- Skill-friendly：CLI 设计让 agent 通过 Bash 自然使用
- LLM-decoupled: corpus 本身不调任何 LLM，extract/compile 责任在 agent 端
- Storage as pure functions：未来需要 MCP 时只是 CLI 的薄包装
"""

__version__ = "0.2.0"
