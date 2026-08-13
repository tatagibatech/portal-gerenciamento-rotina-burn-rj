"""
Gerenciamento de Rotina Logística BURN RJ
Painel de Recebimento de Produto Acabado
Backend Flask — serve o dashboard e as APIs de dados.
"""
import logging
import os
import json
import threading
import time
from datetime import datetime, timezone, timedelta

import requests as _requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from receipt_collector import ReceiptCollector, DEPOSITOS, STATUS_ORDER

_BRT = timezone(timedelta(hours=-3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
POLL_INTERVALO = 30  # segundos

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
CORS(app)

collector = ReceiptCollector(intervalo=POLL_INTERVALO)


def _self_ping():
    """Faz GET no próprio /api/status a cada 10 min para evitar hibernação do Render."""
    # Aguarda 2 min para o app subir completamente antes do primeiro ping
    time.sleep(120)
    host = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5002")
    url  = f"{host}/api/status"
    while True:
        try:
            _requests.get(url, timeout=10)
            log.debug(f"Self-ping OK: {url}")
        except Exception as e:
            log.debug(f"Self-ping falhou (normal na inicializacao): {e}")
        time.sleep(240)  # 4 minutos — evita hibernação no Render free tier


_ping_thread = threading.Thread(target=_self_ping, daemon=True, name="self-ping")
_ping_thread.start()


@app.before_request
def _garantir_collector():
    collector.iniciar()  # idempotente — inicia thread no worker certo após gunicorn fork


# ─────────────────────────────── Frontend ────────────────────────────────────

@app.route("/")
def index():
    resp = send_from_directory(STATIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/swagger.json")
def swagger_json():
    """OpenAPI/Swagger definition — usado pelo ION API Gateway para registrar operações."""
    spec = {
        "swagger": "2.0",
        "info": {
            "title": "PainelRecebimentoPRD",
            "description": "Painel de Recebimento de Produto Acabado BURN RJ",
            "version": "1.0.0",
        },
        "host": "painel-burn-rj.onrender.com",
        "basePath": "/api",
        "schemes": ["https"],
        "paths": {
            "/webhook-asn": {
                "post": {
                    "summary": "Notificacao de nova ASN",
                    "description": "Recebe notificacao do ION DataFlow quando uma ASN e criada ou atualizada no WMS",
                    "operationId": "postWebhookAsn",
                    "consumes": ["application/json"],
                    "produces": ["application/json"],
                    "parameters": [
                        {
                            "in": "body",
                            "name": "body",
                            "required": True,
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "receiptkey": {
                                        "type": "string",
                                        "description": "Chave do recebimento no WMS",
                                    }
                                },
                            },
                        }
                    ],
                    "responses": {
                        "200": {"description": "ASN indexada com sucesso"},
                        "400": {"description": "receiptkey ausente no body"},
                        "404": {"description": "ASN nao encontrada no WMS"},
                    },
                }
            }
        },
    }
    return jsonify(spec)


# ─────────────────────────────── API ─────────────────────────────────────────

@app.get("/api/farol")
def api_farol():
    """Retorna o resumo do farol agrupado por depósito e status."""
    data_filtro = request.args.get("data")  # YYYY-MM-DD opcional
    try:
        farol = collector.get_farol(data_filtro=data_filtro)
        return jsonify(farol)
    except Exception as e:
        log.error(f"Erro em /api/farol: {e}")
        return jsonify({"erro": str(e)}), 500


@app.get("/api/receipts")
def api_receipts():
    """
    Lista receipts com filtros opcionais:
      deposito=308&status=pendente&bucket=hoje|backlog|todos
    """
    deposito = request.args.get("deposito", "")
    status   = request.args.get("status", "")
    bucket   = request.args.get("bucket", "todos")
    try:
        items = collector.get_receipts_by_status_deposito(deposito, status, bucket)
        return jsonify({"items": items, "total": len(items)})
    except Exception as e:
        log.error(f"Erro em /api/receipts: {e}")
        return jsonify({"erro": str(e)}), 500


@app.get("/api/receipt/<receiptkey>")
def api_receipt_detail(receiptkey):
    """Retorna detalhe completo de um recebimento."""
    try:
        rec = collector.get_receipt_detail(receiptkey)
        if rec is None:
            return jsonify({"erro": "Receipt não encontrado"}), 404
        return jsonify(rec)
    except Exception as e:
        log.error(f"Erro em /api/receipt/{receiptkey}: {e}")
        return jsonify({"erro": str(e)}), 500


@app.post("/api/refresh")
def api_refresh():
    """Força atualização imediata dos dados."""
    try:
        collector.forcar_refresh()
        return jsonify({"ok": True, "msg": "Refresh iniciado em background."})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.get("/api/fetch-asn/<receiptkey>")
def api_fetch_asn(receiptkey):
    """Busca e indexa uma ASN específica imediatamente pelo receiptkey."""
    try:
        rec = collector.fetch_and_store(receiptkey)
        if rec:
            return jsonify({"ok": True, "receipt": rec})
        return jsonify({"ok": False, "msg": f"ASN {receiptkey} não encontrada no WMS."}), 404
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.get("/api/depositos")
def api_depositos():
    """Retorna lista de depósitos configurados."""
    return jsonify(DEPOSITOS)


@app.get("/api/status")
def api_status():
    """Health check e status do coletor."""
    state = collector.get_state()
    return jsonify({
        "ok":                  state["erro"] is None,
        "ultima_atualizacao":  state["ultima_atualizacao"],
        "total_receipts":      len(state["receipts"]),
        "erro":                state["erro"],
        "server_time":         datetime.now(_BRT).strftime("%d/%m/%Y %H:%M:%S"),
    })


@app.get("/api/debug-wms-list")
def api_debug_wms_list():
    """Testa métodos de listagem de ASNs — descobre qual endpoint funciona."""
    from receipt_collector import WMSClient
    from datetime import date
    client = WMSClient()
    dt = date.today().isoformat()
    resultado = {}

    # 1. GET /advancedshipnotice sem filtro de data (apenas recordcount)
    for path in ("advancedshipnotice", "receipts"):
        try:
            r = client.get(path, params={"storerkey": "BURN", "recordcount": 10}, timeout=30)
            keys, sample = [], []
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    keys = [x.get("receiptkey") for x in data[:5]]
                    sample = [{k: v for k,v in x.items() if "date" in k.lower() or k in ("receiptkey","status","supplierCode","SupplierCode")} for x in data[:2]]
            resultado[f"GET_{path}_nofilter"] = {"status": r.status_code, "keys": keys, "sample": sample, "body": r.text[:300] if r.status_code != 200 else ""}
        except Exception as e:
            resultado[f"GET_{path}_nofilter"] = {"erro": str(e)}

    # 2. GET por status aberto (sem data)
    for status_val in ("5", "9", "3"):
        try:
            r = client.get("advancedshipnotice", params={"storerkey": "BURN", "status": status_val, "recordcount": 10}, timeout=30)
            keys = []
            if r.status_code == 200 and isinstance(r.json(), list):
                keys = [x.get("receiptkey") for x in r.json()[:10]]
            resultado[f"GET_asn_status_{status_val}"] = {"status": r.status_code, "keys": keys, "body": r.text[:200] if r.status_code != 200 else ""}
        except Exception as e:
            resultado[f"GET_asn_status_{status_val}"] = {"erro": str(e)}

    # 3. GET por editdate (data da atualização)
    for path in ("advancedshipnotice", "receipts"):
        for param in ("editdate", "lastmoddate", "updatedate", "modifieddate"):
            try:
                r = client.get(path, params={"storerkey": "BURN", param: dt, "recordcount": 10}, timeout=15)
                keys = []
                if r.status_code == 200 and isinstance(r.json(), list):
                    keys = [x.get("receiptkey") for x in r.json()[:5]]
                if r.status_code != 405:  # só mostra se não for 405
                    resultado[f"GET_{path}_{param}"] = {"status": r.status_code, "keys": keys, "body": r.text[:200]}
            except Exception as e:
                resultado[f"GET_{path}_{param}"] = {"erro": str(e)}

    # 4. Exports — ver último evento e total
    try:
        r = client.get("exports", params={"type": "ASNCOMPLETED", "restrictrowsto": 5}, timeout=30)
        sample = []
        if r.status_code == 200 and isinstance(r.json(), list):
            sample = [{"key1": x.get("key1","")[:30], "adddate": x.get("adddate","")} for x in r.json()]
        resultado["exports_ASNCOMPLETED_5"] = {"status": r.status_code, "sample": sample}
    except Exception as e:
        resultado["exports_ASNCOMPLETED_5"] = {"erro": str(e)}

    # 5. Inventário nos stages (scan direto)
    try:
        r = client.post("inventorybalance/showinventorybalancelist",
                        params={"recordcount": 5, "loc": "STG.PA.309.001", "owner": "BURN"}, timeout=30)
        fields = []
        if r.status_code == 200 and isinstance(r.json(), list) and r.json():
            fields = list(r.json()[0].keys())
            sample = [{k: v for k, v in r.json()[0].items() if v and v != "0" and v != ""}]
        resultado["stage_309_sample"] = {"status": r.status_code, "fields": fields[:30], "sample": sample if r.status_code == 200 else r.text[:200]}
    except Exception as e:
        resultado["stage_309_sample"] = {"erro": str(e)}

    return jsonify(resultado)


@app.get("/api/debug-asn-raw/<receiptkey>")
def api_debug_asn_raw(receiptkey):
    """Retorna todos os campos brutos do cabeçalho da ASN (sem expandir detalhes)."""
    from receipt_collector import WMSClient
    client = WMSClient()
    r = client.get(f"advancedshipnotice/{receiptkey}", timeout=30)
    if r.status_code == 200:
        raw = r.json()
        # Remove campos muito grandes para leitura
        resumo = {k: v for k, v in raw.items() if k not in ("receiptdetails", "fieldOverrides", "hrefs", "facility", "link")}
        return jsonify({"status_wms": 200, "campos": resumo})
    return jsonify({"status_wms": r.status_code, "body": r.text[:500]}), 404


@app.get("/api/debug-pack/<packkey>")
def api_debug_pack(packkey):
    """Retorna todos os campos brutos do cadastro de embalagem (pack) do WMS."""
    from receipt_collector import WMSClient
    client = WMSClient()
    r = client.get(f"packs/{packkey}", timeout=20)
    if r.status_code == 200:
        raw = r.json()
        # Calcular qpp como o sistema faz
        ti = float(raw.get("palletti") or 0)
        hi = float(raw.get("pallethi") or 0)
        qty = float(raw.get("qty") or 0)
        qpp_calc = ti * hi if (ti > 0 and hi > 0) else (qty if qty > 0 else 0)
        return jsonify({
            "packkey":    packkey,
            "status_wms": r.status_code,
            "qpp_calculado": qpp_calc,
            "formula":    f"palletti({ti}) × pallethi({hi}) = {ti*hi}" if (ti > 0 and hi > 0) else f"fallback qty={qty}",
            "campos_raw": raw,
        })
    return jsonify({"status_wms": r.status_code, "body": r.text[:500]}), 404


@app.get("/api/debug-receipt")
def api_debug_receipt():
    """Diagnóstico: campos chave de até 5 receipts — ajuda a identificar deposito/fromloc/toloc."""
    state = collector.get_state()
    receipts = state["receipts"]
    amostra = []
    for rk, rec in list(receipts.items())[:5]:
        linhas = rec.get("linhas") or []
        linhas_info = [
            {
                "fromloc":      l.get("fromloc"),
                "toloc":        l.get("toloc"),
                "packkey":      l.get("packkey"),
                "qty_por_palete": l.get("qty_por_palete"),
                "qty_previsto": l.get("qty_previsto"),
                "qty_recebido": l.get("qty_recebido"),
            }
            for l in linhas[:3]
        ]
        amostra.append({
            "receiptkey":       rk,
            "externkey":        rec.get("externkey"),
            "deposito":         rec.get("deposito"),
            "supplier_code":    rec.get("supplier_code"),
            "status":           rec.get("status"),
            "status_wms":       rec.get("status_wms"),
            "status_raw":       rec.get("status_raw"),
            "data_criacao":     rec.get("data_criacao"),
            "data_recebimento": rec.get("data_recebimento"),
            "data_fechamento":  rec.get("data_fechamento"),
            "n_linhas":         rec.get("n_linhas"),
            "paletes_total":    rec["paletes"]["paletes_total"] if rec.get("paletes") else None,
            "linhas":           linhas_info,
        })
    return jsonify(amostra)


@app.post("/api/bulk-import")
def api_bulk_import():
    """Importa lista de ASNs pré-parseadas diretamente no cache (sem chamar o WMS)."""
    try:
        body = request.get_json(force=True, silent=True) or {}
        receipts = body.get("receipts", [])
        if not receipts:
            return jsonify({"ok": False, "msg": "lista receipts vazia"}), 400
        imported = collector.bulk_import(receipts)
        return jsonify({"ok": True, "imported": imported})
    except Exception as e:
        log.error(f"Erro em /api/bulk-import: {e}")
        return jsonify({"erro": str(e)}), 500


@app.post("/api/webhook-asn")
def api_webhook_asn():
    """
    Webhook para receber notificações do ION DataFlow AdvanceShipNotice.

    O ION DataFlow DataGatewayPRD_AdvanceShipNotice dispara quando o WMS envia
    um SyncAdvanceShipNotice (criação ou fechamento de ASN), chama a API WMS para
    obter o JSON completo da ASN e faz POST aqui com esse JSON no body.

    Basta configurar o ConnectionPoint DataGatewayPRD no ION Desk apontando para:
      https://painel-burn-rj.onrender.com/api/webhook-asn

    Para receber criações (além de fechamentos), remova o filtro Code=Canceled/Closed
    no DataFlow ou crie um DataFlow paralelo com actionCode=Add sem filtro de status.
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        receiptkey = (
            body.get("receiptkey") or
            body.get("ReceiptKey") or
            body.get("receiptKey") or
            ""
        ).strip()
        if not receiptkey:
            log.warning(f"webhook-asn recebido sem receiptkey: {str(body)[:200]}")
            return jsonify({"ok": False, "msg": "receiptkey ausente no body"}), 400

        rec = collector.fetch_and_store(receiptkey)
        if rec:
            log.info(f"webhook-asn: ASN {receiptkey} indexada via DataFlow (dep={rec.get('deposito')}, status={rec.get('status')}).")
            return jsonify({"ok": True, "receiptkey": receiptkey, "deposito": rec.get("deposito"), "status": rec.get("status")})
        return jsonify({"ok": False, "msg": f"ASN {receiptkey} não encontrada no WMS"}), 404
    except Exception as e:
        log.error(f"Erro em /api/webhook-asn: {e}")
        return jsonify({"erro": str(e)}), 500


# ─────────────────────────────── Inventário ──────────────────────────────────

_inventario_dados = {}  # dados em memória — recarregados via POST /api/inventario/dados
_painel_dados     = {}  # dados do painel ERP×WMS — recarregados via POST /api/inventario/painel
_finalizacoes     = {}  # registros de finalização por nível — POST /api/inventario/finalizar
_pending_abrir      = None   # timestamp do comando "abrir inventário" aguardando execução local
_pending_finalizar  = None   # timestamp do comando "finalizar inventário" aguardando execução local
_inventario_final   = {}     # dados do relatório final (pdf gerado, timestamp)


@app.post("/api/inventario/dados")
def api_inventario_upload():
    """Recebe o inventario_processado.json gerado pelo script local e armazena em memória."""
    try:
        dados = request.get_json(force=True, silent=True) or {}
        if not dados:
            return jsonify({"ok": False, "msg": "body vazio"}), 400
        global _inventario_dados
        _inventario_dados = dados
        _inventario_dados["recebido_em"] = datetime.now(_BRT).isoformat(timespec="seconds")
        log.info(f"Inventário recebido: {dados.get('total_asns',0)} ASNs, {len(dados.get('linhas',[]))} pares loc×sku")
        return jsonify({"ok": True, "total_linhas": len(dados.get("linhas", []))})
    except Exception as e:
        log.error(f"Erro em /api/inventario/dados: {e}")
        return jsonify({"erro": str(e)}), 500


@app.get("/api/inventario")
def api_inventario():
    """Retorna os dados de inventário processados."""
    if not _inventario_dados:
        return jsonify({"vazio": True, "msg": "Nenhum dado de inventário carregado."}), 200
    return jsonify(_inventario_dados)


@app.post("/api/inventario/painel")
def api_painel_upload():
    """Recebe dados do painel ERP×WMS (gerar_dashboard.py) e armazena em memória."""
    try:
        dados = request.get_json(force=True, silent=True) or {}
        if not dados:
            return jsonify({"ok": False, "msg": "body vazio"}), 400
        global _painel_dados
        _painel_dados = dados
        _painel_dados["recebido_em"] = datetime.now(_BRT).isoformat(timespec="seconds")
        log.info(f"Painel recebido: {dados.get('total_skus',0)} SKUs ERP, "
                 f"{dados.get('lidos_end',0)}/{dados.get('total_end',0)} end. lidos, "
                 f"{dados.get('wms_linhas',0)} linhas WMS")
        return jsonify({"ok": True, "total_skus": dados.get("total_skus", 0),
                        "total_end": dados.get("total_end", 0)})
    except Exception as e:
        log.error(f"Erro em /api/inventario/painel: {e}")
        return jsonify({"erro": str(e)}), 500


@app.get("/api/inventario/painel")
def api_painel():
    """Retorna os dados do painel ERP×WMS."""
    if not _painel_dados:
        return jsonify({"vazio": True, "msg": "Nenhum dado do painel carregado."}), 200
    resp = dict(_painel_dados)
    resp["finalizacoes"] = _finalizacoes
    return jsonify(resp)


@app.post("/api/inventario/finalizar")
def api_finalizar():
    """Registra a finalização de um nível de contagem."""
    dados = request.get_json(force=True, silent=True) or {}
    nivel = dados.get("nivel")
    armazem = dados.get("armazem", "todos")
    if not nivel:
        return jsonify({"ok": False, "msg": "nivel obrigatorio"}), 400
    global _finalizacoes
    _finalizacoes[str(nivel)] = {
        "ts":      datetime.now(_BRT).isoformat(timespec="seconds"),
        "armazem": armazem,
        "nivel":   nivel,
    }
    log.info(f"Contagem C{nivel} finalizada — armazém={armazem}")
    return jsonify({"ok": True, "nivel": nivel})


@app.get("/api/inventario/finalizacoes")
def api_finalizacoes():
    """Retorna todos os registros de finalização."""
    return jsonify(_finalizacoes)


@app.post("/api/inventario/abrir")
def api_abrir_inventario():
    """Sinaliza ao script local para carregar a base ERP e criar as ASNs de C1."""
    global _pending_abrir
    _pending_abrir = datetime.now(_BRT).isoformat(timespec="seconds")
    log.info("Comando 'abrir inventário' recebido — aguardando execução local.")
    return jsonify({
        "ok":  True,
        "msg": "Comando recebido. O inventário será aberto na próxima atualização (~1 min).",
        "ts":  _pending_abrir,
    })


@app.post("/api/inventario/cancelar")
def api_cancelar_inventario():
    """Limpa todos os dados de inventário da memória (reset para estado inicial)."""
    global _inventario_dados, _painel_dados, _finalizacoes, _inventario_final
    global _pending_abrir, _pending_finalizar
    _inventario_dados   = {}
    _painel_dados       = {}
    _finalizacoes       = {}
    _inventario_final   = {}
    _pending_abrir      = None
    _pending_finalizar  = None
    log.info("Inventário cancelado — todos os dados foram limpos.")
    return jsonify({"ok": True, "msg": "Inventário cancelado. Dados limpos."})


@app.post("/api/inventario/finalizar_total")
def api_finalizar_total():
    """Sinaliza ao script local para gerar o relatório PDF final e enviar por email."""
    global _pending_finalizar
    _pending_finalizar = datetime.now(_BRT).isoformat(timespec="seconds")
    log.info("Comando 'finalizar inventário' recebido — aguardando execução local.")
    return jsonify({
        "ok":  True,
        "msg": "Comando recebido. O relatório será gerado e enviado por email em ~1 min.",
        "ts":  _pending_finalizar,
    })


@app.post("/api/inventario/reabrir")
def api_reabrir_inventario():
    """Reabre o inventário finalizado — limpa apenas a finalização, mantém dados de contagem."""
    global _inventario_final, _pending_finalizar
    _inventario_final  = {}
    _pending_finalizar = None
    log.info("Inventário reaberto — finalização removida, dados de contagem mantidos.")
    return jsonify({"ok": True, "msg": "Inventário reaberto. Dados de contagem preservados."})


@app.post("/api/inventario/finalizado")
def api_inventario_finalizado():
    """Recebe confirmação do script local após geração do PDF."""
    global _inventario_final
    dados = request.get_json(force=True, silent=True) or {}
    _inventario_final = {
        "finalizado_em": dados.get("finalizado_em"),
        "pdf":           dados.get("pdf"),
        "ts":            datetime.now(_BRT).isoformat(timespec="seconds"),
    }
    log.info(f"Inventário finalizado — PDF: {dados.get('pdf')}")
    return jsonify({"ok": True})


@app.get("/api/inventario/pending")
def api_inventario_pending():
    """Retorna e limpa comandos pendentes para execução local (chamado pelo auto-update)."""
    global _pending_abrir, _pending_finalizar
    resp = {"abrir_inventario": False, "finalizar_inventario": False}
    if _pending_abrir:
        resp["abrir_inventario"] = True
        resp["ts_abrir"] = _pending_abrir
        _pending_abrir = None
    if _pending_finalizar:
        resp["finalizar_inventario"] = True
        resp["ts_finalizar"] = _pending_finalizar
        _pending_finalizar = None
    return jsonify(resp)


@app.get("/api/config-check")
def api_config_check():
    """Diagnóstico de configuração — mostra prefixo das credenciais (não expõe valores completos)."""
    from wms_config import CONFIG as C
    def mask(v):
        if not v:
            return "(vazio)"
        return v[:12] + "..." + f" [{len(v)} chars]"
    return jsonify({
        "tenant":    C.get("tenant", ""),
        "token_url": C.get("token_url", ""),
        "warehouse": C.get("warehouse", ""),
        "owner":     C.get("owner", ""),
        "ci":        mask(C.get("ci", "")),
        "cs":        mask(C.get("cs", "")),
        "saak":      mask(C.get("saak", "")),
        "sask":      mask(C.get("sask", "")),
    })


# ─────────────────────────────── Startup ─────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  Gerenciamento de Rotina Logística BURN RJ")
    log.info("  Painel: http://localhost:5002/")
    log.info("  API:    http://localhost:5002/api/farol")
    log.info(f"  Polling a cada {POLL_INTERVALO}s")
    log.info("=" * 60)

    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)
