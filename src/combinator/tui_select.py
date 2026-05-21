"""Toggle textual's mouse-tracking on/off so the user can select &
copy text natively in the terminal.

Textual enables mouse tracking at startup (``linux_driver._enable_mouse_support``)
so click-drag becomes app events instead of terminal selection. When
the user just wants to copy a few lines out of the chat, that's
exactly the wrong default. This helper exposes a thin runtime toggle
the apps wire to a function key (``F5``):

- ``on=False`` writes the disable counterparts of textual's mouse
  init sequences. The terminal goes back to native click-drag-to-
  select. Scrollwheel still works in most terminals (handled by the
  terminal itself when the app isn't listening).
- ``on=True`` calls the driver's own ``_enable_mouse_support`` so we
  match the exact sequence textual originally emitted.

Private-attribute access (``app._driver``, ``driver._enable_mouse_support``)
is necessary — there's no public toggle in textual 8.x. The shape of
those internals is stable across the 0.x → 8.x line.
"""

from __future__ import annotations

from typing import Any


# Disable sequences mirror ``linux_driver._enable_mouse_support`` ones,
# with the trailing ``h`` flipped to ``l`` (DEC private mode reset).
_DISABLE_MOUSE_TRACKING = (
    "\x1b[?1000l"
    "\x1b[?1003l"
    "\x1b[?1015l"
    "\x1b[?1006l"
)


def set_mouse_tracking(app: Any, *, on: bool) -> None:
    """Enable or disable mouse-tracking escape codes on the app's
    driver. No-op if the driver is not yet attached or the call
    fails."""
    driver = getattr(app, "_driver", None)
    if driver is None:
        return
    try:
        if on:
            driver._enable_mouse_support()  # noqa: SLF001
        else:
            driver.write(_DISABLE_MOUSE_TRACKING)
            driver.flush()
    except Exception:
        pass
