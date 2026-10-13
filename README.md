# İzin Takip ve Denetim Sistemi (v2)

Yerel çalışan, SQL gerektirmeyen Streamlit uygulaması. Veriler `data/` klasöründe JSON olarak tutulur.

## Kurulum

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Eski sürümden geçiş: eski `data/` klasörünü bu klasörün içine kopyalayın. Eski talepler ve eski log zinciri olduğu gibi okunur ve doğrulanır; yeni kayıtlar HMAC ile imzalanır.

## Yapı

```
app.py            giriş, gezinme (rol bazlı menü), yan panel
core/             arayüzden bağımsız mantık
  config.py       sabitler, roller, ayarlar
  storage.py      atomik JSON yazım, dosya kilidi, önbellek, bozuk dosya yedeği
  audit.py        HMAC zincirli log, çapa, mühür, tutarlılık kontrolü
  services.py     iş kuralları (talep, bakiye, uyarı, Excel)
  calendar_utils.py  resmî tatil, iş günü
  documents.py    Word / PDF izin formu
  notify.py       WhatsApp / e-posta bağlantıları
  backup.py       günlük otomatik yedek, ZIP
ui/components.py  detay paneli, diyaloglar
views/            sayfalar
tests/            pytest (python -m pytest)
data/             ilk çalıştırmada oluşur (talepler, log, personel, tatiller, ekler, yedekler)
```

## Süreç akışı

Talep (Beklemede) → Karar (Onaylandı / Reddedildi / İptal) → Çıktı alındı → İK'ya teslim → SAP girişi.
Her adım kim/ne zaman ile loglanır. Teslim yanlış girildiyse **gerekçeli düzeltme / geri alma** vardır; eski kayıt `teslim_gecmisi` ve logda kalır, hiçbir kayıt silinmez.

## Veri kaybı olmaması için

- Talepler **hiçbir yerde silinemez**; yalnızca durum değişir.
- Her yazma atomiktir (geçici dosya + `os.replace`) ve dosya kilidi altındadır.
- Form hata verirse girilen bilgiler silinmez; yalnızca başarılı kayıttan sonra temizlenir.
- Günün ilk açılışında otomatik yedek (`data/backups`, varsayılan 30 gün saklanır); Yönetim > Yedek'ten ZIP indirilebilir.
- **Uyarı:** Streamlit Community Cloud gibi geçici disk kullanan ortamlarda dosyalar yeniden başlatmada silinir. Kalıcı bir bilgisayar/sunucu kullanın ya da `IZIN_DATA_DIR` ile kalıcı bir klasöre yönlendirin.

## Güvenlik notları

- Log zinciri HMAC-SHA256 ile imzalanır. Anahtar `IZIN_AUDIT_KEY` ortam değişkeninden alınır; yoksa `data/.audit_key` dosyası oluşturulur. **Anahtarı ayrıca saklayın**; kaybolursa yeni HMAC kayıtları doğrulanamaz. Anahtar yedeklere konmaz.
- Günlük çapa (`anchors.json`) ve Audit sayfasındaki **mühür** metni, sondan silme ve zinciri baştan yazma girişimlerini ortaya çıkarır. Mühürü arada e-posta ile kendinize gönderin.
- Tutarlılık kontrolü, `request_db.json` elle değiştirilirse log geçmişiyle uyuşmazlığı gösterir.
- **Roller kimlik doğrulama değildir.** Kullanıcı adı ve rol seçimi serbesttir; gerçek koruma için giriş sistemi (ör. `st.login` / OIDC) eklenmelidir. Çok kullanıcılı ağ kullanımında SQLite'a geçiş önerilir.

## Dikkat

- Dinî bayram tarihleri varsayılan olarak 2026–2027 için yüklenir; her yıl Diyanet takvimiyle Yönetim > Tatiller'den kontrol edin.
- PDF çıktısı için sistemde Türkçe karakterli bir TTF gerekir (Windows/macOS'ta Arial, Linux'ta DejaVu bulunur). Farklı bir font için `IZIN_FONT_PATH` verin. Bulunamazsa yalnızca Word çıktısı sunulur.
- Testler: `python -m pytest`
