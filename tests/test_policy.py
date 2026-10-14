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
