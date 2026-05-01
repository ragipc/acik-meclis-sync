import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from io import BytesIO

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
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


def fetch_binary_with_retry(url: str, timeout: int = 90, attempts: int = 3) -> bytes:
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"Downloading binary: {url} | attempt {attempt}")
            response = requests.get(
                url,
                timeout=timeout,
                headers=REQUEST_HEADERS,
            )
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            last_error = e
            print(f"Binary request failed: {e}")
            time.sleep(3)

    raise RuntimeError(f"Binary download failed for {url}. Last error: {last_error}")


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


def normalize_url(href: str):
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://www5.tbmm.gov.tr{href}"
    return f"https://www5.tbmm.gov.tr/{href}"


def clean_text(value: str):
    return re.sub(r"\s+", " ", value).strip()


def extract_kanunlar_sira_no(url: str):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("kanunlar_sira_no")
    if values and values[0].strip():
        return values[0].strip()
    return None


def map_status(last_status_text: str):
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


def try_extract_date(text: str):
    match = re.search(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b", text)
    return match.group(1) if match else ""


def extract_status_from_text(block_text: str):
    patterns = [
        r"Son Durumu\s*:?\s*(.+?)(?:Esas Numarası|Başkanlığa Geliş Tarihi|$)",
        r"Son Durumu\s+(.+?)(?:Esas Numarası|Başkanlığa Geliş Tarihi|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, block_text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return ""


def extract_esas_no_from_text(block_text: str):
    patterns = [
        r"Esas Numarası\s*:?\s*([0-9/ -]+)",
        r"Esas No\s*:?\s*([0-9/ -]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, block_text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1))
    return ""


def pick_title_from_lines(lines):
    bad_fragments = [
        "diğer bilgiler",
        "metni",
        "yazıcı dostu",
        "sonraki",
        "önceki",
        "esas numarası",
        "başkanlığa geliş tarihi",
        "son durumu",
    ]

    candidates = []
    for line in lines:
        t = clean_text(line)
        if not t:
            continue

        lower = t.lower()

        if any(bad in lower for bad in bad_fragments):
            continue

        if len(t) < 10:
            continue

        if re.fullmatch(r"[0-9/ .-]+", t):
            continue

        candidates.append(t)

    if not candidates:
        return ""

    candidates.sort(key=len, reverse=True)
    return candidates[0]


def extract_pdf_from_container(container):
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        text = clean_text(a.get_text(" ", strip=True)).lower()

        if href.lower().endswith(".pdf") or "metni" in text:
            return normalize_url(href)
    return ""


def parse_basic_offers(html: str):
    soup = BeautifulSoup(html, "html.parser")
    offers = []

    all_links = soup.find_all("a", href=True)
    print("Total <a> tags found:", len(all_links))

    for a in all_links:
        href = a["href"].strip()

        if "tasari_teklif_sd.onerge_bilgileri" not in href.lower():
            continue

        kanunlar_sira_no = extract_kanunlar_sira_no(href)
        if not kanunlar_sira_no:
            continue

        detail_url = normalize_url(href)

        container = a.find_parent("tr")
        if container is None:
            container = a.find_parent("table")
        if container is None:
            container = a.parent

        block_text = clean_text(container.get_text("\n", strip=True)) if container else ""
        lines = [clean_text(x) for x in block_text.split("\n") if clean_text(x)]

        title = pick_title_from_lines(lines)
        last_status_text = extract_status_from_text(block_text)
        esas_no = extract_esas_no_from_text(block_text)
        submitted_at_text = try_extract_date(block_text)
        pdf_url = extract_pdf_from_container(container) if container else ""

        status, status_label = map_status(last_status_text)

        if not title:
            title = f"TBMM Kanun Teklifi {kanunlar_sira_no}"

        summary = last_status_text if last_status_text else ""

        offers.append({
            "tbmmId": f"tbmm_{kanunlar_sira_no}",
            "kanunlarSiraNo": kanunlar_sira_no,
            "esasNo": esas_no,
            "title": title,
            "summary": summary,
            "sourceUrl": detail_url,
            "pdfUrl": pdf_url,
            "submittedAtText": submitted_at_text,
            "lastStatusText": last_status_text,
            "status": status,
            "statusLabel": status_label,
        })

    unique = {}
    for item in offers:
        unique[item["tbmmId"]] = item

    result = list(unique.values())

    print(f"Found filtered offers: {len(result)}")
    if result:
        print("Sample offers:")
        for item in result[:10]:
            print(
                f"- {item['tbmmId']} | {item['title']} | {item['statusLabel']} | {item['sourceUrl']}"
            )

    return result


def extract_text_from_pdf(pdf_url: str):
    if not pdf_url:
        return ""

    try:
        pdf_bytes = fetch_binary_with_retry(pdf_url, timeout=90, attempts=2)
        reader = PdfReader(BytesIO(pdf_bytes))
        text_parts = []

        # İlk 2 sayfa çoğu zaman yeterli
        page_count = min(2, len(reader.pages))
        for i in range(page_count):
            try:
                page_text = reader.pages[i].extract_text() or ""
                if page_text.strip():
                    text_parts.append(page_text)
            except Exception as e:
                print(f"PDF page extract error ({pdf_url}, page {i}): {e}")

        text = "\n".join(text_parts)
        return clean_text(text)
    except Exception as e:
        print(f"PDF extract failed for {pdf_url}: {e}")
        return ""


def parse_pdf_fields(pdf_text: str):
    if not pdf_text:
        return {
            "pdfTitle": "",
            "pdfSubmittedAtText": "",
            "pdfEsasNo": "",
        }

    lines = [clean_text(x) for x in pdf_text.split("\n") if clean_text(x)]

    # Başlık için ilk güçlü satırlardan birini seç
    pdf_title = ""
    for line in lines[:20]:
        lower = line.lower()

        if len(line) < 15:
            continue
        if "türkiye büyük millet meclisi" in lower:
            continue
        if "kanun teklifi" in lower and len(line) < 25:
            continue
        if "esas no" in lower or "başkanlığa geliş tarihi" in lower:
            continue

        pdf_title = line
        break

    pdf_submitted = ""
    for line in lines[:40]:
        if "başkanlığa geliş tarihi" in line.lower():
            maybe_date = try_extract_date(line)
            if maybe_date:
                pdf_submitted = maybe_date
                break

    pdf_esas_no = ""
    for line in lines[:40]:
        if "esas no" in line.lower() or "esas numarası" in line.lower():
            m = re.search(r"([0-9/ -]+)", line)
            if m:
                pdf_esas_no = clean_text(m.group(1))
                break

    return {
        "pdfTitle": pdf_title,
        "pdfSubmittedAtText": pdf_submitted,
        "pdfEsasNo": pdf_esas_no,
    }


def enrich_offer_with_detail_page(offer: dict):
    try:
        detail_html = request_with_retry(offer["sourceUrl"], timeout=60, attempts=2)
        soup = BeautifulSoup(detail_html, "html.parser")

        if not offer.get("pdfUrl"):
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                text = clean_text(a.get_text(" ", strip=True)).lower()
                if href.lower().endswith(".pdf") or "metni" in text:
                    offer["pdfUrl"] = normalize_url(href)
                    break

        return offer
    except Exception as e:
        print(f"DETAIL PAGE WARNING for {offer['tbmmId']}: {e}")
        return offer


def apply_pdf_enrichment(offer: dict):
    pdf_url = offer.get("pdfUrl", "")
    if not pdf_url:
        return offer

    pdf_text = extract_text_from_pdf(pdf_url)
    pdf_fields = parse_pdf_fields(pdf_text)

    # Başlık generic ise PDF başlığını tercih et
    current_title = offer.get("title", "")
    if current_title.startswith("TBMM Kanun Teklifi") and pdf_fields["pdfTitle"]:
        offer["title"] = pdf_fields["pdfTitle"]

    if not offer.get("submittedAtText") and pdf_fields["pdfSubmittedAtText"]:
        offer["submittedAtText"] = pdf_fields["pdfSubmittedAtText"]

    if not offer.get("esasNo") and pdf_fields["pdfEsasNo"]:
        offer["esasNo"] = pdf_fields["pdfEsasNo"]

    # Özet boşsa PDF ilk 400 karakterden kısa özet üret
    if not offer.get("summary") and pdf_text:
        offer["summary"] = pdf_text[:400]

    return offer


def upsert_laws(db, offers):
    now = datetime.now(timezone.utc)

    for offer in offers:
        enriched = enrich_offer_with_detail_page(offer)
        enriched = apply_pdf_enrichment(enriched)

        doc_ref = db.collection("laws").document(enriched["tbmmId"])
        existing = doc_ref.get()

        payload = {
            "tbmmId": enriched["tbmmId"],
            "kanunlarSiraNo": enriched.get("kanunlarSiraNo", ""),
            "esasNo": enriched.get("esasNo", ""),
            "title": enriched.get("title", ""),
            "summary": enriched.get("summary", ""),
            "content": "",
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
            print(f"UPDATED: {enriched['tbmmId']} | {payload['title']} | {payload['statusLabel']}")
        else:
            doc_ref.set(payload)
            print(f"CREATED: {enriched['tbmmId']} | {payload['title']} | {payload['statusLabel']}")


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
