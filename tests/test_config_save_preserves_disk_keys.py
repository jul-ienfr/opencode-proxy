"""Anti-perte de configuration : une addition disque survit à une sauvegarde.

[2026-11-18] Incident : un proxy démarré avant l'ajout de
`glm-5.3-flash-free` a réécrit `config.yaml` et supprimé l'entrée (route
déclarée mais cible absente du catalogue). La fusion doit restaurer toute clé
présente sur disque et absente de la mémoire.
"""

from config.settings import _restore_missing_keys


def test_disk_only_top_level_key_is_restored():
    disk = {"a": 1, "b": 2}
    mem = {"a": 1}
    assert _restore_missing_keys(disk, mem) == 1
    assert mem == {"a": 1, "b": 2}


def test_disk_only_nested_key_is_restored():
    disk = {"models": {"glm-5.3-flash-free": {"protocol": "openai"}, "x": 1}}
    mem = {"models": {"x": 1}}
    assert _restore_missing_keys(disk, mem) == 1
    assert mem["models"]["glm-5.3-flash-free"] == {"protocol": "openai"}


def test_memory_value_wins_when_key_exists():
    disk = {"a": 1}
    mem = {"a": 2}
    assert _restore_missing_keys(disk, mem) == 0
    assert mem["a"] == 2


def test_non_dict_inputs_are_ignored():
    assert _restore_missing_keys([], {}) == 0
    assert _restore_missing_keys({}, "nope") == 0
