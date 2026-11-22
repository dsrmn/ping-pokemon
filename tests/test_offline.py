"""Test senza rete con Cardtrader e Telegram finti.
Uso: python tests/test_offline.py"""
import json, os, shutil, sys, tempfile
from datetime import timedelta
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build_card_index, monitor, telegram_bot, state  # noqa: E402


def prod(pid, bp, eur, lang="en", country="DE", cond="Near Mint", hub=True, **extra):
    props = {"condition": cond, "pokemon_language": lang, **extra}
    return {"id": pid, "blueprint_id": bp, "price": {"cents": int(round(eur * 100)), "currency": "EUR"},
            "user": {"username": f"venditore{pid}", "country_code": country, "can_sell_via_hub": hub},
            "properties_hash": props}


MARKET = {
    # Charizard ex SIR inglese: mediana 98 €; 55 € CT Zero = occasione; 50 € non CT Zero = no
    # giapponese a parte: mediana 40 €, 38 € non è un'occasione
    100: {1: [prod(1, 1, 100), prod(2, 1, 105), prod(3, 1, 98), prod(4, 1, 102), prod(5, 1, 55), prod(6, 1, 50, hub=False),
              prod(7, 1, 40, lang="jp"), prod(8, 1, 41, lang="jp"), prod(9, 1, 39, lang="jp"), prod(10, 1, 38, lang="jp"),
              prod(11, 1, 5, lang="it"), prod(12, 1, 6, cond="Played")],
          # Double Rare da ~1,50 €: sotto i 2 € → ignorata anche a −50%
          2: [prod(20, 2, 1.5), prod(21, 2, 1.6), prod(22, 2, 1.4), prod(23, 2, 1.5), prod(24, 2, 0.4)],
          # Pikachu ex giapponese: mediana 10 €, 6 € = occasione (risparmio 4 €)
          3: [prod(30, 3, 10, lang="jp"), prod(31, 3, 11, lang="jp"), prod(32, 3, 9.5, lang="jp"),
              prod(33, 3, 10.5, lang="jp"), prod(34, 3, 6, lang="jp", country="JP")],
          # graded: ignorata
          9: [prod(90, 9, 1, graded=True)]},
}


class FakeCT:
    def __init__(self, token=None): pass
    def get_pokemon_game_id(self): return 5
    def get_expansions(self, gid):
        return [{"id": 100, "game_id": 5, "name": "Obsidian Flames", "code": "obf"},
                {"id": 200, "game_id": 5, "name": "Base Set", "code": "bs"}]
    def get_blueprints_for_expansion(self, eid):
        return {100: [{"id": 1, "name": "Charizard ex", "fixed_properties": {"collector_number": "223", "pokemon_rarity": "Special Illustration Rare"},
                       "image": {"url": "/uploads/1/c.jpg", "show": {"url": "/uploads/1/show_c.jpg"}}},
                      {"id": 2, "name": "Tyranitar ex", "fixed_properties": {"pokemon_rarity": "Double Rare"}},
                      {"id": 3, "name": "Pikachu ex", "fixed_properties": {"pokemon_rarity": "Ultra Rare"}},
                      {"id": 4, "name": "Pidgey", "fixed_properties": {"pokemon_rarity": "Common"}},
                      {"id": 9, "name": "Mew ex", "fixed_properties": {"pokemon_rarity": "Hyper Rare"}},
                      {"id": 5, "name": "Booster Box"}],
                200: [{"id": 6, "name": "Charizard", "fixed_properties": {"pokemon_rarity": "Rare Holo"}}]}[eid]
    def get_marketplace_products_by_expansion(self, eid, language=None):
        return MARKET.get(eid, {})


class FakeTG:
    sent, images = [], []
    def __init__(self, token, chat_id, dry_run=False):
        self.chat_id = str(chat_id)
        self.updates = []
    def send(self, text, image_url=None):
        FakeTG.sent.append(text)
        FakeTG.images.append(image_url)
        return True
    def get_updates(self, offset):
        return [u for u in self.updates if u["update_id"] >= offset]


tmp = tempfile.mkdtemp()
shutil.copy(os.path.join(ROOT, "config.json"), tmp)
os.chdir(tmp)
cfg = json.load(open("config.json"))
cfg["daily_summary"]["hour"] = 0          # riepilogo subito, per il test
json.dump(cfg, open("config.json", "w"))
build_card_index.CardTraderClient = FakeCT
monitor.CardTraderClient = FakeCT
monitor.Telegram = FakeTG
monitor.time.sleep = lambda s: None
os.environ.update({"CARDTRADER_TOKEN": "x", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"})

sys.argv = ["build_card_index.py"]
build_card_index.main()
idx = json.load(open("cards_index.json"))
assert list(idx["expansions"]) == ["100"], idx["expansions"].keys()           # Base Set: nessuna rarità alta
assert set(idx["expansions"]["100"]["blueprints"]) == {"1", "2", "3", "9"}     # niente Common né box
assert build_card_index.index_is_current(cfg)
assert json.load(open("rarities_seen.json"))["Common"] == {"cards": 1, "monitored": False}

# --- 1° giro -----------------------------------------------------------------
sys.argv = ["monitor.py"]
monitor.main()
sent = json.load(open("sent_alerts.json"))
assert set(sent) == {"1:5", "3:34"}, sent
msgs = [m for m in FakeTG.sent if m.startswith("⚡")]
assert len(msgs) == 2 and "Charizard ex" in msgs[0], msgs     # prima lo sconto più alto (−44%)
assert "Pikachu ex" in msgs[1] and "Giapponese" in msgs[1] and "🇯🇵 JP" in msgs[1], msgs[1]
assert any(m.startswith("📊") for m in FakeTG.sent), "manca il riepilogo"
assert FakeTG.images[0] == "https://www.cardtrader.com/uploads/1/show_c.jpg", FakeTG.images
hist = json.load(open("price_history.json"))
assert hist["1:en"][state.today()] == 9800 and hist["1:jp"][state.today()] == 3900, hist
assert "2:en" not in hist, "le carte sotto i 2 € non vanno nello storico"
print(msgs[0], "\n")

# --- 2° giro: niente doppioni; ribasso di un'inserzione già notificata ------
FakeTG.sent = []
MARKET[100][1][4] = prod(5, 1, 48)          # da 55 € a 48 € (−13%)
monitor.main()
msgs = [m for m in FakeTG.sent if m.startswith("⚡")]
assert len(msgs) == 1 and "Ribassata: prima era 55,00 €" in msgs[0], FakeTG.sent
assert not any(m.startswith("📊") for m in FakeTG.sent), "riepilogo già inviato oggi"

# --- /minimo e /escludi cambiano i risultati --------------------------------
bs = state.load_bot_state()
c = json.load(open("config.json"))
sa = state.load_sent_alerts()
telegram_bot.handle_command("/minimo 1", c, bs, sa)
assert bs["overrides"]["min_reference_eur"] == 1.0
telegram_bot.handle_command("/escludi pikachu ex", c, bs, sa)
state.save_json("bot_state.json", bs); state.save_json("config.json", c)
FakeTG.sent = []
MARKET[100][3][4] = prod(35, 3, 5, lang="jp")
monitor.main()
msgs = [m for m in FakeTG.sent if m.startswith("⚡")]
assert len(msgs) == 1 and "Tyranitar ex" in msgs[0], msgs     # ora sopra il minimo; Pikachu escluso

# --- storico ----------------------------------------------------------------
series = {(state.now() - timedelta(days=i)).date().isoformat(): 1500 for i in range(1, 10)}
assert monitor.history_reference(series, 7) == 15.0
assert monitor.history_reference(dict(list(series.items())[:3]), 7) is None

# --- altri comandi Telegram ---------------------------------------------------
r, ch = telegram_bot.handle_command("/sconto 40", c, bs, sa); assert bs["overrides"]["discount_below_average_pct"] == 40
r, ch = telegram_bot.handle_command("/minimo 2,5", c, bs, sa); assert bs["overrides"]["min_reference_eur"] == 2.5
r, ch = telegram_bot.handle_command("/pausa", c, bs, sa); assert bs["paused"]
r, ch = telegram_bot.handle_command("/includi PIKACHU EX", c, bs, sa); assert ch and c["excluded_cards"] == []
r, ch = telegram_bot.handle_command("/oggi", c, bs, sa); assert "Charizard ex" in r, r
r, ch = telegram_bot.handle_command("/stato", c, bs, sa); assert "in pausa" in r and "40%" in r and "2,50 €" in r, r
r, ch = telegram_bot.handle_command("/minimo", c, bs, sa); assert "min_reference_eur" not in bs["overrides"]
tg = FakeTG("t", "42")
tg.updates = [{"update_id": 5, "message": {"chat": {"id": 999}, "text": "/pausa"}},     # altra chat: ignorato
              {"update_id": 6, "message": {"chat": {"id": 42}, "text": "/riprendi"}}]
FakeTG.sent = []
telegram_bot.process_commands(tg, c, bs, sa)
assert bs["telegram_offset"] == 7 and not bs["paused"] and len(FakeTG.sent) == 1
print("TUTTI I TEST OK")
