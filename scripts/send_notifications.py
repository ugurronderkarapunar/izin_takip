"""Günlük bildirim e-postası. Zamanlanmış çalıştırma için (GitHub Actions / cron):

    python -m scripts.send_notifications [--force] [--dry-run]

Gerekli ortam değişkenleri: IZIN_DATABASE_URL, IZIN_AUDIT_KEY (doğrulama için), IZIN_SMTP_HOST/PORT/USER/PASSWORD/FROM
"""
import argparse
import json
import sys

from core import bootstrap, notifier


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="saat / iş günü / gün içi tekrar denetimlerini atla")
    ap.add_argument("--dry-run", action="store_true", help="göndermeden önizle")
    args = ap.parse_args()
    bootstrap.init()
    res = notifier.run_daily(force=args.force, dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in res.items() if k != "mesajlar"}, ensure_ascii=False))
    if args.dry_run:
        for m in res.get("mesajlar", []):
            print(f"--- {m['to']} | {m['subject']}\n{m['text']}\n")
    return 1 if res.get("durum") == "hata" else 0


if __name__ == "__main__":
    sys.exit(main())

# END-OF-FILE v3.2
