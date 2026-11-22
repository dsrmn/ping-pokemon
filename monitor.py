"""
Esegue UNA scansione dei prezzi Cardtrader delle carte Pokémon Double Rare o
superiore (cards_index.json) e notifica su Telegram le occasioni.
Pensato per essere lanciato da GitHub Actions ogni 15-20 minuti.

PREZZO DI RIFERIMENTO: prezzo minimo ("From") della carta su Cardmarket,
scaricato una volta al giorno da update_cardmarket_prices.py
(cardmarket_prices.json); per le reverse holo il minimo delle reverse.
Le carte senza prezzo Cardmarket vengono saltate.

OCCASIONE: inserzione Cardtrader (di un venditore CT Zero se ct_zero_only) che
costa almeno discount_below_average_pct% meno del minimo Cardmarket e almeno
min_saving_eur in meno. Carte con minimo Cardmarket sotto min_reference_eur
(2 €) ignorate. Se anche la mediana delle inserzioni Cardtrader sta sotto la
soglia, il prezzo Cardmarket è probabilmente di un'altra versione: niente avviso.
Un'inserzione già notificata torna solo se scende ancora di
notifications.renotify_if_cheaper_pct%.

Lo storico (price_history.json) tiene il trend Cardmarket di ogni giorno,
per il riepilogo serale delle carte salite/scese.

Variabili d'ambiente: CARDTRADER_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Opzioni: --dry-run  stampa i messaggi nel log senza mandarli né salvare nulla
"""
import html
import os
import statistics
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from cardtrader_api import CardTraderClient
from state import (CONFIG_FILE, HISTORY_FILE, SENT_ALERTS_FILE, BOT_STATE_FILE,
                   now, today, load_json, save_json, load_bot_state, load_sent_alerts,
                   prune_sent_alerts, effective_discount, effective_min_reference)
from telegram_bot import Telegram

INDEX_FILE = "cards_index.json"
PRICES_FILE = "cardmarket_prices.json"
PRICES_MAX_AGE_HOURS = 48
INDEX_VERSION = 2

CONDITION_ORDER = ["Mint", "Near Mint", "Slightly Played", "Moderately Played", "Played", "Poor"]
CONDITION_SHORT = {"Mint": "M", "Near Mint": "NM", "Slightly Played": "SP",
                   "Moderately Played": "MP", "Played": "PL", "Poor": "PO"}
LANG_LABEL = {"en": "🇬🇧 Inglese", "jp": "🇯🇵 Giapponese"}
LANG_ALIASES = {"ja": "jp", "jap": "jp", "japanese": "jp", "english": "en"}
MAX_LISTINGS_PER_MESSAGE = 5
SUSPICIOUS_DISCOUNT = 80  # oltre questo sconto il riferimento è probabilmente falsato


def condition_rank(condition_name):
    try:
        return CONDITION_ORDER.index(condition_name)
    except ValueError:
        return None


def find_prop(product, *needles):
    props = product.get("properties_hash", {}) or {}
    for key, value in props.items():
        if any(n in key.lower() for n in needles):
            return value
    return None


def is_true(value):
    return value is True or str(value).lower() in ("true", "1", "yes")


def get_language(product):
    """Codice lingua dell'inserzione ('en', 'jp', ...) o None se manca."""
    value = find_prop(product, "language")
    if value in (None, ""):
        return None
    v = str(value).strip().lower()
    return LANG_ALIASES.get(v, v)


def is_ct_zero(product):
    """Il venditore spedisce tramite CT Zero (hub di Cardtrader)."""
    user = product.get("user") or {}
    for obj in (user, product):
        for key in ("can_sell_via_hub", "ct_zero", "hub"):
            if obj.get(key) is True:
                return True
    return False


def eur(value):
    return f"{value:.2f} €".replace(".", ",")


def flag(country_code):
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country_code.upper())


# ---------------------------------------------------------------------------
# Storico prezzi
# ---------------------------------------------------------------------------

def value_days_ago(series, days):
    for delta in (days, days + 1, days - 1):
        day = (now() - timedelta(days=delta)).date().isoformat()
        if day in series:
            return series[day] / 100
    return None


def prune_history(history, keep_days):
    limit = (now() - timedelta(days=keep_days)).date().isoformat()
    for key in list(history):
        history[key] = {d: c for d, c in history[key].items() if d >= limit}
        if not history[key]:
            del history[key]


# ---------------------------------------------------------------------------
# Scansione
# ---------------------------------------------------------------------------

def is_reverse(product):
    return is_true(find_prop(product, "reverse"))


def deals_for_group(bp_id, bp, exp, reverse, eligible, settings, sent_alerts, cm, stats):
    """Occasioni tra le inserzioni di UNA carta (normale o reverse holo),
    confrontate con il prezzo minimo ("From") di Cardmarket."""
    found = []
    ref = cm.get("low_rev") if reverse else cm.get("low")
    if not ref or ref < settings["min_reference"]:
        return found
    max_price = ref * (1 - settings["discount"] / 100)
    if len(eligible) >= 3:
        median_ct = statistics.median_low([x for x, _, _ in eligible])
        if median_ct <= max_price:
            # quasi tutte le inserzioni sarebbero "occasioni": il prezzo
            # Cardmarket è di un'altra carta o è sballato -> nessun avviso
            stats["suspect_ref"] += 1
            return found

    for price, p, hub in eligible:
        if settings["ct_zero_only"] and not hub:
            continue
        country = (p.get("user") or {}).get("country_code")
        if settings["deal_countries"] and country not in settings["deal_countries"]:
            continue
        if price > max_price or ref - price < settings["min_saving"]:
            continue
        alert_key = f"{bp_id}:{p.get('id')}"
        previous = sent_alerts.get(alert_key)
        cheaper_than = None
        if previous is not None:
            prev_cents = previous.get("p")
            if not prev_cents or price * 100 > prev_cents * (1 - settings["renotify_pct"] / 100):
                continue
            cheaper_than = prev_cents / 100
        user = p.get("user") or {}
        found.append({
            "price": price,
            "ref": round(ref, 2),
            "trend": cm.get("trend_rev" if reverse else "trend"),
            "discount": round((1 - price / ref) * 100),
            "n_listings": len(eligible),
            "expansion": exp["name"],
            "number": bp.get("number"),
            "rarity": bp.get("rarity"),
            "language": get_language(p) or "?",
            "reverse": reverse,
            "condition": (p.get("properties_hash") or {}).get("condition"),
            "seller": user.get("username", "?"),
            "country": country,
            "hub": hub,
            "cheaper_than": cheaper_than,
            "alert_key": alert_key,
            "url": f"https://www.cardtrader.com/en/cards/{bp_id}",
            "cm_url": "https://www.cardmarket.com/en/Pokemon/Products/Search?searchString="
                      + quote_plus(bp.get("name") or ""),
            "image": bp.get("image"),
        })
    return found


def check_expansion(exp, products_by_bp, settings, sent_alerts, history, prices, stats):
    """Ritorna {nome carta: [occasioni]} per una espansione e aggiorna lo storico."""
    found = {}
    d = today()
    for bp_id, bp in exp["blueprints"].items():
        if (bp.get("name") or "").lower() in settings["excluded"]:
            continue
        cm = prices.get(bp_id)
        if not cm:
            stats["no_cm"] += 1
            continue
        if cm.get("trend"):
            series = history.setdefault(bp_id, {})
            series.setdefault(d, int(round(cm["trend"] * 100)))
        groups = {}
        for p in products_by_bp.get(int(bp_id), []):
            price_info = p.get("price", {}) or {}
            cents = price_info.get("cents")
            if cents is None or price_info.get("currency", "EUR") != "EUR":
                continue
            if p.get("on_vacation") or (p.get("user") or {}).get("on_vacation"):
                continue
            if p.get("graded") or is_true(find_prop(p, "graded", "altered", "signed")):
                continue
            lang = get_language(p)
            if lang and lang not in settings["languages"]:
                continue
            if settings["countries"] and (p.get("user") or {}).get("country_code") not in settings["countries"]:
                continue
            if settings["min_condition_rank"] is not None:
                rank = condition_rank((p.get("properties_hash") or {}).get("condition"))
                if rank is None or rank > settings["min_condition_rank"]:
                    continue
            hub = is_ct_zero(p)
            stats["listings"] += 1
            stats["ct_zero"] += hub
            groups.setdefault((is_reverse(p), lang), []).append((cents / 100.0, p, hub))

        # per lingua: il controllo "prezzo Cardmarket incoerente" confronta
        # inserzioni della stessa lingua (le giapponesi costano spesso meno)
        for (reverse, _lang), items in groups.items():
            deals = deals_for_group(bp_id, bp, exp, reverse, items, settings, sent_alerts, cm, stats)
            if deals:
                found.setdefault(bp["name"], []).extend(deals)
    return found


# ---------------------------------------------------------------------------
# Messaggi
# ---------------------------------------------------------------------------

def details_line(d):
    parts = []
    if d["condition"]:
        parts.append(f"{d['condition']} ({CONDITION_SHORT.get(d['condition'], '?')})")
    parts.append(LANG_LABEL.get(d["language"], "lingua ?" if d["language"] == "?" else d["language"].upper()))
    if d["reverse"]:
        parts.append("Reverse holo")
    if d["hub"]:
        parts.append("CT Zero")
    return " · ".join(parts)


def format_message(card_name, deals):
    deals = sorted(deals, key=lambda d: (d["discount"] >= SUSPICIOUS_DISCOUNT, -d["discount"]))
    n = len(deals)
    best = deals[0]
    lines = [f"⚡ <b>{html.escape(card_name)}</b>",
             f"{n} occasion{'e' if n == 1 else 'i'} · la migliore a <b>{eur(best['price'])}</b> (−{best['discount']}%)"]
    for d in deals[:MAX_LISTINGS_PER_MESSAGE]:
        edition = " · ".join(html.escape(str(x)) for x in (d["expansion"], d["number"], d["rarity"]) if x)
        lines += ["", f"💰 <b>{eur(d['price'])}</b> invece di {eur(d['ref'])} (−{d['discount']}%)"
                      " · prezzo minimo Cardmarket"]
        if d.get("trend"):
            lines.append(f"📈 Trend Cardmarket: {eur(d['trend'])}")
        if d["discount"] >= SUSPICIOUS_DISCOUNT:
            lines.append("⚠️ Sconto sospetto: forse il prezzo Cardmarket è di un'altra versione, controlla")
        if d["cheaper_than"]:
            lines.append(f"⬇️ Ribassata: prima era {eur(d['cheaper_than'])}")
        lines.append(f"📦 {edition}")
        lines.append(f"✅ {html.escape(details_line(d))}")
        country = d.get("country") or ""
        lines.append(f"👤 {html.escape(str(d['seller']))} {flag(country)} {html.escape(country)}".rstrip())
        lines.append(f"🔗 <a href=\"{html.escape(d['url'], quote=True)}\">Apri su Cardtrader</a>"
                     f" · <a href=\"{html.escape(d['cm_url'], quote=True)}\">Cerca su Cardmarket</a>")
    if n > MAX_LISTINGS_PER_MESSAGE:
        lines += ["", f"…e altre {n - MAX_LISTINGS_PER_MESSAGE} inserzioni"]
    return "\n".join(lines)


def build_daily_summary(index, history, sent_alerts, cfg):
    """Carte salite/scese di più rispetto a 7 giorni fa + occasioni del giorno."""
    d = today()
    labels = {}
    for exp in index["expansions"].values():
        for bp_id, bp in exp["blueprints"].items():
            labels[bp_id] = " · ".join(x for x in (bp["name"], exp["name"], bp.get("rarity")) if x)
    moves = []
    for key, series in history.items():
        bp_id = key.split(":")[0]
        if ":" in key or bp_id not in labels or d not in series:
            continue
        cur = series[d] / 100
        old = value_days_ago(series, 7)
        if old is None or max(cur, old) < cfg["min_reference_eur"]:
            continue
        change = round((cur / old - 1) * 100)
        if abs(change) >= cfg["move_pct"]:
            label = labels[bp_id]
            moves.append((change, label, old, cur))
    n_today = sum(1 for v in sent_alerts.values() if v.get("d") == d and v.get("n"))
    lines = [f"📊 <b>Riepilogo Pokémon del {now().strftime('%d/%m')}</b>",
             f"Occasioni notificate oggi: {n_today} (dettagli con /oggi)"]
    if not moves:
        if any(value_days_ago(s, 7) is not None for s in history.values()):
            lines.append(f"Nessuna carta salita o scesa di più del {cfg['move_pct']}% in una settimana.")
        else:
            lines.append("Il confronto con la settimana scorsa partirà quando avrò 7 giorni di prezzi Cardmarket.")
        return "\n".join(lines)
    ups = sorted([m for m in moves if m[0] > 0], key=lambda m: -m[0])[:cfg["max_lines"]]
    downs = sorted([m for m in moves if m[0] < 0], key=lambda m: m[0])[:cfg["max_lines"]]
    if ups:
        lines += ["", "📈 <b>In salita</b> (trend Cardmarket rispetto a 7 giorni fa)"]
        lines += [f"• {html.escape(l)}: {eur(o)} → {eur(c)} (+{ch}%)" for ch, l, o, c in ups]
    if downs:
        lines += ["", "📉 <b>In discesa</b>"]
        lines += [f"• {html.escape(l)}: {eur(o)} → {eur(c)} ({ch}%)" for ch, l, o, c in downs]
    return "\n".join(lines)


# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    ct_token = os.environ.get("CARDTRADER_TOKEN")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    required = [("CARDTRADER_TOKEN", ct_token)]
    if not dry_run:
        required += [("TELEGRAM_BOT_TOKEN", tg_token), ("TELEGRAM_CHAT_ID", tg_chat)]
    missing_env = [n for n, v in required if not v]
    if missing_env:
        sys.exit(f"Errore: variabili d'ambiente mancanti: {', '.join(missing_env)}")

    config = load_json(CONFIG_FILE, None)
    if config is None:
        sys.exit("config.json non trovato!")
    index = load_json(INDEX_FILE, None)
    if not isinstance(index, dict) or index.get("version") != INDEX_VERSION:
        sys.exit("cards_index.json mancante o nel formato vecchio: esegui build_card_index.py")

    prices_file = load_json(PRICES_FILE, None)
    if not isinstance(prices_file, dict) or not prices_file.get("cards"):
        sys.exit("cardmarket_prices.json mancante: esegui update_cardmarket_prices.py")
    prices = prices_file["cards"]
    try:
        age_h = (now() - datetime.fromisoformat(prices_file["updated"])).total_seconds() / 3600
    except (KeyError, ValueError, TypeError):
        age_h = None

    bot_state = load_bot_state()
    sent_alerts = load_sent_alerts()
    history = load_json(HISTORY_FILE, {})
    # vecchio formato (mediane Cardtrader per lingua "id:en"): non serve più
    history = {k: v for k, v in history.items() if ":" not in k}
    tg = Telegram(tg_token, tg_chat, dry_run)
    notif = config.get("notifications", {})
    ph = config.get("price_history", {})
    crit = config["deal_criteria"]

    settings = {
        "discount": effective_discount(config, bot_state),
        "min_saving": crit.get("min_saving_eur", 0),
        "min_reference": effective_min_reference(config, bot_state),
        "ct_zero_only": config.get("ct_zero_only", True),
        "languages": {LANG_ALIASES.get(l.lower(), l.lower()) for l in config.get("languages", ["en", "jp"])},
        "countries": set(config.get("allowed_seller_countries", [])),
        "deal_countries": set(config.get("deal_seller_countries", [])),
        "excluded": {c.strip().lower() for c in config.get("excluded_cards", [])},
        "min_condition_rank": condition_rank(config.get("min_condition", "Slightly Played")),
        "renotify_pct": notif.get("renotify_if_cheaper_pct", 10),
    }

    client = CardTraderClient(ct_token)
    expansions = index["expansions"]
    n_bp = sum(len(e["blueprints"]) for e in expansions.values())
    start = time.time()
    print(f"[{now().strftime('%Y-%m-%d %H:%M:%S')}] Scansione di {n_bp} carte in {len(expansions)} espansioni"
          f" · sconto minimo {settings['discount']}% · min {settings['min_reference']} €"
          f" · solo CT Zero: {settings['ct_zero_only']}{' · IN PAUSA' if bot_state['paused'] else ''}")

    if age_h is not None and age_h > PRICES_MAX_AGE_HOURS:
        print(f"ATTENZIONE: prezzi Cardmarket vecchi di {round(age_h)} ore (workflow 'Update Cardmarket prices').")
    stats = {"listings": 0, "ct_zero": 0, "no_cm": 0, "suspect_ref": 0}
    langs_seen = {}
    sample_printed = False
    deals_by_card = {}
    for i, (exp_id, exp) in enumerate(expansions.items(), 1):
        if i % 25 == 0 or i == len(expansions):
            print(f"  ...controllate {i}/{len(expansions)} espansioni")
        try:
            products = client.get_marketplace_products_by_expansion(int(exp_id))
        except Exception as e:
            print(f"  ! Errore recuperando le inserzioni di {exp['name']}: {e}")
            continue
        for lst in products.values():
            for p in lst:
                lg = get_language(p) or "?"
                langs_seen[lg] = langs_seen.get(lg, 0) + 1
            if lst and not sample_printed:
                p = lst[0]
                print(f"  Proprietà di un'inserzione: {p.get('properties_hash')}; "
                      f"venditore: {sorted((p.get('user') or {}))}")
                sample_printed = True
        for card, deals in check_expansion(exp, products, settings, sent_alerts, history, prices, stats).items():
            deals_by_card.setdefault(card, []).extend(deals)

    print(f"Lingue delle inserzioni scaricate: {dict(sorted(langs_seen.items(), key=lambda kv: -kv[1]))}")
    print(f"Inserzioni idonee: {stats['listings']}, di cui CT Zero: {stats['ct_zero']}")
    print(f"Carte senza prezzo Cardmarket (saltate): {stats['no_cm']}; "
          f"prezzo Cardmarket incoerente con Cardtrader (saltate): {stats['suspect_ref']}")
    if settings["ct_zero_only"] and stats["listings"] and not stats["ct_zero"]:
        print("ATTENZIONE: nessuna inserzione riconosciuta come CT Zero: controlla is_ct_zero().")

    pending = sum(len(v) for v in deals_by_card.values())
    print(f"Occasioni da notificare: {pending} su {len(deals_by_card)} carte")
    sent_msgs = total = 0
    max_msgs = notif.get("max_messages_per_run", 30)
    if bot_state["paused"]:
        print("Bot in pausa: nessuna notifica inviata.")
    else:
        def priority(kv):
            # prima gli sconti credibili più alti (a pari sconto il risparmio più grande)
            ok = [x for x in kv[1] if x["discount"] < SUSPICIOUS_DISCOUNT]
            if not ok:
                return (1, 0, 0)
            best = max(ok, key=lambda x: (x["discount"], x["ref"] - x["price"]))
            return (0, -best["discount"], -(best["ref"] - best["price"]))
        for card, deals in sorted(deals_by_card.items(), key=priority):
            if sent_msgs >= max_msgs:
                print(f"  Limite di {max_msgs} messaggi raggiunto: il resto al prossimo giro.")
                break
            best = sorted(deals, key=lambda x: (x["discount"] >= SUSPICIOUS_DISCOUNT, -x["discount"]))[0]
            if not tg.send(format_message(card, deals), image_url=best.get("image")):
                print("  Telegram ha rifiutato l'invio: il resto verrà ritentato al prossimo giro.")
                break
            for x in deals:
                sent_alerts[x["alert_key"]] = {"p": int(round(x["price"] * 100)), "d": today(),
                                               "n": card, "s": x["discount"]}
            sent_msgs += 1
            total += len(deals)
            print(f"  -> Notificata: {card} ({len(deals)})")
            if not dry_run:
                time.sleep(1)

        summary_cfg = config.get("daily_summary", {})
        if (summary_cfg and bot_state.get("last_summary_date") != today()
                and now().hour >= summary_cfg.get("hour", 20)):
            if tg.send(build_daily_summary(index, history, sent_alerts, summary_cfg)):
                bot_state["last_summary_date"] = today()
                print("  -> Inviato il riepilogo giornaliero")

    if not dry_run:
        prune_history(history, ph.get("keep_days", 40))
        save_json(HISTORY_FILE, history, compact=True)
        save_json(SENT_ALERTS_FILE, prune_sent_alerts(sent_alerts, notif.get("forget_after_days", 60)), indent=0)
        save_json(BOT_STATE_FILE, bot_state)
    print(f"Scansione completata in {round(time.time() - start)}s. "
          f"Messaggi: {sent_msgs}, occasioni notificate: {total}.")


if __name__ == "__main__":
    main()
