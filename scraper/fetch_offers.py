import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


TBMM_LIST_URLS = [
    "https://www5.tbmm.gov.tr/develop/owa/tasari_teklif_sd.sorgu_sonuc?bulunan_kayit=3278&icerik_arama=&kullanici_id=18731517&metin_arama=&sonuc_sira=340&taksim_no=0",
    "https://www.tbmm.gov.tr/develop/owa/tasari_teklif_sd.sorgu_sonuc?bulunan_kayit=3278&icerik_arama=&kullanici_id=18731517&metin_arama=&sonuc_sira=340&taksim_no=0",
]


def init_firestore():
    service_account_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not service_account_path:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON environment variable is missing.")

    if not firebase_admin._apps:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)

    return firestore.client()


def fetch_tbmm_list():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AcikMeclisBot/1.0)"
    }

    last_error = None

    for url in TBMM_LIST_URLS:
        print(f"Trying URL: {url}")

        for attempt in range(1, 4):
            try:
                print(f"Attempt {attempt} for {url}")
                response = requests.get(
                    url,
                    timeout=60,
                    headers=headers,
                )
                response.raise_for_status()
                print(f"Success with URL: {url}")
                return response.text
            except requests.RequestException as e:
                last_error = e
                print(f"Request failed on attempt {attempt}: {e}")
                time.sleep(3)

    raise RuntimeError(f"All TBMM URLs failed. Last error: {last_error}")


def extract_kanunlar_sira_no(url: str):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("kanunlar_sira_no")
    if values and values[0].strip():
        return values[0].strip()
    return None


def parse_basic_offers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    offers = []

    all_links = soup.find_all("a", href=True)
    print("Total <a> tags found:", len(all_links))

    # SADECE gerçek teklif kayıtlarını temsil eden
    # "Diğer Bilgiler..." linklerini alıyoruz.
    for a in all_links:
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        if "tasari_teklif_sd.onerge_bilgileri" not in href.lower():
            continue

        kanunlar_sira_no = extract_kanunlar_sira_no(href)
        if not kanunlar_sira_no:
            continue

        if href.startswith("http"):
            full_url = href
        elif href.startswith("/"):
            full_url = f"https://www5.tbmm.gov.tr{href}"
        else:
            full_url = f"https://www5.tbmm.gov.tr/{href}"

        offers.append({
            "tbmmId": f"tbmm_{kanunlar_sira_no}",
            "title": f"TBMM Kanun Teklifi {kanunlar_sira_no}",
            "sourceUrl": full_url,
            "kanunlarSiraNo": kanunlar_sira_no,
        })

    unique = {}
    for item in offers:
        unique[item["tbmmId"]] = item

    result = list(unique.values())

    print(f"Found filtered offers: {len(result)}")
    if result:
        print("Sample offers:")
        for item in result[:10]:
            print(f"- {item['tbmmId']} | {item['sourceUrl']}")

    return result


def upsert_laws(db, offers):
    now = datetime.now(timezone.utc)

    for offer in offers:
        doc_ref = db.collection("laws").document(offer["tbmmId"])
        existing = doc_ref.get()

        payload = {
            "tbmmId": offer["tbmmId"],
            "kanunlarSiraNo": offer["kanunlarSiraNo"],
            "title": offer["title"],
            "summary": "",
            "content": "",
            "category": "Genel",
            "sourceUrl": offer["sourceUrl"],
            "submittedAt": None,
            "status": "teklif_edildi",
            "statusLabel": "Teklif Edildi",
            "publishedAt": now,
            "lastSyncedAt": now,
            "isActive": True,
            "createdBy": "tbmm_sync_bot",
        }

        if existing.exists:
            doc_ref.set(
                {
                    "title": offer["title"],
                    "sourceUrl": offer["sourceUrl"],
                    "kanunlarSiraNo": offer["kanunlarSiraNo"],
                    "lastSyncedAt": now,
                },
                merge=True,
            )
            print(f"UPDATED: {offer['tbmmId']}")
        else:
            doc_ref.set(payload)
            print(f"CREATED: {offer['tbmmId']}")


def main():
    db = init_firestore()
    html = fetch_tbmm_list()
    offers = parse_basic_offers(html)

    if not offers:
        print("No filtered offers found.")
        return

    upsert_laws(db, offers)
    print(f"Done. Total synced: {len(offers)}")


if __name__ == "__main__":
    main()
