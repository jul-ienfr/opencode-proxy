from __future__ import annotations

import asyncio
import threading

import opencode as oc


class _Corps:
    """Reponse amont dont le parse est long : signale l'entree, puis attend le relachement."""

    def __init__(self, entre, relache, charge):
        self.headers = {"content-type": "application/json"}
        self._entre = entre
        self._relache = relache
        self._charge = charge

    def json(self):
        self._entre.set()
        self._relache.wait(10)
        return self._charge


def test_le_parse_du_corps_amont_ne_gele_pas_la_boucle():
    """[A11] Temoin CAUSAL : la veille ne compte qu'APRES l'entree dans le parse.

    Avec `asyncio.to_thread`, la boucle reste libre et la veille progresse. Avec le parse
    synchrone (mutation), l'entree a lieu sur la boucle, qui est gelee, et la veille ne peut
    plus s'executer. Aucun seuil de duree : l'assertion porte sur l'ordre causal.
    """
    entre = threading.Event()
    relache = threading.Event()
    charge = {"usage": {"input_tokens": 1}, "content": []}
    etat = {"progres": 0}

    async def scenario():
        async def veille():
            while not relache.is_set():
                if entre.is_set():
                    etat["progres"] += 1
                await asyncio.sleep(0)

        tache = asyncio.create_task(veille())
        try:
            return await asyncio.wait_for(
                oc._resp_json_hors_boucle(_Corps(entre, relache, charge)), timeout=5
            )
        finally:
            relache.set()
            tache.cancel()

    minuteur = threading.Timer(0.20, relache.set)
    minuteur.daemon = True
    minuteur.start()
    try:
        data = asyncio.run(scenario())
    finally:
        minuteur.cancel()

    assert data == charge
    assert etat["progres"] > 0, (
        "la boucle d'evenements a ete gelee pendant le parse du corps amont : "
        "aucune autre coroutine n'a pu progresser (A11)"
    )


def test_content_type_non_json_rend_un_dict_vide_sans_parser():
    """Contrat preserve a l'identique : pas de parse, `{}`."""

    class _Texte:
        headers = {"content-type": "text/plain"}

        def json(self):
            raise AssertionError("le parse ne doit pas etre appele pour un content-type non JSON")

    assert asyncio.run(oc._resp_json_hors_boucle(_Texte())) == {}
