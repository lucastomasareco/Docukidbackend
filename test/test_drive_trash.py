"""
Papelera de documentos (v3.0): borrar manda el archivo a la papelera de Drive con sus datos, y
restaurar lo devuelve. Sin base ni red: Drive y la base son dobles de prueba.
"""
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

for _k in ("DATABASE_URL", "SUPABASE_JWKS_URL", "STATE_SECRET", "CRON_SECRET",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI", "GEMINI_API_KEY"):
    os.environ.setdefault(_k, {
        "DATABASE_URL": "postgresql+asyncpg://x:x@localhost/x",
        "SUPABASE_JWKS_URL": "https://example.invalid/.well-known/jwks.json",
    }.get(_k, "x"))

import pytest
from fastapi import HTTPException
from googleapiclient.errors import HttpError

from app import drive_trash, main


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "x"


def _error(status):
    return HttpError(_Resp(status), b"{}")


class _Req:
    def __init__(self, f):
        self.f = f

    def execute(self):
        return self.f()


class _Drive:
    def __init__(self):
        self.log, self.update_error, self.get_resp, self.list_pages = [], None, None, []

    def files(self):
        return self

    def update(self, **kw):
        self.log.append(kw)

        def run():
            if self.update_error:
                raise self.update_error
            return {"id": kw["fileId"], "mimeType": "application/pdf", "webViewLink": "https://drive/x"}
        return _Req(run)

    def get(self, **kw):
        def run():
            if isinstance(self.get_resp, Exception):
                raise self.get_resp
            return self.get_resp
        return _Req(run)

    def list(self, **kw):
        self.log.append(kw)
        return _Req(lambda: self.list_pages.pop(0))


def _doc(**k):
    base = dict(child_id=12, folder_id=3, name="Cartilla", type="salud", expiry_date=None, no_expiry=False,
                drive_file_id="F1")
    base.update(k)
    return SimpleNamespace(**base)


def test_propiedades_respetan_el_limite_de_drive_y_no_traen_vacios():
    p = drive_trash.propiedades_de_documento(_doc(expiry_date=date(2027, 3, 1)))
    assert p == {"dk_doc": "1", "dk_child": "12", "dk_folder": "3", "dk_name": "Cartilla",
                 "dk_type": "salud", "dk_exp": "2027-03-01"}
    p = drive_trash.propiedades_de_documento(_doc(folder_id=None, type=None, no_expiry=True, name="ñ" * 200))
    assert "dk_folder" not in p and "dk_type" not in p and p["dk_noexp"] == "1"
    assert all(len((k + v).encode()) <= 124 for k, v in p.items())


def test_mandar_a_papelera_es_una_sola_llamada_con_los_datos():
    d, props = _Drive(), {"dk_doc": "1", "dk_child": "12"}
    assert drive_trash.mandar_a_papelera_en(d, "F1", props) is True
    assert d.log == [{"fileId": "F1", "body": {"trashed": True, "appProperties": props}, "fields": "id"}]


def test_mandar_a_papelera_404_no_es_error_y_los_demas_si():
    d = _Drive()
    d.update_error = _error(404)
    assert drive_trash.mandar_a_papelera_en(d, "F1", {}) is False
    for estado in (429, 500, 403):
        d.update_error = _error(estado)
        with pytest.raises(HttpError):
            drive_trash.mandar_a_papelera_en(d, "F1", {})


def test_listar_filtra_vencidos_y_lo_que_vino_con_la_cuenta():
    ahora = datetime(2026, 11, 7, tzinfo=timezone.utc)

    def hace(n):
        return (ahora - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def f(id_, dias=None, explicito=True):
        r = {"id": id_, "name": id_, "explicitlyTrashed": explicito, "appProperties": {"dk_doc": "1"}}
        if dias is not None:
            r["trashedTime"] = hace(dias)
        return r

    d = _Drive()
    d.list_pages = [{"files": [f("A", 2), f("B", 40), f("C", 1, False), f("D")], "nextPageToken": "T"},
                    {"files": [f("E", 29)]}]
    salida = drive_trash.listar_papelera_en(d, ahora)
    assert [(x["drive_file_id"], x["days_left"]) for x in salida] == [("A", 28), ("D", 30), ("E", 1)]
    assert "appProperties has" in d.log[0]["q"] and "trashed = true" in d.log[0]["q"]


def test_solo_se_restaura_lo_que_esta_en_papelera_y_es_documento():
    d = _Drive()
    d.get_resp = {"id": "A", "trashed": True, "appProperties": {"dk_doc": "1"}}
    assert drive_trash.leer_de_papelera_en(d, "A")["id"] == "A"
    for resp in ({"id": "A", "trashed": False, "appProperties": {"dk_doc": "1"}},
                 {"id": "A", "trashed": True, "appProperties": {}},
                 _error(404)):
        d.get_resp = resp
        with pytest.raises(drive_trash.NoRestaurable):
            drive_trash.leer_de_papelera_en(d, "A")


def test_restaurar_es_una_llamada_que_saca_de_la_papelera_y_va_a_la_carpeta_viva(monkeypatch):
    monkeypatch.setattr(drive_trash, "_asegurar_carpeta_hijo", lambda d, cid, n, g: "HIJO_VIVO")
    monkeypatch.setattr(drive_trash, "_asegurar_subcarpeta_de_hijo", lambda d, h, n, g: "SUB_VIVA")
    d = _Drive()
    r = drive_trash.restaurar_en(d, {"id": "A", "parents": ["VIEJA"]}, 12, "Ana", "H0", "Salud", "S0")
    assert len(d.log) == 1
    assert d.log[0]["body"] == {"trashed": False}
    assert d.log[0]["addParents"] == "SUB_VIVA" and d.log[0]["removeParents"] == "VIEJA"
    assert r["hijo_vigente"] == "HIJO_VIVO" and r["sub_vigente"] == "SUB_VIVA"

    d = _Drive()
    d.update_error = _error(404)
    with pytest.raises(drive_trash.NoRestaurable):
        drive_trash.restaurar_en(d, {"id": "A", "parents": []}, 12, "Ana", "H0", None, None)


# ---- DELETE /documents/{id} ----

def _borrar(monkeypatch, papelera):
    anotado = {"fila_borrada": False, "propiedades": None}
    documento = _doc()

    async def get_doc(db, doc_id, user_id):
        return documento

    async def delete_doc(db, d):
        anotado["fila_borrada"] = True

    def a_papelera(usuario, drive_file_id, propiedades):
        anotado["propiedades"] = propiedades
        return papelera(drive_file_id)

    monkeypatch.setattr(main.crud, "get_document_owned_by_user", get_doc)
    monkeypatch.setattr(main.crud, "delete_document", delete_doc)
    monkeypatch.setattr(main, "mandar_documento_a_papelera", a_papelera)
    usuario = SimpleNamespace(id="u1")
    respuesta = asyncio.run(main.borrar_documento(doc_id=1, usuario=usuario, db=None))
    return respuesta, anotado


def test_borrar_documento_lo_manda_a_la_papelera_con_sus_datos(monkeypatch):
    respuesta, anotado = _borrar(monkeypatch, lambda fid: True)
    assert respuesta.message == "DeletedToTrash"
    assert anotado["fila_borrada"]
    assert anotado["propiedades"]["dk_child"] == "12" and anotado["propiedades"]["dk_type"] == "salud"


def test_borrar_documento_cuyo_archivo_ya_no_existe_borra_igual_sin_papelera(monkeypatch):
    respuesta, anotado = _borrar(monkeypatch, lambda fid: False)
    assert respuesta.message == "Deleted" and anotado["fila_borrada"]


def test_si_drive_falla_el_registro_no_se_borra(monkeypatch):
    def falla(fid):
        raise _error(500)
    with pytest.raises(HttpError):
        _borrar(monkeypatch, falla)
