"""
Coleta de dados de recebimento PA via API WMS ION PRD.
Estratégias de descoberta de receipts:
  1. GET /exports?type=ASNCOMPLETED  -> receiptkeys de fechados/recebidos
  2. POST inventorybalance/showinventorybalancelist por STG.PA.* -> pendentes/em andamento
  3. Cache local de keys conhecidas
"""
import sys
import json
import math
import logging
import threading
import time
from datetime import datetime, date, timedelta
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
    "0":  "pendente",        # Novo
    "2":  "pendente",        # Em trânsito
    "3":  "em_recebimento",  # Pré-recebido
    "4":  "pendente",        # Programado
    "5":  "em_recebimento",  # No recebimento
    "9":  "recebido",        # Recebido
    "11": "fechado",         # Fechado
    "15": "fechado",         # Verificado fechado
    "20": "cancelado",       # Cancelado
    "21": "recebido",        # RNERP
}

STATUS_LABEL = {
    "pendente":       "Pendente",
    "em_recebimento": "Em Recebimento",
    "recebido":       "Recebido",
    "fechado":        "Fechado",
}

STATUS_ORDER = ["pendente", "em_recebimento", "recebido", "fechado", "cancelado"]

HOJE = date.today()


def _hoje_str():
    return date.today().isoformat()


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

    add_dt  = (receipt.get("adddate") or "")[:10]
    close_dt = (receipt.get("closeddate") or "")[:10]
    rec_dt   = (receipt.get("receiptdate") or "")[:10]
    # Data da última atualização — campo primário para classificar "hoje"
    edit_dt = (
        receipt.get("editdate") or receipt.get("lastmoddate") or
        receipt.get("updatedate") or receipt.get("modifieddate") or ""
    )[:10]

    paletes = _calcula_paletes(details, pack_cache)

    # Regras de status derivado (prioridade: WMS status 11/15 → fechado; demais por qtd)
    diferenca = paletes["diferenca_paletes"]
    total_rec  = paletes["total_recebido"]
    if status_raw in ("11", "15"):
        status_derivado = "fechado"
    elif status_raw == "20":
        status_derivado = "cancelado"
    elif status_raw in ("9", "21") and diferenca <= 0:
        status_derivado = "recebido"
    elif total_rec > 0 and diferenca > 0:
        # Recebimento iniciado mas ainda falta quantidade → em recebimento
        status_derivado = "em_recebimento"
    elif status_raw in ("3", "5"):
        status_derivado = "em_recebimento"
    elif total_rec == 0:
        status_derivado = "pendente"
    else:
        status_derivado = status

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

    return {
        "receiptkey":        receipt.get("receiptkey") or "",
        "externkey":         receipt.get("externreceiptkey") or "",
        "supplier_code":     supplier_code,
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

    CACHE_FILE = "receipt_keys_cache.json"

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
        self._first_scan_done = False   # controla janela ampla no bootstrap
        self._load_cache()

    # ------------------------------------------------------------------ cache

    def _load_cache(self):
        try:
            with open(self.CACHE_FILE) as f:
                self._known_keys = set(json.load(f))
            log.info(f"Cache carregado: {len(self._known_keys)} chaves.")
        except Exception:
            self._known_keys = set()

    def _save_cache(self):
        try:
            with open(self.CACHE_FILE, "w") as f:
                json.dump(list(self._known_keys), f)
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

    def _discover_from_range_scan(self, already_discovered: set | None = None) -> set:
        """
        Varre receiptkeys sequenciais para descobrir ASNs de todos os depósitos.

        Estratégia eficiente: testa sub=1 primeiro — se 404, pula o base inteiro
        (apenas 1 chamada por base vazio). Se encontrar sub=1, continua até 3 404s
        consecutivos.

        Janela:
          • Bootstrap (primeira execução / sem cache): max_base + 1500
            Garante cobertura mesmo quando exports retornam dados antigos.
          • Incremental (ciclos seguintes): max_base + 50
            Descobre apenas chaves novas desde o último refresh.
        """
        keys = set()

        # Maior base numérico dentre known_keys + já descobertos neste ciclo
        all_refs = self._known_keys | (already_discovered or set())
        max_base = 0
        for rk in all_refs:
            try:
                b = int(rk.split(".")[0])
                if b > max_base:
                    max_base = b
            except (ValueError, IndexError):
                pass

        # Garante mínimo razoável para 2026 caso exports só retornem registros antigos
        BASE_MINIMO_2026 = 75800
        if max_base < BASE_MINIMO_2026:
            max_base = BASE_MINIMO_2026

        if not self._first_scan_done:
            # Bootstrap: janela ampla para cobrir o gap entre exports e hoje
            scan_start = max(1, max_base - 100)
            scan_end   = max_base + 1500
            self._first_scan_done = True
            log.info(f"Range scan BOOTSTRAP: bases {scan_start}–{scan_end}")
        else:
            # Incremental: só arredores do máximo conhecido
            scan_start = max(1, max_base - 20)
            scan_end   = max_base + 50
            log.info(f"Range scan incremental: bases {scan_start}–{scan_end}")

        found = 0
        for base in range(scan_end, scan_start - 1, -1):  # mais recente → mais antigo
            rk1 = f"{base}.1"
            try:
                r = self._client.get(f"advancedshipnotice/{rk1}", timeout=8)
            except Exception:
                continue

            if r.status_code != 200:
                continue

            keys.add(rk1)
            found += 1

            consec_miss = 0
            for sub in range(2, 31):
                rk = f"{base}.{sub}"
                try:
                    r2 = self._client.get(f"advancedshipnotice/{rk}", timeout=8)
                    if r2.status_code == 200:
                        keys.add(rk)
                        found += 1
                        consec_miss = 0
                    else:
                        consec_miss += 1
                        if consec_miss >= 3:
                            break
                except Exception:
                    consec_miss += 1
                    if consec_miss >= 3:
                        break

        log.info(f"Range scan concluído: {found} chaves descobertas.")
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

    def _refresh(self):
        log.info("Iniciando refresh dos recebimentos PA...")
        try:
            # 1. Descobrir novas chaves
            keys_exports    = self._discover_from_exports()
            keys_stages     = self._discover_from_stages()
            # Passa exports+stages para o range scan usar como referência de max_base
            keys_range_scan = self._discover_from_range_scan(keys_exports | keys_stages)
            all_keys = keys_exports | keys_stages | keys_range_scan | self._known_keys

            new_receipts = {}

            for rk in all_keys:
                if not rk:
                    continue
                try:
                    receipt = self._client.get_asn(rk)
                    if receipt:
                        # Filtrar: apenas Ordens de Produção (receipttype=10)
                        if not _is_ordem_producao(receipt):
                            log.debug(f"Receipt {rk} ignorado (type={receipt.get('type','?')} ≠ 8/OP).")
                            continue

                        # Buscar pack de cada linha via /{warehouse}/packs/{packkey}
                        for det in (receipt.get("receiptdetails") or []):
                            pk = det.get("packkey") or ""
                            if pk:
                                self._ensure_pack(pk)

                        r_dict = _receipt_to_dict(receipt, self._pack_cache)
                        new_receipts[rk] = r_dict
                        self._known_keys.add(rk)
                    else:
                        log.debug(f"Receipt {rk} não encontrado.")
                except Exception as e:
                    log.warning(f"Erro ao buscar receipt {rk}: {e}")

            with self._lock:
                self._receipts = new_receipts
                self._ultima_atualizacao = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                self._erro = None

            self._save_cache()
            log.info(f"Refresh concluído: {len(new_receipts)} receipts carregados.")

        except Exception as e:
            log.error(f"Erro no refresh: {e}")
            with self._lock:
                self._erro = str(e)

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
        resultado = {}

        # Inicializa estrutura por depósito
        for dep, info in DEPOSITOS.items():
            resultado[dep] = {
                "deposito":   dep,
                "nome":       info["nome"],
                "zona":       info["zona"],
                "hoje": {s: {"count": 0, "paletes": 0, "receiptkeys": []} for s in STATUS_ORDER},
                "backlog": {s: {"count": 0, "paletes": 0, "receiptkeys": []} for s in STATUS_ORDER},
                "total_paletes_dia": 0,
            }

        # SEM depósito identificado
        resultado["OUTROS"] = {
            "deposito":   "OUTROS",
            "nome":       "Outros",
            "zona":       "",
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

            # Hoje = atualizado hoje (editdate); backlog = criado antes e não atualizado hoje
            data_atual = rec.get("data_atualizacao") or rec.get("data_criacao") or ""
            if data_filtro:
                is_hoje = (data_atual == data_filtro)
            else:
                is_hoje = (data_atual == hoje_str)

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
        resultado = []
        for rk, rec in receipts.items():
            dep = rec.get("deposito") or "OUTROS"
            if deposito and dep != deposito:
                continue
            if status and rec.get("status") != status:
                continue
            data_atual = rec.get("data_atualizacao") or rec.get("data_criacao") or ""
            is_hoje = (data_atual == hoje_str)
            if bucket == "hoje" and not is_hoje:
                continue
            if bucket == "backlog" and is_hoje:
                continue
            resultado.append(rec)
        return resultado
