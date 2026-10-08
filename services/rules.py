"""Pure rules: no Discord, filesystem or network side effects."""
import math
import re
import secrets
import unicodedata

MAX_LEVEL = 1000


def xp_for_level(level: int) -> int:
    if not 0 <= level <= MAX_LEVEL:
        raise ValueError("Level must be between 0 and 1000")
    return 50 * level * (level + 1)


MAX_XP = xp_for_level(MAX_LEVEL)


def level_for_xp(xp: int) -> int:
    return min(MAX_LEVEL, (math.isqrt(1 + 4 * max(0, xp) // 50) - 1) // 2)


def role_ids_for_level(level: int, brackets: list[dict]) -> set[int]:
    for bracket in brackets:
        if bracket["min"] <= level <= bracket["max"]:
            return set(bracket["roles"])
    raise ValueError("Level outside configured brackets")


def captcha() -> tuple[str, str]:
    a, b = secrets.randbelow(10) + 1, secrets.randbelow(10) + 1
    op = secrets.choice(("+", "−", "×"))
    if op == "−":
        a, b = max(a, b), min(a, b)
    answer = a + b if op == "+" else a - b if op == "−" else a * b
    return f"{a} {op} {b} = ?", str(answer)


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().replace("ё", "е").split())


class AntiProfanitySystem:
    """Whole-word matching, with optional separators between letters; no substring bans."""

    def __init__(self, blacklist: list[str], whitelist: list[str]):
        self.whitelist = {normalize(word) for word in whitelist}
        self.patterns = []
        for word in blacklist:
            word = normalize(word)
            if word and word not in self.whitelist:
                pattern = r"(?<!\w)" + r"[\s._*\-]*".join(map(re.escape, word)) + r"(?!\w)"
                self.patterns.append(re.compile(pattern))

    def matches(self, text: str) -> bool:
        text = normalize(text)
        text = re.sub(r"\w+", lambda m: " " if m[0] in self.whitelist else m[0], text)
        return any(pattern.search(text) for pattern in self.patterns)


def is_staff(member, config: dict) -> bool:
    return (member.id == member.guild.owner_id or member.guild_permissions.administrator
            or any(role.id == config["staff_role_id"] for role in member.roles))


def hierarchy_error(actor, target, bot, *, timeout: bool = False) -> str | None:
    if target.id in {target.guild.owner_id, bot.id, actor.id}:
        return "Тут стоп: к владельцу, себе или этому боту действие применять нельзя."
    if actor.id != target.guild.owner_id and actor.top_role <= target.top_role:
        return "У участника роль не ниже твоей. Попроси старшего дежурного помочь."
    if bot.top_role <= target.top_role:
        return "Мой допуск ниже нужного: роль бота должна быть выше роли участника."
    if timeout and target.guild_permissions.administrator:
        return "Тут правило самого Discord: администратору timeout не назначить."
    return None
