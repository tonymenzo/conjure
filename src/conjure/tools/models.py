"""``ModelCatalog``: the models an agent may spawn agents on.

See ``conjure.models``. Served whenever the runtime has a model hub.
"""

from __future__ import annotations

from typing import Any

from conjure.tools._base import (
    RuntimeField,
    StateField,
    StatelessRuntimeTool,
    resolve_token,
)
from conjure.tools.primitives import _err


def model_catalog_impl(*, token: str, refresh: bool = False) -> dict[str, Any]:
    resolved = resolve_token(token)
    if resolved is None:
        return _err("no_runtime", "tool is not bound to a runtime")
    runtime, _caller = resolved
    hub = getattr(runtime, "models", None)
    if hub is None:
        return _err("models_unavailable", "this runtime has no model catalog")
    if refresh:
        hub.refresh()
    return hub.describe()


class ModelCatalogTool(StatelessRuntimeTool):
    """List the models you may spawn agents on: API models (including a
    model access gateway, if configured) and subscription models
    (Claude, Codex). Each row has the ``id`` to pass as
    ``Spawn(model=...)``, its engine, provider, access (api /
    subscription), and any note on what it's for. ``refresh`` re-lists
    gateway endpoints."""

    refresh: bool = RuntimeField(default=False, description="Re-list the sources (e.g. a gateway's /models).")
    runtime_token: str = StateField(description="(internal) caller token.")

    def _run(self) -> dict[str, Any]:
        return model_catalog_impl(token=self.runtime_token, refresh=bool(self.refresh))


MODEL_TOOL_CLASSES = (ModelCatalogTool,)
