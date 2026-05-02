import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, unquote

import requests
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, firestore


TBMM_BASE_URL = "https://www.tbmm.gov.tr"
TBMM_NEW_SEARCH_PAGE = "https://www.tbmm.gov.tr/yasama/kanun-teklifleri"

# Yeni TBMM detay sayfaları şu yapıdadır:
# https://www.tbmm.gov.tr/Yasama/KanunTeklifi/<uuid>
DETAIL_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?tbmm\.gov\.tr/Yasama/KanunTeklifi/[a-zA-Z0-9-]+",
    re.IGNORECASE,
)

# TBMM yeni arama sayfası bazı durumlarda detay linklerini düz HTML olarak vermeyebiliyor.
# Bu yüzden güvenilir başlangıç linkleri kullanıyoruz.
# Script yine TBMM detay sayfasını kaynak kabul eder.
SEED_DETAIL_URLS = [
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/23ff85ec-c046-4811-ba19-019ae46eceeb",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/12d348f9-77ee-4f09-8b78-019a5e27521f",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/3a5fb0fa-a80a-4569-8801-019ac5230ca6",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/8bf5742f-9144-41b3-9ec6-019c42d53ce0",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/00558578-4e81-45e5-9d5f-019d6cefe046",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/76c336f0-f27d-4ca0-a43e-019d9bbcaed2",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/4535a07a-284a-4c58-95ba-019cd1574b73",
    "https://www.tbmm.gov.tr/Yasama/KanunTeklifi/99b8a756-ce26-4a72-8ec8-019c09ff8738",
]

DISCOVERY_QUERIES = [
    'site:tbmm.gov.tr/Yasama/KanunTeklifi/ "KANUN TEKLİFİ BİLGİLERİ" "28 / 4"',
    'site:tbmm.gov.tr/Yasama/KanunTeklifi/ "Teklifin Özeti" "Başkanlığa Geliş Tarihi"',
    'site:tbmm.gov.tr/Yasama/KanunTeklifi/ "Son Durumu" "KOMİSYONDA"',
    'site:tbmm.gov.tr/Yasama/KanunTeklifi/ "Son Durumu" "GÜNDEMDE"',
    'site:tbmm.gov.tr/Yasama/KanunTeklifi/ "Son Durumu" "KANUNLAŞTI"',
]

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AcikMeclisBot/2.1; +https://github.com/)",
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
}


def init_firestore():
    service_account_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")

    if not service_account_path:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON environment variable is missing.")

    if not firebase_admin._apps:
        cred = credentials.Certificate(service_account_path)
        firebase_admin.initialize_app(cred)

    return firestore.client()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_turkish_for_search(value: str) -> str:
    """
    TBMM sayfasından bazen 'Komi̇syonda' gibi birleşik Unicode gelebiliyor.
    Bu fonksiyon arama/eşleştirme için metni güvenli hale getirir.
    """
    text = clean_text(value)
    text = unicodedata.normalize("NFKC", text)

    # Türkçe büyük İ küçük harfe çevrilince bazen i + combining dot oluyor.
    text = text.replace("i̇", "i")
    text = text.replace("İ", "i")
    text = text.replace("I", "ı")

    return text.lower()


def request_text(url: str, timeout: int = 60, attempts: int = 3) -> str:
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"Requesting: {url} | attempt {attempt}")
            response = requests.get(url, timeout=timeout, headers=REQUEST_HEADERS)
            response.raise_for_status()

            # TBMM sayfalarında Türkçe karakterler için requests bazen tahmin hatası yapabiliyor.
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"

            return response.text
        except requests.RequestException as e:
            last_error = e
            print(f"Request failed: {e}")
            time.sleep(3)

    raise RuntimeError(f"Request failed for {url}. Last error: {last_error}")


def normalize_detail_url(url: str) -> str:
    url = unquote(url)
    url = url.split("&")[0]
    url = url.split("?")[0]
    url = url.replace("http://", "https://")

    # tbmm.gov.tr -> www.tbmm.gov.tr standardı
    url = url.replace("https://tbmm.gov.tr/", "https://www.tbmm.gov.tr/")

    return url.strip()


def extract_detail_urls_from_html(html: str) -> list[str]:
    urls = set()

    # 1) Direkt regex ile yakala
    for match in DETAIL_URL_PATTERN.findall(html):
        urls.add(normalize_detail_url(match))

    # 2) Anchor href üzerinden yakala
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        absolute = urljoin(TBMM_BASE_URL, href)

        if "/Yasama/KanunTeklifi/" in absolute:
            urls.add(normalize_detail_url(absolute))

    return sorted(urls)


def discover_from_tbmm_search_page() -> list[str]:
    try:
        html = request_text(TBMM_NEW_SEARCH_PAGE, attempts=2)
        urls = extract_detail_urls_from_html(html)
        print(f"TBMM search page discovered detail URLs: {len(urls)}")
        return urls
    except Exception as e:
        print(f"TBMM search page discovery warning: {e}")
        return []


def discover_from_bing(query: str) -> list[str]:
    """
    TBMM'nin yeni sorgu formu her zaman düz HTML sonuç vermediği için
    kamuya açık arama sonucu sadece URL keşfi için kullanılır.
    Veri yine TBMM detay sayfasından çekilir.
    """
    try:
        search_url = "https://www.bing.com/search?q=" + requests.utils.quote(query)
        html = request_text(search_url, timeout=60, attempts=2)
        urls = extract_detail_urls_from_html(html)
        print(f"Bing discovery for query [{query}] -> {len(urls)} URLs")
        return urls
    except Exception as e:
        print(f"Bing discovery warning for query [{query}]: {e}")
        return []


def discover_new_tbmm_detail_urls(max_urls: int = 40) -> list[str]:
    urls = []

    # 1) Güvenilir başlangıç linkleri
    urls.extend(SEED_DETAIL_URLS)

    # 2) TBMM sorgu sayfasından yakalanabilirse ekle
    urls.extend(discover_from_tbmm_search_page())

    # 3) Arama motoru destek olarak kalsın; çalışmazsa sorun değil
    for query in DISCOVERY_QUERIES:
        urls.extend(discover_from_bing(query))
        time.sleep(1)

    unique = []
    seen = set()

    for url in urls:
        normalized = normalize_detail_url(url)

        if normalized in seen:
            continue

        if "/Yasama/KanunTeklifi/" not in normalized:
            continue

        seen.add(normalized)
        unique.append(normalized)

    print(f"Total unique new TBMM detail URLs discovered: {len(unique)}")

    return unique[:max_urls]


def extract_field(full_text: str, label: str, next_labels: list[str]) -> str:
    """
    TBMM detay sayfasında metin şu şekilde geliyor:
    Teklifin Başlığı ...
    Teklifin Özeti ...
    Son Durumu ...
    Bu fonksiyon label ile sonraki label arasını alır.
    """
    escaped_label = re.escape(label)

    if next_labels:
        next_part = "|".join(re.escape(x) for x in next_labels)
        pattern = rf"{escaped_label}\s+(.*?)(?=\s+(?:{next_part})\s+|$)"
    else:
        pattern = rf"{escaped_label}\s+(.*)$"

    match = re.search(pattern, full_text, flags=re.IGNORECASE | re.DOTALL)

    if not match:
        return ""

    return clean_text(match.group(1))


def map_status(status_text: str):
    source = normalize_turkish_for_search(status_text)

    if not source:
        return "teklif_edildi", "Teklif Edildi"

    if "kanunlaştı" in source or "kanunlasti" in source:
        return "kabul_edildi", "Kabul Edildi / Kanunlaştı"

    if "komisyonda" in source:
        return "komisyonda", "Komisyonda"

    if "gündemde" in source or "gundemde" in source:
        return "genel_kurul_gundeminde", "Genel Kurul Gündeminde"

    if "görüşülüyor" in source or "gorusuluyor" in source:
        return "genel_kurulda_gorusuluyor", "Genel Kurulda Görüşülüyor"

    if "oylama" in source:
        return "oylamasi_yapildi", "Oylaması Yapıldı"

    if "işlemde" in source or "islemde" in source:
        return "teklif_edildi", "Teklif Edildi"

    if "geri alındı" in source or "geri alindi" in source:
        return "reddedildi", "Geri Alındı"

    if "hükümsüz" in source or "hukumsuz" in source:
        return "reddedildi", "Hükümsüz"

    if (
        "reddedildi" in source
        or "kadük" in source
        or "kaduk" in source
        or "düştü" in source
        or "dustu" in source
    ):
        return "reddedildi", "Reddedildi / Düştü / Kadük"

    return "teklif_edildi", "Teklif Edildi"


def infer_category(title: str, summary: str) -> str:
    text = normalize_turkish_for_search(f"{title} {summary}")

    if any(x in text for x in ["eğitim", "egitim", "öğrenim", "ogrenim", "okul", "üniversite", "universite", "yükseköğretim", "yuksekogretim", "öğrenci", "ogrenci", "öğretmen", "ogretmen"]):
        return "Eğitim"

    if any(x in text for x in ["vergi", "asgari ücret", "asgari ucret", "ekonomi", "bütçe", "butce", "emekli", "aylık", "aylik", "işveren", "isveren", "destek", "ticaret"]):
        return "Ekonomi"

    if any(x in text for x in ["sağlık", "saglik", "hastane", "ilaç", "ilac", "malullük", "malulluk", "sosyal güvenlik", "sosyal guvenlik", "genel sağlık", "genel saglik"]):
        return "Sağlık"

    if any(x in text for x in ["ceza", "mahkeme", "hukuk", "adalet", "avukat", "suç", "suc", "yargı", "yargi"]):
        return "Adalet"

    if any(x in text for x in ["tarım", "tarim", "orman", "hayvancılık", "hayvancilik", "çiftçi", "ciftci"]):
        return "Tarım"

    if any(x in text for x in ["enerji", "maden", "elektrik", "doğalgaz", "dogalgaz"]):
        return "Enerji"

    if any(x in text for x in ["ulaştırma", "ulastirma", "trafik", "araç", "arac", "skuter", "haberleşme", "haberlesme", "gsm"]):
        return "Ulaşım / İletişim"

    if any(x in text for x in ["çevre", "cevre", "iklim", "imar", "şehir", "sehir", "belediye"]):
        return "Çevre / Şehircilik"

    return "Genel"


def build_citizen_impact(summary: str, status_label: str, category: str) -> str:
    if not summary:
        return ""

    if status_label == "Komisyonda":
        base = (
            "Bu teklif şu anda komisyon aşamasındadır. "
            "İçeriği komisyonda değişebilir, genişleyebilir veya daraltılabilir. "
        )
    elif status_label == "Genel Kurul Gündeminde":
        base = (
            "Bu teklif Genel Kurul gündemine gelmiş görünmektedir. "
            "Bu aşama, teklifin Meclis genelinde görüşülmeye daha yakın olduğunu gösterir. "
        )
    elif status_label == "Kabul Edildi / Kanunlaştı":
        base = (
            "Bu teklif kabul edilmiş veya kanunlaşmış görünmektedir. "
            "Bu nedenle ilgili vatandaşlar açısından uygulanabilir sonuçlar doğurabilir. "
        )
    elif status_label == "Yürürlüğe Girdi":
        base = (
            "Bu düzenleme yürürlüğe girmiş görünmektedir. "
            "Bu nedenle vatandaşlar açısından doğrudan uygulanabilir sonuçlar doğurabilir. "
        )
    else:
        base = (
            f"Bu teklif {status_label.lower()} aşamasındadır. "
            "Vatandaşı nasıl etkileyeceği, teklifin TBMM sürecinde değişip değişmemesine "
            "ve kabul edilip edilmemesine bağlıdır. "
        )

    category_text = normalize_turkish_for_search(category)

    if "eğitim" in category_text or "egitim" in category_text:
        return (
            base
            + "Eğitim alanındaki düzenlemeler öğrencileri, mezunları, velileri veya yükseköğretim kurumlarını etkileyebilir. "
            "Kesin ve bağlayıcı bilgi için resmî metin kontrol edilmelidir."
        )

    if "ekonomi" in category_text:
        return (
            base
            + "Ekonomi alanındaki düzenlemeler vatandaşların gelirleri, vergiler, işletmeler veya kamu bütçesi üzerinde etkiler doğurabilir. "
            "Kesin ve bağlayıcı bilgi için resmî metin kontrol edilmelidir."
        )

    if "sağlık" in category_text or "saglik" in category_text:
        return (
            base
            + "Sağlık alanındaki düzenlemeler hastalar, sağlık çalışanları, hastaneler veya sağlık hizmetlerine erişim üzerinde etkiler doğurabilir. "
            "Kesin ve bağlayıcı bilgi için resmî metin kontrol edilmelidir."
        )

    if "adalet" in category_text:
        return (
            base
            + "Adalet alanındaki düzenlemeler hak arama yollarını, mahkeme süreçlerini veya vatandaşların hukuki yükümlülüklerini etkileyebilir. "
            "Kesin ve bağlayıcı bilgi için resmî metin kontrol edilmelidir."
        )

    return (
        base
        + "Teklifin vatandaş üzerindeki kesin etkisi için resmî metin ve TBMM süreci birlikte değerlendirilmelidir."
    )


def parse_new_tbmm_detail_page(url: str) -> dict | None:
    html = request_text(url, attempts=3)
    soup = BeautifulSoup(html, "html.parser")

    full_text = clean_text(soup.get_text(" ", strip=True))

    if "KANUN TEKLİFİ BİLGİLERİ" not in full_text and "Teklifin Başlığı" not in full_text:
        print(f"Skipping non-detail page: {url}")
        return None

    labels = [
        "Kanun Teklifinin Metni",
        "Dönemi ve Yasama Yılı",
        "Esas Numarası",
        "Başkanlığa Geliş Tarihi",
        "Teklifin Başlığı",
        "Teklifin Özeti",
        "Son Durumu",
        "Teklifin Sonucu",
        "KANUN TEKLİFİ KOMİSYON BİLGİLERİ",
        "KANUN TEKLİFİ İMZA SAHİPLERİ",
    ]

    donem_yasama = extract_field(
        full_text,
        "Dönemi ve Yasama Yılı",
        labels[2:],
    )

    esas_no = extract_field(
        full_text,
        "Esas Numarası",
        labels[3:],
    )

    submitted_at_text = extract_field(
        full_text,
        "Başkanlığa Geliş Tarihi",
        labels[4:],
    )

    official_title = extract_field(
        full_text,
        "Teklifin Başlığı",
        labels[5:],
    )

    plain_summary = extract_field(
        full_text,
        "Teklifin Özeti",
        labels[6:],
    )

    last_status_text = extract_field(
        full_text,
        "Son Durumu",
        labels[7:],
    )

    result_text = extract_field(
        full_text,
        "Teklifin Sonucu",
        labels[8:],
    )

    pdf_url = ""
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True)).lower()
        href = a["href"].strip()

        if "kanun teklifinin metni" in text or href.lower().endswith(".pdf"):
            pdf_url = urljoin(TBMM_BASE_URL, href)
            break

    status, status_label = map_status(last_status_text or result_text)

    # UUID detay URL'den alınır.
    detail_id = url.rstrip("/").split("/")[-1]
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", detail_id)
    tbmm_id = f"tbmm_new_{safe_id}"

    title = official_title or f"TBMM Kanun Teklifi {esas_no or safe_id}"

    category = infer_category(title, plain_summary)

    what_changes = plain_summary
    citizen_impact = build_citizen_impact(plain_summary, status_label, category)

    return {
        "tbmmId": tbmm_id,
        "sourceSystem": "tbmm_new",
        "sourceUrl": normalize_detail_url(url),
        "pdfUrl": pdf_url,
        "title": title,
        "officialTitle": official_title or title,
        "summary": plain_summary,
        "plainSummary": plain_summary,
        "content": "",
        "whatChanges": what_changes,
        "citizenImpact": citizen_impact,
        "category": category,
        "status": status,
        "statusLabel": status_label,
        "lastStatusText": last_status_text,
        "resultText": result_text,
        "esasNo": esas_no,
        "donemYasama": donem_yasama,
        "submittedAtText": submitted_at_text,
        "isActive": True,
        "createdBy": "tbmm_sync_bot",
    }


def fetch_new_offers() -> list[dict]:
    urls = discover_new_tbmm_detail_urls(max_urls=40)

    offers = []

    for url in urls:
        try:
            offer = parse_new_tbmm_detail_page(url)

            if offer:
                offers.append(offer)
                print(f"PARSED: {offer['tbmmId']} | {offer['title']} | {offer['statusLabel']}")
        except Exception as e:
            print(f"DETAIL PARSE WARNING for {url}: {e}")

        time.sleep(1)

    unique = {}

    for offer in offers:
        unique[offer["tbmmId"]] = offer

    result = list(unique.values())

    print(f"Total parsed new offers: {len(result)}")

    return result


def upsert_laws(db, offers: list[dict]):
    now = datetime.now(timezone.utc)

    for offer in offers:
        doc_ref = db.collection("laws").document(offer["tbmmId"])
        existing = doc_ref.get()

        payload = {
            "tbmmId": offer.get("tbmmId", ""),
            "sourceSystem": offer.get("sourceSystem", "tbmm_new"),

            "title": offer.get("title", ""),
            "officialTitle": offer.get("officialTitle", ""),
            "summary": offer.get("summary", ""),
            "plainSummary": offer.get("plainSummary", ""),
            "content": offer.get("content", ""),
            "whatChanges": offer.get("whatChanges", ""),
            "citizenImpact": offer.get("citizenImpact", ""),

            "category": offer.get("category", "Genel"),
            "sourceUrl": offer.get("sourceUrl", ""),
            "pdfUrl": offer.get("pdfUrl", ""),

            "status": offer.get("status", "teklif_edildi"),
            "statusLabel": offer.get("statusLabel", "Teklif Edildi"),
            "lastStatusText": offer.get("lastStatusText", ""),
            "resultText": offer.get("resultText", ""),

            "esasNo": offer.get("esasNo", ""),
            "donemYasama": offer.get("donemYasama", ""),
            "submittedAtText": offer.get("submittedAtText", ""),

            "lastSyncedAt": now,
            "isActive": True,
            "createdBy": "tbmm_sync_bot",
        }

        # publishedAt ilk oluşturulduğu zamanı temsil etsin.
        # Var olan kayıtta her sync'te değişmesin.
        if not existing.exists:
            payload["publishedAt"] = now

        doc_ref.set(payload, merge=True)

        action = "UPDATED" if existing.exists else "CREATED"
        print(f"{action}: {offer['tbmmId']} | {payload['title']} | {payload['statusLabel']}")


def main():
    db = init_firestore()

    offers = fetch_new_offers()

    if not offers:
        print("No new TBMM offers found. Nothing to sync.")
        return

    upsert_laws(db, offers)

    print(f"Done. Total synced: {len(offers)}")


if __name__ == "__main__":
    main()
