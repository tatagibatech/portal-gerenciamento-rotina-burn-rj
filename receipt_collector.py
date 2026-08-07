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

    def list_receipts_by_date(self, adddate: str, page_size=500):
        """Tenta listar receipts por data — fallback silencioso se endpoint não disponível."""
        try:
            r = self.get(
                "receipts",
                params={"storerkey": "BURN", "adddate": adddate, "recordcount": page_size},
                timeout=60,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and data.get("receiptkey"):
                    return [data]
        except Exception:
            pass
        return []


def _deposito_from_fromloc(fromloc: str) -> str | None:
    """Extrai número do depósito do local de origem (ex: STG.PA.308.001 → '308')."""
    if not fromloc:
        return None
    for dep in DEPOSITOS:
        if dep in fromloc:
            return dep
    return None


def _deposito_from_receiptkey(receiptkey: str) -> str | None:
    """Tenta inferir depósito do código do receipt."""
    for dep in DEPOSITOS:
        if dep in (receiptkey or ""):
            return dep
    return None


def _qty_por_palete(pack: dict) -> float:
    """Retorna quantidade de unidades por palete a partir do cadastro de embalagem."""
    ti = float(pack.get("palletti") or 0)
    hi = float(pack.get("pallethi") or 0)
    if ti > 0 and hi > 0:
        return ti * hi
    qty = float(pack.get("qty") or 0)
    if qty > 0:
        return qty
    return 0.0


def _split_paletes(valor: float):
    inteiros = int(valor)
    fracao   = 1 if (valor - inteiros) > 0.01 else 0
    return inteiros, fracao


def _calcula_paletes(details: list, pack_cache: dict) -> dict:
    """
    Calcula paletes PREVISTOS e RECEBIDOS separadamente.
    Fórmula: paletes = qty / qty_por_palete do cadastro de embalagem (pack).
    """
    total_prev   = 0.0
    total_rec    = 0.0
    pal_prev     = 0.0
    pal_rec      = 0.0

    for d in details:
        qty_prev = float(d.get("qtyexpected") or 0)
        qty_rec  = float(d.get("qtyreceived") or 0)
        total_prev += qty_prev
        total_rec  += qty_rec

        packkey = d.get("packkey") or d.get("sku") or ""
        pack    = pack_cache.get(packkey, {})
        qpp     = _qty_por_palete(pack)

        if qpp > 0:
            pal_prev += qty_prev / qpp
            pal_rec  += qty_rec  / qpp
        else:
            # fallback: campo pallet direto do detalhe
            pal_direto = float(d.get("pallet") or 0)
            if pal_direto > 0:
                pal_prev += pal_direto
                pal_rec  += pal_direto * (qty_rec / qty_prev) if qty_prev > 0 else 0
            else:
                # último recurso: 1 palete por linha com quantidade
                if qty_prev > 0:
                    pal_prev += 1
                if qty_rec > 0:
                    pal_rec  += 1

    prev_int, prev_frac = _split_paletes(pal_prev)
    rec_int,  rec_frac  = _split_paletes(pal_rec)
    diferenca = round(pal_prev - pal_rec, 2)

    return {
        "total_previsto":       round(total_prev, 2),
        "total_recebido":       round(total_rec, 2),
        # Previstos
        "paletes_previstos":    round(pal_prev, 2),
        "paletes_inteiros":     prev_int,
        "paletes_fracao":       prev_frac,
        "paletes_total":        prev_int + prev_frac,
        # Recebidos
        "paletes_recebidos":    round(pal_rec, 2),
        "paletes_rec_inteiros": rec_int,
        "paletes_rec_fracao":   rec_frac,
        "paletes_rec_total":    rec_int + rec_frac,
        # Diferença
        "diferenca_paletes":    diferenca,
    }


def _receipt_to_dict(receipt: dict, pack_cache: dict) -> dict:
    """Normaliza um receipt da API para estrutura interna."""
    details = receipt.get("receiptdetails") or []
    status_raw = str(receipt.get("status") or "0")
    status = STATUS_MAP.get(status_raw, "pendente")

    # Identificar depósito pelo fromloc dos detalhes
    deposito = None
    for d in details:
        dep = _deposito_from_fromloc(d.get("fromloc") or "")
        if dep:
            deposito = dep
            break
    if not deposito:
        deposito = _deposito_from_receiptkey(receipt.get("receiptkey") or "")
    if not deposito:
        dep_ref = _deposito_from_fromloc(receipt.get("referencelocation") or "")
        deposito = dep_ref

    add_dt  = (receipt.get("adddate") or "")[:10]
    close_dt = (receipt.get("closeddate") or "")[:10]
    rec_dt   = (receipt.get("receiptdate") or "")[:10]

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

    return {
        "receiptkey":       receipt.get("receiptkey") or "",
        "externkey":        receipt.get("externreceiptkey") or "",
        "status_raw":       status_raw,
        "status":           status_derivado,
        "status_wms":       status,
        "status_label":     STATUS_LABEL.get(status_derivado, status_derivado),
        "deposito":         deposito,
        "data_criacao":     add_dt,
        "data_recebimento": rec_dt,
        "data_fechamento":  close_dt,
        "n_linhas":         len(details),
        "paletes":          paletes,
        "linhas":           linhas,
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
        """Descobre receiptkeys via exports (ASNCOMPLETED + ASN) e listagem por data."""
        keys = set()

        for tipo in ("ASNCOMPLETED", "ASN"):
            try:
                events = self._client.get_exports(tipo=tipo, limit=200)
                for ev in events:
                    k = ev.get("key1") or ev.get("key2") or ev.get("receiptkey") or ""
                    if k:
                        keys.add(k.strip())
            except Exception as e:
                log.warning(f"Exports {tipo}: {e}")

        # Listagem por data (hoje e ontem) para capturar ASNs abertas
        for delta in (0, 1):
            dt = (date.today() - timedelta(days=delta)).isoformat()
            try:
                recs = self._client.list_receipts_by_date(dt)
                for r in recs:
                    k = r.get("receiptkey") or ""
                    if k:
                        keys.add(k.strip())
            except Exception as e:
                log.warning(f"list_receipts_by_date {dt}: {e}")

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
            keys_exports = self._discover_from_exports()
            keys_stages  = self._discover_from_stages()
            all_keys = keys_exports | keys_stages | self._known_keys

            new_receipts = {}

            for rk in all_keys:
                if not rk:
                    continue
                try:
                    receipt = self._client.get_asn(rk)
                    if receipt:
                        # Buscar pack dos itens
                        for det in (receipt.get("receiptdetails") or []):
                            pk = det.get("packkey") or det.get("sku") or ""
                            if pk:
                                self._ensure_pack(pk)

                        r_dict = _receipt_to_dict(receipt, self._pack_cache)
                        new_receipts[rk] = r_dict
                        self._known_keys.add(rk)
                    else:
                        # Key inválida — remover do cache depois
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

            # Determinar se é hoje ou backlog
            data_ref = rec.get("data_criacao") or rec.get("data_recebimento") or ""
            if data_filtro:
                is_hoje = (data_ref == data_filtro)
            else:
                is_hoje = (data_ref == hoje_str)

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
            data_ref = rec.get("data_criacao") or ""
            is_hoje = (data_ref == hoje_str)
            if bucket == "hoje" and not is_hoje:
                continue
            if bucket == "backlog" and is_hoje:
                continue
            resultado.append(rec)
        return resultado
