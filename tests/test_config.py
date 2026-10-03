import copy

import pytest
import yaml

from services.config import load_config


def test_template_requires_production_ids():
    with pytest.raises(ValueError, match="Replace guild ID"):
        load_config()
    assert load_config(offline=True)


@pytest.mark.parametrize("change", ["overlap", "gap", "shared_role", "early_roles", "timeout", "xp_bounds", "staff_auto"])
def test_invalid_config_rejected(config, tmp_path, change):
    cfg = copy.deepcopy(config)
    g = cfg["guilds"][1]
    if change == "overlap":
        g["levels"]["brackets"][1]["min"] = 4
    elif change == "gap":
        g["levels"]["brackets"][1]["min"] = 6
    elif change == "shared_role":
        g["levels"]["brackets"][1]["roles"] = g["levels"]["brackets"][0]["roles"]
    elif change == "early_roles":
        g["verification"]["give_join_roles_before_verification"] = True
    elif change == "timeout":
        g["moderation"]["warn_actions"][3]["timeout_minutes"] = 50000
    elif change == "xp_bounds":
        g["xp"]["xp_min"] = 10
    elif change == "staff_auto":
        g["join_roles"][0] = g["staff_role_id"]
    path = tmp_path/"bad.yml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(path), offline=True)
