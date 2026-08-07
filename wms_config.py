"""
Configuração WMS ION PRD — lê de variáveis de ambiente (cloud) ou config.py local (dev).
"""
import os
import sys

def _get_config():
    """Tenta variáveis de ambiente primeiro; cai back para config.py local."""
    tenant = os.environ.get("ION_TENANT")

    if tenant:
        # ── Modo cloud: tudo via env vars ──────────────────────────────────
        return {
            "env":       os.environ.get("ION_ENV", "PRD"),
            "tenant":    tenant,
            "token_url": os.environ.get("ION_TOKEN_URL",
                         f"https://mingle-sso.inforcloudsuite.com:443/{tenant}/as/token.oauth2"),
            "base_url":  os.environ.get("ION_BASE_URL",
                         f"https://mingle-ionapi.inforcloudsuite.com/{tenant}/WM/wmwebservice_rest"),
            "warehouse": os.environ.get("ION_WAREHOUSE",
                         f"{tenant}_HOTPINKYOUNGDOVE_PRD_SCE_PRD_0_wmwhse1"),
            "owner":     os.environ.get("ION_OWNER", "BURN"),
            "ci":        os.environ.get("ION_CI", ""),
            "cs":        os.environ.get("ION_CS", ""),
            "saak":      os.environ.get("ION_SAAK", ""),
            "sask":      os.environ.get("ION_SASK", ""),
        }

    # ── Modo local: importa config.py do diretório PRD ────────────────────
    ION_PRD = os.environ.get(
        "ION_PRD_PATH",
        r"C:\Users\carlos.tatagiba\OneDrive\Consultoria\Claude AI\ION\PRD"
    )
    if ION_PRD not in sys.path:
        sys.path.insert(0, ION_PRD)

    try:
        from config import CONFIG  # noqa: E402
        return CONFIG
    except ImportError as e:
        raise RuntimeError(
            "Não foi possível carregar as credenciais WMS. "
            "Configure as variáveis de ambiente ION_TENANT, ION_CI, ION_CS, ION_SAAK, ION_SASK "
            f"ou verifique o caminho ION_PRD_PATH. Erro: {e}"
        )


CONFIG = _get_config()
