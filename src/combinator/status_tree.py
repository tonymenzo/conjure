"""``StatusTree`` — Tree subclass that preserves per-span label colors
under the cursor row.

Textual's default ``Tree.render_label`` calls ``label.stylize(style)``
with the cursor style applied to the entire label. That style sets a
foreground color (textual's default is ``$background`` for a reverse
look), and the stylize call appends a span that overrides any
existing per-character colors in the label.

We want the colored ``●`` status dot in agent rows to stay green /
yellow / dim regardless of whether the row is currently the cursor.
This subclass strips the foreground color out of the style applied
to labels — background, bold, underline, dim, blink etc are kept so
the cursor is still visually distinguishable, but the per-span
markup colors (the dot, the agent label) survive the override.
"""

from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import Tree
from textual.widgets._tree import TOGGLE_STYLE


class StatusTree(Tree):
    """``Tree`` that preserves rich-markup colors under the cursor."""

    def render_label(self, node, base_style, style):
        sanitized = Style(
            bgcolor=style.bgcolor,
            bold=style.bold,
            italic=style.italic,
            underline=style.underline,
            dim=style.dim,
            blink=style.blink,
            blink2=style.blink2,
            reverse=style.reverse,
            strike=style.strike,
        )
        label = node._label.copy()
        label.stylize(sanitized)
        if node._allow_expand:
            prefix = (
                self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE,
                base_style + TOGGLE_STYLE,
            )
        else:
            prefix = ("", base_style)
        return Text.assemble(prefix, label)
