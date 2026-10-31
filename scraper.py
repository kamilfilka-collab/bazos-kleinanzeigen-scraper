import csv
import json
import os
import re
import time
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright
import requests

# ==========================================
# KONFIGURÁCIA (Načítanie z GitHub Secrets)
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

WATCHLIST_FILE = "watchlist.txt"
CSV_OUTPUT = "najdene_inzeraty.csv"
SEEN_FILE = "seen_ads.txt"
GEMINI_MODEL = "gemini-2.5-flash"

if not GEMINI_API_KEY:
    raise ValueError("Chýba GEMINI_API_KEY! Nastav ho v GitHub Secrets alebo v prostredí.")

client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# POMOCNÉ FUNKCIE PRE SÚBORY A CENY
# ==========================================
def load_watchlist() -> list[str]:
    """Načíta hľadané položky z jednoduchého textového súboru watchlist.txt."""
    if not os.path.exists(WATCHLIST_FILE):
        print(f"Upozornenie: Súbor {WATCHLIST_FILE} neexistuje!")
        return []
    items = []
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            clean = line.strip()
            if clean and not clean.startswith("#"):
                items.append(clean)
    return items


def load_seen_ads() -> set[str]:
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_ads(seen_set: set[str]):
    # Uchovávame posledných 3000 ID inzerátov, aby súbor nerástol donekonečna
    recent_ads = list(seen_set)[-3000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for ad_id in recent_ads:
            f.write(f"{ad_id}\n")


def parse_euro_price(raw_text: str) -> float | None:
    """Prevedie textovú cenu (napr. '1.479 € VB', '250 €', 'Dohodou') na číslo (float)."""
    if not raw_text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", raw_text).strip(".,")
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(".", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None


# ==========================================
# 1. AI GENEROVANIE HĽADACÍCH VÝRAZOV (AJ S PREKLEPMI)
# ==========================================
def generate_search_queries_with_ai(watchlist: list[str]) -> list[dict]:
    """
    Pomocou Gemini vytvorí pre každú položku v watchlist.txt zoznam krátkych hľadacích
    výrazov (vrátane bežných skratiek alebo preklepov, ktoré ľudia robia).
    """
    prompt = f"""
    Mám zoznam položiek, ktoré hľadám na inzertných portáloch Bazoš.sk a Kleinanzeigen.de:
    {json.dumps(watchlist, ensure_ascii=False)}

    Ľudia v inzerátoch často vynechávajú pomlčky, medzery alebo robia drobné preklepy (napr. "ddj 400", "ddj400", "stratocaster", "psre443").
    Pre každú položku zo vstupného zoznamu vygeneruj JSON pole objektov s týmito poľami:
    - "target_item": pôvodný text položky zo zoznamu
    - "queries": pole 1 až 3 krátkych kľúčových výrazov (2-3 slová bez zátvoriek a poznámok), ktoré najlepšie nájdu túto položku vo vyhľadávači na Bazoši a Kleinanzeigen (vrátane najčastejšieho alternatívneho zápisu bez pomlčky/medzery).

    Vráť čisto JSON pole.
    """
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"[AI Generovanie výrazov - pokus {attempt + 1}/3 zlyhal]: {e}")
            time.sleep(5)

    # Záložné riešenie, ak by AI neodpovedala: odstránime zátvorky a použijeme čistý názov
    fallback = []
    for item in watchlist:
        clean_q = re.sub(r"\(.*?\)", "", item).strip()
        fallback.append({"target_item": item, "queries": [clean_q]})
    return fallback


# ==========================================
# 2. SŤAHOVANIE BAZOŠ.SK (RSS + HTML VYHĽADÁVANIE)
# ==========================================
def parse_bazos_rss_xml(xml_text: str, target_item: str) -> list[dict]:
    ads = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.findall(".//item"):
            title_raw = item.findtext("title") or ""
            link = item.findtext("link") or ""
            desc_html = item.findtext("description") or ""

            match_id = re.search(r"/inzerat/(\d+)/", link)
            ad_id = f"bazos_{match_id.group(1)}" if match_id else f"bazos_{link}"

            price = None
            clean_title = title_raw.strip()
            price_match = re.search(r"[:\-]\s*([\d\s]+)\s*(?:€|Eur)?$", title_raw.strip(), re.IGNORECASE)
            if price_match:
                price_str = price_match.group(1).replace(" ", "").strip()
                if price_str.isdigit():
                    price = float(price_str)
                clean_title = title_raw[: price_match.start()].strip()

            clean_desc = BeautifulSoup(desc_html, "html.parser").get_text(" ", strip=True)

            ads.append({
                "id": ad_id,
                "portal": "Bazos.sk",
                "target_item": target_item,
                "title": clean_title,
                "description": clean_desc,
                "price": price,
                "price_text": f"{int(price)} €" if price is not None else "Dohodou / Neuvedená",
                "url": link,
            })
    except Exception as e:
        print(f"Chyba pri spracovaní Bazoš RSS XML: {e}")
    return ads


def parse_bazos_html(html_text: str, target_item: str) -> list[dict]:
    ads = []
    soup = BeautifulSoup(html_text, "html.parser")
    for inzerat in soup.select("div.inzeraty.inzeratyflex"):
        title_el = inzerat.select_one("h2.nadpis a")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        link = f"https://www.bazos.sk{href}" if href.startswith("/") else href

        match_id = re.search(r"/inzerat/(\d+)/", link)
        ad_id = f"bazos_{match_id.group(1)}" if match_id else f"bazos_{link}"

        desc_el = inzerat.select_one("div.popis")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""

        price_el = inzerat.select_one("div.inzeratycena b")
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price = parse_euro_price(price_raw)

        ads.append({
            "id": ad_id,
            "portal": "Bazos.sk",
            "target_item": target_item,
            "title": title,
            "description": desc,
            "price": price,
            "price_text": price_raw if price_raw else "Dohodou",
            "url": link,
        })
    return ads


def fetch_bazos_for_query(query: str, target_item: str) -> list[dict]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
    }
    encoded_q = quote_plus(query)
    rss_base = f"https://www.bazos.sk/rss.php?hledat={encoded_q}"
    rss_candidates = [
        rss_base,
        f"https://api.allorigins.win/raw?url={quote_plus(rss_base)}",
        f"https://corsproxy.io/?{quote_plus(rss_base)}",
    ]

    for url in rss_candidates:
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200 and "<rss" in r.text:
                ads = parse_bazos_rss_xml(r.text, target_item)
                return ads
        except Exception:
            pass

    # Záložné HTML vyhľadávanie, ak by RSS nefungovalo
    html_url = f"https://www.bazos.sk/search.php?hledat={encoded_q}"
    try:
        r = requests.get(html_url, headers=headers, timeout=12)
        if r.status_code == 200:
            return parse_bazos_html(r.text, target_item)
    except Exception as e:
        print(f" -> Bazoš HTML pokus zlyhal pre '{query}': {e}")

    return []


# ==========================================
# 3. SŤAHOVANIE KLEINANZEIGEN.DE
# ==========================================
def parse_kleinanzeigen_html(html_text: str, target_item: str) -> list[dict]:
    ads = []
    soup = BeautifulSoup(html_text, "html.parser")
    articles = soup.select("article.aditem")

    for art in articles:
        data_id = art.get("data-adid")
        link_el = art.select_one("a.ellipsis") or art.select_one("h2 a")
        if not link_el and not data_id:
            continue

        title = link_el.get_text(strip=True) if link_el else ""
        href = link_el.get("href", "") if link_el else art.get("data-href", "")
        link = f"https://www.kleinanzeigen.de{href}" if href.startswith("/") else href

        ad_id = f"ka_{data_id}" if data_id else f"ka_{link}"

        desc_el = art.select_one("p.aditem-main--middle--description")
        desc = desc_el.get_text(" ", strip=True) if desc_el else ""

        price_el = art.select_one("p.aditem-main--middle--price-shipping--price")
        price_raw = price_el.get_text(strip=True) if price_el else ""
        price = parse_euro_price(price_raw)

        ads.append({
            "id": ad_id,
            "portal": "Kleinanzeigen.de",
            "target_item": target_item,
            "title": title,
            "description": desc,
            "price": price,
            "price_text": price_raw if price_raw else "VB / Dohodou",
            "url": link,
        })
    return ads


def fetch_kleinanzeigen_for_query(query: str, target_item: str, browser_context) -> list[dict]:
    encoded_q = quote_plus(query)
    url = f"https://www.kleinanzeigen.de/s-anzeige:angebote/{encoded_q}/k0"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }

    # 1. Rýchly pokus cez priamy HTTP request
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200 and "aditem" in r.text:
            ads = parse_kleinanzeigen_html(r.text, target_item)
            if ads:
                return ads
    except Exception:
        pass

    # 2. Ak Kleinanzeigen vyžaduje prehliadač (Playwright)
    if browser_context:
        page = browser_context.new_page()
        try:
            page.goto(url, timeout=20000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            html = page.content()
            return parse_kleinanzeigen_html(html, target_item)
        except Exception as e:
            print(f" -> Kleinanzeigen Playwright pokus zlyhal pre '{query}': {e}")
        finally:
            page.close()

    return []


# ==========================================
# 4. HROMADNÁ AI ANALÝZA INZERÁTOV (GEMINI)
# ==========================================
def verify_ads_batch_with_ai(ads: list[dict], watchlist: list[str]) -> dict[str, dict] | None:
    """
    Pošle nájdené inzeráty do Gemini AI, aby vyfiltrovala:
    - či inzerát naozaj predáva hľadanú položku z watchlistu (aj keď má v názve preklep)
    - vyradí dopyty ("Kúpim" / "Suche"), prenájom, obaly, krabice, káble, príslušenstvo, alebo iné modely.
    """
    items_for_prompt = [
        {
            "id": a["id"],
            "portal": a["portal"],
            "searched_for": a["target_item"],
            "title": a["title"],
            "price_text": a["price_text"],
            "description": a["description"][:350],
        }
        for a in ads
    ]

    prompt = f"""
    Si inteligentný nákupný asistent. Používateľ hľadá na Bazoš.sk a Kleinanzeigen.de tieto položky:
    {json.dumps(watchlist, ensure_ascii=False)}

    Tu je zoznam novo nájdených inzerátov:
    {json.dumps(items_for_prompt, ensure_ascii=False)}

    Pre každý inzerát vráť objekt v JSON poli s týmito poľami:
    - "id": rovnaké ID inzerátu ako na vstupe
    - "is_match": true IBA vtedy, ak inzerát skutočne predáva hľadanú položku (pole "searched_for" alebo inú položku zo zoznamu používateľa). Ber do úvahy, že predávajúci mohol spraviť v názve preklep.
      Daj false, ak:
      1. Ide o dopyt ("Kúpim", "Hľadám", "Suche", "Kaufe"), prenájom alebo výmenu.
      2. Predáva sa iba príslušenstvo (samotný kufor, obal, decksaver, krabica, adaptér, kábel, stojan, náhradný diel).
      3. Ide o iný model alebo značku, prípadne je v poznámke položky zo zoznamu uvedené obmedzenie (napr. "nie Squier" alebo cenový strop), ktoré inzerát porušuje.
    - "matched_item_name": Čistý rozpoznaný názov modelu v inzeráte (napr. "Pioneer DDJ-400").
    - "reason": Krátke zhrnutie v slovenčine (1 veta, napr. "Predaj kontroléra Pioneer DDJ-400 v dobrom stave").
    """

    for attempt in range(3):
        try:
            print(f"Posielam dávku {len(ads)} inzerátov do Gemini AI na kontrolu...")
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            parsed_list = json.loads(response.text)
            return {str(item.get("id")): item for item in parsed_list}
        except Exception as e:
            print(f"[AI Analýza pokus {attempt + 1}/3 zlyhal]: {e}")
            time.sleep(10)

    return None


# ==========================================
# 5. ULOŽENIE A NOTIFIKÁCIA (CSV + TELEGRAM)
# ==========================================
def save_and_notify(ad: dict, ai_info: dict):
    file_exists = os.path.isfile(CSV_OUTPUT)
    with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "Dátum",
                "Portál",
                "Hľadaná položka",
                "Rozpoznaný model",
                "Názov inzerátu",
                "Cena",
                "AI Poznámka",
                "URL",
            ])
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M"),
            ad["portal"],
            ad["target_item"],
            ai_info.get("matched_item_name") or ad["target_item"],
            ad["title"],
            ad["price_text"],
            ai_info.get("reason", ""),
            ad["url"],
        ])

    msg = (
        f"🔔 NOVÝ INZERÁT ({ad['portal']})\n"
        f"🎯 Položka: {ai_info.get('matched_item_name') or ad['target_item']}\n"
        f"📌 Názov: {ad['title']}\n"
        f"💰 Cena: {ad['price_text']}\n"
        f"🤖 AI: {ai_info.get('reason', '')}\n"
        f"🔗 Link: {ad['url']}"
    )
    print("\n" + msg + "\n")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        except Exception as e:
            print(f"Chyba pri odosielaní na Telegram: {e}")


# ==========================================
# HLAVNÝ PROGRAM
# ==========================================
def main():
    watchlist = load_watchlist()
    if not watchlist:
        print(f"Zoznam {WATCHLIST_FILE} je prázdny. Pridaj do neho položky na vyhľadávanie.")
        return

    print(f"Načítaných {len(watchlist)} hľadaných položiek z {WATCHLIST_FILE}:")
    for item in watchlist:
        print(f" - {item}")

    seen_ads = load_seen_ads()
    search_plan = generate_search_queries_with_ai(watchlist)

    collected_ads_by_id = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )

        for entry in search_plan:
            target_item = entry.get("target_item", "")
            queries = entry.get("queries", [])
            print(f"\nHľadám: '{target_item}' (výrazy: {queries})")

            for q in queries:
                # 1. Bazoš.sk
                bazos_ads = fetch_bazos_for_query(q, target_item)
                print(f" -> [Bazoš.sk] '{q}': nájdených {len(bazos_ads)} inzerátov")
                for ad in bazos_ads:
                    if ad["id"] not in seen_ads and ad["id"] not in collected_ads_by_id:
                        collected_ads_by_id[ad["id"]] = ad

                # 2. Kleinanzeigen.de
                ka_ads = fetch_kleinanzeigen_for_query(q, target_item, context)
                print(f" -> [Kleinanzeigen.de] '{q}': nájdených {len(ka_ads)} inzerátov")
                for ad in ka_ads:
                    if ad["id"] not in seen_ads and ad["id"] not in collected_ads_by_id:
                        collected_ads_by_id[ad["id"]] = ad

                time.sleep(1.5)

        browser.close()

    new_ads = list(collected_ads_by_id.values())
    print(f"\nCelkovo nájdených {len(new_ads)} nových (ešte nevidených) inzerátov.")

    if not new_ads:
        return

    # Rozdelíme inzeráty na dávky po max 40 kusov, aby sme neprekročili limit odpovede AI
    batch_size = 40
    matches_count = 0

    for i in range(0, len(new_ads), batch_size):
        batch = new_ads[i : i + batch_size]
        ai_results = verify_ads_batch_with_ai(batch, watchlist)

        if ai_results is None:
            print("AI analýza pre túto dávku zlyhala, inzeráty skontrolujem pri ďalšom behu.")
            continue

        for ad in batch:
            seen_ads.add(ad["id"])
            ai_info = ai_results.get(str(ad["id"]), {})

            if ai_info.get("is_match"):
                matches_count += 1
                save_and_notify(ad, ai_info)
            else:
                print(f" [X] Vyradené ({ad['portal']}): {ad['title']} -> {ai_info.get('reason', 'Nezodpovedá zadaniu')}")

    save_seen_ads(seen_ads)
    print(f"\nHotovo! Uložených {matches_count} zhodných inzerátov.")


if __name__ == "__main__":
    main()
