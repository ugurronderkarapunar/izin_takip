# İzin Takip ve Denetim Sistemi (v3.0)

İzin talebi → onay → çıktı → İK teslim → SAP girişi sürecini kayıt altına alan Streamlit uygulaması.
Tek rütbe: ekipteki herkes her işlemi yapabilir; yapılan her işlem **kullanıcı adıyla** değiştirilemez bir denetim günlüğüne yazılır.

## 1. Kalıcı veri: kapatıp açınca kayıt gitmesin

Kurum bilgisayarına SQL kurmaya gerek yok. İki seçenek:

| Seçenek | Ne zaman | Nasıl |
|---|---|---|
| **Bulut PostgreSQL** (önerilen) | Uygulama Streamlit Cloud'da | Ücretsiz Neon / Supabase hesabı → bağlantı adresini `database_url` secret'ı olarak gir |
| **Dosya** | Kendi bilgisayarında / sunucunda | Hiçbir şey yapma; `data/` klasörü kullanılır |

**Neon ile adım adım (ücretsiz, 5 dakika)**
1. neon.tech → hesap aç → yeni proje oluştur.
2. *Connection string* kutusundan adresi kopyala (`postgresql://...?sslmode=require`).
3. Streamlit Cloud → uygulamanız → **Settings → Secrets** bölümüne yapıştır:
   ```toml
   database_url = "postgresql://KULLANICI:SIFRE@HOST/VERITABANI?sslmode=require"
   audit_key = "uzun-rastgele-bir-metin"
   app_password = "ortak-parola"
   ```
4. Uygulamayı yeniden başlat. **Yönetim > Sistem durumu** ekranında *Depolama: veritabanı* ve *Bağlantı: Tamam* görmelisin. Tablolar otomatik oluşur.

> Supabase kullanıyorsan *Direct connection* yerine **Session pooler** adresini al (Streamlit Cloud IPv4 kullanır).
> `audit_key` değerini bir kenara kaydet: log zincirini ve QR doğrulamasını imzalar, kaybolursa eski kayıtlar doğrulanamaz.

**Eski yerel veriyi buluta taşıma** (mevcut `data/` klasörün varsa):
```bash
python -m scripts.migrate_to_db --source data --database-url "postgresql://..."
```
Talepler, personel, ayarlar, ek dosyalar, denetim günlüğü (zincir bozulmadan) ve anahtar taşınır. Hedef veritabanı boş olmalıdır.

## 2. Kurulum (yerel)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```
Gizli değerler için `.streamlit/secrets.toml.example` dosyasını `.streamlit/secrets.toml` olarak kopyala (GitHub'a yükleme!).
PDF çıktısı için sistemde Türkçe karakterli font gerekir (Windows/macOS'ta Arial, Linux'ta `fonts-dejavu-core`; Streamlit Cloud için `packages.txt` ekli).

## 3. Özellikler

- **Süreç:** Beklemede → Onaylandı / Reddedildi / İptal → çıktı → İK teslim → SAP. Toplu işlem, çoklu personel filtresi, arşive silme + geri yükleme, gerekçeli düzeltme.
- **Geri al (undo):** Yan panelde "↩️ Son işlemleri geri al": son 60 dakikadaki, sonradan başka işlem yapılmamış işlemler tek tıkla geri alınır (geri alma da loglanır).
- **Bildirim merkezi:** Başkalarının yaptığı işlemler, gecikme uyarıları; menüde okunmamış sayısı.
- **Otomatik e-posta (günlük özet):** gecikmiş işler + sistem sorunları. Üç yol: (a) uygulama o gün ilk açıldığında otomatik, (b) GitHub Actions zamanlaması (`.github/workflows/notify.yml`; uygulama kapalı olsa da çalışır), (c) Yönetim > Sistem durumu'ndan elle. Ayarlar: Yönetim > Ayarlar; SMTP bilgileri secrets'ta (`smtp_*`). Gmail için "uygulama şifresi" kullan.
- **Doğrulanabilir izin formu:** Word/PDF çıktısında QR kod; okutunca herkese açık sayfa belgenin kayıtla eşleşip eşleşmediğini ve **güncel durumunu** (sonradan iptal edildi mi) gösterir. Sahte veya sonradan düzenlenmiş belge eşleşmez. Çalışması için Yönetim > Ayarlar'da *Uygulama adresi* girilmelidir.
- **Takvim:** aylık takvim ızgarası (resmî tatil, arife, yoğunluk eşiği), gün detayı, zaman çizelgesi.
- **İş kuralları:** yarım gün (sabah/öğleden sonra), saatlik izin (günlük çalışma saatine göre kesirli gün), arife günleri 0,5 iş günü, aynı kişide gün bazında kapasite kontrolü, **işe giriş tarihine göre otomatik yıllık izin hakkı** (elle girilen değer önceliklidir).
- **Gözlem:** Yönetim > Sistem durumu: bağlantı, sayaçlar, son yedek/bildirim, günlük işlem grafiği, hata olayları.
- **Tek rütbe + parola:** Yönetim > Ayarlar'dan kullanıcı listesi tanımlanır; `app_password` ile ortak parola konur.

### İş kuralları hakkında notlar
- Kıdem tablosu varsayılan olarak 4857 sayılı İş Kanunu m.53 asgari sürelerini kullanır (1–5 yıl: 14, 6–14 yıl: 20, 15+: 26 gün; yıl sonu itibarıyla tamamlanan hizmet yılı). **Kurumunuzun toplu iş sözleşmesi farklıysa Yönetim > Ayarlar'dan değiştirin.** Yaş istisnaları (18 yaş altı / 50 yaş üstü) dahil değildir.
- Dinî bayram tarihleri 2026–2027 için varsayılan yüklüdür; her yıl Diyanet takvimiyle kontrol edin (Yönetim > Tatiller).
- Çok kademeli onay / vekil atama eklenmedi (tek rütbe).

## 4. Veri modeli ve güvenlik

- **Tipli modeller (pydantic):** Talep, personel, ayar ve tatil kayıtları yazılmadan önce katı doğrulanır; alan adı yazım hatası ya da yanlış tip kaydı engeller ve işlem geri alınır.
- **Denetim günlüğü:** HMAC-SHA256 zincirli, yalnızca ekleme. Dosya modunda `audit_logs.jsonl` (satır satır), veritabanı modunda ayrı tablo. Eski `audit_logs.json` ilk açılışta otomatik dönüştürülür (zincir korunur). Günlük çapa + mühür + talep-log tutarlılık kontrolü vardır.
- **İşlem bütünlüğü:** Her yazma tek işlemdir (veritabanında gerçek transaction, dosyada geri alma); biri hata verirse hiçbiri uygulanmaz. Veritabanında eşzamanlı yazma `pg_advisory_xact_lock` ile sıraya alınır; 3 kişi aynı anda girebilir.
- **Yedek:** Her gün ilk açılışta otomatik; veritabanı modunda yedekler veritabanındadır. Yönetim > Yedek'ten ZIP indirilir / yedeğe dönülür.
- **KVKK:** Personel izin verisi kişisel veridir. Parola koyun, uygulamayı gerekirse Streamlit'te yalnızca davetli e-postalara açın.
- **Rol/yetki:** Tek rütbe olduğu için kimlik doğrulama yoktur; kullanıcı seçimi güven esaslıdır. Ortak parola yalnızca dışarıdan erişimi engeller. Kişi bazlı giriş gerekirse `st.login` eklenebilir.

## 5. Test ve CI

```bash
pip install -r requirements-dev.txt
python -m pytest                              # dosya arka ucu (varsayılan)
IZIN_TEST_BACKEND=sqlite   python -m pytest   # SQLite
IZIN_TEST_BACKEND=pgserver python -m pytest   # gömülü gerçek PostgreSQL (pip install pgserver)
ruff check .
```
GitHub Actions (`ci.yml`) her push'ta lint + testleri **dosya / SQLite / PostgreSQL** üçünde çalıştırır ve dosya sürüm işaretlerini kontrol eder.

**Bildirim zamanlaması için** GitHub repo → Settings → Secrets → Actions: `IZIN_DATABASE_URL`, `IZIN_AUDIT_KEY`, `IZIN_SMTP_HOST`, `IZIN_SMTP_PORT`, `IZIN_SMTP_USER`, `IZIN_SMTP_PASSWORD`, `IZIN_SMTP_FROM`, `IZIN_APP_URL`.

## 6. Yapı

```
app.py                giriş: kurulum kontrolü, secrets, parola, kullanıcı, gezinme
core/  config · models · rules · storage (dosya / SQL arka uç) · audit · services
       calendar_utils · documents · verify · notifier · inbox · backup · notify · utils
ui/    components · calendar_grid
views/ sayfalar (dashboard, new_request, requests_list, team_calendar, notifications,
       balances, reports, audit_view, admin)
scripts/  send_notifications · migrate_to_db
tests/    pytest (66 test)        .github/workflows/  ci · notify
```

## 7. Sorun giderme
- **"Kurulum eksik veya karışık"**: listelenen dosyalar eksik/kesik/eski. Tüm klasörleri zip'ten eksiksiz yükleyin; her dosyanın son satırı `# END-OF-FILE v3.0` olmalıdır.
- **"Veri deposuna bağlanılamadı"**: `database_url` doğru mu, veritabanı uyanık mı (Neon ücretsiz planda boşta uyur; birkaç saniye sonra tekrar deneyin).
- **QR kod basılmıyor**: Yönetim > Ayarlar > *Uygulama adresi* boş.
