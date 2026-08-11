"""
Coleta de dados de recebimento PA via API WMS ION PRD.
Estratégias de descoberta de receipts:
  1. GET /exports?type=ASNCOMPLETED  -> receiptkeys de fechados/recebidos
  2. POST inventorybalance/showinventorybalancelist por STG.PA.* -> pendentes/em andamento
  3. Cache local de keys conhecidas
"""
import os
import sys
import json
import math
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from wms_config import CONFIG  # noqa: E402 — suporte a env vars (cloud) e config.py (local)

log = logging.getLogger(__name__)

# Depósitos de fábrica e seus stages de recebimento
DEPOSITOS = {
    "308": {"nome": "Depósito 308", "zona": "DEP.308_FB", "stages": ["STG.PA.308.001"]},
    "309": {"nome": "Depósito 309", "zona": "DEP.309_FB", "stages": ["STG.PA.309.001", "STG.PA.309.002"]},
    "310": {"nome": "Depósito 310", "zona": "DEP.310_FB", "stages": ["STG.PA.310.001"]},
    "311": {"nome": "Depósito 311", "zona": "DEP.311_FB", "stages": ["STG.PA.311.001"]},
    "312": {"nome": "Depósito 312", "zona": "DEP.312_FB", "stages": ["STG.PA.312.001"]},
    "313": {"nome": "Depósito 313", "zona": "DEP.313_FB", "stages": ["STG.PA.313.001"]},
    "314": {"nome": "Depósito 314", "zona": "DEP.314_FB", "stages": ["STG.PA.314.001"]},
    "315": {"nome": "Depósito 315", "zona": "DEP.315_FB", "stages": ["STG.PA.315.001"]},
    "316": {"nome": "Depósito 316", "zona": "DEP.316_FB", "stages": ["STG.PA.316.001"]},
    "317": {"nome": "Depósito 317", "zona": "DEP.317_FB", "stages": ["STG.PA.317.001"]},
    "318": {"nome": "Depósito 318", "zona": "DEP.318_FB", "stages": ["STG.PA.318.001"]},
    "321": {"nome": "Depósito 321", "zona": "DEP.321_FB", "stages": ["STG.PA.321.001"]},
    "323": {"nome": "Depósito 323", "zona": "DEP.323_FB", "stages": []},
    "339": {"nome": "Depósito 339", "zona": "DEP.339_FB", "stages": []},
}

STATUS_MAP = {
    "0":  "pendente",         # Novo
    "2":  "pendente",         # Em trânsito
    "3":  "no_recebimento",   # Pré-recebido
    "4":  "pendente",         # Programado
    "5":  "no_recebimento",   # No recebimento
    "9":  "recebido",         # Recebido
    "11": "fechado",          # Fechado
    "15": "fechado",          # Verificado fechado
    "20": "cancelado",        # Cancelado
    "21": "recebido",         # RNERP
}

STATUS_LABEL = {
    "pendente":        "Pendente",
    "no_recebimento":  "No Recebimento",
    "recebido":        "Recebido",
    "fechado":         "Fechado",
}

STATUS_ORDER = ["pendente", "no_recebimento", "recebido", "fechado", "cancelado"]

_BRT = timezone(timedelta(hours=-3))
HOJE = datetime.now(_BRT).date()


def _hoje_str():
    return datetime.now(_BRT).date().isoformat()


def _meses_validos() -> set:
    """Retorna YYYY-MM do mês atual e do mês anterior (janela do backlog)."""
    hoje = datetime.now(_BRT).date()
    mes_atual = hoje.strftime("%Y-%m")
    if hoje.month == 1:
        mes_anterior = f"{hoje.year - 1}-12"
    else:
        mes_anterior = f"{hoje.year}-{hoje.month - 1:02d}"
    return {mes_atual, mes_anterior}


class WMSClient:
    def __init__(self):
        self._token = None
        self._token_ts = 0
        self._lock = threading.Lock()

    def _renovar_token(self):
        r = requests.post(CONFIG["token_url"], data={
            "grant_type":    "password",
            "client_id":     CONFIG["ci"],
            "client_secret": CONFIG["cs"],
            "username":      CONFIG["saak"],
            "password":      CONFIG["sask"],
        }, verify=False, timeout=30)
        r.raise_for_status()
        self._token = r.json()["access_token"]
        self._token_ts = time.time()
        log.debug("Token renovado.")

    @property
    def headers(self):
        with self._lock:
            if not self._token or (time.time() - self._token_ts) > 3000:
                self._renovar_token()
            return {"Authorization": f"Bearer {self._token}"}

    def _base(self):
        return f"{CONFIG['base_url']}/{CONFIG['warehouse']}"

    def get(self, path, params=None, timeout=30):
        url = f"{self._base()}/{path}"
        r = requests.get(url, headers=self.headers, params=params, verify=False, timeout=timeout)
        if r.status_code == 401:
            with self._lock:
                self._token = None
            r = requests.get(url, headers=self.headers, params=params, verify=False, timeout=timeout)
        return r

    def post(self, path, params=None, body=None, timeout=30):
        url = f"{self._base()}/{path}"
        r = requests.post(url, headers=self.headers, params=params, json=body or {}, verify=False, timeout=timeout)
        if r.status_code == 401:
            with self._lock:
                self._token = None
            r = requests.post(url, headers=self.headers, params=params, json=body or {}, verify=False, timeout=timeout)
        return r

    def get_asn(self, receiptkey):
        """GET /advancedshipnotice/{receiptkey} — endpoint correto do WMS para ASNs."""
        r = self.get(f"advancedshipnotice/{receiptkey}", params={"expand": "receiptdetails"}, timeout=45)
        if r.status_code == 200:
            return r.json()
        return None

    def get_asn_by_externkey(self, externkey):
        """GET /advancedshipnotice/externreceiptkey/{externkey}"""
        r = self.get(f"advancedshipnotice/externreceiptkey/{externkey}", params={"expand": "receiptdetails"}, timeout=45)
        if r.status_code == 200:
            return r.json()
        return None

    def get_exports(self, tipo="ASNCOMPLETED", limit=200):
        r = self.get("exports", params={"type": tipo, "restrictrowsto": limit}, timeout=60)
        if r.status_code == 200:
            return r.json() if isinstance(r.json(), list) else []
        return []

    def get_inventory_stage(self, loc, page_size=500):
        r = self.post(
            "inventorybalance/showinventorybalancelist",
            params={"recordcount": page_size, "loc": loc, "owner": "BURN"},
            timeout=45,
        )
        if r.status_code == 200 and isinstance(r.json(), list):
            return r.json()
        return []

    def get_pack(self, packkey):
        r = self.get(f"packs/{packkey}", timeout=20)
        if r.status_code == 200:
            return r.json()
        return {}

    def list_receipts_by_date(self, date_str: str, page_size=500):
        """Lista receipts por adddate OU receiptdate — tenta ambos silenciosamente."""
        found = {}
        for param in ("receiptdate", "adddate", "scheddate"):
            try:
                r = self.get(
                    "receipts",
                    params={"storerkey": "BURN", param: date_str, "recordcount": page_size},
                    timeout=60,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        for rec in data:
                            k = rec.get("receiptkey") or ""
                            if k:
                                found[k] = rec
                    elif isinstance(data, dict) and data.get("receiptkey"):
                        found[data["receiptkey"]] = data
            except Exception:
                pass
        return list(found.values())

    def list_asn_by_type(self, receipttype="8", page_size=500, max_pages=40) -> dict:
        """
        Lista ASNs por receipttype, com paginação automática.
        Retorna dict: {receiptkey: status_raw_str} para permitir filtragem sem fetch adicional.
        """
        result = {}   # receiptkey → status (string)
        for offset in range(0, max_pages * page_size, page_size):
            params = {
                "storerkey":      "BURN",
                "receipttype":    receipttype,
                "recordcount":    page_size,
                "startingrecord": offset,
            }
            try:
                r = self.get("advancedshipnotice", params=params, timeout=120)
                if r.status_code != 200:
                    break
                data = r.json()
                if isinstance(data, list):
                    for rec in data:
                        k = rec.get("receiptkey") or ""
                        if k:
                            result[k] = str(rec.get("status") or "")
                    if len(data) < page_size:
                        break
                elif isinstance(data, dict):
                    k = data.get("receiptkey") or ""
                    if k:
                        result[k] = str(data.get("status") or "")
                    break
                else:
                    break
            except Exception as e:
                log.warning(f"list_asn_by_type erro (offset={offset}): {e}")
                break
        abertos   = sum(1 for s in result.values() if s not in ("11","15","20"))
        fechados  = len(result) - abertos
        log.info(f"list_asn_by_type(type={receipttype}): {len(result)} total ({abertos} abertos, {fechados} fechados).")
        return result

    def list_asn_by_date(self, date_str: str, page_size=500):
        """Lista ASNs abertas por data via endpoint advancedshipnotice."""
        found = {}
        for param in ("adddate", "receiptdate"):
            try:
                r = self.get(
                    "advancedshipnotice",
                    params={"storerkey": "BURN", param: date_str, "recordcount": page_size},
                    timeout=60,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        for rec in data:
                            k = rec.get("receiptkey") or ""
                            if k:
                                found[k] = rec
                    elif isinstance(data, dict) and data.get("receiptkey"):
                        found[data["receiptkey"]] = data
            except Exception:
                pass
        return list(found.values())


def _deposito_from_loc(loc: str) -> str | None:
    """Extrai número do depósito de qualquer campo de localização (ex: STG.PA.308.001 → '308')."""
    if not loc:
        return None
    for dep in DEPOSITOS:
        if dep in loc:
            return dep
    return None


def _deposito_from_receiptkey(receiptkey: str) -> str | None:
    """Tenta inferir depósito do código do receipt."""
    for dep in DEPOSITOS:
        if dep in (receiptkey or ""):
            return dep
    return None


def _qty_por_palete(pack: dict) -> float:
    """Retorna qty/palete usando palletti × pallethi do cadastro de embalagem (packs API).
    Retorna 0 se o pack não tiver as dimensões de palete configuradas no WMS."""
    ti = float(pack.get("palletti") or 0)
    hi = float(pack.get("pallethi") or 0)
    if ti > 0 and hi > 0:
        return ti * hi
    return 0.0


def _split_paletes(valor: float):
    inteiros = int(valor)
    fracao   = 1 if (valor - inteiros) > 0.01 else 0
    return inteiros, fracao


def _calcula_paletes(details: list, pack_cache: dict) -> dict:
    """
    Calcula paletes previstos e recebidos usando a API de embalagens (packs).

    Fluxo principal (pack configurado no WMS):
      qpp = palletti × pallethi  (via GET /packs/{packkey})
      pal_prev = total_qtyexpected / qpp
      pal_rec  = total_qtyreceived / qpp

    Fallback (pack sem dimensões de palete configuradas no WMS):
      qpp estimado = mediana das qtyreceived das linhas recebidas
      pal_rec      = contagem de linhas com qtyreceived > 0 (1 linha = 1 palete escaneado)
    """
    total_prev     = 0.0
    total_rec      = 0.0
    qpp_global     = 0.0     # qpp do pack (palletti × pallethi)
    qtys_recebidas = []      # para estimativa de qpp quando pack não configurado

    for d in details:
        qty_prev = float(d.get("qtyexpected") or 0)
        qty_rec  = float(d.get("qtyreceived") or 0)
        total_prev += qty_prev
        total_rec  += qty_rec

        # Lê qpp do cadastro de embalagem (pack) via cache já populado pela API
        packkey = d.get("packkey") or ""
        if packkey and qpp_global == 0.0:
            pack = pack_cache.get(packkey, {})
            qpp  = _qty_por_palete(pack)   # palletti × pallethi — 0 se não configurado
            if qpp > 0:
                qpp_global = qpp

        if qty_rec > 0:
            qtys_recebidas.append(qty_rec)

    # Pack configurado com palletti × pallethi → usa diretamente
    if qpp_global > 0:
        pal_prev_f = round(total_prev / qpp_global, 2)
        pal_rec_f  = round(total_rec  / qpp_global, 2)
        qpp_source = "pack"
    elif qtys_recebidas:
        # Pack sem configuração de palete: estima qpp pela mediana das qtds recebidas
        qtys_sorted = sorted(qtys_recebidas)
        qpp_global  = qtys_sorted[len(qtys_sorted) // 2]
        pal_prev_f  = round(total_prev / qpp_global, 2) if qpp_global > 0 else 0.0
        pal_rec_f   = float(len(qtys_recebidas))        # 1 linha = 1 palete escaneado
        qpp_source  = "estimado"
    else:
        pal_prev_f = 0.0
        pal_rec_f  = 0.0
        qpp_source = "indisponivel"

    prev_int, prev_frac = _split_paletes(pal_prev_f)
    rec_int,  rec_frac  = _split_paletes(pal_rec_f)
    diferenca = max(0.0, round(pal_prev_f - pal_rec_f, 2))

    return {
        "total_previsto":       round(total_prev, 2),
        "total_recebido":       round(total_rec, 2),
        "qpp":                  round(qpp_global, 2),
        "qpp_source":           qpp_source,
        # Previstos
        "paletes_previstos":    pal_prev_f,
        "paletes_inteiros":     prev_int,
        "paletes_fracao":       prev_frac,
        "paletes_total":        prev_int + prev_frac,
        # Recebidos
        "paletes_recebidos":    pal_rec_f,
        "paletes_rec_inteiros": rec_int,
        "paletes_rec_fracao":   rec_frac,
        "paletes_rec_total":    rec_int + rec_frac,
        # Em recebimento (diferença previsto − recebido)
        "diferenca_paletes":    diferenca,
    }


TIPO_ORDEM_PRODUCAO = {"8"}  # API WMS: type=8 = Ordem de Produção (UI mostra "10")


def _is_ordem_producao(receipt: dict) -> bool:
    """Retorna True se a ASN é do tipo Ordem de Produção (type=8 na API WMS)."""
    tipo = str(receipt.get("type") or receipt.get("receipttype") or
               receipt.get("receipttypecode") or "").strip()
    return tipo in TIPO_ORDEM_PRODUCAO


def _norm_date(val) -> str:
    """Normaliza data WMS para YYYY-MM-DD, aceitando ISO e formato americano MM/DD/YYYY."""
    s = str(val or "").strip()
    if not s:
        return ""
    # ISO: "2026-08-10" ou "2026-08-10T10:30:00"
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    # Americano: "08/10/2026" ou "08/10/2026 10:30 AM"
    if len(s) >= 10 and s[2:3] == "/":
        parts = s[:10].split("/")
        if len(parts) == 3:
            return f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}"
    return s[:10]


def _receipt_to_dict(receipt: dict, pack_cache: dict) -> dict:
    """Normaliza um receipt da API para estrutura interna."""
    details = receipt.get("receiptdetails") or []
    status_raw = str(receipt.get("status") or "0")
    status = STATUS_MAP.get(status_raw, "pendente")

    # Identificar depósito — SupplierCode é o campo primário no WMS
    deposito = None
    for field in ("SupplierCode", "supplierCode", "suppliercode", "supplierKey", "supplierkey"):
        val = str(receipt.get(field) or "").strip()
        if val:
            dep = _deposito_from_loc(val)
            if dep:
                deposito = dep
                break
            # SupplierCode pode ser exatamente o número (ex: "308", "309")
            if val in DEPOSITOS:
                deposito = val
                break
    if not deposito:
        # Fallback: toloc → fromloc das linhas
        for d in details:
            dep = _deposito_from_loc(d.get("toloc") or "")
            if not dep:
                dep = _deposito_from_loc(d.get("fromloc") or "")
            if dep:
                deposito = dep
                break
    if not deposito:
        # Fallback: outros campos do cabeçalho
        for field in ("referencelocation", "susr1", "susr2", "susr3", "susr4", "susr5"):
            dep = _deposito_from_loc(str(receipt.get(field) or ""))
            if dep:
                deposito = dep
                break
    if not deposito:
        deposito = _deposito_from_receiptkey(receipt.get("receiptkey") or "")
    if not deposito:
        deposito = _deposito_from_receiptkey(receipt.get("externreceiptkey") or "")

    add_dt   = _norm_date(receipt.get("adddate"))
    close_dt = _norm_date(receipt.get("closeddate"))
    rec_dt   = _norm_date(receipt.get("receiptdate"))
    edit_dt  = _norm_date(
        receipt.get("editdate") or receipt.get("lastmoddate") or
        receipt.get("updatedate") or receipt.get("modifieddate") or ""
    )

    paletes = _calcula_paletes(details, pack_cache)

    # Status derivado espelha diretamente o status WMS conforme mapeamento do usuário:
    # 0=Pendente | 5=No Recebimento | 9=Recebido | 11=Fechado
    total_rec  = paletes["total_recebido"]
    if status_raw in ("11", "15"):
        status_derivado = "fechado"
    elif status_raw == "20":
        status_derivado = "cancelado"
    elif status_raw in ("9", "21"):
        status_derivado = "recebido"   # WMS=Recebido → sempre Recebido
    elif status_raw in ("3", "5"):
        status_derivado = "no_recebimento"
    elif total_rec > 0:
        status_derivado = "no_recebimento"  # iniciou recebimento sem status 5/9
    else:
        status_derivado = STATUS_MAP.get(status_raw, "pendente")

    linhas = []
    for det in details:
        packkey_det = det.get("packkey") or det.get("sku") or ""
        pack_det    = pack_cache.get(packkey_det, {})
        qpp_det     = _qty_por_palete(pack_det)
        qty_prev_det = float(det.get("qtyexpected") or 0)
        qty_rec_det  = float(det.get("qtyreceived") or 0)
        linhas.append({
            "sku":              det.get("sku") or "",
            "linha":            det.get("receiptlinenumber") or "",
            "qty_previsto":     qty_prev_det,
            "qty_recebido":     qty_rec_det,
            "qty_por_palete":   round(qpp_det, 2),
            "pal_previsto":     round(qty_prev_det / qpp_det, 2) if qpp_det > 0 else None,
            "pal_recebido":     round(qty_rec_det  / qpp_det, 2) if qpp_det > 0 else None,
            "pal_diferenca":    round((qty_prev_det - qty_rec_det) / qpp_det, 2) if qpp_det > 0 else None,
            "uom":              det.get("uom") or "",
            "packkey":          packkey_det,
            "toloc":            det.get("toloc") or "",
            "fromloc":          det.get("fromloc") or "",
            "lote_ref":         det.get("lottable01") or "",
            "nf":               det.get("lottable02") or "",
            "lote_forn":        det.get("lottable03") or "",
            "vencimento":       (det.get("lottable05") or "")[:10],
            "data_receb":       (det.get("datereceived") or "")[:10],
            "status":           STATUS_MAP.get(str(det.get("status") or "0"), "pendente"),
            "condcode":         det.get("conditioncode") or "",
        })

    supplier_code = (
        receipt.get("SupplierCode") or receipt.get("supplierCode") or
        receipt.get("suppliercode") or receipt.get("supplierKey") or ""
    )
    receipttype = str(receipt.get("receipttype") or receipt.get("type") or
                      receipt.get("receipttypecode") or "").strip()

    # Nome descritivo do expedidor (fornecedor/fábrica de origem)
    supplier_name = (
        receipt.get("suppliername") or receipt.get("SupplierName") or
        receipt.get("supplierName") or receipt.get("SupplierDesc") or
        receipt.get("supplierdesc") or receipt.get("vendorname") or
        receipt.get("VendorName") or ""
    )
    # Fallback: nome do depósito configurado localmente
    if not supplier_name and deposito:
        supplier_name = DEPOSITOS.get(deposito, {}).get("nome", "")

    # Usuário que fez a última atualização
    editwho = (
        receipt.get("editwho") or receipt.get("editWho") or
        receipt.get("EditWho") or receipt.get("editedby") or
        receipt.get("lastupdatedby") or receipt.get("modifiedby") or
        receipt.get("addwho") or receipt.get("addWho") or ""
    )

    return {
        "receiptkey":        receipt.get("receiptkey") or "",
        "externkey":         receipt.get("externreceiptkey") or "",
        "supplier_code":     supplier_code,
        "supplier_name":     supplier_name,
        "editwho":           editwho,
        "receipttype":       receipttype,
        "status_raw":        status_raw,
        "status":            status_derivado,
        "status_wms":        status,
        "status_label":      STATUS_LABEL.get(status_derivado, status_derivado),
        "deposito":          deposito,
        "data_criacao":      add_dt,
        "data_recebimento":  rec_dt,
        "data_fechamento":   close_dt,
        "data_atualizacao":  edit_dt or add_dt,  # fallback para adddate se editdate vazio
        "n_linhas":          len(details),
        "paletes":           paletes,
        "linhas":            linhas,
    }


class ReceiptCollector:
    """Thread de coleta periódica dos recebimentos PA."""

    # Usa disco persistente do Render (/var/data) se disponível; senão diretório local
    _DATA_DIR       = "/var/data" if os.path.isdir("/var/data") else "."
    CACHE_FILE      = os.path.join(_DATA_DIR, "receipt_keys_cache.json")
    DATA_CACHE_FILE = os.path.join(_DATA_DIR, "receipt_data_cache.json")

    def __init__(self, intervalo: int = 30):
        self._client     = WMSClient()
        self._intervalo  = intervalo
        self._lock       = threading.Lock()
        self._receipts   = {}        # receiptkey → dict normalizado
        self._pack_cache = {}        # packkey → pack dict
        self._known_keys = set()     # receiptkeys conhecidos
        self._ultima_atualizacao = None
        self._erro       = None
        self._thread     = None
        self._running    = False
        self._first_scan_done    = False
        self._range_scan_running = False
        self._range_scan_done    = False
        self._load_cache()

    # ------------------------------------------------------------------ cache

    def _load_cache(self):
        """Carrega dados completos do disco (sobrevive a restarts, não a deploys)."""
        # 1. Tenta carregar dados completos
        try:
            with open(self.DATA_CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self._receipts   = data.get("receipts", {})
            self._known_keys = set(data.get("keys", []))
            self._ultima_atualizacao = data.get("ultima_atualizacao")
            log.info(f"Cache de dados carregado: {len(self._receipts)} receipts, {len(self._known_keys)} chaves.")
            return
        except Exception:
            pass
        # 2. Fallback: só as chaves conhecidas
        try:
            with open(self.CACHE_FILE) as f:
                self._known_keys = set(json.load(f))
            log.info(f"Cache de chaves carregado: {len(self._known_keys)} chaves.")
        except Exception:
            self._known_keys = set()

    def _save_cache(self):
        try:
            with self._lock:
                receipts_snap = dict(self._receipts)
                keys_snap     = list(self._known_keys)
                ts            = self._ultima_atualizacao
            with open(self.DATA_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"receipts": receipts_snap, "keys": keys_snap,
                           "ultima_atualizacao": ts}, f, ensure_ascii=False)
            with open(self.CACHE_FILE, "w") as f:
                json.dump(keys_snap, f)
        except Exception as e:
            log.warning(f"Erro ao salvar cache: {e}")

    # ------------------------------------------------------------------ discovery

    def _discover_from_exports(self):
        """Descobre receiptkeys via listagem direta + exports."""
        keys = set()

        # 1. Listagem direta — sem filtro de data, pega os mais recentes (recordcount alto)
        for path in ("advancedshipnotice", "receipts"):
            for params in (
                {"storerkey": "BURN", "recordcount": 500},
                {"storerkey": "BURN", "status": "9", "recordcount": 200},   # Recebido
                {"storerkey": "BURN", "status": "5", "recordcount": 200},   # No recebimento
                {"storerkey": "BURN", "status": "3", "recordcount": 200},   # Pré-recebido
                {"storerkey": "BURN", "status": "4", "recordcount": 200},   # Programado
            ):
                try:
                    r = self._client.get(path, params=params, timeout=60)
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            for rec in data:
                                k = rec.get("receiptkey") or ""
                                if k:
                                    keys.add(k.strip())
                        elif isinstance(data, dict) and data.get("receiptkey"):
                            keys.add(data["receiptkey"].strip())
                except Exception as e:
                    log.debug(f"Listagem {path} {params}: {e}")

        # 2. POST showreceiptlist / showadvancedshipnoticelist
        for path in ("receipts/showreceiptlist", "advancedshipnotice/showadvancedshipnoticelist"):
            for body in (
                {"storerkey": "BURN", "recordcount": 500},
                {"storerkey": "BURN", "status": "9", "recordcount": 200},
            ):
                try:
                    r = self._client.post(path, body=body, timeout=60)
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            for rec in data:
                                k = rec.get("receiptkey") or ""
                                if k:
                                    keys.add(k.strip())
                except Exception as e:
                    log.debug(f"POST {path}: {e}")

        # 3. Exports ASNCOMPLETED + ASN (fallback — podem trazer dados antigos)
        for tipo in ("ASNCOMPLETED", "ASN"):
            try:
                events = self._client.get_exports(tipo=tipo, limit=500)
                for ev in events:
                    k = ev.get("key1") or ev.get("key2") or ev.get("receiptkey") or ""
                    if k:
                        keys.add(k.strip())
            except Exception as e:
                log.warning(f"Exports {tipo}: {e}")

        # 4. Listagem por data — últimos 7 dias (silencioso se não suportado)
        for delta in range(7):
            dt = (date.today() - timedelta(days=delta)).isoformat()
            try:
                for rec in self._client.list_receipts_by_date(dt):
                    k = rec.get("receiptkey") or ""
                    if k:
                        keys.add(k.strip())
            except Exception:
                pass
            try:
                for rec in self._client.list_asn_by_date(dt):
                    k = rec.get("receiptkey") or ""
                    if k:
                        keys.add(k.strip())
            except Exception:
                pass

        log.info(f"Descoberta: {len(keys)} chaves encontradas.")
        return keys

    def _discover_from_range_scan(self, already_discovered: set | None = None,
                                   max_workers: int = 20) -> set:
        """
        Varre receiptkeys sequenciais para descobrir ASNs.
        Usa probing paralelo (max_workers) com timeout reduzido para ser rápido.

        Janela:
          • Bootstrap (primeira execução): max_base + 1500
          • Incremental (ciclos seguintes): max_base + 50
        """
        all_refs = self._known_keys | (already_discovered or set())
        max_base = 0

        base_known_subs: dict[int, set[int]] = defaultdict(set)
        for rk in all_refs:
            parts = rk.split(".")
            if len(parts) >= 2:
                try:
                    b = int(parts[0]); s = int(parts[1])
                    base_known_subs[b].add(s)
                    if b > max_base:
                        max_base = b
                except (ValueError, IndexError):
                    pass

        BASE_MINIMO_2026 = 73000  # receipts 2026 começam ~75000; varredura cobre margem abaixo
        if max_base < BASE_MINIMO_2026:
            max_base = BASE_MINIMO_2026

        if not self._first_scan_done:
            scan_start = max_base            # parte do mínimo absoluto
            scan_end   = max_base + 6000     # cobre toda a faixa do ano (~6000 bases)
            self._first_scan_done = True
            log.info(f"Range scan BOOTSTRAP paralelo ({max_workers} workers): bases {scan_start}-{scan_end}")
        else:
            scan_start = max(1, max_base - 300)
            scan_end   = max_base + 200      # incremental: busca novos além do máximo conhecido
            log.info(f"Range scan incremental paralelo ({max_workers} workers): bases {scan_start}-{scan_end}")

        bases = list(range(scan_end, scan_start - 1, -1))
        client = self._client

        def probe_and_scan_base(base):
            local_keys = set()
            known_subs = sorted(base_known_subs.get(base, set()))
            probe_subs = [1]
            if known_subs and known_subs[0] > 1:
                probe_subs.append(known_subs[0])
            if known_subs:
                next_sub = known_subs[-1] + 1
                if next_sub not in probe_subs:
                    probe_subs.append(next_sub)

            anchor_sub = None
            for ps in probe_subs:
                rk_ps = f"{base}.{ps}"
                try:
                    r = client.get(f"advancedshipnotice/{rk_ps}", timeout=4)
                except Exception:
                    continue
                if r.status_code == 200:
                    # Filtra por tipo antes de varrer os subs — pula base inteiro se não é OP
                    try:
                        data = r.json()
                        tipo = str(
                            data.get("receipttype") or data.get("type") or
                            data.get("receipttypecode") or ""
                        ).strip()
                        if tipo and tipo not in TIPO_ORDEM_PRODUCAO:
                            return local_keys  # base inteiro não é tipo 8 — ignora
                    except Exception:
                        pass  # JSON inválido: prossegue e filtra no fetch_and_store
                    local_keys.add(rk_ps)
                    anchor_sub = ps
                    break

            if anchor_sub is None:
                return local_keys

            consec_miss   = 0
            max_known_sub = known_subs[-1] if known_subs else anchor_sub
            sub = 1
            while True:  # sub pode ter 3 dígitos (até .999)
                rk = f"{base}.{sub}"
                if rk in local_keys:
                    sub += 1
                    consec_miss = 0
                    continue
                try:
                    r2 = client.get(f"advancedshipnotice/{rk}", timeout=4)
                    if r2.status_code == 200:
                        local_keys.add(rk)
                        consec_miss = 0
                        if sub > max_known_sub:
                            max_known_sub = sub
                    else:
                        consec_miss += 1
                        if consec_miss >= 5 and sub > max(anchor_sub, max_known_sub):
                            break
                except Exception:
                    consec_miss += 1
                    if consec_miss >= 5 and sub > max(anchor_sub, max_known_sub):
                        break
                sub += 1
            return local_keys

        keys: set = set()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(probe_and_scan_base, base): base for base in bases}
            for fut in as_completed(futures):
                try:
                    keys.update(fut.result())
                except Exception as e:
                    log.debug(f"Range scan worker erro: {e}")

        log.info(f"Range scan concluído: {len(keys)} chaves descobertas.")
        return keys

    def _discover_from_stages(self):
        """
        Busca em cada STG.PA.* por itens com lottable01 contendo receiptkey.
        Retorna set de keys descobertas.
        """
        keys = set()
        all_stages = []
        for dep_info in DEPOSITOS.values():
            all_stages.extend(dep_info.get("stages", []))

        for loc in all_stages:
            try:
                items = self._client.get_inventory_stage(loc)
                for item in items:
                    # lottable01 geralmente contém receiptkey ou referência
                    lt1 = item.get("lottable01") or ""
                    if lt1 and lt1.strip():
                        keys.add(lt1.strip())
                    # Também tentar o campo lot
                    lt = item.get("lot") or ""
                    if lt and lt.strip():
                        keys.add(lt.strip())
            except Exception as e:
                log.warning(f"Erro ao buscar stage {loc}: {e}")

        return keys

    # ------------------------------------------------------------------ mini range scan

    def _scan_bases_quick(self, bases: list, max_workers: int = 10, timeout: int = 3) -> set:
        """
        Sonda rapidamente uma lista de bases e retorna receiptkeys tipo 8 encontradas.
        Usado para mini scan incremental (ranges pequenos, ~20 bases).
        Render→WMS ~200ms/req, 10 workers: ~400ms para 20 bases.
        """
        client = self._client

        def probe(base):
            local = set()
            try:
                r = client.get(f"advancedshipnotice/{base}.1", timeout=timeout)
            except Exception:
                return local
            if r.status_code != 200:
                return local
            try:
                data = r.json()
                tipo = str(data.get("receipttype") or data.get("type") or "").strip()
                if tipo and tipo not in TIPO_ORDEM_PRODUCAO:
                    return local  # não é tipo 8
            except Exception:
                pass
            local.add(f"{base}.1")
            miss = 0
            sub = 2
            while miss < 5:  # sub pode ter 3 dígitos (até .999)
                try:
                    r2 = client.get(f"advancedshipnotice/{base}.{sub}", timeout=timeout)
                    if r2.status_code == 200:
                        local.add(f"{base}.{sub}")
                        miss = 0
                    else:
                        miss += 1
                except Exception:
                    miss += 1
                sub += 1
            return local

        found = set()
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(probe, b): b for b in bases}
            for fut in as_completed(futures):
                try:
                    found.update(fut.result())
                except Exception:
                    pass
        return found

    # ------------------------------------------------------------------ pack cache

    def _ensure_pack(self, packkey: str):
        if packkey and packkey not in self._pack_cache:
            try:
                pack = self._client.get_pack(packkey)
                if pack:
                    self._pack_cache[packkey] = pack
            except Exception:
                self._pack_cache[packkey] = {}

    # ------------------------------------------------------------------ full refresh

    # Status WMS considerados "abertos" — precisam de re-fetch a cada ciclo
    _STATUS_WMS_ABERTO = {"0", "2", "3", "4", "5", "9", "21"}
    # Status WMS "fechados/cancelados" — não mudam mais, não precisam de re-fetch
    _STATUS_WMS_FECHADO = {"11", "15", "20"}

    def _refresh(self):
        """
        Dois modos de operação:

        STARTUP (sem histórico em memória):
          Tenta descobrir via list_asn_by_type + exports + stages + cache local.
          O WMS BURN retorna 405 para todas as queries de lista, então na prática
          só o cache local e os stages têm algum resultado. A carga real do histórico
          é feita localmente pelo script carga_inicial_asn.py.

        INCREMENTAL (histórico já carregado):
          1. Mini range scan das ~20 bases além do máximo conhecido: detecta ASNs
             novas (status 0/pendente) em ~400ms assim que criadas no WMS.
          2. Stages (POST inventorybalance): descobre ASNs que chegaram ao STG.PA.*.
          3. Re-busca ASNs ABERTAS criadas hoje ou ontem para atualização de status.
          4. ASNs fechadas ou anteriores a ontem: ignoradas no ciclo automático.
        """
        log.info("Iniciando refresh dos recebimentos PA...")
        try:
            with self._lock:
                existing = dict(self._receipts)

            startup       = len(existing) == 0
            today_str     = _hoje_str()
            yesterday_str = (datetime.now(_BRT).date() - timedelta(days=1)).isoformat()

            # ── Descoberta de ASNs ─────────────────────────────────────────────
            type_map = self._client.list_asn_by_type(receipttype="8")  # 405 → {}

            if not type_map:
                if startup:
                    # Bootstrap: usa todos os mecanismos disponíveis
                    keys_exports = self._discover_from_exports()
                    keys_stages  = self._discover_from_stages()
                    with self._lock:
                        known_cache = set(self._known_keys)
                    all_keys = keys_exports | keys_stages | known_cache
                    type_map = {k: "" for k in all_keys}
                    log.info(
                        f"Bootstrap: {len(type_map)} chaves "
                        f"(exp={len(keys_exports)}, stg={len(keys_stages)}, cache={len(known_cache)})."
                    )
                else:
                    # Incremental: stages + mini range scan de novas bases tipo 8
                    keys_stages    = self._discover_from_stages()
                    new_via_stages = keys_stages - set(existing.keys())
                    type_map       = {k: "" for k in new_via_stages}

                    # Mini range scan: detecta ASNs recém-criadas (incluindo status 0/novo).
                    # Sonda apenas bases ainda não indexadas além do máximo conhecido.
                    # ~20 bases × 10 workers × 200ms latência ≈ 400ms por ciclo.
                    with self._lock:
                        known = set(self._known_keys)
                    known_bases = {
                        int(k.split(".")[0])
                        for k in known
                        if "." in k and k.split(".")[0].isdigit()
                    }
                    if known_bases:
                        max_base = max(known_bases)
                        # Bases completamente novas (fora do índice)
                        scan_ini = max(1, max_base - 5)
                        scan_fim = max_base + 1001
                        bases_novas = [
                            b for b in range(scan_ini, scan_fim)
                            if b not in known_bases
                        ]
                        # Bases recentes já conhecidas: re-sonda para capturar
                        # sub-keys criados hoje em bases já indexadas (ex: 76491.15)
                        bases_recentes = sorted(
                            [b for b in known_bases if b >= max_base - 800],
                        )[-200:]  # últimas 200 bases conhecidas
                        scan_list = bases_novas + bases_recentes
                        if scan_list:
                            scan_result = self._scan_bases_quick(
                                scan_list, max_workers=15, timeout=4
                            )
                            novel = scan_result - set(existing.keys())
                            if novel:
                                log.info(
                                    f"Mini scan: {len(novel)} novas ASNs tipo 8 descobertas "
                                    f"(novas={len(bases_novas)} recentes={len(bases_recentes)})."
                                )
                                for k in novel:
                                    type_map[k] = ""

            self._known_keys.update(type_map.keys())

            # ── Decide quais buscar ─────────────────────────────────────────────
            need_fetch = set()

            if startup:
                # Bootstrap: busca tudo que não está fechado no cache
                for rk, st_raw in type_map.items():
                    if st_raw in self._STATUS_WMS_FECHADO:
                        if rk not in existing:
                            need_fetch.add(rk)
                    else:
                        need_fetch.add(rk)
            else:
                # Incremental:
                # 1. Novas keys descobertas (via stages ou webhook externo)
                for rk in type_map:
                    if rk not in existing:
                        need_fetch.add(rk)
                # 2. ASNs abertas de hoje ou ontem → re-busca para atualização de status
                for rk, rec in existing.items():
                    st_raw = rec.get("status_raw", "")
                    if st_raw in self._STATUS_WMS_FECHADO:
                        continue  # fechada/cancelada — dados congelados, pula
                    data_criacao = (rec.get("data_criacao") or "")[:10]
                    data_rec_stored = (rec.get("data_recebimento") or "")[:10]
                    # Re-busca se adddate=hoje/ontem OU se receiptdate=hoje/ontem
                    # (captura ASNs criadas antes de ontem mas recebidas hoje)
                    if (data_criacao in (today_str, yesterday_str)
                            or data_rec_stored in (today_str, yesterday_str)):
                        need_fetch.add(rk)
                    # ASN aberta sem atividade recente: não re-busca automaticamente

            log.info(
                f"Startup={startup} | discovered={len(type_map)} | "
                f"today={today_str} | yesterday={yesterday_str} | "
                f"need_fetch={len(need_fetch)}"
            )

            # ── Busca em paralelo ───────────────────────────────────────────────
            updated = dict(existing)

            def _fetch_one(rk):
                try:
                    receipt = self._client.get_asn(rk)
                    if not receipt:
                        receipt = self._client.get_asn_by_externkey(rk)
                    if receipt and _is_ordem_producao(receipt):
                        for det in (receipt.get("receiptdetails") or []):
                            pk = det.get("packkey") or ""
                            if pk:
                                self._ensure_pack(pk)
                        return rk, _receipt_to_dict(receipt, self._pack_cache)
                except Exception as e:
                    log.warning(f"Erro ao buscar {rk}: {e}")
                return rk, None

            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(_fetch_one, rk): rk for rk in need_fetch}
                for fut in as_completed(futures):
                    rk, rec = fut.result()
                    if rec:
                        updated[rk] = rec

            # ── Atualiza estado ─────────────────────────────────────────────────
            with self._lock:
                self._receipts = updated
                self._ultima_atualizacao = datetime.now(_BRT).strftime("%d/%m/%Y %H:%M:%S")
                self._erro = None

            self._save_cache()
            log.info(f"Refresh concluído: {len(updated)} receipts ({len(need_fetch)} buscados).")

            # Descoberta do histórico via range scan é feita localmente pelo script
            # carga_inicial_asn.py (muito mais rápido que no servidor Render).

        except Exception as e:
            log.error(f"Erro no refresh: {e}")
            with self._lock:
                self._erro = str(e)

    def _background_range_scan(self):
        """Executa range scan paralelo e busca ASNs novas em paralelo (10 workers)."""
        log.info("Background range scan: início.")
        try:
            with self._lock:
                known = set(self._known_keys)
            new_keys = self._discover_from_range_scan(already_discovered=known)
            keys_to_fetch = [rk for rk in new_keys if rk not in self._known_keys]
            log.info(f"Background range scan: {len(new_keys)} chaves descobertas, {len(keys_to_fetch)} novas.")

            added = 0

            def _bg_fetch(rk):
                try:
                    return self.fetch_and_store(rk)
                except Exception as e:
                    log.debug(f"Range scan fetch {rk}: {e}")
                    return None

            with ThreadPoolExecutor(max_workers=10) as ex:
                futures = {ex.submit(_bg_fetch, rk): rk for rk in keys_to_fetch}
                for fut in as_completed(futures):
                    if fut.result():
                        added += 1

            log.info(f"Background range scan concluído: {added} novas ASNs adicionadas.")
            self._range_scan_done = True
        except Exception as e:
            log.error(f"Erro no background range scan: {e}")
        finally:
            self._range_scan_running = False

    # ------------------------------------------------------------------ background thread

    def _loop(self):
        while self._running:
            self._refresh()
            for _ in range(self._intervalo):
                if not self._running:
                    break
                time.sleep(1)

    def iniciar(self):
        if self._running and self._thread and self._thread.is_alive():
            return  # já rodando — idempotente para suportar gunicorn fork
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="receipt-collector")
        self._thread.start()
        log.info(f"ReceiptCollector iniciado — intervalo {self._intervalo}s.")

    def parar(self):
        self._running = False

    def forcar_refresh(self):
        threading.Thread(target=self._refresh, daemon=True).start()

    def fetch_and_store(self, receiptkey: str) -> dict | None:
        """Busca uma ASN específica imediatamente e a adiciona ao estado."""
        try:
            receipt = self._client.get_asn(receiptkey)
            if not receipt:
                receipt = self._client.get_asn_by_externkey(receiptkey)
            if not receipt:
                return None
            if not _is_ordem_producao(receipt):
                log.info(f"ASN {receiptkey} não é Ordem de Produção (type={receipt.get('type','?')}) — ignorada.")
                return None
            for det in (receipt.get("receiptdetails") or []):
                pk = det.get("packkey") or ""
                if pk:
                    self._ensure_pack(pk)
            r_dict = _receipt_to_dict(receipt, self._pack_cache)
            rk = r_dict["receiptkey"] or receiptkey
            with self._lock:
                self._receipts[rk] = r_dict
                self._known_keys.add(rk)
            self._save_cache()
            log.info(f"ASN {rk} buscada manualmente e indexada.")
            return r_dict
        except Exception as e:
            log.error(f"Erro ao buscar ASN {receiptkey}: {e}")
            return None

    # ------------------------------------------------------------------ state access

    def get_state(self):
        with self._lock:
            return {
                "receipts":            dict(self._receipts),
                "ultima_atualizacao":  self._ultima_atualizacao,
                "erro":                self._erro,
            }

    # ------------------------------------------------------------------ farol summary

    def get_farol(self, data_filtro: str | None = None):
        """
        Retorna resumo agrupado por depósito e status.
        data_filtro: 'YYYY-MM-DD' para filtrar dia específico (None = todos os dias).
        """
        with self._lock:
            receipts = dict(self._receipts)

        hoje_str = _hoje_str()
        meses_validos = _meses_validos()
        resultado = {}

        # Inicializa estrutura por depósito
        for dep, info in DEPOSITOS.items():
            resultado[dep] = {
                "deposito":      dep,
                "nome":          info["nome"],
                "zona":          info["zona"],
                "supplier_name": "",  # preenchido com o primeiro encontrado nas ASNs
                "hoje": {s: {"count": 0, "paletes": 0, "receiptkeys": []} for s in STATUS_ORDER},
                "backlog": {s: {"count": 0, "paletes": 0, "receiptkeys": []} for s in STATUS_ORDER},
                "total_paletes_dia": 0,
            }

        # SEM depósito identificado
        resultado["OUTROS"] = {
            "deposito":      "OUTROS",
            "nome":          "Outros",
            "zona":          "",
            "supplier_name": "",
            "hoje":   {s: {"count": 0, "paletes": 0, "receiptkeys": []} for s in STATUS_ORDER},
            "backlog": {s: {"count": 0, "paletes": 0, "receiptkeys": []} for s in STATUS_ORDER},
            "total_paletes_dia": 0,
        }

        for rk, rec in receipts.items():
            dep = rec.get("deposito") or "OUTROS"
            if dep not in resultado:
                dep = "OUTROS"

            status = rec.get("status") or "pendente"
            paletes = rec["paletes"]["paletes_total"]

            # Propaga supplier_name para o card do depósito (usa o primeiro encontrado)
            if not resultado[dep]["supplier_name"] and rec.get("supplier_name"):
                resultado[dep]["supplier_name"] = rec["supplier_name"]

            # Bucket pela data de CRIAÇÃO (adddate): hoje = criada hoje; backlog = demais
            data_atual = (rec.get("data_criacao") or "")[:10]
            ref_date   = data_filtro if data_filtro else hoje_str
            is_hoje    = (data_atual == ref_date)

            # Backlog: ignora ASNs anteriores ao mês passado
            if not is_hoje and data_atual[:7] not in meses_validos:
                continue

            bucket = "hoje" if is_hoje else "backlog"
            resultado[dep][bucket][status]["count"]      += 1
            resultado[dep][bucket][status]["paletes"]    += paletes
            resultado[dep][bucket][status]["receiptkeys"].append(rk)

            if is_hoje:
                resultado[dep]["total_paletes_dia"] += paletes

        # Totais globais
        totais = {
            "hoje":   {s: {"count": 0, "paletes": 0} for s in STATUS_ORDER},
            "backlog": {s: {"count": 0, "paletes": 0} for s in STATUS_ORDER},
            "total_paletes_dia": 0,
            "total_receipts": len(receipts),
        }
        for dep_data in resultado.values():
            for s in STATUS_ORDER:
                for bucket in ["hoje", "backlog"]:
                    totais[bucket][s]["count"]   += dep_data[bucket][s]["count"]
                    totais[bucket][s]["paletes"]  += dep_data[bucket][s]["paletes"]
            totais["total_paletes_dia"] += dep_data["total_paletes_dia"]

        return {
            "depositos":  resultado,
            "totais":     totais,
            "ultima_atualizacao": self._ultima_atualizacao,
            "erro":       self._erro,
            "hoje":       hoje_str,
        }

    def get_receipt_detail(self, receiptkey: str):
        with self._lock:
            return self._receipts.get(receiptkey)

    def get_receipts_by_status_deposito(self, deposito: str, status: str, bucket: str = "todos"):
        """Retorna lista de receipts filtrados por depósito e status."""
        with self._lock:
            receipts = dict(self._receipts)

        hoje_str = _hoje_str()
        meses_validos = _meses_validos()
        resultado = []
        for rk, rec in receipts.items():
            dep = rec.get("deposito") or "OUTROS"
            if deposito and dep != deposito:
                continue
            st = rec.get("status") or "pendente"
            if status and st != status:
                continue
            # Bucket pela data de criação (adddate): hoje = criada hoje; backlog = demais
            data_atual = (rec.get("data_criacao") or "")[:10]
            is_hoje    = (data_atual == hoje_str)
            if bucket == "hoje" and not is_hoje:
                continue
            if bucket == "backlog" and is_hoje:
                continue
            # Backlog: ignora ASNs anteriores ao mês passado
            if not is_hoje and data_atual[:7] not in meses_validos:
                continue
            resultado.append(rec)
        return resultado
