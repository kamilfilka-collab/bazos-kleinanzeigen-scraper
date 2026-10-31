# Bazoš.sk & Kleinanzeigen.de AI Scraper

Automatický vyhľadávač inzerátov na **Bazoš.sk** a **Kleinanzeigen.de**, ktorý využíva umelú inteligenciu **Google Gemini** na:
1. **Generovanie hľadacích výrazov aj s bežnými preklepmi** (napr. `ddj-400` -> `ddj 400`, `ddj400`).
2. **Inteligentné vyfiltrovanie výsledkov** – vyradí dopyty (*"Kúpim"*, *"Suche"*), prázdne krabice, obaly, káble, nefunkčné kusy či nesúvisiace modely.
3. **Okamžité upozornenie cez Telegram** a zápis do tabuľky `najdene_inzeraty.csv`.

---

## Ako pridávať alebo mazať hľadané položky

Otvor súbor **`watchlist.txt`** (môžeš ho upravovať priamo na webe GitHubu kliknutím na ikonu ceruzky ✏️):
- **1 riadok = 1 hľadaná položka**
- Ak chceš pridať novú vec, jednoducho ju napíš na nový riadok.
- Do zátvorky môžeš pridať spresnenie pre AI (napr. `Fender Stratocaster (iba Mexiko alebo USA, nie Squier)`).
- Ak chceš položku vymazať, vymaž celý riadok (alebo pred neho napíš `#`).

---

## Ako to nahrať na GitHub

1. Vytvor si na svojom GitHube nový repozitár (môže byť **Private** aj **Public**).
2. Nahraj doň všetky súbory z tohto priečinka:
   - `scraper.py`
   - `watchlist.txt`
   - `requirements.txt`
   - `seen_ads.txt`
   - `.github/workflows/scraper.yml`
3. V repozitári na GitHube choď do **Settings** -> **Secrets and variables** -> **Actions** -> **New repository secret** a pridaj:
   - `GEMINI_API_KEY` – tvoj API kľúč z Google AI Studio
   - `TELEGRAM_BOT_TOKEN` – (voliteľné) token tvojho Telegram bota
   - `TELEGRAM_CHAT_ID` – (voliteľné) tvoje Telegram Chat ID
4. Choď do záložky **Actions** na GitHube, klikni na **Bazos & Kleinanzeigen AI Scraper** a stlač **Run workflow**. Od tej chvíle bude bežať automaticky každú hodinu.
