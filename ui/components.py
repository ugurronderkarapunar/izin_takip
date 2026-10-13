"""Ortak arayüz bileşenleri: oturum bilgisi, bildirimler, diyaloglar, talep detay paneli."""
from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd
import streamlit as st

from core import config, documents, services
from core.config import S_APPROVED, S_CANCELLED, S_PENDING, S_REJECTED, STATUS_ICON
from core.utils import fmt_date, fmt_dt

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ----------------------------------------------------------------------------
# Oturum, yetki, bildirim
# ----------------------------------------------------------------------------
def current_user() -> str:
    return st.session_state.get("kullanici", "").strip()


def current_role() -> str:
    return st.session_state.get("rol", "Planlama")


def can(perm: str) -> bool:
    return config.can(current_role(), perm)


def user_ok() -> bool:
    if not current_user():
        st.error("Önce sol menüden **kullanıcı adınızı** girin; tüm işlemler bu isimle kaydedilir.")
        return False
    return True


def flash(msg: str, icon: str = "✅") -> None:
    st.session_state["_flash"] = (msg, icon)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        st.toast(item[0], icon=item[1])


def run(fn: Callable, *args, ok_msg: str, **kwargs) -> bool:
    """Servis fonksiyonunu (user, role, ...) çağırır; hatayı kullanıcıya gösterir."""
    if not user_ok():
        return False
    try:
        fn(current_user(), current_role(), *args, **kwargs)
    except (ValueError, PermissionError, OSError) as exc:
        st.error(str(exc))
        return False
    flash(ok_msg)
    return True


def status_label(durum: str) -> str:
    return f"{STATUS_ICON.get(durum, '')} {durum}"


def open_request(talep_id: str) -> None:
    st.session_state["secili_talep"] = talep_id
    st.session_state["_son_secim"] = None
    st.switch_page("views/requests_list.py")


# ----------------------------------------------------------------------------
# Diyaloglar (geri alınamaz / gerekçeli işlemler için onay)
# ----------------------------------------------------------------------------
def _summary(r: dict) -> None:
    st.markdown(f"**{r['id']}** · {r['personel_adi']} · {r['izin_turu']} · "
                f"{fmt_date(r['baslangic'])} – {fmt_date(r['bitis'])} ({r['is_gunu']} iş günü)")


@st.dialog("Durum güncelle")
def dialog_status(talep_id: str, yeni: str) -> None:
    r = services.get_request(talep_id)
    _summary(r)
    st.write(f"{status_label(r['durum'])}  →  **{status_label(yeni)}**")
    needs = yeni in (S_REJECTED, S_CANCELLED)
    note = st.text_area("Gerekçe" + (" *" if needs else " (isteğe bağlı)"), key=f"dlg_note_{talep_id}_{yeni}")
    c1, c2 = st.columns(2)
    if c1.button("Vazgeç", key=f"dlg_cancel_{talep_id}_{yeni}", width="stretch"):
        st.rerun()
    if c2.button("Onayla ve kaydet", type="primary", key=f"dlg_ok_{talep_id}_{yeni}", width="stretch"):
        if needs and not note.strip():
            st.error("Bu işlem için gerekçe zorunludur.")
        elif run(services.update_status, talep_id, yeni, note, ok_msg=f"{talep_id} → {yeni}"):
            st.rerun()


@st.dialog("İK'ya teslim")
def dialog_deliver(talep_id: str) -> None:
    r = services.get_request(talep_id)
    _summary(r)
    confirm = st.checkbox("İzin kağıdının çıktısı alındı ve İK'ya teslim edildi", key=f"dlg_tc_{talep_id}")
    tarih = st.date_input("Teslim tarihi", value=date.today(), max_value=date.today(), format="DD.MM.YYYY", key=f"dlg_td_{talep_id}")
    alan = st.text_input("Teslim alan İK personeli *", key=f"dlg_ta_{talep_id}")
    st.caption("Kayıt loga işlenir. Hata olursa 'Teslimi düzelt / geri al' ile gerekçeli düzeltilir; eski kayıt silinmez.")
    if st.button("Teslimi kaydet", type="primary", key=f"dlg_tok_{talep_id}", width="stretch"):
        if not confirm:
            st.error("Teslim onay kutusunu işaretleyin.")
        elif run(services.mark_delivered, talep_id, tarih, alan, ok_msg=f"{talep_id} İK'ya teslim edildi"):
            st.rerun()


@st.dialog("Teslim kaydını düzelt / geri al")
def dialog_fix_delivery(talep_id: str) -> None:
    r = services.get_request(talep_id)
    _summary(r)
    st.info(f"Mevcut kayıt: {fmt_date(r['ik_teslim_tarihi'])} · {r['ik_teslim_alan']}")
    mode = st.radio("İşlem", ["Bilgileri düzelt", "Teslimi geri al"], horizontal=True, key=f"dlg_fm_{talep_id}")
    tarih, alan = date.today(), ""
    if mode == "Bilgileri düzelt":
        tarih = st.date_input("Yeni teslim tarihi", value=date.today(), max_value=date.today(), format="DD.MM.YYYY", key=f"dlg_fd_{talep_id}")
        alan = st.text_input("Yeni teslim alan *", value=r["ik_teslim_alan"] or "", key=f"dlg_fa_{talep_id}")
    gerekce = st.text_area("Gerekçe *", key=f"dlg_fg_{talep_id}")
    if st.button("Uygula", type="primary", key=f"dlg_fok_{talep_id}", width="stretch"):
        if mode == "Bilgileri düzelt":
            ok = run(services.correct_delivery, talep_id, tarih, alan, gerekce, ok_msg="Teslim kaydı düzeltildi")
        else:
            ok = run(services.revoke_delivery, talep_id, gerekce, ok_msg="Teslim kaydı geri alındı")
        if ok:
            st.rerun()


@st.dialog("SAP girişi")
def dialog_sap(talep_id: str, revoke: bool = False) -> None:
    r = services.get_request(talep_id)
    _summary(r)
    if revoke:
        gerekce = st.text_area("Geri alma gerekçesi *", key=f"dlg_sg_{talep_id}")
        if st.button("SAP işaretini geri al", type="primary", key=f"dlg_sr_{talep_id}", width="stretch"):
            if run(services.revoke_sap, talep_id, gerekce, ok_msg="SAP işareti geri alındı"):
                st.rerun()
        return
    tarih = st.date_input("SAP giriş tarihi", value=date.today(), max_value=date.today(), format="DD.MM.YYYY", key=f"dlg_sd_{talep_id}")
    note = st.text_input("Not (isteğe bağlı)", key=f"dlg_sn_{talep_id}")
    if st.button("SAP'ye girildi olarak kaydet", type="primary", key=f"dlg_sok_{talep_id}", width="stretch"):
        if run(services.mark_sap, talep_id, tarih, note, ok_msg=f"{talep_id} SAP girişi kaydedildi"):
            st.rerun()


@st.dialog("Ek dosya ekle")
def dialog_attach(talep_id: str) -> None:
    files = st.file_uploader("Dosya(lar)", accept_multiple_files=True, key=f"dlg_up_{talep_id}",
                             type=[e.strip(".") for e in sorted(config.ALLOWED_EXT)])
    st.caption(f"En fazla {config.MAX_UPLOAD_MB} MB. Eklenen dosyalar silinemez; SHA-256 özeti loga yazılır.")
    if st.button("Ekle", type="primary", key=f"dlg_upok_{talep_id}", width="stretch", disabled=not files):
        ok_all = True
        for f in files:
            ok_all &= run(services.add_attachment, talep_id, f.name, f.getvalue(), ok_msg=f"{f.name} eklendi")
        if ok_all:
            st.rerun()


# ----------------------------------------------------------------------------
# Talep detay paneli
# ----------------------------------------------------------------------------
def render_steps(r: dict) -> None:
    steps = [
        ("Talep", True, fmt_dt(r["olusturma_zamani"])),
        ("Karar", r["durum"] != S_PENDING, f"{r['durum']} · {fmt_dt(r['onay_zamani'])}" if r["onay_zamani"] else r["durum"]),
        ("Çıktı", bool(r["cikti_alindi"]), fmt_dt(r["cikti_zamani"]) if r["cikti_alindi"] else "bekliyor"),
        ("İK Teslim", bool(r["ik_teslim"]), fmt_date(r["ik_teslim_tarihi"]) + f" · {r['ik_teslim_alan']}" if r["ik_teslim"] else "bekliyor"),
        ("SAP", bool(r["sap_girildi"]), fmt_date(r["sap_tarihi"]) if r["sap_girildi"] else "bekliyor"),
    ]
    closed = r["durum"] in (S_REJECTED, S_CANCELLED)
    for col, (name, ok, info) in zip(st.columns(5), steps):
        icon = "✅" if ok else ("➖" if closed else "⬜")
        col.markdown(f"{icon} **{name}**")
        col.caption(info)


def render_detail(talep_id: str, key_prefix: str = "d") -> None:
    r = services.get_request(talep_id)
    if r is None:
        st.warning("Talep bulunamadı.")
        return
    k = lambda a: f"{key_prefix}_{a}_{talep_id}"  # noqa: E731

    st.subheader(f"{r['id']} · {r['personel_adi']}")
    st.markdown(f"**{status_label(r['durum'])}** · {r['izin_turu']} · {fmt_date(r['baslangic'])} – {fmt_date(r['bitis'])} "
                f"· **{r['is_gunu']} iş günü** ({r['gun_sayisi']} takvim günü)")
    if r["aciklama"]:
        st.caption(f"Açıklama: {r['aciklama']}")
    render_steps(r)

    actions: list[tuple[str, str, Callable[[], None]]] = []
    approved = r["durum"] == S_APPROVED
    if r["durum"] == S_PENDING and can("onay"):
        actions += [("✅ Onayla", "ap", lambda: dialog_status(talep_id, S_APPROVED)),
                    ("❌ Reddet", "rj", lambda: dialog_status(talep_id, S_REJECTED)),
                    ("🚫 İptal et", "cn", lambda: dialog_status(talep_id, S_CANCELLED))]
    if approved and can("onay"):
        actions.append(("🚫 İptal et", "cn", lambda: dialog_status(talep_id, S_CANCELLED)))
    if approved and not r["cikti_alindi"] and can("cikti"):
        def _print() -> None:
            if run(services.mark_printed, talep_id, ok_msg="Çıktı alındı olarak işaretlendi"):
                st.rerun()
        actions.append(("🖨️ Çıktı alındı", "pr", _print))
    if approved and r["cikti_alindi"] and not r["ik_teslim"] and can("teslim"):
        actions.append(("📨 İK'ya teslim et", "dl", lambda: dialog_deliver(talep_id)))
    if r["ik_teslim"] and can("duzeltme"):
        actions.append(("✏️ Teslimi düzelt / geri al", "fx", lambda: dialog_fix_delivery(talep_id)))
    if approved and not r["sap_girildi"] and can("sap"):
        actions.append(("🧾 SAP'ye girildi", "sp", lambda: dialog_sap(talep_id)))
    if approved and r["sap_girildi"] and can("duzeltme"):
        actions.append(("↩️ SAP işaretini geri al", "sr", lambda: dialog_sap(talep_id, revoke=True)))
    if can("ek_ekle"):
        actions.append(("📎 Ek ekle", "at", lambda: dialog_attach(talep_id)))
    for i in range(0, len(actions), 3):
        for col, (label, key, fn) in zip(st.columns(3), actions[i:i + 3]):
            if col.button(label, key=k(key), width="stretch"):
                fn()

    if approved:
        person = services.get_person(r["personel_adi"]) or {}
        c1, c2 = st.columns(2)
        c1.download_button("📄 İzin formu (Word)", documents.build_docx(r, person), file_name=f"{r['id']}.docx",
                           mime=DOCX_MIME, key=k("docx"), width="stretch")
        pdf = documents.build_pdf(r, person)
        if pdf:
            c2.download_button("📕 İzin formu (PDF)", pdf, file_name=f"{r['id']}.pdf", mime="application/pdf",
                               key=k("pdf"), width="stretch")

    t_ek, t_gecmis = st.tabs([f"📎 Ekler ({len(r['ekler'])})", "🕘 Geçmiş"])
    with t_ek:
        if not r["ekler"]:
            st.caption("Ek dosya yok.")
        for i, e in enumerate(r["ekler"]):
            c = st.columns([5, 2])
            c[0].write(f"📎 {e['orijinal_ad']} · {max(e['boyut'] // 1024, 1)} KB · {e['yukleyen']} · {fmt_dt(e['zaman'])}")
            try:
                c[1].download_button("İndir", services.read_attachment(r["id"], e["dosya"]),
                                     file_name=e["orijinal_ad"], key=k(f"ek{i}"))
            except FileNotFoundError:
                c[1].error("Dosya bulunamadı")
    with t_gecmis:
        hist = services.history(r["id"])
        if hist:
            df = pd.DataFrame(hist)
            df["zaman"] = df["zaman"].map(fmt_dt)
            df = df.rename(columns={"zaman": "Zaman", "kullanici": "Kim", "islem": "İşlem", "eski_durum": "Eski",
                                    "yeni_durum": "Yeni", "detay": "Detay"})
            st.dataframe(df[["Zaman", "Kim", "İşlem", "Eski", "Yeni", "Detay"]].iloc[::-1], hide_index=True,
                         width="stretch")
        else:
            st.caption("Kayıt yok.")
