# DokkanDB → Telegram

Il bot gira solo quando GitHub Actions avvia un controllo programmato: non richiede un PC acceso e non mantiene un processo attivo 24 ore su 24. Gli orari usano `Europe/Rome`, quindi seguono automaticamente il passaggio tra ora solare e legale. GitHub può ritardare o saltare alcuni avvii durante periodi di carico elevato.

## Programmazione giornaliera

- **News:** Global e JP ogni 30 minuti, agli orari :01 e :31.
- **Summon stone e ticket:** entrambe le categorie per Global e JP ogni ora.
- **Personaggi:** Global ogni 5 minuti dalle 02:30 alle 03:30; JP ogni 5 minuti dalle 06:30 alle 07:30. Fuori da queste finestre, entrambi ogni ora, alle :01.
- **Challenge:** Global ogni 5 minuti dalle 02:30 alle 03:30; JP ogni 5 minuti dalle 06:30 alle 07:30. Non ci sono controlli Challenge fuori da queste finestre.
- **Altre categorie evento:** tutte le altre 11 liste vengono controllate due volte al giorno per versione: Global alle 03:30 e 15:30; JP alle 07:30 e 19:30.
- **Quest:** agli stessi quattro orari il bot controlla direttamente l'area attiva e quella successiva, a partire dalla 42. Quando la successiva ha stage, diventa quella attiva: per esempio passa da 42+43 a 43+44. Non apre le pagine dei singoli stage.
- Le finestre delle carte restano Global ogni 5 minuti dalle 02:30 alle 03:30 e JP ogni 5 minuti dalle 06:30 alle 07:30; fuori finestra le carte sono controllate ogni ora.
- Per un nuovo Challenge Event il messaggio elenca gli stage e collega le pagine Boss Stats. Le altre categorie non elencano gli stage.

## Stima del carico su DokkanDB

Con questa pianificazione, GitHub avvia **76 workflow al giorno**. Le visite pianificate alle pagine sono circa **342 al giorno**:

- Challenge: 26 pagine elenco al giorno.
- Altre categorie evento: 44 pagine elenco al giorno.
- Quest: 8 pagine area al giorno (area corrente e successiva nei quattro controlli).
- Carte: circa 72 pagine al giorno.
- News: 96 pagine al giorno.
- Summon: 96 pagine al giorno.

Queste sono aperture di pagina, non il totale esatto delle richieste HTTP: le pagine possono richiedere anche documenti o dati aggiuntivi. Il browser blocca immagini, video e font e il log di Actions stampa le richieste dirette a DokkanDB che ha consentito. A queste visite si aggiungono eventuali schede di personaggi nuovi e pagine Boss Stats dei nuovi Challenge Event. Il numero reale di richieste HTTP va quindi misurato nei log dopo l'avvio.

## Avvio

1. Crea un bot con [@BotFather](https://t.me/BotFather), apri una chat con il bot e invia `/start`.
2. Dopo aver scritto al bot, ricava il `chat_id` da `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Crea un repository GitHub **pubblico** e carica i file del progetto.
4. In **Settings → Secrets and variables → Actions**, aggiungi i secrets `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`.
5. In **Actions → Check DokkanDB → Run workflow**, attiva `test_telegram` per provare l'invio. La prima scansione programmata crea la baseline e non avvisa per i contenuti già presenti.

Il confronto usa `state.json`, che contiene gli ID già visti e viene aggiornato dal workflow quando cambia. Il bot legge le ultime carte e, quando rileva nuovi ID, scorre le pagine successive fino a incontrare un ID già noto. Per news e summon applica lo stesso criterio alle liste più recenti.
