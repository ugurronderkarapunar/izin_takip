"""Audit trail: bütünlük durumu, filtrelenebilir log tablosu, mühür."""
import json
import math

import pandas as pd
import streamlit as st

from core import audit, services
from core.utils import fmt_dt

st.title("🛡️ Audit Trail (Denetim Geçmişi)")
rep = services.integrity_report()
c1, c2, c3 = st.columns(3)
if rep["chain"]:
    c1.error("🚨 Log zinciri: SORUN")
else:
    c1.success(f"🔒 Log zinciri sağlam ({rep['log_count']} kayıt)")
if rep["consistency"]:
    c2.error("🚨 Talep verisi loglarla uyuşmuyor")
else:
    c2.success("✅ Talep verisi loglarla tutarlı")
c3.info(f"⚓ {rep['anchor_count']} çapa · anahtar: {rep['key_source']}")
for p in rep["chain"] + rep["consistency"]:
    st.error(p)

logs = audit.logs_store.read()
if not logs:
    st.info("Henüz log kaydı yok.")
    st.stop()

df = pd.DataFrame(logs)
if "rol" not in df.columns:
    df["rol"] = "—"
df["rol"] = df["rol"].fillna("—")

f1, f2, f3 = st.columns(3)
ara = f1.text_input("Talep ID / kullanıcı / detay ara")
islem_f = f2.multiselect("İşlem", sorted(df["islem"].unique()))
size = f3.selectbox("Sayfa boyutu", [25, 50, 100, 250], index=1)
if ara.strip():
    mask = df[["talep_id", "kullanici", "detay"]].apply(lambda s: s.astype(str).str.contains(ara.strip(), case=False, na=False)).any(axis=1)
    df = df[mask]
if islem_f:
    df = df[df["islem"].isin(islem_f)]
df = df.sort_values("log_id", ascending=False)
pages = max(1, math.ceil(len(df) / size))
page = st.number_input("Sayfa", 1, pages, 1)
st.caption(f"{len(df)} kayıt · salt okunur (silinemez / düzenlenemez).")
view = df.iloc[(page - 1) * size: page * size].copy()
view["zaman"] = view["zaman"].map(fmt_dt)
view = view.rename(columns={"log_id": "Log #", "zaman": "Zaman", "kullanici": "Kim", "rol": "Rol", "talep_id": "Talep",
                            "islem": "İşlem", "eski_durum": "Eski", "yeni_durum": "Yeni", "detay": "Detay"})
st.dataframe(view[["Log #", "Zaman", "Kim", "Rol", "Talep", "İşlem", "Eski", "Yeni", "Detay"]], hide_index=True, width="stretch")
d1, d2 = st.columns(2)
d1.download_button("⬇️ Log (JSON)", json.dumps(logs, ensure_ascii=False, indent=2).encode("utf-8"), "audit_logs.json", "application/json")
d2.download_button("⬇️ Log (CSV)", pd.DataFrame(logs).to_csv(index=False).encode("utf-8-sig"), "audit_logs.csv", "text/csv")

with st.expander("🔏 Mühür (zincirin dışarıda saklanabilir özeti)"):
    st.caption("Bu satırı e-posta ile kendinize gönderin ya da yazdırın. İleride kayıtlar silinirse/değiştirilirse mühür bunu ortaya çıkarır.")
    st.code(audit.seal_text(logs), language=None)
    seal = st.text_input("Doğrulanacak mühür", placeholder="MUHUR|2026-10-13 …|log_id=…|hash=…")
    if seal.strip():
        ok, msg = audit.check_seal(seal, logs)
        (st.success if ok else st.error)(msg)

# END-OF-FILE v3.1
