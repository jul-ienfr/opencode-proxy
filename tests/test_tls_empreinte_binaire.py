"""Verrouille l'empreinte TLS sur la MESURE DU VRAI BINAIRE opencode.exe.

Contexte (sondes AP + AQ, 2026-09-27)
--------------------------------------
La reference « ClientHello officiel » utilisee jusqu'ici etait CIRCULAIRE :
elle provenait de la constante _OPENCODE_JA3 du proxy lui-meme. Comparer
l'emission du proxy a cette constante ne prouvait rien sur le vrai client.

Mesure directe : opencode.exe lance contre un captureur TLS local
(OPENCODE_CONFIG_CONTENT -> provider pointant sur https://localhost:<port>).
Resultat sur 3 executions, STABLE :

  extensions (13, dans l'ordre) :
    0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43
  ciphers (17) : 4865-4866-4867-49195-49199-49196-49200-52393-52392-
                 49161-49171-49162-49172-156-157-47-53
  taille : 517 octets (avec PADDING 21, ajoute par BoringSSL)
  65037 (ALPS) : ABSENT

Le proxy portait 65037 dans son ja3 : il emettait donc une extension en trop,
dont la longueur VARIAIT aleatoirement (186/218/250/282 -> 4 profils
distincts). Retire -> profil exactement identique au binaire.

Ces tests sont volontairement PURS (aucun reseau, aucune execution du
binaire) : ils verrouillent la constante contre la mesure ci-dessus, pour
qu'une regression soit detectee immediatement.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import opencode as oc  # noqa: E402

# --- MESURE DU VRAI BINAIRE (sonde AQ, 3 executions stables) --------------
MESURE_CIPHERS = [
    4865, 4866, 4867, 49195, 49199, 49196, 49200, 52393, 52392,
    49161, 49171, 49162, 49172, 156, 157, 47, 53,
]
MESURE_EXTS = [0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43]
MESURE_CURVES = [29, 23, 24]
MESURE_FORMATS = [0]


def _champs() -> list[str]:
    return oc._OPENCODE_JA3.split(",")


class TestJa3ConformeAuBinaire:
    """Le ja3 du proxy doit refléter le VRAI client, pas une supposition."""

    def test_nb_champs(self):
        """Un ja3 a exactement 5 champs."""
        assert len(_champs()) == 5

    def test_version_tls(self):
        """TLS 1.2 en version legacy (771), comme le binaire."""
        assert _champs()[0] == "771"

    def test_ciphers_identiques_au_binaire(self):
        """Les 17 ciphers, dans l'ordre mesuré sur opencode.exe."""
        got = [int(x) for x in _champs()[1].split("-")]
        assert got == MESURE_CIPHERS, (
            f"ciphers divergents du binaire.\n  mesure : {MESURE_CIPHERS}\n"
            f"  code   : {got}"
        )

    def test_extensions_identiques_au_binaire(self):
        """Les 13 extensions du binaire, dans l'ordre, SANS 65037."""
        got = [int(x) for x in _champs()[2].split("-")]
        assert got == MESURE_EXTS, (
            f"extensions divergentes du binaire.\n  mesure : {MESURE_EXTS}\n"
            f"  code   : {got}"
        )

    def test_65037_alps_absent(self):
        """65037 (ALPS/ECH) : le client ne l'envoie PAS.

        Régression historique : le proxy la portait, ce qui ajoutait une
        extension en trop et rendait l'empreinte INSTABLE (longueur
        aléatoire 186/218/250/282 -> 4 profils distincts).
        """
        exts = [int(x) for x in _champs()[2].split("-")]
        assert 65037 not in exts, (
            "65037 (ALPS) est de retour dans le ja3 : le vrai binaire ne "
            "l'emet jamais (mesure sonde AQ). Il rend l'empreinte instable."
        )

    def test_sni_en_premier(self):
        """SNI (0) en premier, comme Bun."""
        assert _champs()[2].split("-")[0] == "0"

    def test_alpn_present(self):
        """ALPN (16) présent — http/1.1 via http_version=v1."""
        assert "16" in _champs()[2].split("-")

    def test_ocsp_et_sct_presents(self):
        """OCSP (5) et SCT (18) présents : extensions portées par chrome131."""
        exts = _champs()[2].split("-")
        assert "5" in exts, "status_request (OCSP) absent"
        assert "18" in exts, "SCT absent"

    def test_curves_et_formats(self):
        """Courbes x25519/secp384/secp256 et format uncompressed."""
        assert [int(x) for x in _champs()[3].split("-")] == MESURE_CURVES
        assert [int(x) for x in _champs()[4].split("-")] == MESURE_FORMATS

    def test_padding_non_declare(self):
        """21 (PADDING) n'est PAS déclaré : c'est BoringSSL qui l'ajoute.

        Le binaire émet bien un padding sur le fil, mais il est calculé par
        la pile TLS (règle « jamais entre 256 et 511 octets »), pas déclaré
        par le client. Le déclarer fausserait l'empreinte.
        """
        assert "21" not in _champs()[2].split("-")


class TestOrdreDesEnTetes:
    """L'ordre des en-têtes doit être imposé explicitement (sonde AK)."""

    def test_header_order_dans_extra_fp(self):
        """header_order est bien posé dans extra_fp (sinon Connection en 12e)."""
        _, fp = oc._get_free_fp_kwargs()
        assert fp is not None
        assert "header_order" in fp["extra_fp"], (
            "header_order absent : libcurl emettra Connection en DERNIERE "
            "position (12/12) au lieu de la 8e comme le client."
        )

    def test_header_order_valeur(self):
        """L'ordre imposé est exactement celui mesuré sur le client."""
        attendu = (
            "Authorization,Content-Type,User-Agent,x-opencode-client,"
            "x-opencode-project,x-opencode-request,x-opencode-session,"
            "Connection,Accept,Host,Accept-Encoding,Content-Length"
        )
        _, fp = oc._get_free_fp_kwargs()
        assert fp["extra_fp"]["header_order"] == attendu

    def test_header_order_est_une_chaine(self):
        """Doit être une CHAÎNE : une liste casse le binding cffi."""
        _, fp = oc._get_free_fp_kwargs()
        assert isinstance(fp["extra_fp"]["header_order"], str)

    def test_connection_en_8e_position(self):
        """Connection est en 8e dans l'ordre imposé (= ordre du client)."""
        _, fp = oc._get_free_fp_kwargs()
        champs = fp["extra_fp"]["header_order"].split(",")
        assert champs.index("Connection") == 7, champs  # index 0 -> 8e
        assert len(champs) == 12

    def test_kill_switch_header_order(self, monkeypatch):
        """OPENCODE_FREE_HEADER_ORDER_ON=0 retire l'option (repli sûr)."""
        monkeypatch.setattr(oc, "_FREE_HEADER_ORDER_ON", False)
        _, fp = oc._get_free_fp_kwargs()
        assert "header_order" not in fp["extra_fp"]

    def test_kill_switch_tls_impersonate(self, monkeypatch):
        """OPENCODE_TLS_IMPERSONATE neutralise tout l'override."""
        monkeypatch.setattr(oc, "_OPENCODE_TLS_IMPERSONATE", "chrome131")
        imp, fp = oc._get_free_fp_kwargs()
        assert imp == "chrome131"
        assert fp is None


class TestCoherence:
    """Le profil reste cohérent avec les contraintes connues."""

    @pytest.mark.parametrize(
        "ext",
        [0, 5, 10, 11, 13, 16, 18, 23, 35, 43, 45, 51, 65281],
    )
    def test_extension_attendue_presente(self, ext):
        """Les 13 extensions mesurées sont toutes déclarées."""
        assert str(ext) in _champs()[2].split("-")

    def test_aucune_extension_browser_artefact(self):
        """Pas d'artefact de preset navigateur dans le ja3.

        17613 (application_settings), 27 (compress_certificate) et 17513
        (ALPS legacy) viennent des presets Chrome récents, pas du binaire.
        """
        exts = {int(x) for x in _champs()[2].split("-")}
        for artefact in (17613, 27, 17513, 65037):
            assert artefact not in exts, f"artefact navigateur {artefact} présent"

    def test_sigalgs_au_nombre_de_9(self):
        """Les 9 sigalgs de Bun (dont rsa_pkcs1_sha1, absent des presets)."""
        assert len(oc._OPENCODE_SIG_ALGS) == 9
        assert oc._OPENCODE_SIG_ALGS[-1] == "rsa_pkcs1_sha1"