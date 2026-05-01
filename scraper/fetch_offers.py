import os
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


# Geçici olarak doğrudan sonuç sayfasını hedefliyoruz
TBMM_LIST_URL = "https://www.tbmm.gov.tr/develop/owa/tasari_teklif_sd.sorgu_sonuc?bulunan_kayit=3278&icerik_arama=&kullanici_id=18731517&metin_arama=&sonuc_sira=340&taksim_no=0"


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

    soup = BeautifulSoup(html, "html.parser")
    offers = []

    all_links = soup.find_all("a", href=True)
    print("Total <a> tags found:", len(all_links))

    for a in all_links:
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)

        href_lower = href.lower()
        text_lower = text.lower()

        # Teklif / tasarı / metin / özet / detay linklerini daha esnek yakala
        if (
            "tasari_teklif" in href_lower
            or "kanun_teklifi" in href_lower
            or "kanunteklifi" in href_lower
            or "metni" in text_lower
            or "özet" in text_lower
            or "ozet" in text_lower
            or "detay" in text_lower
        ):
            full_url = href if href.startswith("http") else f"https://www.tbmm.gov.tr{href}"

            # URL'den güvenli document id üret
            tbmm_id = re.sub(r"[^a-zA-Z0-9]+", "_", full_url).strip("_")

            title = text if text else "TBMM Kanun Teklifi"

            offers.append({
                "tbmmId": tbmm_id,
                "title": title,
                "sourceUrl": full_url,
            })

    unique = {}
    for item in offers:
        unique[item["tbmmId"]] = item

    result = list(unique.values())

    print(f"Found raw offers: {len(result)}")
    if result:
        print("Sample offers:")
        for item in result[:10]:
            print(f"- {item['tbmmId']} | {item['title']} | {item['sourceUrl']}")

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
