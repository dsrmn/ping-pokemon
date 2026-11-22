"""
Costruisce cards_index.json: tutte le carte Pokémon su Cardtrader con rarità
Double Rare o superiore (config.json → allowed_rarities), raggruppate per
espansione. Non serve una lista di carte: entra tutto ciò che ha la rarità giusta.

Il raggruppamento per espansione serve a monitor.py, che così scarica le
inserzioni di un'intera espansione con UNA richiesta.

Salva anche rarities_seen.json: tutte le rarità trovate su Cardtrader con
il numero di carte, per capire che nome usare se ne vuoi aggiungere una.

Viene lanciato:
  - a mano dal workflow "Build card index";
  - da solo ogni lunedì (per i set nuovi);
  - dal monitor, in automatico, se cambi allowed_rarities o l'indice manca.

Uso:
    python build_card_index.py               # ricostruisce sempre
    python build_card_index.py --if-outdated # solo se le rarità sono cambiate
"""
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from cardtrader_api import CardTraderClient

CONFIG_FILE = "config.json"
INDEX_FILE = "cards_index.json"
RARITIES_FILE = "rarities_seen.json"
INDEX_VERSION = 2
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")


def normalize(text) -> str:
    return " ".join(str(text).split()).lower()


def find_prop(bp, *needles):
    """Cerca nelle proprietà della blueprint una chiave che contenga una
    delle parole date (Cardtrader usa nomi come 'pokemon_rarity')."""
    for container in (bp.get("fixed_properties"), bp.get("properties_hash"), bp):
        if not isinstance(container, dict):
            continue
        for key, value in container.items():
            if any(n in key.lower() for n in needles) and value not in (None, "") \
                    and not isinstance(value, (dict, list)):
                return str(value).strip()
    return None


def find_image(bp):
    """URL dell'immagine (preferita la versione media 'show')."""
    found = {}

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, path + [k.lower()])
        elif isinstance(obj, str) and obj.lower().split("?")[0].endswith(IMAGE_EXT):
            found[".".join(path)] = obj

    walk({k: v for k, v in bp.items() if "image" in k.lower()}, [])
    if not found:
        return None
    order = ("show", "preview", "url")
    best = sorted(found.items(), key=lambda kv: next((i for i, w in enumerate(order) if w in kv[0]), 9))[0][1]
    if best.startswith("/"):
        best = "https://www.cardtrader.com" + best
    return best


def find_cardmarket_ids(bp):
    """ID prodotto Cardmarket della carta, se Cardtrader li fornisce
    (campo tipo 'card_market_ids'): servono a verificare l'abbinamento."""
    for key, value in bp.items():
        if "card_market" in key.lower() or "cardmarket" in key.lower():
            vals = value if isinstance(value, list) else [value]
            out = []
            for v in vals:
                try:
                    out.append(int(v))
                except (TypeError, ValueError):
                    pass
            return out
    return []


def rarities_fingerprint(config) -> str:
    rar = sorted(normalize(r) for r in config["allowed_rarities"])
    return hashlib.sha1(json.dumps(rar).encode()).hexdigest()[:12]


def index_is_current(config) -> bool:
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            idx = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return (isinstance(idx, dict) and idx.get("version") == INDEX_VERSION
            and idx.get("fingerprint") == rarities_fingerprint(config))


def main():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)

    if "--if-outdated" in sys.argv and index_is_current(config):
        print("Indice carte già aggiornato, niente da fare.")
        return

    token = os.environ.get("CARDTRADER_TOKEN")
    if not token:
        sys.exit("Errore: variabile d'ambiente CARDTRADER_TOKEN non impostata.")

    client = CardTraderClient(token)
    allowed = {normalize(r) for r in config["allowed_rarities"]}

    print("Recupero l'ID del gioco Pokémon...")
    game_id = client.get_pokemon_game_id()
    print(f"  -> game_id = {game_id}")
    expansions = client.get_expansions(game_id)
    print(f"  -> {len(expansions)} espansioni Pokémon")

    out = {}
    seen = Counter()
    total = 0
    for i, exp in enumerate(expansions, 1):
        exp_id = exp["id"]
        exp_name = exp.get("name", str(exp_id))
        if i % 50 == 0 or i == len(expansions):
            print(f"[{i}/{len(expansions)}] ...")
        try:
            blueprints = client.get_blueprints_for_expansion(exp_id)
        except Exception as e:
            print(f"  ! Errore sul set '{exp_name}', salto: {e}")
            continue

        kept = {}
        for bp in blueprints:
            if not isinstance(bp, dict) or "id" not in bp:
                continue
            rarity = find_prop(bp, "rarity")
            if not rarity:
                continue  # prodotti sigillati, accessori, ecc.
            seen[rarity] += 1
            if normalize(rarity) not in allowed:
                continue
            kept[str(bp["id"])] = {
                "name": bp.get("name"),
                "number": find_prop(bp, "collector_number", "card_number"),
                "rarity": rarity,
                "image": find_image(bp),
                "cm_ids": find_cardmarket_ids(bp),
            }
        if kept:
            out[str(exp_id)] = {
                "name": exp_name,
                "code": (exp.get("code") or "").lower(),
                "blueprints": kept,
            }
            total += len(kept)

    print("\n=== RISULTATO ===")
    print(f"Carte da monitorare: {total} in {len(out)} espansioni")
    with_img = sum(1 for e in out.values() for b in e["blueprints"].values() if b.get("image"))
    print(f"Con immagine: {with_img}/{total}")
    print("\nRarità trovate su Cardtrader (✓ = monitorata):")
    for r, n in seen.most_common():
        print(f"  {'✓' if normalize(r) in allowed else ' '} {r}: {n}")
    unused = sorted(allowed - {normalize(r) for r in seen})
    if unused:
        print("\nRarità in config.json che Cardtrader non usa (nome sbagliato?): " + ", ".join(unused))

    if total == 0:
        sys.exit("Nessuna carta trovata: controlla allowed_rarities con l'elenco qui sopra. Indice NON salvato.")

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "version": INDEX_VERSION,
            "fingerprint": rarities_fingerprint(config),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "expansions": out,
        }, f, indent=1, ensure_ascii=False)
    with open(RARITIES_FILE, "w", encoding="utf-8") as f:
        json.dump({r: {"cards": n, "monitored": normalize(r) in allowed} for r, n in seen.most_common()},
                  f, indent=1, ensure_ascii=False)
    print(f"\nSalvato in {INDEX_FILE} e {RARITIES_FILE}")


if __name__ == "__main__":
    main()
