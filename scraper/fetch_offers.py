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

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AcikMeclisBot/1.0)"
}


def init_firestore():
    service_account_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not service_account_path:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON environment variable is missing.")

    if not firebase_admin._apps:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)

    return firestore.client()


def request_with_retry(url: str, timeout: int = 60, attempts: int = 3) -> str:
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"Requesting: {url} | attempt {attempt}")
            response = requests.get(
                url,
                timeout=timeout,
                headers=REQUEST_HEADERS,
            )
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            last_error = e
            print(f"Request failed: {e}")
            time.sleep(3)

    raise RuntimeError(f"Request failed for {url}. Last error: {last_error}")


def fetch_tbmm_list():
    last_error = None

    for url in TBMM_LIST_URLS:
        try:
            print(f"Trying list URL: {url}")
            html = request_with_retry(url, timeout=60, attempts=3)
            print(f"Success with list URL: {url}")
            return html
        except Exception as e:
            last_error = e
            print(f"List URL failed: {e}")

    raise RuntimeError(f"All list URLs failed. Last error: {last_error}")


def extract_kanunlar_sira_no(url: str):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("kanunlar_sira_no")
    if values and values[0].strip():
        return values[0].strip()
    return None


def normalize_url(href: str):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://www5.tbmm.gov.tr{href}"
    return f"https://www5.tbmm.gov.tr/{href}"


def parse_basic_offers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    offers = []

    all_links = soup.find_all("a", href=True)
    print("Total <a> tags found:", len(all_links))

    # Sadece gerçek teklif detay girişleri:
    # tasari_teklif_sd.onerge_bilgileri?kanunlar_sira_no=...
    for a in all_links:
        href = a["href"].strip()

        if "tasari_teklif_sd.onerge_bilgileri" not in href.lower():
            continue

        kanunlar_sira_no = extract_kanunlar_sira_no(href)
        if not kanunlar_sira_no:
            continue

        full_url = normalize_url(href)

        offers.append({
            "tbmmId": f"tbmm_{kanunlar_sira_no}",
            "kanunlarSiraNo": kanunlar_sira_no,
            "title": f"TBMM Kanun Teklifi {kanunlar_sira_no}",
            "sourceUrl": full_url,
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


def clean_text(value: str):
    return re.sub(r"\s+", " ", value).strip()


def extract_label_value(lines, label):
    """
    Satır bazlı kaba yakalama.
    Örn:
    Teklifin Başlığı
    ...
    """
    label_lower = label.lower()

    for i, line in enumerate(lines):
        current = clean_text(line)
        if current.lower() == label_lower or label_lower in current.lower():
            # Aynı satırda "Etiket: Değer" ise
            if ":" in current:
                left, right = current.split(":", 1)
                if label_lower in left.lower() and right.strip():
                    return clean_text(right)

            # Sonraki dolu satırı değer kabul et
            for j in range(i + 1, min(i + 6, len(lines))):
                candidate = clean_text(lines[j])
                if not candidate:
                    continue
                if candidate.lower() == label_lower:
                    continue
                return candidate

    return ""


def extract_label_value_regex(text, label):
    """
    Tam metin üzerinden daha serbest regex arama.
    """
    pattern = rf"{re.escape(label)}\s*:?\s*(.+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))
    return ""


def map_status(last_status_text: str):
    """
    Kullanıcıya gösterilecek status yapısına çevirir.
    """
    source = clean_text(last_status_text).lower()

    if not source:
        return "teklif_edildi", "Teklif Edildi"

    if "komisyonda" in source:
        return "komisyonda", "Komisyonda"

    if "komisyon raporu" in source or "raporu" in source:
        return "komisyon_raporu_hazir", "Komisyon Raporu Hazır"

    if "genel kurul gündeminde" in source or "gündemde" in source:
        return "genel_kurul_gundeminde", "Genel Kurul Gündeminde"

    if "genel kurulda görüşül" in source or "görüşülüyor" in source:
        return "genel_kurulda_gorusuluyor", "Genel Kurulda Görüşülüyor"

    if "oylama" in source:
        return "oylamasi_yapildi", "Oylaması Yapıldı"

    if "kanunlaştı" in source or "kabul edildi" in source or "kabul edilmiştir" in source:
        return "kabul_edildi", "Kabul Edildi / Kanunlaştı"

    if "yürürlüğe girdi" in source or "resmi gazete" in source:
        return "yururluge_girdi", "Yürürlüğe Girdi"

    if "reddedildi" in source or "kadük" in source or "düştü" in source:
        return "reddedildi", "Reddedildi / Düştü / Kadük"

    return "teklif_edildi", "Teklif Edildi"


def parse_offer_detail(detail_html: str, default_url: str):
    soup = BeautifulSoup(detail_html, "html.parser")
    full_text = soup.get_text("\n", strip=True)
    lines = [clean_text(x) for x in full_text.splitlines() if clean_text(x)]

    # Başlık için birkaç farklı aday deniyoruz
    title = (
        extract_label_value(lines, "Teklifin Başlığı")
        or extract_label_value_regex(full_text, "Teklifin Başlığı")
    )

    # Bazı sayfalarda title tag de işe yarayabilir
    if not title and soup.title:
        title_tag_text = clean_text(soup.title.get_text(" ", strip=True))
        if "TÜRKİYE BÜYÜK MİLLET MECLİSİ" not in title_tag_text.upper():
            title = title_tag_text

    esas_no = (
        extract_label_value(lines, "Esas Numarası")
        or extract_label_value_regex(full_text, "Esas Numarası")
    )

    submitted_at = (
        extract_label_value(lines, "Başkanlığa Geliş Tarihi")
        or extract_label_value_regex(full_text, "Başkanlığa Geliş Tarihi")
    )

    last_status = (
        extract_label_value(lines, "Son Durumu")
        or extract_label_value_regex(full_text, "Son Durumu")
    )

    # PDF / metin linki
    pdf_url = ""
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = clean_text(a.get_text(" ", strip=True)).lower()

        if href.lower().endswith(".pdf") or "metni" in text:
            pdf_url = normalize_url(href)
            break

    status, status_label = map_status(last_status)

    # Çok uzun içerik basmayalım; ilk sürümde özet yoksa son durumdan kısa açıklama üret
    summary = last_status if last_status else ""
    content = f"TBMM detay kaynağı: {default_url}"
    if pdf_url:
        content += f"\nMetin/PDF: {pdf_url}"

    return {
        "title": title,
        "esasNo": esas_no,
        "submittedAtText": submitted_at,
        "lastStatusText": last_status,
        "status": status,
        "statusLabel": status_label,
        "summary": summary,
        "content": content,
        "pdfUrl": pdf_url,
    }


def enrich_offer(offer: dict):
    try:
        detail_html = request_with_retry(offer["sourceUrl"], timeout=60, attempts=3)
        detail_data = parse_offer_detail(detail_html, offer["sourceUrl"])
        merged = {**offer, **detail_data}
        return merged
    except Exception as e:
        print(f"DETAIL ERROR for {offer['tbmmId']}: {e}")
        return offer


def upsert_laws(db, offers):
    now = datetime.now(timezone.utc)

    for offer in offers:
        enriched = enrich_offer(offer)

        doc_ref = db.collection("laws").document(enriched["tbmmId"])
        existing = doc_ref.get()

        payload = {
            "tbmmId": enriched["tbmmId"],
            "kanunlarSiraNo": enriched.get("kanunlarSiraNo", ""),
            "esasNo": enriched.get("esasNo", ""),
            "title": enriched.get("title", f"TBMM Kanun Teklifi {enriched.get('kanunlarSiraNo', '')}") or f"TBMM Kanun Teklifi {enriched.get('kanunlarSiraNo', '')}",
            "summary": enriched.get("summary", ""),
            "content": enriched.get("content", ""),
            "category": "Genel",
            "sourceUrl": enriched.get("sourceUrl", ""),
            "pdfUrl": enriched.get("pdfUrl", ""),
            "submittedAtText": enriched.get("submittedAtText", ""),
            "lastStatusText": enriched.get("lastStatusText", ""),
            "status": enriched.get("status", "teklif_edildi"),
            "statusLabel": enriched.get("statusLabel", "Teklif Edildi"),
            "publishedAt": now,
            "lastSyncedAt": now,
            "isActive": True,
            "createdBy": "tbmm_sync_bot",
        }

        if existing.exists:
            doc_ref.set(payload, merge=True)
            print(f"UPDATED: {enriched['tbmmId']} | {payload['title']}")
        else:
            doc_ref.set(payload)
            print(f"CREATED: {enriched['tbmmId']} | {payload['title']}")


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
