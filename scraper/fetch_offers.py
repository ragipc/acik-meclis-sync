import os
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


TBMM_LIST_URL = "https://www.tbmm.gov.tr/yasama/kanun-teklifleri"


def init_firestore():
    service_account_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not service_account_path:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON environment variable is missing.")

    if not firebase_admin._apps:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)

    return firestore.client()


def fetch_tbmm_list():
    response = requests.get(
        TBMM_LIST_URL,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; AcikMeclisBot/1.0)"
        },
    )
    response.raise_for_status()
    return response.text


def parse_basic_offers(html: str):
    soup = BeautifulSoup(html, "html.parser")

    offers = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        if "/Yasama/KanunTeklifi/" in href and text:
            full_url = href if href.startswith("http") else f"https://www.tbmm.gov.tr{href}"
            tbmm_id = full_url.rstrip("/").split("/")[-1]

            offers.append({
                "tbmmId": tbmm_id,
                "title": text,
                "sourceUrl": full_url,
            })

    unique = {}
    for item in offers:
        unique[item["tbmmId"]] = item

    return list(unique.values())


def upsert_laws(db, offers):
    now = datetime.now(timezone.utc)

    for offer in offers:
        doc_ref = db.collection("laws").document(offer["tbmmId"])
        existing = doc_ref.get()

        payload = {
            "tbmmId": offer["tbmmId"],
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
        }

        if existing.exists:
            doc_ref.set(
                {
                    "title": offer["title"],
                    "sourceUrl": offer["sourceUrl"],
                    "lastSyncedAt": now,
                },
                merge=True,
            )
            print(f"UPDATED: {offer['tbmmId']} - {offer['title']}")
        else:
            doc_ref.set(payload)
            print(f"CREATED: {offer['tbmmId']} - {offer['title']}")


def main():
    db = init_firestore()
    html = fetch_tbmm_list()
    offers = parse_basic_offers(html)

    if not offers:
        print("No offers found.")
        return

    upsert_laws(db, offers)
    print(f"Done. Total synced: {len(offers)}")


if __name__ == "__main__":
    main()
