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
