"""Envelopes — immutable messages flowing between agents.

An envelope captures the full state of one message in transit. ``seq``
is monotonic *per receiving inbox*, not globally; ``msg_id`` is globally
unique. ``thread_id`` defaults to the opening message's ``msg_id`` and
is carried through subsequent replies via ``in_reply_to``.

Implemented as a frozen slotted dataclass — Envelopes are constructed on
every Send and every supervision event, and serialized into the journal
the same way. The dataclass form is ~5× faster to construct than the
pydantic equivalent and ~2× cheaper on memory. The journal-and-wire
shape (with the ``from_`` field rendered as ``from``) is preserved by
``to_dict`` / ``from_dict``; ``model_dump`` / ``model_validate`` /
``model_copy`` aliases keep call-site compatibility with the older
pydantic API.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

from spawn.address import Address


def _empty_headers() -> dict[str, str]:
    return {}


@dataclass(frozen=True, slots=True)
class Envelope:
    seq: int
    msg_id: str
    from_: Address
    to: Address
    thread_id: str
    body: Any
    in_reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=_empty_headers)
    ts: float = field(default_factory=time.time)

    # ----- Wire format (journal + recv/wait_for return values) ---------

    def to_dict(self) -> dict[str, Any]:
        """Pydantic-equivalent ``model_dump(by_alias=True)`` shape.

        The ``from_`` field becomes ``from`` on the wire so existing
        journal files and LLM-facing payloads keep their format."""
        return {
            "seq": self.seq,
            "msg_id": self.msg_id,
            "from": self.from_.to_dict(),
            "to": self.to.to_dict(),
            "thread_id": self.thread_id,
            "in_reply_to": self.in_reply_to,
            "body": self.body,
            "headers": dict(self.headers),
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Envelope":
        # Tolerant of either ``from`` or ``from_`` so test fixtures
        # written before the alias change still load.
        sender_raw = data.get("from", data.get("from_"))
        if sender_raw is None:
            raise KeyError("envelope payload missing 'from'")
        return cls(
            seq=int(data["seq"]),
            msg_id=str(data["msg_id"]),
            from_=Address.from_dict(sender_raw)
            if isinstance(sender_raw, dict)
            else sender_raw,
            to=Address.from_dict(data["to"])
            if isinstance(data["to"], dict)
            else data["to"],
            thread_id=str(data["thread_id"]),
            in_reply_to=data.get("in_reply_to"),
            body=data.get("body"),
            headers=dict(data.get("headers") or {}),
            ts=float(data.get("ts", time.time())),
        )

    # ----- Pydantic interop (kept lean — call sites lean on these) -----

    def model_dump(self, *, by_alias: bool = False, **_: Any) -> dict[str, Any]:
        """``by_alias`` is honored for the ``from_`` → ``from`` rename;
        every other field is alias-free, so the two paths converge."""
        if by_alias:
            return self.to_dict()
        # Same shape but with the python attribute name preserved.
        out = self.to_dict()
        out["from_"] = out.pop("from")
        return out

    @classmethod
    def model_validate(cls, data: Any) -> "Envelope":
        if isinstance(data, cls):
            return data
        return cls.from_dict(data)

    def model_copy(
        self, *, update: dict[str, Any] | None = None
    ) -> "Envelope":
        if not update:
            return self
        return dataclasses.replace(self, **update)
