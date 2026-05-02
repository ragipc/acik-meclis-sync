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

DETAIL_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?tbmm\.gov\.tr/Yasama/KanunTeklifi/[a-zA-Z0-9-]+",
    re.IGNORECASE,
)

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
    "User-Agent": "Mozilla/5.0 (compatible; AcikMeclisBot/2.4; +https://github.com/)",
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
    text = clean_text(value)
    text = unicodedata.normalize("NFKC", text)
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
    url = url.replace("https://tbmm.gov.tr/", "https://www.tbmm.gov.tr/")
    return url.strip()


def extract_detail_urls_from_html(html: str) -> list[str]:
    urls = set()

    for match in DETAIL_URL_PATTERN.findall(html):
        urls.add(normalize_detail_url(match))

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

    urls.extend(SEED_DETAIL_URLS)
    urls.extend(discover_from_tbmm_search_page())

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

    if any(x in text for x in [
        "eğitim", "egitim", "öğrenim", "ogrenim", "okul", "üniversite",
        "universite", "yükseköğretim", "yuksekogretim", "öğrenci",
        "ogrenci", "öğretmen", "ogretmen",
    ]):
        return "Eğitim"

    if any(x in text for x in [
        "ücret", "ucret", "maaş", "maas", "çalışma", "calisma", "işçi",
        "isci", "işveren", "isveren", "sendika", "emek", "emekli",
        "sosyal yardım", "sosyal yardim",
    ]):
        return "Çalışma / Sosyal Politika"

    if any(x in text for x in [
        "vergi", "asgari ücret", "asgari ucret", "ekonomi", "bütçe", "butce",
        "aylık", "aylik", "destek", "ticaret", "piyasa", "finans",
    ]):
        return "Ekonomi"

    if any(x in text for x in [
        "sağlık", "saglik", "hastane", "ilaç", "ilac", "malullük",
        "malulluk", "sosyal güvenlik", "sosyal guvenlik", "genel sağlık",
        "genel saglik",
    ]):
        return "Sağlık"

    if any(x in text for x in [
        "ceza", "mahkeme", "hukuk", "adalet", "avukat", "suç", "suc",
        "yargı", "yargi",
    ]):
        return "Adalet"

    if any(x in text for x in [
        "tarım", "tarim", "orman", "hayvancılık", "hayvancilik", "çiftçi",
        "ciftci",
    ]):
        return "Tarım"

    if any(x in text for x in [
        "enerji", "maden", "elektrik", "doğalgaz", "dogalgaz",
    ]):
        return "Enerji"

    if any(x in text for x in [
        "ulaştırma", "ulastirma", "trafik", "araç", "arac", "skuter",
        "haberleşme", "haberlesme", "gsm",
    ]):
        return "Ulaşım / İletişim"

    if any(x in text for x in [
        "çevre", "cevre", "iklim", "imar", "şehir", "sehir", "belediye",
    ]):
        return "Çevre / Şehircilik"

    return "Genel"


def build_what_changes(summary: str, category: str) -> str:
    if not summary:
        return ""

    summary_key = normalize_turkish_for_search(summary)
    category_key = normalize_turkish_for_search(category)

    if "eğitim" in category_key or "egitim" in category_key:
        if "öğrenci affı" in summary_key or "ogrenci affi" in summary_key:
            return (
                "Mevcut durumda:\n"
                "Yükseköğretim kurumlarıyla ilişiği kesilen öğrenciler, mevcut kurallara göre eğitimlerine devam edemeyebiliyor.\n\n"
                "Teklif kabul edilirse:\n"
                "Bu öğrencilere yeniden kayıt yaptırma ve eğitimlerine devam etme imkânı tanınabilir."
            )

        return (
            "Mevcut durumda:\n"
            "Eğitim alanındaki mevcut kurallar, bazı öğrenciler veya eğitim kurumları için sınırlayıcı sonuçlar doğurabiliyor.\n\n"
            "Teklif kabul edilirse:\n"
            "Eğitim sistemiyle ilgili bazı haklar, süreçler veya kurumların işleyişi yeniden düzenlenebilir."
        )

    if "çalışma" in category_key or "calisma" in category_key or "sosyal politika" in category_key:
        if "eşit değerde işe eşit ücret" in summary_key or "esit degerde ise esit ucret" in summary_key:
            return (
                "Mevcut durumda:\n"
                "Eşit değerde iş yapan kişiler arasında ücret farkları konusunda özel bir denetim yapısı yeterince belirgin olmayabilir.\n\n"
                "Teklif kabul edilirse:\n"
                "Eşit değerde işe eşit ücret ilkesini denetleyecek özel bir kurum kurulması ve bu kurumun görevlerinin belirlenmesi amaçlanır."
            )

        return (
            "Mevcut durumda:\n"
            "Çalışma hayatı ve sosyal politika alanındaki bazı haklar veya uygulamalar mevcut kanunlarla sınırlı şekilde yürütülüyor.\n\n"
            "Teklif kabul edilirse:\n"
            "Çalışanlar, işverenler, ücret politikaları veya sosyal haklarla ilgili bazı süreçler yeniden düzenlenebilir."
        )

    if "ekonomi" in category_key:
        return (
            "Mevcut durumda:\n"
            "Ekonomiyle ilgili mevcut kurallar vergi, destek, bütçe veya işletme süreçlerini belirli sınırlar içinde düzenliyor.\n\n"
            "Teklif kabul edilirse:\n"
            "Vergiler, ekonomik destekler, kamu bütçesi veya ticari faaliyetlerle ilgili bazı kurallar değişebilir."
        )

    if "sağlık" in category_key or "saglik" in category_key:
        return (
            "Mevcut durumda:\n"
            "Sağlık hizmetleri ve sağlık kurumları mevcut mevzuata göre yürütülüyor.\n\n"
            "Teklif kabul edilirse:\n"
            "Hastalar, sağlık çalışanları, hastaneler veya sağlık hizmetlerine erişimle ilgili bazı kurallar değişebilir."
        )

    if "adalet" in category_key:
        return (
            "Mevcut durumda:\n"
            "Hukuk ve adalet alanındaki süreçler mevcut kanunlara göre yürütülüyor.\n\n"
            "Teklif kabul edilirse:\n"
            "Mahkeme süreçleri, hak arama yolları, cezai hükümler veya hukuki yükümlülüklerde değişiklik yapılabilir."
        )

    return (
        "Mevcut durumda:\n"
        "Bu konudaki süreçler mevcut kanun ve kurallara göre yürütülmektedir.\n\n"
        "Teklif kabul edilirse:\n"
        "İlgili alandaki mevcut kurallarda değişiklik yapılabilir. Kesin değişiklikleri anlamak için resmî teklif metni kontrol edilmelidir."
    )


def build_citizen_impact(summary: str, status_label: str, category: str) -> str:
    if not summary:
        return ""

    if status_label == "Komisyonda":
        base = (
            "Bu teklif şu anda komisyon aşamasındadır. "
            "Bu yüzden içerik komisyonda değişebilir, genişleyebilir veya daraltılabilir. "
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
            "Teklif kabul edilirse bazı öğrenciler için yeni haklar veya başvuru imkânları doğabilir. "
            "Ancak kesin sonuç için resmî metin ve TBMM süreci kontrol edilmelidir."
        )

    if "çalışma" in category_text or "calisma" in category_text or "sosyal politika" in category_text:
        return (
            base
            + "Çalışma hayatı ve sosyal politika alanındaki düzenlemeler çalışanları, işverenleri, ücret politikalarını veya sosyal hakları etkileyebilir. "
            "Teklif kabul edilirse iş hayatındaki hak ve yükümlülüklerde değişiklikler olabilir. "
            "Kesin ve bağlayıcı bilgi için resmî metin kontrol edilmelidir."
        )

    if "ekonomi" in category_text:
        return (
            base
            + "Ekonomi alanındaki düzenlemeler vatandaşların gelirleri, vergiler, işletmeler veya kamu bütçesi üzerinde etkiler doğurabilir. "
            "Bu nedenle teklifin kimleri kapsadığı ve hangi şartları getirdiği resmî metinden kontrol edilmelidir."
        )

    if "sağlık" in category_text or "saglik" in category_text:
        return (
            base
            + "Sağlık alanındaki düzenlemeler hastalar, sağlık çalışanları, hastaneler veya sağlık hizmetlerine erişim üzerinde etkiler doğurabilir. "
            "Uygulamadaki kesin sonuçlar için resmî metin incelenmelidir."
        )

    if "adalet" in category_text:
        return (
            base
            + "Adalet alanındaki düzenlemeler hak arama yollarını, mahkeme süreçlerini veya vatandaşların hukuki yükümlülüklerini etkileyebilir. "
            "Bu nedenle teklifin kabul edilip edilmediği ve son hâli özellikle kontrol edilmelidir."
        )

    return (
        base
        + "Teklifin vatandaş üzerindeki kesin etkisi için resmî metin ve TBMM süreci birlikte değerlendirilmelidir."
    )


def build_detailed_explanation(title: str, summary: str, what_changes: str, citizen_impact: str, status_label: str, category: str) -> str:
    if not summary:
        return ""

    return (
        f"Bu teklifin konusu: {title}\n\n"
        f"Kısa anlamı:\n{summary}\n\n"
        f"Süreç durumu:\nTeklif şu anda \"{status_label}\" aşamasındadır. Bu aşamada teklifin içeriği TBMM sürecinde değişebilir.\n\n"
        f"Değişiklik mantığı:\n{what_changes}\n\n"
        f"Vatandaş açısından değerlendirme:\n{citizen_impact}\n\n"
        "Not: Bu açıklama bilgilendirme amaçlıdır. Kesin ve bağlayıcı bilgi için TBMM’deki resmî teklif metni ve kaynak bağlantısı kontrol edilmelidir."
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

    donem_yasama = extract_field(full_text, "Dönemi ve Yasama Yılı", labels[2:])
    esas_no = extract_field(full_text, "Esas Numarası", labels[3:])
    submitted_at_text = extract_field(full_text, "Başkanlığa Geliş Tarihi", labels[4:])
    official_title = extract_field(full_text, "Teklifin Başlığı", labels[5:])
    plain_summary = extract_field(full_text, "Teklifin Özeti", labels[6:])
    last_status_text = extract_field(full_text, "Son Durumu", labels[7:])
    result_text = extract_field(full_text, "Teklifin Sonucu", labels[8:])

    pdf_url = ""
    for a in soup.find_all("a", href=True):
        text = clean_text(a.get_text(" ", strip=True)).lower()
        href = a["href"].strip()

        if "kanun teklifinin metni" in text or href.lower().endswith(".pdf"):
            pdf_url = urljoin(TBMM_BASE_URL, href)
            break

    status, status_label = map_status(last_status_text or result_text)

    detail_id = url.rstrip("/").split("/")[-1]
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", detail_id)
    tbmm_id = f"tbmm_new_{safe_id}"

    title = official_title or f"TBMM Kanun Teklifi {esas_no or safe_id}"
    category = infer_category(title, plain_summary)

    what_changes = build_what_changes(plain_summary, category)
    citizen_impact = build_citizen_impact(plain_summary, status_label, category)
    detailed_explanation = build_detailed_explanation(
        title,
        plain_summary,
        what_changes,
        citizen_impact,
        status_label,
        category,
    )

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
        "detailedExplanation": detailed_explanation,
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


def create_status_change_notifications(db, law_id: str, law_title: str, old_status: str, new_status: str, now):
    """
    Bir yasa teklifinin statusLabel değeri değişirse:
    lawFollows koleksiyonunda bu yasayı takip eden kullanıcıları bulur
    ve her kullanıcı için notifications kaydı oluşturur.
    """
    if not old_status or not new_status:
        return

    if old_status == new_status:
        return

    try:
        follows = (
            db.collection("lawFollows")
            .where("lawId", "==", law_id)
            .get()
        )

        if not follows:
            print(f"NOTIFICATION SKIP: no followers for {law_id}")
            return

        batch = db.batch()
        created_count = 0

        for follow_doc in follows:
            follow_data = follow_doc.to_dict() or {}
            user_id = follow_data.get("userId", "")

            if not user_id:
                continue

            notification_ref = db.collection("notifications").document()

            batch.set(notification_ref, {
                "userId": user_id,
                "lawId": law_id,
                "lawTitle": law_title,
                "title": "Takip ettiğin teklifte gelişme var",
                "message": f"“{law_title}” teklifi “{old_status}” aşamasından “{new_status}” aşamasına geçti.",
                "type": "status_change",
                "oldStatusLabel": old_status,
                "newStatusLabel": new_status,
                "isRead": False,
                "createdAt": now,
                "readAt": None,
            })

            created_count += 1

        if created_count > 0:
            batch.commit()
            print(f"NOTIFICATIONS CREATED: {created_count} | {law_id} | {old_status} -> {new_status}")
        else:
            print(f"NOTIFICATION SKIP: followers found but no valid userId for {law_id}")

    except Exception as e:
        print(f"CREATE STATUS CHANGE NOTIFICATIONS ERROR for {law_id}: {e}")


def upsert_laws(db, offers: list[dict]):
    now = datetime.now(timezone.utc)

    for offer in offers:
        doc_ref = db.collection("laws").document(offer["tbmmId"])
        existing = doc_ref.get()

        old_status_label = ""
        if existing.exists:
            existing_data = existing.to_dict() or {}
            old_status_label = existing_data.get("statusLabel", "") or ""

        new_status_label = offer.get("statusLabel", "Teklif Edildi")

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
            "detailedExplanation": offer.get("detailedExplanation", ""),

            "category": offer.get("category", "Genel"),
            "sourceUrl": offer.get("sourceUrl", ""),
            "pdfUrl": offer.get("pdfUrl", ""),

            "status": offer.get("status", "teklif_edildi"),
            "statusLabel": new_status_label,
            "lastStatusText": offer.get("lastStatusText", ""),
            "resultText": offer.get("resultText", ""),

            "esasNo": offer.get("esasNo", ""),
            "donemYasama": offer.get("donemYasama", ""),
            "submittedAtText": offer.get("submittedAtText", ""),

            "lastSyncedAt": now,
            "isActive": True,
            "createdBy": "tbmm_sync_bot",
        }

        if not existing.exists:
            payload["publishedAt"] = now

        doc_ref.set(payload, merge=True)

        action = "UPDATED" if existing.exists else "CREATED"
        print(f"{action}: {offer['tbmmId']} | {payload['title']} | {payload['statusLabel']}")

        # İlk kayıt oluşturulurken bildirim üretmeyelim.
        # Sadece daha önce var olan teklifin statüsü değişirse bildirim üretelim.
        if existing.exists and old_status_label and old_status_label != new_status_label:
            create_status_change_notifications(
                db=db,
                law_id=offer["tbmmId"],
                law_title=payload["title"],
                old_status=old_status_label,
                new_status=new_status_label,
                now=now,
            )


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
