"""Browser UI for hylo sessions — a tmux-free alternative to the
cockpit, plus views tmux can't offer (agent graph, timeline, replay).

``SessionView`` (``state``) is the read/write surface over a live
runtime or a replayed journal; ``WebServer`` (``server``) serves it and
the static page. ``hylo web`` and ``hylo run --web`` wire them up.
"""

from hylo.web.server import WebServer
from hylo.web.state import ReplayCache, SessionView

__all__ = ["WebServer", "SessionView", "ReplayCache"]
