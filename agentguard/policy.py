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
from decimal import Decimal, InvalidOperation
import re
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal

import yaml

Rule = Literal["deny", "require_approval", "allow"]

_TOP_LEVEL_KEYS = {
    "name", "description", "allow", "deny", "require_approval",
    "tool_costs", "budget_inr", "loop", "safe_to_retry", "llm_costs", "llm_max_tokens", "arg_rules",
}
_ARG_RULE_KEYS = {"tool", "when", "then", "reason"}
_OPERATORS = {"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "matches"}
_SEVERITY = {"allow": 0, "require_approval": 1, "deny": 2}
_LOOP_INT_KEYS = {"max_identical_calls", "max_calls_per_task", "window_seconds", "max_same_intent"}
_LOOP_KEYS = _LOOP_INT_KEYS | {"intent_args"}
_LLM_PRICE_KEYS = {"input_per_million_inr", "cached_input_per_million_inr", "output_per_million_inr"}


class PolicyError(ValueError):
    """The policy file is malformed. Raised at load time, never at call time."""


@dataclass(frozen=True)
class LoopLimits:
    max_identical_calls: int = 3     # same tool + same args within the window
    max_calls_per_task: int = 50     # any tool, whole task lifetime
    window_seconds: int = 600
    # Same *intent* with varying args (get_logs lines=5,6,7; scale replicas=4,6,8,12).
    # None disables it. See Policy.intent_args_for() for what "intent" means.
    max_same_intent: int | None = None
    intent_args: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ArgRule:
    """`when` every condition matches the call's arguments, the call needs at least `then`.

    Rules only *tighten*: they can raise allow -> require_approval -> deny, never lower
    a tool's base rule. So adding a rule can't open anything up."""
    tool: str                                   # pattern, like the allow/deny lists
    conditions: tuple[tuple[str, str, Any], ...]   # (arg, operator, operand)
    then: Rule
    reason: str

    def matches(self, tool: str, args: dict[str, Any]) -> bool:
        return fnmatchcase(tool, self.tool) and all(
            _condition_holds(args, arg, op, operand) for arg, op, operand in self.conditions)


@dataclass(frozen=True)
class LLMPrice:
    """Paise per million tokens (integers, like all money here)."""
    input: int
    cached_input: int
    output: int


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
    llm_prices: dict[str, LLMPrice] = field(default_factory=dict)   # model -> price; unpriced = refused
    llm_max_tokens: int = 2048                                       # cap on output tokens per LLM call
    arg_rules: tuple[ArgRule, ...] = ()

    # ---------- loading ----------

    @classmethod
    def from_yaml(cls, path: str | Path) -> Policy:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Policy:
        if not isinstance(data, dict):
            raise PolicyError("policy must be a mapping")
        # Reject unknown keys: in a safety config, a typo like `require_aproval`
        # silently turning into "no approval needed" is the worst possible failure.
        unknown = set(data) - _TOP_LEVEL_KEYS
        if unknown:
            raise PolicyError(f"unknown policy keys: {sorted(unknown)}")
        if not isinstance(data.get("name"), str) or not data["name"]:
            raise PolicyError("policy needs a name")

        loop_data = data.get("loop") or {}
        if not isinstance(loop_data, dict):
            raise PolicyError("loop must be a mapping")
        unknown_loop = set(loop_data) - _LOOP_KEYS
        if unknown_loop:
            raise PolicyError(f"unknown loop keys: {sorted(unknown_loop)}")
        for key in _LOOP_INT_KEYS & set(loop_data):
            _positive_int(loop_data[key], f"loop.{key}")
        intent_args = loop_data.get("intent_args") or {}
        if not isinstance(intent_args, dict):
            raise PolicyError("loop.intent_args must map tool patterns to lists of argument names")
        loop_data = {**loop_data, "intent_args": {
            str(tool): _string_list(names, f"loop.intent_args[{tool}]") for tool, names in intent_args.items()}}

        raw_costs = data.get("tool_costs") or {}
        if not isinstance(raw_costs, dict):
            raise PolicyError("tool_costs must be a mapping")
        costs = {str(pattern): _to_paise(v) for pattern, v in raw_costs.items()}

        return cls(
            name=data["name"],
            description=str(data.get("description") or ""),
            allow=_patterns(data, "allow"),
            deny=_patterns(data, "deny"),
            require_approval=_patterns(data, "require_approval"),
            tool_costs_paise=costs,
            budget_paise=_to_paise(data.get("budget_inr", 0)),
            loop=LoopLimits(**loop_data),
            safe_to_retry=_patterns(data, "safe_to_retry"),
            llm_prices=_llm_prices(data.get("llm_costs") or {}),
            llm_max_tokens=_positive_int(data.get("llm_max_tokens", 2048), "llm_max_tokens"),
            arg_rules=_arg_rules(data.get("arg_rules") or []),
        )

    # ---------- questions ----------

    def rule_for_call(self, tool: str, args: dict[str, Any]) -> tuple[Rule, str | None]:
        """The rule for this exact call: the tool's rule, tightened by any matching
        argument rule. Returns (rule, reason of the rule that tightened it, if any)."""
        rule, reason = self.rule_for(tool), None
        for arg_rule in self.arg_rules:
            if _SEVERITY[arg_rule.then] > _SEVERITY[rule] and arg_rule.matches(tool, args):
                rule, reason = arg_rule.then, arg_rule.reason
        return rule, reason

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

    def intent_args_for(self, tool: str) -> tuple[str, ...] | None:
        """Argument names that define a call's intent, if configured for this tool.
        Unconfigured tools use a default (see guard.intent_key)."""
        if tool in self.loop.intent_args:
            return self.loop.intent_args[tool]
        for pattern, names in self.loop.intent_args.items():
            if fnmatchcase(tool, pattern):
                return names
        return None


def _condition_holds(args: dict[str, Any], arg: str, op: str, operand: Any) -> bool:
    """Does argument `arg` satisfy `op operand`?

    Missing argument: no match (the server's own default applies).
    Present but not comparable (replicas="15" against gt 6): MATCH, i.e. fail closed.
    Otherwise quoting a number would slip past `replicas > 6`.
    """
    if arg not in args:
        return False
    value = args[arg]
    if op in ("gt", "gte", "lt", "lte"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return True
        return {"gt": value > operand, "gte": value >= operand, "lt": value < operand, "lte": value <= operand}[op]
    if op == "matches":
        return not isinstance(value, str) or operand.search(value) is not None
    if op == "eq":
        return value == operand
    if op == "ne":
        return value != operand
    if op == "in":
        return value in operand
    return value not in operand   # not_in


def _arg_rules(raw: Any) -> tuple[ArgRule, ...]:
    if not isinstance(raw, list):
        raise PolicyError("arg_rules must be a list")
    rules = []
    for i, item in enumerate(raw):
        where = f"arg_rules[{i}]"
        if not isinstance(item, dict) or set(item) - _ARG_RULE_KEYS or not {"tool", "when", "then"} <= set(item):
            raise PolicyError(f"{where} needs tool, when, then (and optional reason), got {item!r}")
        if item["then"] not in ("require_approval", "deny"):
            raise PolicyError(f"{where}.then must be require_approval or deny (rules only tighten)")
        when = item["when"]
        if not isinstance(when, dict) or not when:
            raise PolicyError(f"{where}.when must map argument names to conditions")
        conditions = []
        for arg, cond in when.items():
            if not isinstance(cond, dict) or len(cond) != 1 or next(iter(cond)) not in _OPERATORS:
                raise PolicyError(f"{where}.when.{arg} must be one of {sorted(_OPERATORS)}: value, got {cond!r}")
            (op, operand), = cond.items()
            if op in ("gt", "gte", "lt", "lte") and (isinstance(operand, bool) or not isinstance(operand, (int, float))):
                raise PolicyError(f"{where}.when.{arg}.{op} needs a number, got {operand!r}")
            if op in ("in", "not_in"):
                if not isinstance(operand, list):
                    raise PolicyError(f"{where}.when.{arg}.{op} needs a list, got {operand!r}")
                operand = tuple(operand)
            if op == "matches":
                try:
                    operand = re.compile(str(operand))
                except re.error as e:
                    raise PolicyError(f"{where}.when.{arg}.matches is not a valid regex: {e}") from None
            conditions.append((str(arg), op, operand))
        rules.append(ArgRule(str(item["tool"]), tuple(conditions), item["then"],
                             str(item.get("reason") or f"{item['tool']} with these arguments")))
    return tuple(rules)


def _positive_int(value: Any, name: str) -> int:
    # bool is a subclass of int in Python, so `true` would pass as 1.
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyError(f"{name} must be a positive integer, got {value!r}")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise PolicyError(f"{name} must be a non-empty list of argument names, got {value!r}")
    return tuple(value)


def _llm_prices(raw: Any) -> dict[str, LLMPrice]:
    if not isinstance(raw, dict):
        raise PolicyError("llm_costs must map model names to prices")
    prices = {}
    for model, price in raw.items():
        if not isinstance(price, dict) or set(price) - _LLM_PRICE_KEYS or                 not {"input_per_million_inr", "output_per_million_inr"} <= set(price):
            raise PolicyError(f"llm_costs[{model}] needs input_per_million_inr and output_per_million_inr "
                              f"(optional cached_input_per_million_inr), got {price!r}")
        input_ = _to_paise(price["input_per_million_inr"])
        prices[str(model)] = LLMPrice(
            input=input_,
            cached_input=_to_paise(price.get("cached_input_per_million_inr", price["input_per_million_inr"])),
            output=_to_paise(price["output_per_million_inr"]),
        )
    return prices


def _matches(tool: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(tool, p) for p in patterns)


def _patterns(data: dict[str, Any], key: str) -> tuple[str, ...]:
    # Must be a real list. tuple("cloud__*") would split a lone string into
    # characters, and the "*" among them matches every tool.
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise PolicyError(f"{key} must be a list of non-empty strings, got {value!r}")
    return tuple(value)


def _to_paise(rupees: Any) -> int:
    # Money as integer paise: floats can't represent 0.1 exactly, and Redis
    # INCRBY is atomic on integers. Decimal(str(x)) avoids float rounding on parse.
    if isinstance(rupees, bool) or not isinstance(rupees, (int, float, str)):
        raise PolicyError(f"invalid rupee amount: {rupees!r}")
    try:
        paise = Decimal(str(rupees)) * 100
    except InvalidOperation:
        raise PolicyError(f"invalid rupee amount: {rupees!r}") from None
    if not paise.is_finite() or paise != paise.to_integral_value() or paise < 0:
        raise PolicyError(f"invalid rupee amount: {rupees!r}")
    return int(paise)
