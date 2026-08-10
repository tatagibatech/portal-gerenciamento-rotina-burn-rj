"""
Gerenciamento de Rotina Logística BURN RJ
Painel de Recebimento de Produto Acabado
Backend Flask — serve o dashboard e as APIs de dados.
"""
import logging
import os
import json
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from receipt_collector import ReceiptCollector, DEPOSITOS, STATUS_ORDER

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


@app.before_request
def _garantir_collector():
    collector.iniciar()  # idempotente — inicia thread no worker certo após gunicorn fork


# ─────────────────────────────── Frontend ────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


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
        "server_time":         datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
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
