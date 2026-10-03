import logging
from types import SimpleNamespace

import pytest

from services.logging_setup import SafeJSONFormatter
from services.rules import (AntiProfanitySystem, MAX_XP, captcha, hierarchy_error, is_staff,
                            level_for_xp, role_ids_for_level, xp_for_level)


@pytest.mark.parametrize("level", [0, 1, 4, 5, 9, 10, 14, 15, 19, 20, 999, 1000])
def test_level_formula_and_bracket_boundaries(level, config):
    assert level_for_xp(xp_for_level(level)) == level
    if level:
        assert level_for_xp(xp_for_level(level)-1) == level-1
    brackets = config["guilds"][1]["levels"]["brackets"]
    expected = 0 if level < 5 else 1 if level < 10 else 2 if level < 15 else 3 if level < 20 else 4
    assert role_ids_for_level(level, brackets) == set(brackets[expected]["roles"])


def test_all_level_boundaries():
    for level in range(1001):
        assert level_for_xp(xp_for_level(level)) == level
    assert level_for_xp(MAX_XP + 99999999) == 1000
    assert level_for_xp(-1) == 0


def test_captcha_is_bounded_integer_math():
    for _ in range(500):
        question, answer = captcha()
        a, op, b, _, _ = question.split()
        a, b = int(a), int(b)
        assert 1 <= a <= 10 and 1 <= b <= 10
        expected = a+b if op == "+" else a-b if op == "−" else a*b
        assert int(answer) == expected and expected >= 0


@pytest.mark.parametrize("text,blocked", [("БЛЯТЬ!", True), ("б.л.я.т.ь", True), ("б л я т ь", True),
                                           ("Обычное сообщение", False), ("страхуй меня", False),
                                           ("суперсукаслово", False), ("сука!", True), ("СУКА", True)])
def test_profanity(text, blocked):
    assert AntiProfanitySystem(["блять", "сука", "хуй"], ["страхуй"]).matches(text) is blocked


def test_whitelist_overrides_exact_word():
    assert not AntiProfanitySystem(["test"], ["TEST"]).matches("test")


def test_staff_role_owner_admin(member):
    cfg = {"staff_role_id": 555}
    assert not is_staff(member, cfg)
    member.roles = [SimpleNamespace(id=555)]
    assert is_staff(member, cfg)
    member.roles = []
    member.guild_permissions.administrator = True
    assert is_staff(member, cfg)
    member.guild_permissions.administrator = False
    member.id = member.guild.owner_id
    assert is_staff(member, cfg)


@pytest.mark.parametrize("target_id,top,admin,expected", [(1, 2, False, True), (10, 50, False, True),
                                                         (99, 2, False, True), (10, 2, True, True),
                                                         (10, 2, False, False)])
def test_hierarchy(member, target_id, top, admin, expected):
    actor = SimpleNamespace(id=20, top_role=30)
    member.id, member.top_role = target_id, top
    member.guild_permissions.administrator = admin
    assert bool(hierarchy_error(actor, member, member.guild.me, timeout=True)) is expected


def test_owner_bypasses_actor_hierarchy_but_not_bot(member):
    actor = SimpleNamespace(id=member.guild.owner_id, top_role=1)
    assert hierarchy_error(actor, member, member.guild.me) is None
    member.top_role = 200
    assert hierarchy_error(actor, member, member.guild.me)


def test_logging_redacts_token_and_exception():
    record = logging.LogRecord("test", logging.ERROR, "", 1, "secret-abc authorization=bad", (), None)
    record.exc_info = (ValueError, ValueError("private DM"), None)
    result = SafeJSONFormatter("secret-abc").format(record)
    assert "secret-abc" not in result and "bad" not in result and "private DM" not in result
