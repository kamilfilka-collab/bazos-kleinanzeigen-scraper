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

GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.8-flash-lite",
]

if not GEMINI_API_KEY:
    raise ValueError("Chýba GEMINI_API_KEY! Nastav ho v GitHub Secrets alebo v prostredí.")

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=15000),
)


def load_watchlist() -> list[str]:
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
    recent_ads = list(seen_set)[-3000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for ad_id in recent_ads:
            f.write(f"{ad_id}\n")


def parse_euro_price(raw_text: str) -> float | None:
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


def call_gemini_json(prompt: str) -> list | dict | None:
    for model_name in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            print(f" -> [Gemini {model_name} nedostupný]: {e}", flush=True)
            time.sleep(1)
    return None


def build_smart_fallback_queries(item: str) -> list[str]:
    base = re.sub(r"\(.*?\)", "", item).strip()
    queries = [base]
    simplified = re.sub(r"\b(?:mk\s*(?:ii|2|i|iii|3)|'?\d{2})\b", "", base, flags=re.IGNORECASE)
    simplified = re.sub(r"\s+", " ", simplified).strip(" -")
    if simplified and simplified.lower() != base.lower() and len(simplified) >= 3:
        queries.append(simplified)
    if "-" in base:
        no_dash = base.replace("-", " ")
        no_dash = re.sub(r"\s+", " ", no_dash).strip()
        if no_dash not in queries:
            queries.append(no_dash)
    return queries[:3]


def generate_search_queries_with_ai(watchlist: list[str]) -> list[dict]:
    prompt = f"""
    Mám zoznam položiek, ktoré hľadám na inzertných portáloch Bazoš.sk a Kleinanzeigen.de:
    {json.dumps(watchlist, ensure_ascii=False)}

    Pre každú položku zo vstupného zoznamu vygeneruj JSON pole objektov s týmito poľami:
    - "target_item": pôvodný text položky zo zoznamu
    - "queries": pole 2 až 3 krátkych kľúčových výrazov (2-3 najdôležitejšie slová, od špecifického po širší základný model bez zbytočných prívlastkov ako MK II alebo 65), ktoré najlepšie nájdu túto položku vo vyhľadávači na Bazoši a Kleinanzeigen.
    Vráť čisto JSON pole.
    """
    parsed = call_gemini_json(prompt)
    if isinstance(parsed, list) and parsed:
        return parsed

    print(" -> Používam lokálne generovanie vyhľadávacích výrazov (záložný režim).", flush=True)
    return [{"target_item": item, "queries": build_smart_fallback_queries(item)} for item in watchlist]


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
        print(f"Chyba pri spracovaní Bazoš RSS XML: {e}", flush=True)
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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
    }
    encoded_q = quote_plus(query)
    rss_base = f"https://www.bazos.sk/rss.php?hledat={encoded_q}"
    for url in [rss_base, f"https://corsproxy.io/?{quote_plus(rss_base)}"]:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code == 200 and "<rss" in r.text:
                return parse_bazos_rss_xml(r.text, target_item)
        except Exception:
            pass

    html_url = f"https://www.bazos.sk/search.php?hledat={encoded_q}"
    try:
        r = requests.get(html_url, headers=headers, timeout=8)
        if r.status_code == 200:
            return parse_bazos_html(r.text, target_item)
    except Exception as e:
        print(f" -> Bazoš HTML pokus zlyhal pre '{query}': {e}", flush=True)
    return []


def parse_kleinanzeigen_html(html_text: str, target_item: str) -> list[dict]:
    ads = []
    soup = BeautifulSoup(html_text, "html.parser")
    for art in soup.select("article[data-adid], article.aditem"):
        data_id = art.get("data-adid")
        href = art.get("data-href") or ""
        title = ""
        desc = ""

        ld_script = art.select_one('script[type="application/ld+json"]')
        if ld_script and ld_script.string:
            try:
                ld_data = json.loads(ld_script.string)
                title = ld_data.get("title", "").strip()
                desc = ld_data.get("description", "").strip()
            except Exception:
                pass

        link_el = (
            art.select_one("h3 a")
            or art.select_one("h2 a")
            or art.select_one("a.ellipsis")
            or art.select_one('a[href^="/s-anzeige/"]')
        )
        if not title and link_el:
            title = link_el.get_text(" ", strip=True)
        if not href and link_el:
            href = link_el.get("href", "")
        if not title and not data_id:
            continue

        link = f"https://www.kleinanzeigen.de{href}" if href.startswith("/") else href
        ad_id = f"ka_{data_id}" if data_id else f"ka_{link}"

        if not desc:
            desc_el = art.select_one("p.aditem-main--middle--description") or art.select_one("p.text-bodyRegular.text-onSurfaceSubdued")
            if desc_el:
                desc = desc_el.get_text(" ", strip=True)

        price_el = art.select_one("p.aditem-main--middle--price-shipping--price") or art.select_one("p.text-title3.font-strong")
        price_raw = price_el.get_text(" ", strip=True) if price_el else ""
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


def fetch_kleinanzeigen_for_query(query: str, target_item: str) -> list[dict]:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", query.strip().lower()).strip("-")
    if not slug:
        return []
    url = f"https://www.kleinanzeigen.de/s-anzeige:angebote/{slug}/k0"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200 and "data-adid" in r.text:
            return parse_kleinanzeigen_html(r.text, target_item)
    except Exception as e:
        print(f" -> Kleinanzeigen pokus zlyhal pre '{query}': {e}", flush=True)
    return []


def verify_ads_fallback_locally(ads: list[dict]) -> dict[str, dict]:
    bad_keywords = [
        "kúpim", "kupim", "hľadám", "hladam", "suche", "kaufe",
        "flightcase", "case", "cover", "hülle", "abdeckung", "tasche",
        "leerkarton", "karton", "ovp", "footswitch", "fußschalter",
        "röhre", "tubes", "speaker", "ersatzteil", "schutzhaube",
    ]
    results = {}
    for ad in ads:
        text_lower = f"{ad['title']} {ad['description']}".lower()
        target_words = [
            w.lower()
            for w in re.findall(r"[a-zA-Z0-9]+", ad["target_item"])
            if len(w) >= 3 and w.lower() not in ("fender", "victory", "mk", "ii", "the")
        ]
        if any(bad in text_lower for bad in bad_keywords):
            results[str(ad["id"])] = {
                "id": ad["id"],
                "is_match": False,
                "matched_item_name": ad["target_item"],
                "reason": "Vyradené lokálnym filtrom (príslušenstvo / dopyt)",
            }
        elif target_words and not all(tw in text_lower for tw in target_words):
            results[str(ad["id"])] = {
                "id": ad["id"],
                "is_match": False,
                "matched_item_name": ad["target_item"],
                "reason": "Vyradené lokálnym filtrom (chýba kľúčové označenie modelu)",
            }
        else:
            results[str(ad["id"])] = {
                "id": ad["id"],
                "is_match": True,
                "matched_item_name": ad["target_item"],
                "reason": "Nájdená zhoda (overené záložným filtrom počas výpadku AI)",
            }
    return results


def verify_ads_batch_with_ai(ads: list[dict], watchlist: list[str]) -> dict[str, dict]:
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
    - "is_match": true IBA vtedy, ak inzerát skutočne predáva hľadanú položku (pole "searched_for" alebo inú položku zo zoznamu používateľa). Daj false, ak ide o dopyt ("Kúpim", "Suche"), prenájom, výmenu, samotné príslušenstvo (obal, flightcase, footswitch, reproduktor, krabica) alebo iný model.
    - "matched_item_name": Čistý rozpoznaný názov modelu v inzeráte.
    - "reason": Krátke zhrnutie v slovenčine (1 veta).
    """
    print(f"Posielam dávku {len(ads)} inzerátov do Gemini AI na kontrolu...", flush=True)
    parsed_list = call_gemini_json(prompt)
    if isinstance(parsed_list, list):
        return {str(item.get("id")): item for item in parsed_list}

    print(" -> Gemini API je momentálne vyťažené (503), používam záložný lokálny filter.", flush=True)
    return verify_ads_fallback_locally(ads)


def save_and_notify(ad: dict, ai_info: dict):
    file_exists = os.path.isfile(CSV_OUTPUT)
    with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "Dátum", "Portál", "Hľadaná položka", "Rozpoznaný model",
                "Názov inzerátu", "Cena", "AI Poznámka", "URL",
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
    print("\n" + msg + "\n", flush=True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)
        except Exception as e:
            print(f"Chyba pri odosielaní na Telegram: {e}", flush=True)


def main():
    watchlist = load_watchlist()
    if not watchlist:
        print(f"Zoznam {WATCHLIST_FILE} je prázdny.", flush=True)
        return

    print(f"Načítaných {len(watchlist)} hľadaných položiek z {WATCHLIST_FILE}:", flush=True)
    for item in watchlist:
        print(f" - {item}", flush=True)

    seen_ads = load_seen_ads()
    search_plan = generate_search_queries_with_ai(watchlist)
    collected_ads_by_id = {}

    for entry in search_plan:
        target_item = entry.get("target_item", "")
        queries = entry.get("queries", [])
        print(f"\nHľadám: '{target_item}' (výrazy: {queries})", flush=True)

        for q in queries:
            bazos_ads = fetch_bazos_for_query(q, target_item)
            print(f" -> [Bazoš.sk] '{q}': nájdených {len(bazos_ads)} inzerátov", flush=True)
            for ad in bazos_ads:
                if ad["id"] not in seen_ads and ad["id"] not in collected_ads_by_id:
                    collected_ads_by_id[ad["id"]] = ad

            ka_ads = fetch_kleinanzeigen_for_query(q, target_item)
            print(f" -> [Kleinanzeigen.de] '{q}': nájdených {len(ka_ads)} inzerátov", flush=True)
            for ad in ka_ads:
                if ad["id"] not in seen_ads and ad["id"] not in collected_ads_by_id:
                    collected_ads_by_id[ad["id"]] = ad

            time.sleep(1.0)

    new_ads = list(collected_ads_by_id.values())
    print(f"\nCelkovo nájdených {len(new_ads)} nových (ešte nevidených) inzerátov.", flush=True)

    if not new_ads:
        return

    batch_size = 40
    matches_count = 0

    for i in range(0, len(new_ads), batch_size):
        batch = new_ads[i : i + batch_size]
        ai_results = verify_ads_batch_with_ai(batch, watchlist)

        for ad in batch:
            seen_ads.add(ad["id"])
            ai_info = ai_results.get(str(ad["id"]), {})

            if ai_info.get("is_match"):
                matches_count += 1
                save_and_notify(ad, ai_info)
            else:
                print(
                    f" [X] Vyradené ({ad['portal']}): {ad['title']} -> {ai_info.get('reason', 'Nezodpovedá zadaniu')}",
                    flush=True,
                )

    save_seen_ads(seen_ads)
    print(f"\nHotovo! Uložených {matches_count} zhodných inzerátov.", flush=True)


if __name__ == "__main__":
    main()
