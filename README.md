# Combinator

A multi-agent harness built on [`orchestral`](https://pypi.org/project/orchestral-ai/).
Organized around three primitives — recursive agent spawning, addressable
mailboxes, and capability passing — with a small library of FP-style
combinators (`agent_map`, `agent_fold`, `agent_filter`, `agent_fixed_point`)
built on top.

See [`DESIGN.md`](DESIGN.md) for the design philosophy and architecture.

## Install (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[test]
pytest -q
```

## Status

Pre-release. v0.1 substrate under construction.
