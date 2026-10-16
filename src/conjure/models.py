"""Model catalog: every model an agent may spawn on, and how it resolves.

An agent choosing a model for a child has to know what exists. Models
come from three kinds of source, and the catalog lists them all under
one id scheme, ``<source>/<model>``:

- **API LLMs** from the config's ``llms:`` (orchestral engine). Each
  entry ``<name>`` contributes ``<name>/<its model>``. An
  ``openai_compatible`` entry with ``discover: true`` (a model access
  gateway, vLLM, LiteLLM, ...) also contributes every model its
  endpoint lists at ``GET <base_url>/models``.
- **Claude subscription** (``claude/<alias>``: ``claude/opus``,
  ``claude/haiku``, ...) when the ``claude`` CLI and SDK are present,
  with the exact model each alias resolves to for this account.
- **Codex / ChatGPT subscription** (``codex/<slug>``) when ``codex`` is
  installed and logged in, from Codex's own model list.

``models.allow`` (glob patterns over ids) is the policy: what any agent
in the session may use. ``models.default`` picks the model for a child
spawned without one, and ``models.notes`` (plus each LLM's ``note``)
is guidance shown to agents.

``Spawn(model=...)`` takes a catalog id, a bare model name or alias when
unique (``haiku``, ``gpt-6-astra``), or the name of an ``llms`` entry.
The runtime resolves it to the engine, LLM entry and provider model
before anything spawns, and refuses ids outside the policy with
``code=model_denied`` and the list of what is allowed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import threading
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from conjure.errors import ModelDenied

if TYPE_CHECKING:
    from conjure.record import AgentRecord, AgentSpec
    from conjure.runtime import Runtime


@dataclass
class ModelEntry:
    id: str  # what Spawn(model=...) takes: "<source>/<model>"
    engine: str  # orchestral | claude_agent | codex
    source: str  # an llms entry name, "claude", or "codex"
    model: str  # the provider's model name (or the CLI alias)
    provider: str  # anthropic, openai_compatible, ... / claude subscription / chatgpt subscription
    access: str  # "api" | "subscription"
    resolves_to: str | None = None  # exact model an alias resolves to
    context_window: int | None = None
    description: str = ""
    note: str = ""
    # USD per million tokens {input, output, cached_input?, source}, when
    # known (config or public list price); None for subscriptions and
    # unknown models.
    price_per_mtok: dict[str, Any] | None = None

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class ModelsPolicy:
    allow: list[str] = field(default_factory=lambda: ["*"])
    default: str | None = None
    notes: dict[str, str] = field(default_factory=dict)


class ModelHub:
    """Per-runtime catalog plus policy. ``llm_configs`` are the config's
    ``llms:`` entries as dicts; subscription sources are probed lazily on
    first use and cached (``refresh()`` re-reads)."""

    def __init__(
        self,
        *,
        llm_configs: Mapping[str, Mapping[str, Any]] | None = None,
        policy: ModelsPolicy | None = None,
        claude: bool | None = None,
        codex: bool | None = None,
        codex_child_model: str | None = None,
        discover_timeout_s: float = 10.0,
    ) -> None:
        self._llms = {k: dict(v) for k, v in (llm_configs or {}).items()}
        self.policy = policy or ModelsPolicy()
        self._claude = claude
        self._codex = codex
        self._codex_child_model = codex_child_model
        self._timeout = discover_timeout_s
        self._entries: list[ModelEntry] | None = None
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()

    # ----- catalog -----

    def entries(self, *, refresh: bool = False) -> list[ModelEntry]:
        with self._lock:
            if self._entries is None or refresh:
                self._errors = {}
                self._entries = self._build()
            return list(self._entries)

    def refresh(self) -> list[ModelEntry]:
        return self.entries(refresh=True)

    @property
    def errors(self) -> dict[str, str]:
        """Sources that couldn't be listed (gateway unreachable, ...)."""
        self.entries()
        return dict(self._errors)

    def allowed(self, entry: ModelEntry) -> bool:
        return any(fnmatch.fnmatchcase(entry.id, pat) for pat in self.policy.allow)

    def allowed_entries(self) -> list[ModelEntry]:
        return [e for e in self.entries() if self.allowed(e)]

    def describe(self) -> dict[str, Any]:
        rows = []
        for e in self.allowed_entries():
            row = e.public()
            note = self.policy.notes.get(e.id) or e.note
            if note:
                row["note"] = note
            rows.append(row)
        out: dict[str, Any] = {
            "ok": True,
            "models": rows,
            "default": self.policy.default,
            "how": (
                "Spawn(model=<id>) — or a bare alias / model name when "
                "unique. The id picks the engine for you."
            ),
        }
        if self._errors:
            out["unavailable_sources"] = dict(self._errors)
        return out

    # ----- resolution -----

    def find(self, name: str) -> ModelEntry | None:
        """``name`` as an id, an ``llms`` entry name, or a bare model /
        alias / resolved model name when exactly one entry matches."""
        entries = self.entries()
        for e in entries:
            if e.id == name:
                return e
        by_source = [e for e in entries if e.source == name and e.engine == "orchestral"]
        configured = [e for e in by_source if e.model == (self._llms.get(name) or {}).get("model")]
        if configured:
            return configured[0]
        if len(by_source) == 1:
            return by_source[0]
        bare = [e for e in entries if name in (e.model, e.resolves_to)]
        if len(bare) == 1:
            return bare[0]
        exact = [e for e in bare if e.model == name]
        if len(exact) == 1:
            return exact[0]
        # Several aliases for one exact model (claude/default and
        # claude/opus → claude-opus-5-5): it's the same model either way;
        # prefer the named alias over "default".
        if bare and len({(e.engine, e.resolves_to or e.model) for e in bare}) == 1:
            return sorted(bare, key=lambda e: e.model == "default")[0]
        if len(bare) > 1:
            allowed = [e for e in bare if self.allowed(e)]
            if len(allowed) == 1:
                return allowed[0]
        return None

    def resolve_spec(
        self, spec: "AgentSpec", *, parent_engine: str | None
    ) -> "AgentSpec":
        """Return ``spec`` with ``engine`` / ``llm`` / ``model`` set from
        its model choice (or the policy default), after checking the
        choice against ``models.allow``. Raises ``ModelDenied``."""
        name = spec.model
        explicit_engine = spec.engine not in (None, "", "auto")
        if explicit_engine and spec.engine not in _CATALOG_ENGINES:
            return spec  # an engine the catalog doesn't cover (scripted, custom)
        if not name and not self.policy.default and self.policy.allow == ["*"]:
            return spec  # nothing named, nothing restricted: no catalog needed
        if not name:
            # ``models.default`` is for children. The root (no parent) runs
            # on its own configuration, e.g. the llms entry's ``model``.
            if self.policy.default and parent_engine is not None:
                name = self.policy.default
            else:
                entry = self._implied(spec, parent_engine)
                if entry is None or self.allowed(entry):
                    return spec
                raise ModelDenied(
                    f"no model named, and the implied one ({entry.id}) isn't "
                    f"allowed in this session. Pick one: {self._allowed_ids()}"
                )
        entry = self._find_for(name, spec.engine if explicit_engine else None)
        if entry is None:
            raise ModelDenied(
                f"unknown model {name!r}. Available: {self._allowed_ids()} "
                "(ModelCatalog describes them)"
            )
        if not self.allowed(entry):
            raise ModelDenied(
                f"model {entry.id} isn't allowed in this session. "
                f"Allowed: {self._allowed_ids()}"
            )
        if explicit_engine and spec.engine != entry.engine:
            raise ModelDenied(
                f"model {entry.id} runs on engine {entry.engine}, not {spec.engine}"
            )
        update: dict[str, Any] = {"engine": entry.engine, "model": entry.model}
        if entry.engine == "orchestral":
            update["llm"] = entry.source
        return spec.model_copy(update=update)

    def _find_for(self, name: str, engine: str | None) -> ModelEntry | None:
        if engine is None:
            return self.find(name)
        matches = [
            e for e in self.entries()
            if e.engine == engine and name in (e.id, e.model, e.resolves_to, e.source)
        ]
        # Not on that engine: find it anywhere, so the caller can say
        # which engine it does run on.
        return matches[0] if matches else self.find(name)

    def _implied(self, spec: "AgentSpec", parent_engine: str | None) -> ModelEntry | None:
        """The entry a spec without a model would run on."""
        from conjure.engines import resolve_engine_name

        engine = spec.engine
        if engine in (None, "", "auto"):
            engine = "codex" if parent_engine == "codex" else resolve_engine_name(engine)
        child = parent_engine is not None
        for e in self.entries():
            if e.engine != engine:
                continue
            if engine == "orchestral" and e.source == (spec.llm or "default"):
                if e.model == (self._llms.get(e.source) or {}).get("model") or len(
                    [x for x in self.entries() if x.source == e.source]
                ) == 1:
                    return e
            if engine == "claude_agent" and e.model == ("haiku" if child else "default"):
                return e
            if engine == "codex":
                want = self._codex_child_model if child else None
                if want and e.model == want:
                    return e
                if not want and e.description.startswith("(default)"):
                    return e
        return None

    def _allowed_ids(self) -> list[str]:
        return [e.id for e in self.allowed_entries()]

    # ----- sources -----

    def _build(self) -> list[ModelEntry]:
        out: list[ModelEntry] = []
        for name, cfg in self._llms.items():
            out.extend(self._api_entries(name, cfg))
        if self._claude if self._claude is not None else _claude_available():
            out.extend(_claude_entries())
        if self._codex if self._codex is not None else shutil.which("codex") is not None:
            out.extend(_codex_entries())
        seen: set[str] = set()
        unique = []
        for e in out:
            if e.id not in seen:
                seen.add(e.id)
                unique.append(e)
        return unique

    def _api_entries(self, name: str, cfg: Mapping[str, Any]) -> list[ModelEntry]:
        from conjure.llm import provider_spec

        provider = str(cfg.get("provider", "")).lower()
        note = str(cfg.get("note") or "")
        try:
            default_model = provider_spec(provider)["default_model"]
        except ValueError:
            default_model = ""
        configured = cfg.get("model") or default_model
        entries: list[ModelEntry] = []
        from conjure.pricing import PriceBook

        book = PriceBook(cfg.get("pricing"), use_list_prices=True)

        def priced(model: str) -> dict[str, Any] | None:
            price = book.price(model)
            return price.public() if price is not None else None

        if configured:
            entries.append(
                ModelEntry(
                    id=f"{name}/{configured}", engine="orchestral", source=name,
                    model=configured, provider=provider, access="api", note=note,
                    price_per_mtok=priced(configured),
                )
            )
        if provider == "openai_compatible" and cfg.get("discover"):
            try:
                for mid in _list_openai_models(cfg, self._timeout):
                    entries.append(
                        ModelEntry(
                            id=f"{name}/{mid}", engine="orchestral", source=name,
                            model=mid, provider=provider, access="api", note=note,
                            price_per_mtok=priced(mid),
                        )
                    )
            except Exception as exc:
                self._errors[name] = f"couldn't list {cfg.get('base_url')}/models: {exc}"
        return entries


def _list_openai_models(cfg: Mapping[str, Any], timeout: float) -> list[str]:
    from conjure.llm import gateway_auth

    api_key, headers = gateway_auth(dict(cfg))
    req_headers = {"Accept": "application/json", **headers}
    if api_key:
        req_headers.setdefault("Authorization", f"Bearer {api_key}")
    url = str(cfg["base_url"]).rstrip("/") + "/models"
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured URL)
        data = json.loads(resp.read())
    items = data.get("data") if isinstance(data, dict) else data
    ids = []
    for item in items or []:
        mid = item.get("id") if isinstance(item, dict) else item
        if isinstance(mid, str) and mid:
            ids.append(mid)
    return ids


def _claude_available() -> bool:
    from conjure.engines import _claude_agent_available

    return _claude_agent_available()


_CLAUDE_ALIASES = ("default", "opus", "sonnet", "haiku")
_CATALOG_ENGINES = frozenset({"orchestral", "claude_agent", "codex"})


def _claude_entries() -> list[ModelEntry]:
    from conjure.engines import claude_agent as ca

    if not ca._MODEL_MAP and os.environ.get("CONJURE_MODEL_PROBE", "1") != "0":
        # One CLI start, no prompt: the account's alias → model table.
        import asyncio

        try:
            asyncio.run(asyncio.wait_for(ca._probe_models({}), timeout=60))
        except Exception:
            pass
    table = dict(ca._MODEL_MAP)
    aliases = [a for a in table if table.get(a) and a != table.get(a)] or list(_CLAUDE_ALIASES)
    out = []
    for alias in aliases:
        out.append(
            ModelEntry(
                id=f"claude/{alias}", engine="claude_agent", source="claude",
                model=alias, provider="claude subscription", access="subscription",
                resolves_to=table.get(alias),
                description="(default) the CLI's default model" if alias == "default" else "",
            )
        )
    return out


def _codex_entries() -> list[ModelEntry]:
    from conjure.engines.codex import codex_default_model

    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    default = codex_default_model()
    out = []
    for m in sorted(
        (m for m in data.get("models") or [] if isinstance(m, dict)),
        key=lambda m: m.get("priority", 1 << 30),
    ):
        slug = m.get("slug")
        if not slug or m.get("visibility", "list") != "list":
            continue
        desc = str(m.get("description") or "")
        if slug == default:
            desc = f"(default) {desc}".strip()
        window = m.get("context_window") or m.get("max_context_window")
        out.append(
            ModelEntry(
                id=f"codex/{slug}", engine="codex", source="codex", model=slug,
                provider="chatgpt subscription", access="subscription",
                context_window=int(window) if isinstance(window, (int, float)) else None,
                description=desc,
            )
        )
    return out


def build_model_hub(config: Any) -> ModelHub:
    models_cfg = getattr(config, "models", None)
    policy = ModelsPolicy()
    if models_cfg is not None:
        policy = ModelsPolicy(
            allow=list(models_cfg.allow or ["*"]),
            default=models_cfg.default,
            notes=dict(models_cfg.notes or {}),
        )
    llms = {name: cfg.model_dump() for name, cfg in (config.llms or {}).items()}
    codex_cfg = getattr(config, "codex", None)
    return ModelHub(
        llm_configs=llms,
        policy=policy,
        codex_child_model=getattr(codex_cfg, "child_model", None),
    )


MODEL_GUIDANCE = """
## Models

`ModelCatalog()` lists the models you may spawn agents on: API models
(including any model access gateway this session is configured with)
and subscription models (Claude, Codex), each with its id, engine and
any note about what it's for. Pick a child's model with
`Spawn(model="<id>")` (also `Call` / combinator `spec`s); the id
chooses the engine. Match the model to the task: a strong model for
hard reasoning, a cheaper one for routine work (`price_per_mtok`, where
known, is USD per million input / output tokens). A model outside the
session's policy is refused with the list of allowed ones.
"""
