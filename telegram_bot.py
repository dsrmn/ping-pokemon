"""
Invio dei messaggi e comandi Telegram.

Il bot non è sempre acceso (gira su GitHub Actions ogni 20 minuti), quindi
i comandi vengono letti all'inizio di ogni giro: la risposta può arrivare
dopo qualche minuto. Vengono accettati solo i messaggi dalla chat
TELEGRAM_CHAT_ID.

Comandi:
  /stato               impostazioni attuali e ultime occasioni
  /pausa  /riprendi    ferma o riavvia le notifiche
  /sconto 35           sconto minimo in % (vuoto = torna al valore di config)
  /minimo 5            ignora le carte che valgono meno di 5 € (vuoto = config)
  /escludi Nome carta  non segnalare più una carta (es. /escludi Pikachu ex)
  /includi Nome carta  toglie una carta dagli esclusi
  /oggi                le migliori occasioni notificate oggi
  /aiuto               questo elenco

Uso dal workflow:  python telegram_bot.py   (elabora i comandi in attesa)
"""
import html
import os
import sys

import requests

from state import (CONFIG_FILE, BOT_STATE_FILE, load_json, save_json, load_bot_state,
                   load_sent_alerts, effective_discount, effective_min_reference, today)

API = "https://api.telegram.org/bot{token}/{method}"

HELP = (
    "<b>Comandi disponibili</b>\n"
    "/stato — impostazioni e situazione\n"
    "/pausa — ferma le notifiche\n"
    "/riprendi — riattiva le notifiche\n"
    "/sconto 35 — sconto minimo in % (/sconto da solo = valore predefinito)\n"
    "/minimo 5 — ignora le carte che valgono meno di 5 € (/minimo da solo = predefinito)\n"
    "/escludi Nome carta — non segnalare più quella carta\n"
    "/includi Nome carta — toglila dagli esclusi\n"
    "/oggi — migliori occasioni di oggi\n"
    "/aiuto — questo elenco\n\n"
    "Il bot legge i comandi a ogni giro (ogni 15-20 minuti)."
)


class Telegram:
    def __init__(self, token, chat_id, dry_run=False):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.dry_run = dry_run

    def _call(self, method, **payload):
        resp = requests.post(API.format(token=self.token, method=method), json=payload, timeout=30)
        return resp

    def send(self, text, image_url=None):
        """True se inviato (o in dry-run). Con image_url Telegram mostra una
        piccola anteprima dell'immagine accanto al testo."""
        if self.dry_run:
            print("----- [dry-run] messaggio Telegram -----\n" + text
                  + (f"\n[immagine: {image_url}]" if image_url else "") + "\n")
            return True
        if image_url:
            preview = {"url": image_url, "prefer_small_media": True, "show_above_text": False}
        else:
            preview = {"is_disabled": True}
        try:
            resp = self._call("sendMessage", chat_id=self.chat_id, text=text,
                              parse_mode="HTML", link_preview_options=preview)
        except requests.RequestException as e:
            print(f"  ! Errore invio Telegram: {e}")
            return False
        if resp.status_code == 400 and image_url:
            # anteprima rifiutata (es. immagine non raggiungibile): riprova senza
            print(f"  ! Anteprima immagine rifiutata, invio senza: {resp.text[:120]}")
            return self.send(text)
        if resp.status_code != 200:
            print(f"  ! Errore invio Telegram: {resp.status_code} {resp.text}")
            return False
        return True

    def get_updates(self, offset):
        resp = self._call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
        resp.raise_for_status()
        return resp.json().get("result", [])


# ---------------------------------------------------------------------------

def _eur(value):
    return f"{value:.2f} €".replace(".", ",")


def handle_command(text, config, bot_state, sent_alerts):
    """Applica un comando e ritorna (risposta, config_cambiata)."""
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    excluded = config.setdefault("excluded_cards", [])

    if cmd in ("/start", "/aiuto", "/help"):
        return HELP, False

    if cmd == "/pausa":
        bot_state["paused"] = True
        return "⏸ Notifiche in pausa. Scrivi /riprendi per riattivarle.", False

    if cmd == "/riprendi":
        bot_state["paused"] = False
        return "▶️ Notifiche riattivate.", False

    if cmd == "/sconto":
        if not arg:
            bot_state["overrides"].pop("discount_below_average_pct", None)
            return f"Sconto minimo tornato al valore predefinito: {effective_discount(config, bot_state)}%.", False
        try:
            value = int(arg.rstrip("%"))
            assert 5 <= value <= 90
        except (ValueError, AssertionError):
            return "Scrivi un numero tra 5 e 90, es. /sconto 35", False
        bot_state["overrides"]["discount_below_average_pct"] = value
        return f"✅ Sconto minimo impostato al {value}%.", False

    if cmd == "/minimo":
        if not arg:
            bot_state["overrides"].pop("min_reference_eur", None)
            return f"Valore minimo tornato al predefinito: {_eur(effective_min_reference(config, bot_state))}.", False
        try:
            value = float(arg.replace("€", "").replace(",", ".").strip())
            assert 0 <= value <= 1000
        except (ValueError, AssertionError):
            return "Scrivi un importo in euro, es. /minimo 5", False
        bot_state["overrides"]["min_reference_eur"] = value
        return f"✅ Ignoro le carte che valgono meno di {_eur(value)}.", False

    if cmd == "/escludi":
        if not arg:
            if not excluded:
                return "Nessuna carta esclusa. Es. /escludi Pikachu ex", False
            return "<b>Carte escluse</b>\n" + "\n".join(f"• {html.escape(c)}" for c in excluded), False
        if any(c.lower() == arg.lower() for c in excluded):
            return f"{html.escape(arg)} è già esclusa.", False
        excluded.append(arg)
        return f"🚫 Non segnalo più <b>{html.escape(arg)}</b> (nome esatto, tutte le espansioni).", True

    if cmd == "/includi":
        found = next((c for c in excluded if c.lower() == arg.lower()), None) if arg else None
        if not found:
            return f"«{html.escape(arg)}» non è tra le carte escluse.", False
        config["excluded_cards"] = [c for c in excluded if c != found]
        return f"✅ Torno a segnalare <b>{html.escape(found)}</b>.", True

    if cmd == "/stato":
        d = today()
        n_today = sum(1 for v in sent_alerts.values() if v.get("d") == d and v.get("n"))
        state = "⏸ in pausa" if bot_state["paused"] else "▶️ attivo"
        langs = ", ".join(l.upper() for l in config.get("languages", []))
        return (f"<b>Stato bot Pokémon</b>\n"
                f"Notifiche: {state}\n"
                f"Riferimento: prezzo minimo Cardmarket\n"
                f"Sconto minimo: {effective_discount(config, bot_state)}%\n"
                f"Valore minimo carta: {_eur(effective_min_reference(config, bot_state))}\n"
                f"Lingue: {langs}\n"
                f"Solo CT Zero: {'sì' if config.get('ct_zero_only', True) else 'no'}\n"
                f"Carte escluse: {len(excluded)}\n"
                f"Occasioni notificate oggi: {n_today}"), False

    if cmd == "/oggi":
        d = today()
        rows = [v for v in sent_alerts.values() if v.get("d") == d and v.get("n")]
        if not rows:
            return "Oggi nessuna occasione notificata, per ora.", False
        rows.sort(key=lambda v: -(v.get("s") or 0))
        lines = [f"<b>Migliori occasioni di oggi</b> ({len(rows)} in totale)"]
        for v in rows[:10]:
            price = _eur(v["p"] / 100) if v.get("p") else "?"
            lines.append(f"• {html.escape(v['n'])} — {price} (−{v.get('s', '?')}%)")
        return "\n".join(lines), False

    return "Comando non riconosciuto. Scrivi /aiuto per l'elenco.", False


def process_commands(tg, config, bot_state, sent_alerts):
    """Legge i messaggi arrivati dall'ultimo giro. Ritorna True se config
    è cambiato (carte escluse/incluse)."""
    try:
        updates = tg.get_updates(bot_state["telegram_offset"])
    except Exception as e:
        print(f"  ! Non riesco a leggere i comandi Telegram: {e}")
        return False
    changed = False
    for u in updates:
        bot_state["telegram_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if str((msg.get("chat") or {}).get("id")) != tg.chat_id:
            continue  # messaggi da altre chat: ignorati
        if not text.startswith("/"):
            continue
        print(f"Comando ricevuto: {text}")
        reply, cfg_changed = handle_command(text, config, bot_state, sent_alerts)
        changed = changed or cfg_changed
        tg.send(reply)
    return changed


def main():
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID non impostati")
    config = load_json(CONFIG_FILE, None)
    bot_state = load_bot_state()
    sent_alerts = load_sent_alerts()
    tg = Telegram(token, chat_id)
    if process_commands(tg, config, bot_state, sent_alerts):
        save_json(CONFIG_FILE, config)
    save_json(BOT_STATE_FILE, bot_state)


if __name__ == "__main__":
    main()
