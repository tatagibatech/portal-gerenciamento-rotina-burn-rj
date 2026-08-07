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
    """Testa endpoint /receipts e /advancedshipnotice com data de hoje e ontem."""
    from wms_config import CONFIG as C
    from receipt_collector import WMSClient
    from datetime import date, timedelta
    client = WMSClient()
    resultado = {}
    for delta in (0, 1):
        dt = (date.today() - timedelta(days=delta)).isoformat()
        resultado[dt] = {}
        for param in ("receiptdate", "adddate"):
            try:
                r = client.get("receipts", params={"storerkey": "BURN", param: dt, "recordcount": 20}, timeout=30)
                resultado[dt][f"receipts_{param}"] = {
                    "status_code": r.status_code,
                    "keys": [x.get("receiptkey") for x in (r.json() if isinstance(r.json(), list) else [])][:10] if r.status_code == 200 else r.text[:300],
                }
            except Exception as e:
                resultado[dt][f"receipts_{param}"] = {"erro": str(e)}
        try:
            r = client.get("advancedshipnotice", params={"storerkey": "BURN", "adddate": dt, "recordcount": 20}, timeout=30)
            resultado[dt]["asn_adddate"] = {
                "status_code": r.status_code,
                "keys": [x.get("receiptkey") for x in (r.json() if isinstance(r.json(), list) else [])][:10] if r.status_code == 200 else r.text[:300],
            }
        except Exception as e:
            resultado[dt]["asn_adddate"] = {"erro": str(e)}
    return jsonify(resultado)


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
