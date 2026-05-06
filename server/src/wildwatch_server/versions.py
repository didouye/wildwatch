"""Pinned versions the server expects.

Bumped manually when a new agent / capture release ships. Kept in
lockstep with the agent's `pyproject.toml` version (no automated sync;
intentional: the bump should appear in the same PR that changes the
agent code, so reviewers can spot a missed bump).
"""

from __future__ import annotations

LATEST_AGENT_VERSION: str = "1.2.0"
