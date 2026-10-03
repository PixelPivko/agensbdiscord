import json
from pathlib import Path

import yaml


def load_config(path: str = "config/config.yml", *, offline: bool = False) -> dict:
    with open(path, encoding="utf-8") as stream:
        cfg = yaml.safe_load(stream)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("guilds"), dict) or not cfg["guilds"]:
        raise ValueError("config must contain a nonempty guilds mapping")
    if cfg.get("logging_level") not in {"INFO", "WARNING", "ERROR"}:
        raise ValueError("logging_level must be INFO, WARNING or ERROR")
    if not isinstance(cfg.get("database"), str) or not cfg["database"]:
        raise ValueError("database path required")
    for guild_id, g in cfg["guilds"].items():
        if type(guild_id) is not int or guild_id < 0 or (not offline and not guild_id):
            raise ValueError("Replace guild ID 0 in config/config.yml with your Discord server ID")
        for name in ("staff_role_id", "verification_channel_id", "modlog_channel_id"):
            if type(g.get(name)) is not int or g[name] < 0 or (not offline and g[name] == 0):
                raise ValueError(f"Configure a positive {name} for guild {guild_id}")
        join = g["join_roles"]
        if not join or len(join) != len(set(join)) or any(type(r) is not int or r <= 0 for r in join):
            raise ValueError("join_roles must be unique positive IDs")
        v = g["verification"]
        if v["timeout_days"] != 7 or not 1 <= v["max_attempts"] <= 10:
            raise ValueError("Verification requires seven days and 1–10 attempts")
        if not 60 <= v["captcha_ttl_seconds"] <= 3600 or v["resend_cooldown_seconds"] < 10:
            raise ValueError("Invalid captcha time limits")
        if type(v["give_join_roles_before_verification"]) is not bool:
            raise ValueError("give_join_roles_before_verification must be boolean")
        # This build deliberately enforces the user's verified-only role policy.
        if v["give_join_roles_before_verification"]:
            raise ValueError("This secure build only allows give_join_roles_before_verification: false")
        all_roles, expected_min = [], 0
        for bracket in g["levels"]["brackets"]:
            if bracket["min"] != expected_min or bracket["max"] < bracket["min"]:
                raise ValueError("Level brackets must cover 0–1000 without gaps or overlap")
            expected_min = bracket["max"] + 1
            roles = bracket["roles"]
            if len(roles) != 2 or any(type(r) is not int or r <= 0 for r in roles):
                raise ValueError("Each bracket needs two positive role IDs")
            all_roles.extend(roles)
        if expected_min != 1001 or len(set(all_roles)) != len(all_roles) or set(join) & set(all_roles):
            raise ValueError("Invalid or overlapping level roles")
        if g["staff_role_id"] in set(join + all_roles):
            raise ValueError("Staff role cannot be an automatic role")
        if g["levels"]["repair_interval_seconds"] < 60 or g["levels"]["sync_delay_seconds"] < 0.1:
            raise ValueError("Role repair frequency is too high")
        x = g["xp"]
        if not 1 <= x["xp_min"] <= x["xp_max"] <= 100 or x["message_cooldown_seconds"] < 1:
            raise ValueError("Invalid XP bounds/cooldown")
        for name in ("min_message_length", "duplicate_window_seconds", "spam_window_seconds", "spam_max_messages"):
            if type(x[name]) is not int or x[name] < 1:
                raise ValueError(f"Invalid xp.{name}")
        if not 1 <= x["settlement_seconds"] <= 30:
            raise ValueError("settlement_seconds must be 1–30")
        for threshold, action in g["moderation"]["warn_actions"].items():
            if type(threshold) is not int or threshold < 1 or not isinstance(action, dict):
                raise ValueError("Invalid warning threshold")
            if set(action) - {"timeout_minutes", "notify_staff", "ban"}:
                raise ValueError("Unknown warn action")
            if "timeout_minutes" in action and not 1 <= action["timeout_minutes"] <= 40320:
                raise ValueError("Timeout must be 1–40320 minutes")
            for flag in ("notify_staff", "ban"):
                if flag in action and type(action[flag]) is not bool:
                    raise ValueError(f"{flag} must be boolean")
        for field in ("blacklist", "whitelist"):
            words = json.loads(Path(g["profanity"][field]).read_text(encoding="utf-8"))
            if not isinstance(words, list) or any(not isinstance(w, str) or not w.strip() for w in words):
                raise ValueError(f"{field} must contain nonempty strings")
    return cfg
