# Sonde live multi-tours — parité raisonnement (correctif post-livraison PLAN-raisonnement)
# Tour 1 : question ouverte → capturer thinking + 2 fragments distinctifs
# Tour 2 : renvoyer l'historique complète (bloc thinking inclus) + demander de citer
#          son raisonnement précédent → compter les fragments retrouvés.
import json
import re
import sys
import urllib.request

BASE = f"http://localhost:{sys.argv[2] if len(sys.argv) > 2 else 4000}/v1/messages"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "muse-spark-1.2-contributor"

Q1 = "Explique en deux phrases pourquoi le ciel est bleu, puis réfléchis longuement avant de répondre."
Q2 = (
    "Dans la conversation ci-dessus, ton message précédent contient un bloc de "
    "réflexion interne qui t'a été renvoyé. Pour vérifier que tu y as accès : "
    "recopie-en les premières phrases telles quelles entre guillemets, sans commentaire."
)


def post(body):
    req = urllib.request.Request(
        BASE,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-api-key": "probe", "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def extract(resp):
    think = next((b["thinking"] for b in resp.get("content", []) if b.get("type") == "thinking"), "")
    text = next((b["text"] for b in resp.get("content", []) if b.get("type") == "text"), "")
    sig = next((b.get("signature") for b in resp.get("content", []) if b.get("type") == "thinking"), None)
    return think, text, sig


def fragments(think):
    # 2 fragments distinctifs : mots consécutifs significatifs au milieu du thinking
    words = [w for w in re.findall(r"[a-zA-ZÀ-ÿ']{5,}", think) if w.lower() not in {
        "parce", "quelle", "avoir", "deux", "cette", "raison", "réponse", "question",
    }]
    if len(words) < 6:
        return []
    mid = len(words) // 2
    f1 = f"{words[mid - 1]} {words[mid]}"
    f2 = f"{words[min(mid + 3, len(words) - 2)]} {words[min(mid + 4, len(words) - 1)]}"
    return [f1, f2]


print(f"=== Sonde multi-tours {MODEL} ===")
r1 = post({"model": MODEL, "max_tokens": 2048, "stream": False,
           "thinking": {"type": "enabled", "budget_tokens": 1024},
           "messages": [{"role": "user", "content": Q1}]})
think1, text1, sig1 = extract(r1)
frags = fragments(think1)
print(f"Tour 1: thinking={len(think1)} chars, signature={'OUI' if sig1 else 'NON'}, texte={len(text1)} chars")
print(f"Fragments distinctifs: {frags}")

history = [
    {"role": "user", "content": Q1},
    {"role": "assistant", "content": r1["content"]},
    {"role": "user", "content": Q2},
]
r2 = post({"model": MODEL, "max_tokens": 4096, "stream": False,
           "thinking": {"type": "enabled", "budget_tokens": 1024},
           "messages": history})
think2, text2, _ = extract(r2)
hay = (think2 + " " + text2)
found = sum(1 for f in frags if f.lower() in hay.lower())
print(f"Tour 2: thinking={len(think2)} chars, texte={len(text2)} chars")
print(f"FRAGMENTS RETROUVÉS: {found}/2")
print("--- extrait réponse tour 2 ---")
print((text2 or think2)[:600])
sys.exit(0 if found == 2 else 1)
