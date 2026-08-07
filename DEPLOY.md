# Deploy — Painel BURN RJ na Web

## Passo 1 — Criar repositório GitHub (privado)

1. Acesse https://github.com/new
2. Crie um repositório **privado** chamado `painel-burn-rj`
3. **NÃO** inicialize com README (deixe vazio)

## Passo 2 — Fazer push do código

Abra o terminal (cmd ou PowerShell) **na pasta do projeto** e execute:

```
git init
git add .
git commit -m "Painel Recebimento PA BURN RJ — deploy inicial"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/painel-burn-rj.git
git push -u origin main
```

> ⚠️ O `.gitignore` já exclui `config.py` e `receipt_keys_cache.json` — as credenciais nunca vão para o repositório.

## Passo 3 — Deploy no Render.com

1. Acesse https://render.com e crie conta gratuita
2. Clique em **New → Web Service**
3. Conecte ao GitHub e selecione `painel-burn-rj`
4. Configure:
   - **Name**: painel-burn-rj
   - **Region**: Oregon (US West)
   - **Branch**: main
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --workers 1 --timeout 120`
   - **Plan**: Free

5. Vá em **Environment → Add Environment Variable** e adicione:

| Variável | Valor |
|---|---|
| `ION_TENANT` | `LGUQQUWZZUCXNAR3_PRD` |
| `ION_OWNER` | `BURN` |
| `ION_CI` | (valor de `ci` em config.py) |
| `ION_CS` | (valor de `cs` em config.py) |
| `ION_SAAK` | (valor de `saak` em config.py) |
| `ION_SASK` | (valor de `sask` em config.py) |

6. Clique em **Create Web Service**
7. Aguarde o build (3–5 minutos)
8. A URL pública ficará em: `https://painel-burn-rj.onrender.com`

## Alternativa rápida — ngrok (sem deploy, acesso imediato)

Se quiser acessar o servidor local de qualquer lugar sem fazer deploy:

1. Baixe ngrok: https://ngrok.com/download
2. Execute localmente: `python app.py` (ou `iniciar.bat`)
3. Em outro terminal: `ngrok http 5002`
4. Use a URL gerada (ex: `https://abc123.ngrok.io`)

> ⚠️ ngrok grátis: URL muda a cada sessão. Para URL fixa use plano pago ou Render.

## Atualizar após mudanças no código

```
git add .
git commit -m "atualização: descrição da mudança"
git push origin main
```
O Render detecta o push e faz redeploy automático em ~2 minutos.
