import pytest

from agentguard.policy import Policy, PolicyError


def test_precedence_deny_beats_approval_beats_allow():
    p = Policy.from_dict({
        "name": "t",
        "allow": ["cloud__*"],
        "require_approval": ["cloud__delete_*"],
        "deny": ["cloud__delete_iam*"],
    })
    assert p.rule_for("cloud__get_logs") == "allow"
    assert p.rule_for("cloud__delete_bucket") == "require_approval"
    assert p.rule_for("cloud__delete_iam_role") == "deny"


def test_unlisted_tools_are_denied_and_invisible(prod_policy):
    assert prod_policy.rule_for("github__merge_pr") == "deny"
    assert not prod_policy.is_visible("github__merge_pr")
    assert prod_policy.is_visible("cloud__delete_bucket")  # needs approval, but visible


def test_costs_are_integer_paise(prod_policy):
    assert prod_policy.cost_paise("cloud__create_instance") == 25_000
    assert prod_policy.cost_paise("cloud__get_logs") == 50  # default ₹0.5
    assert prod_policy.budget_paise == 100_000


def test_typo_in_policy_is_rejected_at_load_time():
    with pytest.raises(PolicyError, match="require_aproval"):
        Policy.from_dict({"name": "t", "require_aproval": ["cloud__delete_*"]})


def test_fractional_paise_rejected():
    with pytest.raises(PolicyError):
        Policy.from_dict({"name": "t", "budget_inr": 0.001})


def test_pattern_list_given_as_string_is_rejected():
    # tuple("cloud__get_*") would contain "*", which allows every tool.
    with pytest.raises(PolicyError, match="allow"):
        Policy.from_dict({"name": "t", "allow": "cloud__get_*"})


@pytest.mark.parametrize("value", ["3", True, 0, -1, 2.5])
def test_bad_loop_limit_is_rejected(value):
    with pytest.raises(PolicyError, match="max_identical_calls"):
        Policy.from_dict({"name": "t", "loop": {"max_identical_calls": value}})


@pytest.mark.parametrize("value", ["abc", "NaN", "Infinity", -5, True, [1]])
def test_bad_money_is_rejected(value):
    with pytest.raises(PolicyError):
        Policy.from_dict({"name": "t", "budget_inr": value})


def test_non_mapping_policy_is_rejected():
    with pytest.raises(PolicyError):
        Policy.from_dict(["name", "t"])


def test_only_reads_are_safe_to_retry(prod_policy):
    assert prod_policy.is_safe_to_retry("cloud__list_instances")
    assert not prod_policy.is_safe_to_retry("cloud__create_instance")


# ------------------------------------------------------------ argument rules

ARG_POLICY = {
    "name": "t",
    "allow": ["cloud__*"],
    "deny": ["cloud__delete_iam_role"],
    "arg_rules": [
        {"tool": "cloud__scale_service", "when": {"replicas": {"gt": 6}}, "then": "require_approval",
         "reason": "big"},
        {"tool": "cloud__create_instance", "when": {"instance_type": {"not_in": ["t3.micro"]}}, "then": "deny"},
        {"tool": "cloud__note", "when": {"body": {"matches": r"(?i)password\s*="}}, "then": "deny"},
        {"tool": "cloud__delete_iam_role", "when": {"role": {"eq": "temp"}}, "then": "require_approval"},
    ],
}


def test_arg_rules_tighten_by_argument():
    p = Policy.from_dict(ARG_POLICY)
    assert p.rule_for_call("cloud__scale_service", {"service": "api", "replicas": 4}) == ("allow", None)
    assert p.rule_for_call("cloud__scale_service", {"service": "api", "replicas": 8}) == ("require_approval", "big")
    assert p.rule_for_call("cloud__create_instance", {"instance_type": "p4d.24xlarge"})[0] == "deny"
    assert p.rule_for_call("cloud__note", {"body": "PASSWORD = hunter2"})[0] == "deny"


def test_arg_rules_never_loosen():
    # A rule saying "require_approval" can't open up a denied tool.
    p = Policy.from_dict(ARG_POLICY)
    assert p.rule_for_call("cloud__delete_iam_role", {"role": "temp"})[0] == "deny"


def test_type_confusion_fails_closed():
    # replicas="15" can't be compared to 6; skipping the rule would let quoting bypass it.
    p = Policy.from_dict(ARG_POLICY)
    assert p.rule_for_call("cloud__scale_service", {"service": "api", "replicas": "15"})[0] == "require_approval"
    assert p.rule_for_call("cloud__scale_service", {"service": "api", "replicas": True})[0] == "require_approval"


def test_missing_argument_does_not_match():
    p = Policy.from_dict(ARG_POLICY)
    assert p.rule_for_call("cloud__create_instance", {"name": "x"})[0] == "allow"   # server default applies


@pytest.mark.parametrize("bad_rule", [
    {"tool": "x", "when": {"n": {"gt": 1}}, "then": "allow"},              # rules can't loosen
    {"tool": "x", "when": {"n": {"bigger": 1}}, "then": "deny"},           # unknown operator
    {"tool": "x", "when": {"n": {"gt": "6"}}, "then": "deny"},             # non-numeric threshold
    {"tool": "x", "when": {"s": {"matches": "("}}, "then": "deny"},        # broken regex
    {"tool": "x", "when": {"s": {"in": "api"}}, "then": "deny"},           # in needs a list
    {"tool": "x", "when": {}, "then": "deny"},                             # empty when
    {"tool": "x", "when": {"n": {"gt": 1}}, "then": "deny", "typo": 1},    # unknown key
])
def test_bad_arg_rules_fail_at_load(bad_rule):
    with pytest.raises(PolicyError):
        Policy.from_dict({"name": "t", "arg_rules": [bad_rule]})


def test_prod_safe_catches_the_observed_overscaling():
    p = Policy.from_yaml("skills/policies/prod-safe.yaml")
    assert p.rule_for_call("cloud__scale_service", {"service": "api", "replicas": 6})[0] == "allow"
    assert p.rule_for_call("cloud__scale_service", {"service": "api", "replicas": 12})[0] == "require_approval"
    assert p.rule_for_call("cloud__scale_service", {"service": "db", "replicas": 2})[0] == "require_approval"
    assert p.rule_for_call("github__comment_on_issue", {"body": "api_key: sk-123"})[0] == "deny"
