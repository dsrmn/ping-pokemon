"""
Scarica una volta al giorno i prezzi Cardmarket delle carte di
cards_index.json usando TCGdex (api.tcgdex.net, gratis, senza chiave),
che pubblica per ogni carta i valori di Cardmarket: prezzo minimo ("From"),
trend e medie a 1/7/30 giorni. Cardmarket li aggiorna una volta al giorno.

Abbinamento Cardtrader -> TCGdex:
  1. espansione: per nome (inglese) o per codice (es. "sv11W", set giapponesi);
  2. carta: per numero di collezione (223/197 -> 223).
Se Cardtrader e TCGdex forniscono entrambi l'ID prodotto Cardmarket, lo usa
per scartare gli abbinamenti sbagliati.

Salva cardmarket_prices.json:
  {"updated": "...", "cards": {blueprint_id: {"low", "trend", "avg30",
   "low_rev", "trend_rev", "tcgdex", "lang"}}}
e stampa quante carte non è riuscito ad abbinare (cardmarket_unmatched.json).

Uso:  python update_cardmarket_prices.py
"""
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

API = "https://api.tcgdex.net/v2/{lang}"
INDEX_FILE = "cards_index.json"
PRICES_FILE = "cardmarket_prices.json"
UNMATCHED_FILE = "cardmarket_unmatched.json"
LANGS = ("en", "ja")
WORKERS = 4

session = requests.Session()
session.headers.update({"Accept": "application/json", "User-Agent": "ping-pokemon (GitHub Actions)"})


def get(path, lang):
    url = API.format(lang=lang) + path
    for attempt in range(4):
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as e:
            if attempt == 3:
                raise
            print(f"  ! {e}, riprovo")
            time.sleep(3 * (attempt + 1))
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(resp.headers.get("Retry-After", 5 * (attempt + 1))))
            continue
        resp.raise_for_status()
        return resp.json()
    return None


def norm_name(text):
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " and ")
    t = re.sub(r"\bpokemon\b", " ", t)
    return re.sub(r"[^a-z0-9]+", "", t)


def norm_number(num):
    """'223/197' -> '223', '007' -> '7', 'TG01' -> 'tg1', 'SV001' -> 'sv1'."""
    if num is None:
        return None
    n = str(num).split("/")[0].strip().lower()
    m = re.match(r"^([a-z]*)0*(\d+)([a-z]*)$", n)
    if m:
        return f"{m.group(1)}{int(m.group(2))}{m.group(3)}"
    return n or None


def expansion_keys(exp):
    """Chiavi possibili per trovare l'espansione su TCGdex."""
    keys = set()
    parts = [p.strip() for p in str(exp.get("name", "")).split("|")]
    for p in parts:
        if p:
            keys.add(("name", norm_name(p)))
            keys.add(("code", p.lower()))
    if exp.get("code"):
        keys.add(("code", exp["code"].lower()))
    return keys


def load_sets():
    """{(tipo, chiave): (lang, set_id)} per tutti i set TCGdex."""
    lookup = {}
    for lang in LANGS:
        sets = get("/sets", lang) or []
        print(f"TCGdex {lang}: {len(sets)} set")
        for s in sets:
            sid = s.get("id")
            if not sid:
                continue
            entries = [("code", sid.lower()), ("name", norm_name(s.get("name", "")))]
            for k in entries:
                if k[1]:
                    lookup.setdefault(k, (lang, sid))
    return lookup


def pick(cm, *keys):
    for k in keys:
        v = cm.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return round(float(v), 2)
    return None


def card_pricing(lang, card_id):
    card = get(f"/cards/{card_id}", lang)
    if not card:
        return None
    cm = (card.get("pricing") or {}).get("cardmarket") or {}
    if not cm:
        return {"tcgdex": card_id, "lang": lang}
    return {
        "tcgdex": card_id,
        "lang": lang,
        "id_product": cm.get("idProduct"),
        "low": pick(cm, "low"),
        "trend": pick(cm, "trend", "avg"),
        "avg30": pick(cm, "avg30"),
        "low_rev": pick(cm, "low-holo"),
        "trend_rev": pick(cm, "trend-holo", "avg-holo"),
    }


def main():
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            index = json.load(f)
    except FileNotFoundError:
        sys.exit("cards_index.json mancante: esegui prima build_card_index.py")

    set_lookup = load_sets()
    if not set_lookup:
        sys.exit("TCGdex non risponde: prezzi NON aggiornati (resta il file di ieri).")

    # 1) abbina le espansioni e scarica l'elenco carte di ogni set TCGdex
    set_cards = {}
    jobs = []           # (bp_id, lang, tcgdex_card_id, cm_ids)
    unmatched_sets = []
    unmatched_cards = 0
    for exp_id, exp in index["expansions"].items():
        # prima il nome, poi il codice (i codici possono coincidere tra set EN e JP)
        keys = sorted(expansion_keys(exp), key=lambda k: k[0] != "name")
        found = next((set_lookup[k] for k in keys if k in set_lookup), None)
        if not found:
            unmatched_sets.append(exp["name"])
            unmatched_cards += len(exp["blueprints"])
            continue
        if found not in set_cards:
            data = get(f"/sets/{found[1]}", found[0]) or {}
            set_cards[found] = {norm_number(c.get("localId")): c["id"]
                                for c in data.get("cards", []) if c.get("id")}
        by_number = set_cards[found]
        for bp_id, bp in exp["blueprints"].items():
            cid = by_number.get(norm_number(bp.get("number")))
            if cid:
                jobs.append((bp_id, found[0], cid, bp.get("cm_ids") or []))
            else:
                unmatched_cards += 1
    total = sum(len(e["blueprints"]) for e in index["expansions"].values())
    print(f"Espansioni abbinate: {len(index['expansions']) - len(unmatched_sets)}/{len(index['expansions'])}")
    print(f"Carte abbinate: {len(jobs)}/{total}. Scarico i prezzi...")

    # 2) prezzi carta per carta (in parallelo)
    out = {}
    stats = {"no_price": 0, "id_mismatch": 0}
    start = time.time()

    def work(job):
        bp_id, lang, cid, cm_ids = job
        return bp_id, cm_ids, card_pricing(lang, cid)

    with ThreadPoolExecutor(WORKERS) as pool:
        for i, (bp_id, cm_ids, pr) in enumerate(pool.map(work, jobs), 1):
            if i % 1000 == 0:
                print(f"  ...{i}/{len(jobs)} ({round(time.time() - start)}s)")
            if not pr or not pr.get("low"):
                stats["no_price"] += 1
                continue
            if cm_ids and pr.get("id_product") and int(pr["id_product"]) not in cm_ids:
                stats["id_mismatch"] += 1   # carta sbagliata: meglio niente prezzo
                continue
            pr.pop("id_product", None)
            out[bp_id] = {k: v for k, v in pr.items() if v is not None}

    print("\n=== RISULTATO ===")
    print(f"Carte con prezzo Cardmarket: {len(out)}/{total}")
    print(f"  senza prezzo su Cardmarket: {stats['no_price']}")
    print(f"  scartate perché l'ID Cardmarket non corrisponde: {stats['id_mismatch']}")
    print(f"  non abbinate a TCGdex: {unmatched_cards} (di cui in {len(unmatched_sets)} espansioni non trovate)")
    by_lang = {}
    for v in out.values():
        by_lang[v["lang"]] = by_lang.get(v["lang"], 0) + 1
    print(f"  per lingua TCGdex: {by_lang}")

    if len(out) < 0.2 * total and "--force" not in sys.argv:
        sys.exit("Troppe poche carte con prezzo: qualcosa non va, file NON salvato (resta quello di ieri).")

    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "cards": out}, f, separators=(",", ":"), sort_keys=True)
        f.write("\n")
    with open(UNMATCHED_FILE, "w", encoding="utf-8") as f:
        json.dump({"expansions": sorted(unmatched_sets)}, f, indent=1, ensure_ascii=False)
    print(f"Salvato in {PRICES_FILE}")


if __name__ == "__main__":
    main()
