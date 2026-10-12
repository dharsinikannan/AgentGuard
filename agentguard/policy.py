"""Policy packs ("skills"): YAML files that say what an agent may do.

A policy answers four questions about a namespaced tool name like `cloud__delete_bucket`:
  1. rule_for()          -> "deny" | "require_approval" | "allow"
  2. cost_paise()        -> how much one call costs against the task budget
  3. is_safe_to_retry()  -> may the gateway retry it after a dropped connection?
  4. is_visible()        -> should the agent even see it in tools/list?

Precedence is deny > require_approval > allow > (implicit) deny. Anything not
mentioned is denied: a new downstream tool is invisible until someone opts in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml

Rule = Literal["deny", "require_approval", "allow"]

_TOP_LEVEL_KEYS = {
    "name", "description", "allow", "deny", "require_approval",
    "tool_costs", "budget_inr", "loop", "safe_to_retry",
}
_LOOP_KEYS = {"max_identical_calls", "max_calls_per_task", "window_seconds"}


class PolicyError(ValueError):
    """The policy file is malformed. Raised at load time, never at call time."""


@dataclass(frozen=True)
class LoopLimits:
    max_identical_calls: int = 3     # same tool + same args within the window
    max_calls_per_task: int = 50     # any tool, whole task lifetime
    window_seconds: int = 600


@dataclass(frozen=True)
class Policy:
    name: str
    description: str = ""
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    require_approval: tuple[str, ...] = ()
    tool_costs_paise: dict[str, int] = field(default_factory=dict)
    budget_paise: int = 0
    loop: LoopLimits = field(default_factory=LoopLimits)
    safe_to_retry: tuple[str, ...] = ()

    # ---------- loading ----------

    @classmethod
    def from_yaml(cls, path: str | Path) -> Policy:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        # Reject unknown keys: in a safety config, a typo like `require_aproval`
        # silently turning into "no approval needed" is the worst possible failure.
        unknown = set(data) - _TOP_LEVEL_KEYS
        if unknown:
            raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
        if not data.get("name"):
            raise PolicyError("policy needs a name")

        loop_data = data.get("loop") or {}
        unknown_loop = set(loop_data) - _LOOP_KEYS
        if unknown_loop:
            raise PolicyError(f"unknown loop keys: {sorted(unknown_loop)}")

        costs = {pattern: _to_paise(v) for pattern, v in (data.get("tool_costs") or {}).items()}
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            allow=tuple(data.get("allow") or ()),
            deny=tuple(data.get("deny") or ()),
            require_approval=tuple(data.get("require_approval") or ()),
            tool_costs_paise=costs,
            budget_paise=_to_paise(data.get("budget_inr", 0)),
            loop=LoopLimits(**loop_data),
            safe_to_retry=tuple(data.get("safe_to_retry") or ()),
        )

    # ---------- questions ----------

    def rule_for(self, tool: str) -> Rule:
        if _matches(tool, self.deny):
            return "deny"
        if _matches(tool, self.require_approval):
            return "require_approval"
        if _matches(tool, self.allow):
            return "allow"
        return "deny"

    def is_visible(self, tool: str) -> bool:
        return self.rule_for(tool) != "deny"

    def cost_paise(self, tool: str) -> int:
        # Exact name wins over patterns; first matching pattern wins after that.
        if tool in self.tool_costs_paise:
            return self.tool_costs_paise[tool]
        for pattern, cost in self.tool_costs_paise.items():
            if pattern != "default" and fnmatchcase(tool, pattern):
                return cost
        return self.tool_costs_paise.get("default", 0)

    def is_safe_to_retry(self, tool: str) -> bool:
        return _matches(tool, self.safe_to_retry)


def _matches(tool: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(tool, p) for p in patterns)


def _to_paise(rupees: Any) -> int:
    # Money as integer paise: floats can't represent 0.1 exactly, and Redis
    # INCRBY is atomic on integers. Decimal(str(x)) avoids float rounding on parse.
    paise = Decimal(str(rupees)) * 100
    if paise != paise.to_integral_value() or paise < 0:
        raise PolicyError(f"invalid rupee amount: {rupees!r}")
    return int(paise)
