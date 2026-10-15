"""Shared pytest fixtures.

Concrete fixtures (ephemeral runtime, scripted agents, temp stores) are
added as the corresponding modules land.
"""

import os

# Never start a real ``claude`` CLI to read its model table in tests.
os.environ.setdefault("CONJURE_MODEL_PROBE", "0")
