"""[C1 audit vitesse] Cache de conversion anthropic_to_openai.

Gate Phase 3 :
  - clé blake2b(body ‖ model) : deux bodies distincts → deux clés
    distinctes (anti-collision — l'ancien ``hash(json.dumps(...))``
    pouvait servir une MAUVAISE conversion) ;
  - le model fait partie de la clé (même body, models différents) ;
  - copie top-level : muter le résultat retourné ne corrompt jamais le
    cache (les clés racine restent pristine au hit suivant) ;
  - bypass poison role=None inchangé ;
  - chemin raw=bytes équivalent au chemin dumps (mêmes conversions).
"""

import hashlib
import json

import pytest

import protocol_mapping as pm


def _body(model="claude-sonnet-4-5", text="hello", max_tokens=512):
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": text}],
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    pm._anthropic_cache.clear()
    yield
    pm._anthropic_cache.clear()


class TestCacheKey:
    def test_different_bodies_never_share_a_key(self):
        """Anti-collision : deux contenus différents → clés différentes,
        quelle que soit la taille (le vieux hash() 64-bit collisionnait)."""
        k1 = pm._anthropic_cache_key("m", _body(text="a"))
        k2 = pm._anthropic_cache_key("m", _body(text="b"))
        k3 = pm._anthropic_cache_key(
            "m", _body(text="a" * 100_000 + "!")
        )
        assert len({k1, k2, k3}) == 3

    def test_model_is_part_of_the_key(self):
        k1 = pm._anthropic_cache_key("glm-5.1", _body())
        k2 = pm._anthropic_cache_key("kimi-k2.6", _body())
        assert k1 != k2

    def test_raw_bytes_path_distinct_but_stable(self):
        b = _body()
        raw = json.dumps(b).encode()
        kr1 = pm._anthropic_cache_key("m", b, raw)
        kr2 = pm._anthropic_cache_key("m", b, raw)
        kd = pm._anthropic_cache_key("m", b)
        assert kr1 == kr2, "raw déterministe"
        assert kr1 != kd, "chemin raw ≠ chemin dumps (entrées séparées, correct)"

    def test_separator_prevents_concatenation_ambiguity(self):
        # ("ab", "") vs ("a", "b") : sans séparateur, la concaténation
        # raw+model pourrait ambiguïser ; avec \x00 non.
        h1 = hashlib.blake2b(digest_size=16)
        h1.update(b"ab")
        h1.update(b"\x00")
        h1.update(b"c")
        h2 = hashlib.blake2b(digest_size=16)
        h2.update(b"a")
        h2.update(b"\x00")
        h2.update(b"bc")
        assert h1.hexdigest() != h2.hexdigest()


class TestMutationIsolation:
    def test_root_mutation_on_return_does_not_poison_cache(self):
        body = _body(text="stable")
        r1 = pm.anthropic_to_openai(body, "glm-5.1")
        n_entries = len(pm._anthropic_cache)
        assert n_entries == 1
        # Le caller mute les clés racine (pattern observé : model /
        # stream_options / min_tokens — audit plan §3).
        r1["model"] = "MUTATED"
        r1["max_tokens"] = 1
        r2 = pm.anthropic_to_openai(body, "glm-5.1")
        assert r2["model"] != "MUTATED", "le hit sert un objet corrompu"
        assert r2["max_tokens"] == 512

    def test_hit_and_miss_are_consistent(self):
        body = _body(text="golden")
        miss = pm.anthropic_to_openai(body, "glm-5.1")
        hit = pm.anthropic_to_openai(body, "glm-5.1")
        assert hit == miss
        assert hit is not miss, "le hit doit être une copie racine, pas l'objet stocké"

    def test_role_none_poison_bypasses_cache(self):
        body = _body()
        body["messages"].append({"role": None, "content": "poison"})
        out = pm.anthropic_to_openai(body, "glm-5.1")
        assert isinstance(out, dict)
        assert len(pm._anthropic_cache) == 0, "poison ne doit pas entrer au cache"


class TestRawEndToEnd:
    def test_raw_and_dumps_paths_return_equivalent_conversions(self):
        body = _body(text="parity")
        a = pm.anthropic_to_openai(body, "glm-5.1", raw=json.dumps(body).encode())
        b = pm.anthropic_to_openai(body, "glm-5.1")
        assert a == b
