"""[audit 2026-10-30] Sauvegarde des clés API — régression P0.

L'UI ne renvoie que des clés masquées pour les lignes existantes. L'ancien
merge ne préservait que les lignes POSTées : sauvegarder une nouvelle clé
supprimait toutes les autres (et désactivait le fallback .env au reboot).
"""

import dashboard.api as api
import config.settings as settings


def _mask(key: str) -> str:
    return key[:4] + "****" + key[-4:] if len(key) > 8 else "****"


def test_existing_row_survives_a_masked_save(monkeypatch):
    monkeypatch.setattr(
        api,
        "API_KEYS",
        [
            {"alias": "A", "api_key": "sk-aaaaaaaaaaaa", "go_workspace_id": "wrk_a"},
            {"alias": "B", "api_key": "sk-bbbbbbbbbbbb", "go_workspace_id": "wrk_b"},
        ],
    )
    rows = [
        {
            "alias": "A",
            "api_key": "",
            "api_key_masked": _mask("sk-aaaaaaaaaaaa"),
            "go_workspace_id": "",
            "go_auth_cookie": "",
        }
    ]
    out = api._merge_preserved_api_keys(rows, replace=True)
    assert len(out) == 1
    assert out[0]["api_key"] == "sk-aaaaaaaaaaaa"
    assert out[0]["go_workspace_id"] == "wrk_a"


def test_alias_rename_matches_by_masked_value(monkeypatch):
    monkeypatch.setattr(
        api,
        "API_KEYS",
        [{"alias": "old", "api_key": "sk-aaaaaaaaaaaa"}],
    )
    out = api._merge_preserved_api_keys(
        [{"alias": "new", "api_key": "", "api_key_masked": _mask("sk-aaaaaaaaaaaa")}],
        replace=True,
    )
    assert out[0]["alias"] == "new"
    assert out[0]["api_key"] == "sk-aaaaaaaaaaaa"


def test_partial_payload_keeps_unposted_keys(monkeypatch):
    monkeypatch.setattr(
        api,
        "API_KEYS",
        [{"alias": "A", "api_key": "sk-aaaaaaaaaaaa"}, {"alias": "B", "api_key": "sk-bbbbbbbbbbbb"}],
    )
    out = api._merge_preserved_api_keys(
        [{"alias": "A", "api_key": "sk-newnewnewnew"}],
        replace=False,
    )
    assert {k["alias"] for k in out} == {"A", "B"}


def test_blank_new_row_is_not_stored(monkeypatch):
    monkeypatch.setattr(api, "API_KEYS", [])
    out = api._merge_preserved_api_keys([{"alias": "empty", "api_key": ""}], replace=True)
    assert out == []


def test_save_api_keys_is_atomic_and_rejects_blank(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(settings, "API_KEYS_PATH", str(path))
    monkeypatch.setattr(settings, "API_KEYS", [])
    settings.save_api_keys(
        [
            {"alias": "A", "api_key": "sk-aaaaaaaaaaaa"},
            {"alias": "blank", "api_key": "   "},
        ]
    )
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == [{"alias": "A", "api_key": "sk-aaaaaaaaaaaa"}]
    assert settings.API_KEYS == data
    assert not (tmp_path / "api_keys.json.tmp").exists()
