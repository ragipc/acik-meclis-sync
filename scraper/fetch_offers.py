import os
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


TBMM_LIST_URL = "https://www.tbmm.gov.tr/develop/owa/kanun_teklifi_sd.sorgu_baslangic"

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
    print("HTML length:", len(html))
    print("HTML first 1000 chars:")
    print(html[:1000])

    soup = BeautifulSoup(html, "html.parser")

    all_links = soup.find_all("a", href=True)
    print("Total <a> tags found:", len(all_links))

    print("First 30 href values:")
    for a in all_links[:30]:
        try:
            print("-", a["href"])
        except Exception:
            pass

    offers = []

    # 1) Linklerden yakalamaya çalış
    for a in all_links:
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        if "/Yasama/KanunTeklifi/" in href:
            full_url = href if href.startswith("http") else f"https://www.tbmm.gov.tr{href}"
            tbmm_id = full_url.rstrip("/").split("/")[-1]

            if not text:
                text = "TBMM Kanun Teklifi"

            offers.append({
                "tbmmId": tbmm_id,
                "title": text,
                "sourceUrl": full_url,
            })

    # 2) Ham HTML içinden regex ile ara
    html_matches = re.findall(
        r"https://www\.tbmm\.gov\.tr/Yasama/KanunTeklifi/([a-zA-Z0-9\-]+)",
        html,
    )

    print("Regex matches found:", len(html_matches))

    for tbmm_id in html_matches:
        full_url = f"https://www.tbmm.gov.tr/Yasama/KanunTeklifi/{tbmm_id}"
        offers.append({
            "tbmmId": tbmm_id,
            "title": "TBMM Kanun Teklifi",
            "sourceUrl": full_url,
        })

    unique = {}
    for item in offers:
        unique[item["tbmmId"]] = item

    result = list(unique.values())

    print(f"Found raw offers: {len(result)}")
    if result:
        print("Sample offers:")
        for item in result[:5]:
            print(f"- {item['tbmmId']} | {item['title']}")

    return result


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
