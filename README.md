# ping-pokemon

Bot che cerca occasioni di prezzo su carte **Pokémon** di rarità **Double Rare
o superiore** su **Cardtrader** (inglese e giapponese) e le manda su Telegram.
Gira da solo su GitHub Actions. Fratello di `ping-carte` (Yu-Gi-Oh!).

## Come funziona

1. **Indice** – `build_card_index.py` scorre tutte le espansioni Pokémon di
   Cardtrader e tiene le carte con una rarità in `allowed_rarities`
   (`cards_index.json`). Non serve una lista di carte: i set nuovi entrano da
   soli, perché l'indice si rifà ogni lunedì. In `rarities_seen.json` trovi
   tutte le rarità che Cardtrader usa, con ✓ quelle monitorate.
2. **Monitor** – `monitor.py` scarica le inserzioni un'espansione alla volta,
   calcola il prezzo di riferimento e manda un messaggio per carta, prima
   quelle con lo sconto più alto (max `notifications.max_messages_per_run`
   per giro, il resto al giro dopo).

## Quando è un'occasione

**Prezzo di riferimento** di ogni carta, separato per lingua (e per reverse
holo, se c'è): mediana delle inserzioni attuali (tolti gli estremi,
`trim_pct`). Dopo `price_history.min_days_for_reference` giorni di storico si
usa il più basso tra la mediana di oggi e quella delle ultime settimane, così
un rialzo improvviso non crea false occasioni.

**Occasione**: inserzione che costa
- almeno `discount_below_average_pct`% meno del riferimento (30%), **e**
- almeno `min_saving_eur` in meno (1 €).

Le carte con riferimento sotto `min_reference_eur` (2 €) vengono ignorate.

Filtri: solo venditori CT Zero (`ct_zero_only`), lingue in `languages`
(`en`, `jp`), condizione minima `min_condition`, niente carte gradate.
Venditori di tutti i Paesi (`deal_seller_countries` vuoto; es. `["US","CA"]`
per limitarli). Un'inserzione già segnalata torna solo se scende di almeno
`notifications.renotify_if_cheaper_pct`%.

## Comandi Telegram

| Comando | Cosa fa |
|---|---|
| `/stato` | impostazioni e occasioni di oggi |
| `/pausa` · `/riprendi` | ferma / riattiva le notifiche |
| `/sconto 35` | sconto minimo in % (`/sconto` da solo = valore di config) |
| `/minimo 5` | ignora le carte che valgono meno di 5 € (`/minimo` = config) |
| `/escludi Nome carta` | non segnalare più quella carta (`/escludi` da solo = elenco) |
| `/includi Nome carta` | toglila dagli esclusi |
| `/oggi` | migliori occasioni notificate oggi |
| `/aiuto` | elenco comandi |

Usa **lo stesso bot Telegram e la stessa chat di ping-carte**: i messaggi
Pokémon iniziano con ⚡, quelli Yu-Gi-Oh! con 🃏. `/stato`, `/pausa`,
`/riprendi`, `/sconto` e `/oggi` rispondono per entrambi i giochi; aggiungi
`pokemon` o `yugioh` per darli a uno solo, es. `/pausa pokemon`,
`/sconto pokemon 35`. Accetta comandi solo dalla chat `TELEGRAM_CHAT_ID`.

Funziona perché nessuno dei due bot "consuma" i messaggi (getUpdates senza
offset): ognuno ricorda l'ultimo messaggio letto. Per questo anche
ping-carte deve avere la versione aggiornata di `telegram_bot.py`.

## Installazione

1. Crea un repository GitHub **privato** `ping-pokemon` e caricaci questi file.
2. Aggiorna ping-carte con il nuovo `telegram_bot.py` (vedi sopra), prima
   di avviare questo bot.
3. *Settings → Secrets and variables → Actions*: aggiungi
   `CARDTRADER_TOKEN`, `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, **gli stessi
   valori di ping-carte**.
4. *Actions → Build card index → Run workflow*. Nel log controlla l'elenco
   "Rarità trovate su Cardtrader": se una rarità che vuoi non ha la ✓,
   aggiungila (in minuscolo) ad `allowed_rarities` in `config.json`.
5. *Actions → Monitor Cardtrader Pokemon → Run workflow* con `dry_run`
   attivo: stampa i messaggi nel log senza mandarli. Se va bene, lancialo
   senza dry_run.
6. Come per ping-carte, su **cron-job.org** crea un job ogni 15 minuti che
   avvia il workflow `monitor.yml` di questo repository, sfasato di qualche
   minuto rispetto a quello di ping-carte (es. ping-carte a :00/:15/:30/:45,
   questo a :07/:22/:37/:52), così i due bot non leggono Telegram nello
   stesso istante.

Test senza rete: `python tests/test_offline.py`
