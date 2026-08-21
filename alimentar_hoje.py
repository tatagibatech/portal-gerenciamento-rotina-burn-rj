"""
Alimenta o portal com as ASNs de hoje e dos últimos dias via scan inteligente.

Problema: o cache tem max_base ~75850 (jul/2026), mas ASNs de ago/2026
estão nas bases 77000+. O scan incremental (+21) nunca alcança.

Solução:
  1. Sonda a cada 50 bases para encontrar o range de agosto/2026
  2. Faz scan denso no range encontrado
  3. Envia cada ASN nova ao portal via /api/fetch-asn

Uso:
  python alimentar_hoje.py                   # últimos 7 dias
  python alimentar_hoje.py --dias 30         # últimos 30 dias
  python alimentar_hoje.py --inicio 77000    # scan a partir de uma base específica
"""
import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Garante saída UTF-8 sem quebrar redirecionamento para arquivo
import io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from datetime import datetime, timezone, timedelta, date
import requests as _req
import concurrent.futures

_BRT = timezone(timedelta(hours=-3))
hoje_str = datetime.now(_BRT).date().isoformat()

ap = argparse.ArgumentParser()
ap.add_argument("--dias",    type=int, default=7,     help="Janela de dias a buscar (default=7)")
ap.add_argument("--inicio",  type=int, default=0,     help="Base inicial para o scan (0=auto)")
ap.add_argument("--portal",  default="https://painel-burn-rj.onrender.com", help="URL do portal")
ap.add_argument("--workers", type=int, default=15,    help="Workers paralelos para scan")
args = ap.parse_args()

JANELA = timedelta(days=args.dias)
DATA_INICIO = (datetime.now(_BRT).date() - JANELA).isoformat()
PORTAL = args.portal.rstrip("/")

print(f"\n{'='*65}")
print(f"  ALIMENTAR PORTAL — ASNs de {DATA_INICIO} a {hoje_str}")
print(f"  Portal: {PORTAL}")
print(f"{'='*65}\n")

# ── WMS Client ────────────────────────────────────────────────────────────────
from receipt_collector import WMSClient, TIPO_ORDEM_PRODUCAO
client = WMSClient()

# ── 1. Determina base inicial ──────────────────────────────────────────────────
if args.inicio > 0:
    base_ini = args.inicio
    print(f"[1] Base inicial definida manualmente: {base_ini}")
else:
    # Sonda a cada 100 bases (75000 → 79000) para encontrar range do período
    print("[1] Sondando range de bases para encontrar ASNs do período...")
    candidatas = {}
    SONDA_START = 74000
    SONDA_END   = 79000
    SONDA_STEP  = 100

    def sonda(base):
        try:
            r = client.get(f"advancedshipnotice/{base}.1", timeout=4)
            if r.status_code == 200:
                d = r.json()
                add = str(d.get("adddate") or "")[:10]
                tp  = str(d.get("receipttype") or d.get("type") or "")
                return base, add, tp
        except Exception:
            pass
        return base, None, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        bases_sonda = range(SONDA_START, SONDA_END + 1, SONDA_STEP)
        futs = {ex.submit(sonda, b): b for b in bases_sonda}
        for f in concurrent.futures.as_completed(futs):
            base, add, tp = f.result()
            if add:
                candidatas[base] = add

    # Encontra a menor base com adddate >= DATA_INICIO
    base_ini = None
    for b in sorted(candidatas.keys()):
        if candidatas[b] >= DATA_INICIO:
            # Volta 200 bases para não perder nada
            base_ini = max(SONDA_START, b - 200)
            break

    if base_ini is None:
        # Nenhuma sonda encontrou data recente — usa a maior base com data
        if candidatas:
            ultimo = max(candidatas.keys(), key=lambda b: candidatas[b])
            base_ini = max(SONDA_START, ultimo - 100)
            print(f"  Nenhuma ASN no período — usando base_ini={base_ini} (ultima data: {candidatas[ultimo]})")
        else:
            base_ini = 75000
            print(f"  Nenhuma sonda retornou dados — usando base_ini={base_ini} (padrão)")
    else:
        print(f"  Range encontrado: primeira base com dado recente = {base_ini}")

# ── 2. Scan denso no range ────────────────────────────────────────────────────
BASE_FIM = base_ini + 2500   # cobre ~2500 bases
print(f"\n[2] Scan denso: bases {base_ini} → {BASE_FIM} ({BASE_FIM - base_ini} bases)")
print(f"    Filtrando por adddate >= {DATA_INICIO} e receipttype=8")

encontradas = []   # lista de receiptkeys no período
total_sondadas = 0
total_type8 = 0
total_no_periodo = 0

def scan_base(base):
    """Varre subs 1..N de uma base até 5 misses consecutivos."""
    keys = []
    miss = 0
    sub = 1
    while miss < 5:
        rk = f"{base}.{sub}"
        try:
            r = client.get(f"advancedshipnotice/{rk}", timeout=4)
            if r.status_code == 200:
                d = r.json()
                tp  = str(d.get("receipttype") or d.get("type") or "")
                add = str(d.get("adddate") or "")[:10]
                if tp in TIPO_ORDEM_PRODUCAO and add >= DATA_INICIO:
                    keys.append((rk, add, str(d.get("status") or "")))
                miss = 0
            elif r.status_code == 404:
                miss += 1
            else:
                miss += 1
        except Exception:
            miss += 1
        sub += 1
    return keys

bases_range = list(range(base_ini, BASE_FIM + 1))
lote_size = 200  # imprime progresso a cada 200 bases

with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
    futs = {ex.submit(scan_base, b): b for b in bases_range}
    done = 0
    for f in concurrent.futures.as_completed(futs):
        resultado = f.result()
        done += 1
        if resultado:
            total_type8 += len(resultado)
            novas_periodo = [x for x in resultado]
            total_no_periodo += len(novas_periodo)
            encontradas.extend(novas_periodo)
        if done % lote_size == 0:
            pct = done / len(bases_range) * 100
            print(f"  {done}/{len(bases_range)} bases ({pct:.0f}%) — encontradas no período: {total_no_periodo}")

print(f"\n  Scan concluído: {total_no_periodo} ASNs tipo 8 no período encontradas")

if not encontradas:
    print("\n  Nenhuma ASN encontrada no período. Verifique o range de bases.")
    sys.exit(0)

# Ordena por receiptkey
encontradas.sort(key=lambda x: x[0])
print(f"\n  Exemplos (primeiras 10):")
for rk, add, st in encontradas[:10]:
    print(f"    {rk}  add={add}  st={st}")

# ── 3. Envia para o portal via /api/fetch-asn ─────────────────────────────────
print(f"\n[3] Enviando {len(encontradas)} ASNs para o portal...")

ok = 0
err = 0
skip = 0

# Aguarda portal subir (cold start Render pode levar até 90s)
print("  Aguardando portal responder...", flush=True)
for tentativa in range(12):
    try:
        rp = _req.get(f"{PORTAL}/api/status", timeout=20)
        if rp.status_code == 200:
            print(f"  Portal respondeu (tentativa {tentativa+1})", flush=True)
            break
    except Exception:
        pass
    print(f"  Portal ainda não respondeu — aguardando 15s... ({tentativa+1}/12)", flush=True)
    time.sleep(15)
else:
    print("  AVISO: portal não respondeu após 3 min. Tentando enviar mesmo assim.", flush=True)

for i, (rk, add, st) in enumerate(encontradas, 1):
    for tentativa in range(3):
        try:
            r = _req.get(f"{PORTAL}/api/fetch-asn/{rk}", timeout=60)
            if r.status_code == 200:
                ok += 1
                break
            elif r.status_code == 404:
                skip += 1
                break
            else:
                if tentativa == 2:
                    err += 1
                    if err <= 5:
                        print(f"  ERRO [{i}] {rk}: HTTP {r.status_code}", flush=True)
        except Exception as e:
            if tentativa == 2:
                err += 1
                if err <= 5:
                    print(f"  ERRO [{i}] {rk}: {type(e).__name__}", flush=True)
            else:
                time.sleep(10)

    if i % 20 == 0:
        print(f"  [{i}/{len(encontradas)}] OK={ok} skip={skip} err={err}", flush=True)

print(f"\n{'='*65}")
print(f"  RESULTADO")
print(f"  ASNs encontradas no período: {len(encontradas)}")
print(f"  Enviadas ao portal   : {ok}")
print(f"  Não encontradas (404): {skip}")
print(f"  Erros                : {err}")
print(f"{'='*65}")
print(f"\n  Acesse o portal e clique em Atualizar para ver os dados.")
