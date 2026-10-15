"""Browser UI for conjure sessions — a tmux-free alternative to the
cockpit, plus views tmux can't offer (agent graph, timeline, replay).

``SessionView`` (``state``) is the read/write surface over a live
runtime or a replayed journal; ``WebServer`` (``server``) serves it and
the static page. ``conjure web`` and ``conjure run --web`` wire them up.
"""

from conjure.web.server import WebServer
from conjure.web.state import ReplayCache, SessionView

__all__ = ["WebServer", "SessionView", "ReplayCache"]
