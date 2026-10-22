"""Ortak arayüz bileşenleri: oturum bilgisi, bildirimler, diyaloglar, talep detay paneli."""
from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd
import streamlit as st
from filelock import Timeout

import hmac

from core import config, documents, rules, services, verify
from core.config import S_APPROVED, S_CANCELLED, S_PENDING, S_REJECTED, STATUS_ICON
from core.utils import fmt_date, fmt_dt

UI_VERSION = "3.2"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


# ----------------------------------------------------------------------------
# Oturum, yetki, bildirim
# ----------------------------------------------------------------------------
def current_user() -> str:
    return st.session_state.get("kullanici", "").strip()


def current_role() -> str:
    return st.session_state.get("rol", "Kullanıcı")


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
    except Timeout:
        st.error("Veri dosyası şu an başka bir işlem tarafından kullanılıyor. Birkaç saniye sonra tekrar deneyin.")
        return False
    except (ValueError, PermissionError, OSError) as exc:
        st.error(str(exc))
        return False
    st.session_state.pop("_saglik", None)  # bütünlük özeti yeniden hesaplansın
    if ok_msg:
        flash(ok_msg)
    return True


def status_label(durum: str, silindi: bool = False) -> str:
    return "🗑️ Silindi" if silindi else f"{STATUS_ICON.get(durum, '')} {durum}"


def reset_table_selection() -> None:
    """Liste değiştiğinde (silme/geri yükleme) eski satır seçiminin başka talebi açmasını önler."""
    st.session_state["tablo_nonce"] = st.session_state.get("tablo_nonce", 0) + 1
    st.session_state["_son_secim"] = ()


def open_request(talep_id: str) -> None:
    st.session_state["secili_talep"] = talep_id
    st.session_state["_son_secim"] = ()
    st.switch_page("views/requests_list.py")


# ----------------------------------------------------------------------------
# Diyaloglar (geri alınamaz / gerekçeli işlemler için onay)
# ----------------------------------------------------------------------------
def _summary(r: dict) -> None:
    st.markdown(f"**{r['id']}** · {r['personel_adi']} · {r['izin_turu']} · "
                f"{fmt_date(r['baslangic'])} – {fmt_date(r['bitis'])} ({rules.fmt_days(r['is_gunu'])} iş günü · {rules.sure_etiket(r)})")


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


@st.dialog("Talebi düzenle")
def dialog_edit(talep_id: str) -> None:
    r = services.get_request(talep_id)
    _summary(r)
    k = lambda n: f"dlg_e{n}_{talep_id}"  # noqa: E731
    tur = st.selectbox("İzin türü", config.LEAVE_TYPES, index=config.LEAVE_TYPES.index(r["izin_turu"]), key=k("t"))
    c1, c2 = st.columns(2)
    start = c1.date_input("Başlangıç", value=date.fromisoformat(r["baslangic"]), format="DD.MM.YYYY", key=k("s"))
    end = c2.date_input("Bitiş", value=date.fromisoformat(r["bitis"]), format="DD.MM.YYYY", key=k("e"))
    tipler = list(rules.SURE_ETIKET)
    sure = st.selectbox("Süre türü", tipler, index=tipler.index(r["sure_tipi"]), format_func=rules.SURE_ETIKET.get, key=k("st"))
    saat = None
    if sure == "saatlik":
        saat = st.number_input("Saat", 0.5, 24.0, float(r["saat"] or 1), step=0.5, key=k("sa"))
    if sure != "tam":
        end = start
    aciklama = st.text_area("Açıklama", value=r["aciklama"], key=k("a"))
    approved = r["durum"] == S_APPROVED
    if approved:
        st.info("Bu talep onaylı. Tür veya tarih değişirse talep **yeniden onaya** düşer; yalnızca açıklama değişirse onay korunur.")
    gerekce = st.text_area("Düzenleme gerekçesi" + (" *" if approved else " (isteğe bağlı)"), key=k("g"))
    pv = services.preview_request(r["personel_adi"], tur, start, end, exclude_id=talep_id, sure_tipi=sure, saat=saat)
    st.caption(f"{rules.fmt_days(pv['is_gunu'])} iş günü · {pv['takvim_gun']} takvim günü")
    for e in pv["errors"]:
        st.error(e)
    for w in pv["warnings"]:
        st.warning(w)
    override = False
    if pv["bakiye_yetersiz"]:
        st.warning("Yıllık izin bakiyesi yetersiz.")
        override = st.checkbox("Bakiye yetersiz olsa da kaydet (loga işlenir)", key=k("o"))
    blocked = bool(pv["errors"]) or (pv["bakiye_yetersiz"] and not override)
    if st.button("Değişiklikleri kaydet", type="primary", disabled=blocked, key=k("ok"), width="stretch"):
        if run(services.edit_request, talep_id, tur, start, end, aciklama, gerekce, override, sure, saat,
               ok_msg=f"{talep_id} düzenlendi"):
            st.rerun()


@st.dialog("Talebi sil")
def dialog_delete(talep_id: str) -> None:
    r = services.get_request(talep_id)
    _summary(r)
    risky = r["durum"] == S_APPROVED or r["ik_teslim"] or r["sap_girildi"]
    if risky:
        st.warning("Bu izin onaylanmış" + (", İK'ya teslim edilmiş" if r["ik_teslim"] else "")
                   + (" ve SAP'ye girilmiş" if r["sap_girildi"] else "")
                   + ". Sonradan uyuşmazlık çıkmaması için silmek yerine **İptal Edildi** durumu önerilir.")
    st.info("Silinen talep listelerden, bakiyeden, takvimden ve uyarılardan kalkar. Kayıt ve log arşivde saklanır; "
            "'Silinenleri göster' ile bulunup geri yüklenebilir.")
    gerekce = st.text_area("Silme gerekçesi *", key=f"dlg_dg_{talep_id}")
    anladim = st.checkbox("Riski anladım, yine de silmek istiyorum", key=f"dlg_dk_{talep_id}") if risky else True
    c1, c2 = st.columns(2)
    if c1.button("Vazgeç", key=f"dlg_dc_{talep_id}", width="stretch"):
        st.rerun()
    if c2.button("🗑️ Sil", type="primary", key=f"dlg_dok_{talep_id}", width="stretch"):
        if not gerekce.strip():
            st.error("Silme gerekçesi zorunludur.")
        elif not anladim:
            st.error("Devam etmek için onay kutusunu işaretleyin.")
        elif run(services.delete_request, talep_id, gerekce, ok_msg=f"{talep_id} silindi (arşive kaldırıldı)"):
            reset_table_selection()
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
@st.dialog("Toplu işlem", width="large")
def dialog_bulk(action: str, ids: list[str]) -> None:
    label, _ = config.BULK_ACTIONS[action]
    by_id = {r["id"]: r for r in services.load_requests()}
    reqs = [by_id[i] for i in ids if i in by_id]
    st.markdown(f"**{label}** · {len(reqs)} talep · {len({r['personel_adi'] for r in reqs})} personel")
    st.dataframe(pd.DataFrame([{"Talep": r["id"], "Personel": r["personel_adi"], "Tür": r["izin_turu"],
                                "Başlangıç": fmt_date(r["baslangic"]), "Bitiş": fmt_date(r["bitis"]), "İş günü": rules.fmt_days(r["is_gunu"])}
                               for r in reqs]), hide_index=True, width="stretch", height=min(38 + 35 * len(reqs), 260))
    k = lambda n: f"dlg_b{n}_{action}"  # noqa: E731
    note, tarih, alan = "", date.today(), ""
    if action in ("reddet", "iptal"):
        note = st.text_area("Gerekçe *", key=k("n"))
    elif action == "onayla":
        note = st.text_input("Not (isteğe bağlı)", key=k("n"))
    elif action == "teslim":
        c1, c2 = st.columns(2)
        tarih = c1.date_input("Teslim tarihi", value=date.today(), max_value=date.today(), format="DD.MM.YYYY", key=k("d"))
        alan = c2.text_input("Teslim alan İK personeli *", key=k("a"))
    elif action == "sap":
        c1, c2 = st.columns(2)
        tarih = c1.date_input("SAP giriş tarihi", value=date.today(), max_value=date.today(), format="DD.MM.YYYY", key=k("d"))
        note = c2.text_input("Not (isteğe bağlı)", key=k("n"))
    onay = st.checkbox(f"{len(reqs)} talebe '{label}' işlemini uygulamak istiyorum", key=k("c"))
    c1, c2 = st.columns(2)
    if c1.button("Vazgeç", key=k("x"), width="stretch"):
        st.rerun()
    if c2.button("Uygula", type="primary", key=k("go"), width="stretch"):
        if not onay:
            st.error("Devam etmek için onay kutusunu işaretleyin.")
            return
        if not user_ok():
            return
        try:
            res = services.bulk_apply(current_user(), current_role(), [r["id"] for r in reqs], action, note, tarih, alan)
        except Timeout:
            st.error("Veri dosyası şu an başka bir işlem tarafından kullanılıyor. Birkaç saniye sonra tekrar deneyin.")
            return
        except (ValueError, PermissionError, OSError) as exc:
            st.error(str(exc))
            return
        st.session_state.pop("_saglik", None)
        st.session_state["_bulk_report"] = {"label": label, **res}
        reset_table_selection()
        st.rerun()


def render_bulk_panel(selected_ids: list[str], filtered_ids: list[str]) -> None:
    """Tabloda seçilen ya da filtredeki tüm talepler için toplu işlem paneli."""
    avail = {a: v for a, v in config.BULK_ACTIONS.items() if can(v[1])}
    if not avail:
        return
    many = len(selected_ids) >= 2
    title = "⚡ Toplu işlem" + (f" — {len(selected_ids)} talep seçili" if selected_ids else "")
    with st.expander(title, expanded=many):
        scopes = [f"Tabloda seçilenler ({len(selected_ids)})", f"Filtredeki tüm talepler ({len(filtered_ids)})"]
        scope = st.radio("Kapsam", scopes, horizontal=True, index=0 if many else 1, key=f"bulk_scope_{many}",
                         help="Personel filtresiyle birkaç kişiyi seçip 'Filtredeki tüm talepler'i uygulayabilirsiniz.")
        ids = selected_ids if scope == scopes[0] else filtered_ids
        action = st.selectbox("İşlem", list(avail), format_func=lambda a: avail[a][0], key="bulk_action")
        by_id = {r["id"]: r for r in services.load_requests()}
        eligible = [i for i in ids if i in by_id and services.bulk_eligible(by_id[i], action)]
        st.caption(f"{len(ids)} talep → **{len(eligible)} uygun**, {len(ids) - len(eligible)} atlanacak "
                   "(bu işleme uygun durumda olmayanlar).")
        if eligible:
            st.dataframe(pd.DataFrame([{"Talep": i, "Personel": by_id[i]["personel_adi"], "Tür": by_id[i]["izin_turu"],
                                        "Başlangıç": fmt_date(by_id[i]["baslangic"]), "Bitiş": fmt_date(by_id[i]["bitis"]),
                                        "İş günü": rules.fmt_days(by_id[i]["is_gunu"])} for i in eligible[:100]]),
                         hide_index=True, width="stretch", height=min(38 + 35 * len(eligible[:100]), 240))
        if st.button(f"{avail[action][0]} — {len(eligible)} talebe uygula", type="primary", disabled=not eligible, key="bulk_go"):
            dialog_bulk(action, eligible)


def show_bulk_report() -> None:
    rep = st.session_state.pop("_bulk_report", None)
    if not rep:
        return
    if rep["ok"]:
        st.success(f"{rep['label']}: {len(rep['ok'])} talep işlendi.")
    if rep["failed"]:
        st.error(f"{len(rep['failed'])} talep işlenemedi.")
        with st.expander("Hata ayrıntıları", expanded=True):
            for tid, msg in rep["failed"].items():
                st.write(f"- **{tid}**: {msg}")


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
    deleted = bool(r["silindi"])
    approved = r["durum"] == S_APPROVED and not deleted

    st.subheader(f"{r['id']} · {r['personel_adi']}")
    if deleted:
        st.error(f"🗑️ **Silinmiş talep** — {r['silen']} · {fmt_dt(r['silinme_zamani'])}\n\nGerekçe: {r['silme_gerekcesi']}")
    st.markdown(f"**{status_label(r['durum'], deleted)}** · {r['izin_turu']} · {fmt_date(r['baslangic'])} – {fmt_date(r['bitis'])} "
                f"· **{rules.fmt_days(r['is_gunu'])} iş günü** ({r['gun_sayisi']} takvim günü) · {rules.sure_etiket(r)}")
    if r["aciklama"]:
        st.caption(f"Açıklama: {r['aciklama']}")
    render_steps(r)

    actions: list[tuple[str, str, Callable[[], None]]] = []
    if not deleted:
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
        if can("duzenle") and services.can_edit_request(r):
            actions.append(("✏️ Düzenle", "ed", lambda: dialog_edit(talep_id)))
        if can("ek_ekle"):
            actions.append(("📎 Ek ekle", "at", lambda: dialog_attach(talep_id)))
        if can("sil"):
            actions.append(("🗑️ Sil", "del", lambda: dialog_delete(talep_id)))
    elif can("sil"):
        def _restore() -> None:
            if run(services.restore_request, talep_id, ok_msg=f"{talep_id} geri yüklendi"):
                reset_table_selection()
                st.rerun()
        actions.append(("♻️ Geri yükle", "rs", _restore))
    for i in range(0, len(actions), 3):
        for col, (label, key, fn) in zip(st.columns(3), actions[i:i + 3]):
            if col.button(label, key=k(key), width="stretch"):
                fn()

    if approved:
        person = services.get_person(r["personel_adi"]) or {}
        c1, c2 = st.columns(2)
        vurl = verify.url_for(r)
        c1.download_button("📄 İzin formu (Word)", documents.build_docx(r, person, vurl), file_name=f"{r['id']}.docx",
                           mime=DOCX_MIME, key=k("docx"), width="stretch")
        pdf = documents.build_pdf(r, person, vurl)
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


# ----------------------------------------------------------------------------
# Parola kapısı, geri al paneli, doğrulama sayfası
# ----------------------------------------------------------------------------
def password_gate() -> None:
    """IZIN_APP_PASSWORD (secrets: app_password) tanımlıysa uygulamayı ortak parolayla korur."""
    pw = config.env("app_password")
    if not pw or st.session_state.get("_auth_ok"):
        return
    st.title("🔒 İzin Takip Sistemi")
    entered = st.text_input("Parola", type="password", key="_pw_input")
    if entered:
        if hmac.compare_digest(entered.encode("utf-8"), pw.encode("utf-8")):
            st.session_state["_auth_ok"] = True
            st.rerun()
        st.error("Parola hatalı.")
    st.stop()


def render_undo_panel() -> None:
    """Kenar çubuğu: son işlemleri (süre dolmadıysa) tek tıkla geri alır."""
    if not current_user():
        return
    items = services.recent_undoable(5)
    if not items:
        return
    with st.expander(f"↩️ Son işlemleri geri al ({len(items)})"):
        names = {r["id"]: r["personel_adi"] for r in services.load_requests(include_deleted=True)}
        for e in items:
            st.caption(f"**{e['islem']}** · {e['talep_id']} · {names.get(e['talep_id'], '')} — {e['kullanici']}, {fmt_dt(e['zaman'])}")
            if st.button("Geri al", key=f"undo_{e['log_id']}", width="stretch"):
                if run(services.undo_last, e["talep_id"], ok_msg=f"{e['talep_id']}: {e['islem']} geri alındı"):
                    reset_table_selection()
                    st.rerun()
        st.caption(f"Son {services.UNDO_WINDOW_MIN} dakika içindeki, sonradan başka işlem yapılmamış işlemler.")


def render_verification(talep_id: str, k: str) -> None:
    """İzin kağıdındaki QR kodun açtığı herkese açık doğrulama sayfası (parola kapısından önce çalışır)."""
    st.title("✅ İzin Belgesi Doğrulama")
    res = verify.check(talep_id, k)
    if res["sonuc"] == "gecerli":
        t = res["talep"]
        if t["durum"] == S_APPROVED:
            st.success("Belge **geçerli**: kayıtla eşleşiyor ve izin **onaylı**.")
        elif t["durum"] == S_PENDING:
            st.warning("Belge kayıtla eşleşiyor ancak izin henüz **onaylanmamış** (Beklemede).")
        else:
            st.error(f"Belge kayıtla eşleşiyor ancak izin artık **{t['durum']}**. Bu belge geçerli sayılmamalıdır.")
        st.markdown(f"**Talep no:** {t['id']}  \n**Personel:** {t['personel']}  \n**İzin türü:** {t['tur']}  \n"
                    f"**Tarih:** {fmt_date(t['baslangic'])} – {fmt_date(t['bitis'])}  \n**Güncel durum:** {t['durum']}"
                    + (f"  \n**Onaylayan:** {t['karar_veren']} ({fmt_dt(t['onay_zamani'])})" if t["karar_veren"] else "")
                    + (f"  \n**İK teslim tarihi:** {fmt_date(t['ik_teslim_tarihi'])}" if t["ik_teslim_tarihi"] else ""))
    elif res["sonuc"] == "eslesmiyor":
        st.error("Belgedeki kod kayıtla **eşleşmiyor**: belge sahte olabilir ya da talep, belge yazdırıldıktan sonra düzenlenmiş olabilir. "
                 "Lütfen İK / planlama ile görüşün.")
    else:
        st.error("Bu numarayla geçerli bir izin kaydı **bulunamadı**.")


# END-OF-FILE v3.2
