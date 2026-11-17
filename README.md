#!/usr/bin/env python3
"""
OData -> ham CSV + günlük request sayımı

Kurulum:
    pip install pandas requests

Ortam değişkenleri (parolayı koda yazma, .env / ortam değişkeni kullan):
    ODATA_URL      Entity set URL'si, örn. https://sunucu/odata/Requests   (zorunlu)
    ODATA_USER     Kullanıcı adı (Basic Auth)
    ODATA_PASS     Parola (Basic Auth)
    ODATA_TOKEN    Bearer token (varsa Basic yerine bu kullanılır)
    DATE_FIELD     Oluşturma zamanı alanı, örn. CreateDateTime              (zorunlu)
    ID_FIELD       Kayıt kimliği alanı (tekrar temizleme için, önerilir)
    EXTRA_FIELDS   Ek alanlar, virgülle: Category,Priority,Status
    ODATA_VERSION  v4 (varsayılan) ya da v2
    LOCAL_TZ       Günlük sınırı için saat dilimi (varsayılan Europe/Istanbul)

Kullanım:
    python odata_extract.py --metadata            # servis şemasını out/metadata.xml'e kaydeder
    python odata_extract.py --probe               # ilk 5 kaydı gösterir, alan adlarını doğrula
    python odata_extract.py --start 2026-01-01    # ilk tam çekim
    python odata_extract.py --since-days 21       # artımlı: son 21 günü yeniden çek, eskiyi koru

Çıktılar (out/ klasörü):
    requests_raw.csv     ham satırlar (tarih UTC, ISO biçiminde)
    requests_daily.csv   date, requests, partial  (eksik günler 0 ile doldurulur)
"""