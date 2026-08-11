"""
Carga inicial de ASNs no portal painel-burn-rj.onrender.com

Faz o range scan LOCALMENTE (rapido, sem limite de tempo) e envia
cada receiptkey encontrada para o portal via POST /api/webhook-asn.
O portal entao busca a ASN do WMS e a indexa no estado interno.

Uso: python carga_inicial_asn.py [--dias 30] [--base-ini 75000] [--base-fim 77500]
"""
import sys
import time
import argparse
import requests
from datetime import date, timedelta

from receipt_collector import WMSClient

PORTAL = "https://painel-burn-rj.onrender.com"


def scan_range(client, base_ini, base_fim):
    """Varre bases base_ini..base_fim e retorna set de receiptkeys encontradas."""
    keys = set()
    total_bases = base_fim - base_ini + 1
    print(f"Range scan: bases {base_ini} a {base_fim} ({total_bases} bases)...")

    for i, base in enumerate(range(base_fim, base_ini - 1, -1), 1):
        if i % 100 == 0:
            print(f"  {i}/{total_bases} bases varridas, {len(keys)} keys ate agora...")

        # Sonda sub=1 primeiro
        r = None
        try:
            r = client.get(f"advancedshipnotice/{base}.1", timeout=6)
        except Exception:
            continue

        if r.status_code != 200:
            continue

        keys.add(f"{base}.1")

        # Varredura de subs ate 40 com early exit apos 5 misses consecutivos
        miss = 0
        for sub in range(2, 41):
            rk = f"{base}.{sub}"
            try:
                r2 = client.get(f"advancedshipnotice/{rk}", timeout=6)
                if r2.status_code == 200:
                    keys.add(rk)
                    miss = 0
                else:
                    miss += 1
                    if miss >= 5:
                        break
            except Exception:
                miss += 1
                if miss >= 5:
                    break

    return keys


def scan_exports(client, dias=30):
    """Descobre keys via exports ASNCOMPLETED dos ultimos N dias."""
    keys = set()
    print(f"Exports scan (ultimos {dias} dias)...")
    for tipo in ("ASNCOMPLETED", "ASN"):
        try:
            events = client.get_exports(tipo=tipo, limit=2000)
            for ev in events:
                k = ev.get("key1") or ev.get("key2") or ev.get("receiptkey") or ""
                if k:
                    keys.add(k.strip())
        except Exception as e:
            print(f"  Exports {tipo}: {e}")
    print(f"  {len(keys)} keys via exports.")
    return keys


def scan_por_data(client, dias=7):
    """Descobre keys via listagem por data (adddate)."""
    keys = set()
    print(f"Scan por data (ultimos {dias} dias)...")
    for delta in range(dias):
        dt = (date.today() - timedelta(days=delta)).isoformat()
        for rec in client.list_receipts_by_date(dt):
            k = rec.get("receiptkey") or ""
            if k:
                keys.add(k.strip())
        for rec in client.list_asn_by_date(dt):
            k = rec.get("receiptkey") or ""
            if k:
                keys.add(k.strip())
    print(f"  {len(keys)} keys por data.")
    return keys


def enviar_para_portal(keys, apenas_abertas=True):
    """Envia cada receiptkey para o portal via POST /api/webhook-asn."""
    total = len(keys)
    ok = err = skip = 0
    print(f"\nEnviando {total} receiptkeys para o portal...")

    for i, rk in enumerate(sorted(keys), 1):
        try:
            r = requests.post(
                f"{PORTAL}/api/webhook-asn",
                json={"receiptkey": rk},
                timeout=30,
            )
            if r.status_code == 200:
                d = r.json()
                status = d.get("status", "?")
                dep = d.get("deposito", "?")
                if apenas_abertas and status == "fechado":
                    skip += 1
                else:
                    ok += 1
                    if i % 5 == 0 or i == total:
                        print(f"  [{i}/{total}] {rk} dep={dep} status={status}")
            elif r.status_code == 404:
                skip += 1  # nao encontrada no WMS (descartada)
            else:
                err += 1
                print(f"  [{i}/{total}] {rk} ERRO {r.status_code}: {r.text[:80]}")
        except Exception as e:
            err += 1
            print(f"  [{i}/{total}] {rk} EXCECAO: {e}")

        time.sleep(0.1)  # evita sobrecarregar o portal

    print(f"\nResultado: {ok} indexadas, {skip} ignoradas (fechadas/nao encontradas), {err} erros.")


def main():
    parser = argparse.ArgumentParser(description="Carga inicial de ASNs no portal")
    parser.add_argument("--base-ini", type=int, default=74000, help="Base minima do range scan (default: 74000)")
    parser.add_argument("--base-fim", type=int, default=79000, help="Base maxima do range scan (default: 79000)")
    parser.add_argument("--dias", type=int, default=7, help="Dias para scan por data e exports (default: 7)")
    parser.add_argument("--sem-range", action="store_true", help="Pula range scan (usa apenas exports e data)")
    parser.add_argument("--todas", action="store_true", help="Envia todas incluindo fechadas")
    parser.add_argument("--excel", type=str, default="", help="Arquivo Excel com receiptkeys na coluna A (pula range scan)")
    args = parser.parse_args()

    client = WMSClient()

    # 1. Descoberta
    keys = set()

    if args.excel:
        # Lê receiptkeys diretamente do Excel (coluna A, a partir da linha 2)
        try:
            import openpyxl
            wb = openpyxl.load_workbook(args.excel, read_only=True, data_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
                val = row[0]
                if val:
                    keys.add(str(val).strip())
            wb.close()
            print(f"Excel: {len(keys)} receiptkeys carregadas de '{args.excel}'.")
        except ImportError:
            print("ERRO: instale openpyxl:  pip install openpyxl")
            sys.exit(1)
        except Exception as e:
            print(f"ERRO ao ler Excel: {e}")
            sys.exit(1)
    else:
        keys |= scan_exports(client, dias=args.dias)
        keys |= scan_por_data(client, dias=args.dias)
        if not args.sem_range:
            keys |= scan_range(client, args.base_ini, args.base_fim)

    print(f"\nTotal descoberto: {len(keys)} receiptkeys unicas.")

    if not keys:
        print("Nenhuma key encontrada. Verifique a conexao com o WMS.")
        sys.exit(1)

    # 2. Envio para o portal
    enviar_para_portal(keys, apenas_abertas=not args.todas)

    # 3. Status final
    try:
        s = requests.get(f"{PORTAL}/api/status", timeout=15).json()
        print(f"\nPortal apos carga:")
        print(f"  Total receipts   : {s['total_receipts']}")
        print(f"  Ultima atualizacao: {s['ultima_atualizacao']}")
        print(f"  Erro             : {s['erro']}")
    except Exception as e:
        print(f"Erro ao verificar status: {e}")


if __name__ == "__main__":
    main()
