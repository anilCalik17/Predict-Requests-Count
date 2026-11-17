import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urljoin

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Istanbul")
MS_DATE = r"/Date\((-?\d+)(?:[+-]\d+)?\)/"  # OData V2: /Date(1700000000000)/


# --------------------------------------------------------------------------- HTTP
def build_session():
    s = requests.Session()
    token = os.getenv("ODATA_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    else:
        user, pwd = os.getenv("ODATA_USER"), os.getenv("ODATA_PASS")
        if not (user and pwd):
            sys.exit("ODATA_USER/ODATA_PASS ya da ODATA_TOKEN tanımlı değil.")
        s.auth = (user, pwd)
    s.headers["Accept"] = "application/json"
    retry = Retry(total=5, backoff_factor=2, allowed_methods=["GET"],
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def build_query(params):
    # boşluk -> %20 (bazı OData sunucuları '+' kabul etmez), $ ve OData karakterleri korunur
    return "&".join(f"{k}={quote(str(v), safe=chr(36) + chr(39) + ',()-:/')}"
                    for k, v in params.items())


def parse_page(j):
    """(satırlar, sonraki_sayfa_linki) döndürür. V4 ve V2 biçimlerini destekler."""
    if "value" in j:                                    # V4
        return j["value"], j.get("@odata.nextLink") or j.get("odata.nextLink")
    d = j.get("d", {})                                  # V2
    if isinstance(d, list):
        return d, None
    return d.get("results", []), d.get("__next")


def fetch_all(s, url, params, page_size=None, timeout=120):
    rows, page_no, skip = [], 0, 0
    p = dict(params)
    if page_size:
        p["$top"], p["$skip"] = page_size, 0
    next_url = f"{url}?{build_query(p)}"
    while next_url:
        r = s.get(next_url, timeout=timeout)
        if r.status_code >= 400:
            sys.exit(f"HTTP {r.status_code}: {r.text[:500]}")
        page, link = parse_page(r.json())
        rows.extend(page)
        page_no += 1
        print(f"  sayfa {page_no}: +{len(page)} (toplam {len(rows)})")
        if link:                                        # sunucu sayfalıyor
            next_url = urljoin(url + "/", link)
        elif page_size and len(page) == page_size:      # nextLink yok ama sayfa dolu: $skip ile devam
            skip += page_size
            next_url = f"{url}?{build_query({**params, '$top': page_size, '$skip': skip})}"
        else:
            next_url = None
    return rows


# --------------------------------------------------------------------------- veri
def to_utc(series):
    """V2 /Date(ms)/ ve ISO metin tarihlerini UTC datetime'a çevirir."""
    s = series.astype("string")
    ms = s.str.extract(MS_DATE)[0]
    from_ms = pd.to_datetime(pd.to_numeric(ms), unit="ms", utc=True, errors="coerce")
    from_iso = pd.to_datetime(s.where(ms.isna()), utc=True, errors="coerce")
    return from_ms.fillna(from_iso)


def date_filter(field, start, version):
    if version == "v2":
        return f"{field} ge datetime'{start:%Y-%m-%dT%H:%M:%S}'"
    return f"{field} ge {start:%Y-%m-%dT%H:%M:%S}Z"


def daily_counts(df, date_field):
    local = df[date_field].dt.tz_convert(LOCAL_TZ)
    day = local.dt.tz_localize(None).dt.normalize().dropna()
    counts = day.value_counts().sort_index()
    full = pd.date_range(counts.index.min(), counts.index.max(), freq="D")
    out = counts.reindex(full, fill_value=0).rename_axis("date").rename("requests").reset_index()
    today = pd.Timestamp.now(tz=LOCAL_TZ).tz_localize(None).normalize()
    out["partial"] = out["date"] == today               # bugün henüz tamamlanmadı
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=os.getenv("START_DATE", "2026-01-01"))
    ap.add_argument("--since-days", type=int, help="Artımlı: son N günü yeniden çek, eski kayıtları koru")
    ap.add_argument("--out", default="out")
    ap.add_argument("--page-size", type=int, help="Sunucu nextLink vermiyorsa $top/$skip ile sayfala")
    ap.add_argument("--version", default=os.getenv("ODATA_VERSION", "v4"), choices=["v2", "v4"])
    ap.add_argument("--probe", action="store_true", help="İlk 5 kaydı göster ve çık")
    ap.add_argument("--metadata", action="store_true", help="$metadata'yı kaydet ve çık")
    a = ap.parse_args()

    try:
        url = os.environ["ODATA_URL"].rstrip("/")
        date_field = os.environ["DATE_FIELD"]
    except KeyError as e:
        sys.exit(f"{e.args[0]} ortam değişkeni tanımlı değil.")
    id_field = os.getenv("ID_FIELD")
    extras = [f.strip() for f in os.getenv("EXTRA_FIELDS", "").split(",") if f.strip()]
    fields = list(dict.fromkeys([f for f in [date_field, id_field, *extras] if f]))

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    s = build_session()

    if a.metadata:
        root = url.rsplit("/", 1)[0]
        r = s.get(f"{root}/$metadata", headers={"Accept": "application/xml"}, timeout=120)
        r.raise_for_status()
        (out / "metadata.xml").write_text(r.text, encoding="utf-8")
        print(f"Kaydedildi: {out / 'metadata.xml'}")
        return

    if a.probe:
        rows = fetch_all(s, url, {"$top": 5}, None)
        df = pd.DataFrame(rows).drop(columns=["__metadata"], errors="ignore")
        print("\nAlanlar:", list(df.columns))
        print(df.head())
        return

    raw_path = out / "requests_raw.csv"
    existing = None
    if a.since_days and raw_path.exists():
        existing = pd.read_csv(raw_path)
        existing[date_field] = to_utc(existing[date_field])
        start = pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(days=a.since_days)
        print(f"Artımlı mod: {start:%Y-%m-%d} sonrası yeniden çekiliyor.")
    else:
        start = pd.Timestamp(a.start)
        print(f"Tam çekim: {start:%Y-%m-%d} sonrası.")

    params = {"$select": ",".join(fields),
              "$filter": date_filter(date_field, start, a.version),
              "$orderby": date_field}
    rows = fetch_all(s, url, params, a.page_size)
    new = pd.DataFrame(rows).drop(columns=["__metadata"], errors="ignore")
    if new.empty and existing is None:
        sys.exit("Hiç kayıt dönmedi. --probe ile alan adlarını ve filtreyi kontrol et.")
    if not new.empty:
        new[date_field] = to_utc(new[date_field])

    if existing is not None:
        cutoff = pd.Timestamp(start, tz="UTC")
        existing = existing[existing[date_field] < cutoff]
        df = pd.concat([existing, new], ignore_index=True)
    else:
        df = new

    n_before = len(df)
    df = df.drop_duplicates(subset=[id_field] if id_field else None)
    n_null = int(df[date_field].isna().sum())

    df.to_csv(raw_path, index=False, encoding="utf-8")
    daily = daily_counts(df, date_field)
    daily.to_csv(out / "requests_daily.csv", index=False, encoding="utf-8")

    print("\n--- Özet ---")
    print(f"Satır: {len(df)}  (tekrar silinen: {n_before - len(df)}, tarihi boş: {n_null})")
    print(f"Aralık: {daily['date'].min():%Y-%m-%d} .. {daily['date'].max():%Y-%m-%d}  ({len(daily)} gün)")
    print(f"Kaydı olmayan gün (0 ile dolduruldu): {int((daily['requests'] == 0).sum())}")
    print(f"Bugün tamamlanmadıysa son satır 'partial' işaretli. Çıktılar: {out.resolve()}")


if __name__ == "__main__":
    main()
