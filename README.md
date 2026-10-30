# DokkanDB → Telegram

Il bot gira solo quando GitHub Actions avvia un controllo programmato: non richiede un PC acceso e non mantiene un processo attivo 24 ore su 24. Gli orari usano `Europe/Rome`, quindi seguono automaticamente il passaggio tra ora solare e legale. GitHub può ritardare o saltare alcuni avvii durante periodi di carico elevato.

## Programmazione giornaliera

- **News:** Global e JP ogni 30 minuti, agli orari :07 e :37.
- **Summon stone e ticket:** entrambe le categorie per Global e JP ogni ora.
- **Personaggi:** JP ogni 5 minuti dalle 02:30 alle 03:30; Global ogni 5 minuti dalle 06:30 alle 07:30. Fuori da queste finestre, entrambi ogni ora, alle :07.
- **Challenge:** JP ogni 5 minuti dalle 02:30 alle 03:30; Global ogni 5 minuti dalle 06:30 alle 07:30. Non ci sono controlli Challenge fuori da queste finestre.
- **Altre categorie evento:** tutte le altre 11 liste vengono controllate due volte al giorno per versione: Global alle 03:30 e 15:30; JP alle 07:30 e 19:30.
- **Quest:** agli stessi quattro orari il bot controlla direttamente l'area attiva e quella successiva, a partire dalla 42. Quando la successiva ha stage, diventa quella attiva: per esempio passa da 42+43 a 43+44. Non apre le pagine dei singoli stage.
- Le finestre delle carte sono JP ogni 5 minuti dalle 02:30 alle 03:30 e Global ogni 5 minuti dalle 06:30 alle 07:30; fuori finestra le carte sono controllate ogni ora.
- Per un nuovo Challenge Event il messaggio elenca gli stage e collega le pagine Boss Stats. Le altre categorie non elencano gli stage.
