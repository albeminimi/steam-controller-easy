# Coda Steam Controller

Pagina per controllare al volo a che punto è la coda di prenotazione dello Steam Controller (2026), con attenzione all'Europa.

Mostra:

- **Annunci Valve** sul Controller e sulle prenotazioni (feed RSS ufficiale dell'hardware Steam).
- **Stato dello store**: se la pagina mostra ancora il modulo di prenotazione e il prezzo in IT, DE, FR, ES, NL, GB.
- **Mail e acquisti segnalati** sul forum Steam del Controller, con un'etichetta quando il messaggio cita un paese o un corriere europeo.
- **La tua prenotazione**: finestra stimata, data di prenotazione e, quando arriva la mail, il conto alla rovescia delle 72 ore. Resta salvata solo nel tuo browser.

## Cosa non può fare

La posizione in coda e lo stato della tua prenotazione li vede solo Valve, da loggato sulla [pagina Steam Controller](https://store.steampowered.com/hardware/steamcontroller). Valve non pubblica quante mail ha inviato per regione: le segnalazioni del forum sono un indizio, non un conteggio.

## Come funziona

- `scripts/update_status.py` raccoglie i dati (solo libreria standard Python) e scrive `data/status.json`.
- `.github/workflows/update.yml` lo esegue ogni 3 ore, salva il JSON nel repository e pubblica il sito su GitHub Pages.
- `index.html` è la pagina: legge `data/status.json`, senza dipendenze esterne a parte i font.

### Attivare GitHub Pages (una volta sola)

1. Su GitHub: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. **Actions → "Aggiorna stato e pubblica" → Run workflow**.
3. Il sito sarà su `https://albeminimi.github.io/steam-controller-easy/`.

### In locale

```sh
python3 scripts/update_status.py
python3 -m http.server 8000   # poi apri http://localhost:8000
```

Sito non ufficiale, non affiliato a Valve.
