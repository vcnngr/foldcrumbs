"""engram — persistent cross-session memory for coding agents.

File-based memory store + lifecycle hooks for Claude Code. No external
service, no vector DB: retrieval is done by the agent's own grep over the
memory folder, and a local OpenAI-compatible LLM is used only for async
distillation. Pure stdlib, so hook scripts never fail on a missing import.
"""

__version__ = "0.1.0"
