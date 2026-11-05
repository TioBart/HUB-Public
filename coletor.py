"""
Coletor do Hub Econômico.

Busca dados públicos (Banco Central, Focus, brapi, Yahoo Finance, FRED, FMI, Banco Mundial,
BIS e feeds RSS) e grava tudo em data/hub.json, que é o arquivo lido pela página.

Se uma fonte falhar, o coletor mantém o último dado salvo daquela seção,
então a página nunca fica em branco por causa de uma API fora do ar.

Variáveis de ambiente (opcionais):
  BRAPI_TOKEN  token da brapi.dev (cotações da B3)
  FRED_KEY     chave da API do FRED (S&P 500 e petróleo Brent)
"""
import csv
import datetime as dt
import functools
import io
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import feedparser
import requests

SAIDA = Path(__file__).resolve().parent / "data" / "hub.json"
BRAPI_TOKEN = os.getenv("BRAPI_TOKEN", "")
FRED_KEY = os.getenv("FRED_KEY", "")
TIMEOUT = 30
HOJE = dt.date.today()
HTTP = requests.Session()
HTTP.headers["User-Agent"] = "hub-economico/1.0"
OLINDA = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata"

# Códigos das séries no SGS do Banco Central.
# Confira ou troque em https://www3.bcb.gov.br/sgspub
SGS = {
    "selic": 432,          # Selic meta (% a.a., diária)
    "cdi": 4389,           # CDI anualizado base 252 (% a.a., diária)
    "ipca_mes": 433,       # IPCA variação mensal (%)
    "ipca_12m": 13522,     # IPCA acumulado em 12 meses (%)
    "inpc_mes": 188,       # INPC variação mensal (%)
    "igpm_mes": 189,       # IGP-M variação mensal (%)
    "igpdi_mes": 190,      # IGP-DI variação mensal (%)
    "ipca15_mes": 7478,    # IPCA-15 variação mensal (%)
    "ipcfipe_mes": 193,    # IPC-Fipe variação mensal (%)
    "incc_mes": 192,       # INCC variação mensal (%)
    "tjlp": 256,           # TJLP (% a.a.)
    "ibcbr": 24364,        # IBC-Br dessazonalizado (índice)
    "credito": 20539,      # Saldo da carteira de crédito total (R$ milhões)
    "inadimplencia": 21082,  # Inadimplência da carteira de crédito total (%)
    "divida_bruta": 13762, # Dívida bruta do governo geral (% do PIB)
    "dlsp": 4513,          # Dívida líquida do setor público (% do PIB)
    "reservas": 13621,     # Reservas internacionais (US$ milhões, diária)
    "tr": 226,             # Taxa Referencial (% no período), usada no rendimento da poupança
    "desemprego": 24369,   # Taxa de desocupação PNAD Contínua (%)
    "dolar": 1,           # Dólar PTAX venda (R$, diária)
    "euro": 21619,         # Euro PTAX venda (R$, diária)
    "exportacoes": 22708,  # Exportações de bens, mensal (US$ milhões)
    "importacoes": 22709,  # Importações de bens, mensal (US$ milhões)
}

ACOES = ["PETR4", "VALE3", "ITUB4", "BBDC4", "BBAS3", "WEGE3", "ABEV3", "B3SA3"]
# Nomes exibidos na página (o plano gratuito da brapi às vezes devolve só o ticker)
NOMES_ACOES = {
    "PETR4": "Petrobras PN", "VALE3": "Vale ON", "ITUB4": "Itaú Unibanco PN",
    "BBDC4": "Bradesco PN", "BBAS3": "Banco do Brasil ON", "WEGE3": "WEG ON",
    "ABEV3": "Ambev ON", "B3SA3": "B3 ON",
}

# Página "Países": código ISO de 3 letras (FMI) -> (nome, código de 2 letras do BIS)
PAISES = {
    "BRA": ("Brasil", "BR"), "USA": ("Estados Unidos", "US"), "CHN": ("China", "CN"),
    "DEU": ("Alemanha", "DE"), "JPN": ("Japão", "JP"), "GBR": ("Reino Unido", "GB"),
    "FRA": ("França", "FR"), "ITA": ("Itália", "IT"), "CAN": ("Canadá", "CA"),
    "IND": ("Índia", "IN"), "MEX": ("México", "MX"), "ARG": ("Argentina", "AR"),
    "CHL": ("Chile", "CL"), "COL": ("Colômbia", "CO"), "KOR": ("Coreia do Sul", "KR"),
    "ZAF": ("África do Sul", "ZA"),
}
INDICADORES_FMI = {
    "pib": "NGDP_RPCH",            # crescimento real do PIB (%)
    "inflacao": "PCPIPCH",         # inflação média ao consumidor (%)
    "desemprego": "LUR",           # taxa de desemprego (%)
    "divida": "GGXWDG_NGDP",       # dívida bruta do governo (% do PIB)
    "conta_corrente": "BCA_NGDPD", # saldo em conta corrente (% do PIB)
    "pib_usd": "NGDPD",            # PIB nominal (US$ bilhões)
    "pib_pc": "NGDPDPC",           # PIB per capita (US$)
}
# Plano B quando o FMI recusa a coleta: Banco Mundial (API aberta, só dados realizados).
# chave -> (código do indicador, divisor para chegar na unidade usada na página)
INDICADORES_BM = {
    "pib": ("NY.GDP.MKTP.KD.ZG", 1),
    "inflacao": ("FP.CPI.TOTL.ZG", 1),
    "desemprego": ("SL.UEM.TOTL.ZS", 1),
    "divida": ("GC.DOD.TOTL.GD.ZS", 1),
    "conta_corrente": ("BN.CAB.XOKA.GD.ZS", 1),
    "pib_usd": ("NY.GDP.MKTP.CD", 1e9),
    "pib_pc": ("NY.GDP.PCAP.CD", 1),
}

# Tabela "Juros pelo mundo": código do BIS -> (país, banco central, código ISO-3 para a inflação)
BANCOS_CENTRAIS = {
    "BR": ("Brasil", "Banco Central do Brasil", "BRA"),
    "US": ("Estados Unidos", "Federal Reserve", "USA"),
    "XM": ("Zona do euro", "Banco Central Europeu", None),
    "GB": ("Reino Unido", "Banco da Inglaterra", "GBR"),
    "JP": ("Japão", "Banco do Japão", "JPN"),
    "CN": ("China", "Banco Popular da China", "CHN"),
    "CA": ("Canadá", "Banco do Canadá", "CAN"),
    "CH": ("Suíça", "Banco Nacional Suíço", None),
    "AU": ("Austrália", "Reserve Bank of Australia", None),
    "KR": ("Coreia do Sul", "Banco da Coreia", "KOR"),
    "IN": ("Índia", "Reserve Bank of India", "IND"),
    "ID": ("Indonésia", "Bank Indonesia", None),
    "MX": ("México", "Banco de México", "MEX"),
    "CL": ("Chile", "Banco Central do Chile", "CHL"),
    "CO": ("Colômbia", "Banco de la República", "COL"),
    "PE": ("Peru", "Banco Central de Reserva do Peru", None),
    "AR": ("Argentina", "Banco Central da Argentina", "ARG"),
    "ZA": ("África do Sul", "South African Reserve Bank", "ZAF"),
    "TR": ("Turquia", "Banco Central da Turquia", None),
    "RU": ("Rússia", "Banco da Rússia", None),
}

# Mercados internacionais (Yahoo Finance; o FRED entra como reserva quando houver série equivalente)
# grupo -> [(nome, símbolo do Yahoo, unidade, casas decimais)]
MERCADOS = [
    ("Brasil", [
        ("Ibovespa", "^BVSP", "pts", 0), ("Dólar comercial", "BRL=X", "R$", 4),
        ("Euro", "EURBRL=X", "R$", 4), ("Libra", "GBPBRL=X", "R$", 4),
    ]),
    ("Estados Unidos", [
        ("S&P 500", "^GSPC", "pts", 0), ("Nasdaq", "^IXIC", "pts", 0),
        ("Dow Jones", "^DJI", "pts", 0), ("VIX (volatilidade)", "^VIX", "pts", 2),
    ]),
    ("Europa", [
        ("Euro Stoxx 50", "^STOXX50E", "pts", 0), ("DAX (Alemanha)", "^GDAXI", "pts", 0),
        ("FTSE 100 (Reino Unido)", "^FTSE", "pts", 0), ("CAC 40 (França)", "^FCHI", "pts", 0),
    ]),
    ("Ásia", [
        ("Nikkei 225 (Japão)", "^N225", "pts", 0), ("Hang Seng (Hong Kong)", "^HSI", "pts", 0),
        ("Xangai (China)", "000001.SS", "pts", 0), ("Kospi (Coreia do Sul)", "^KS11", "pts", 0),
    ]),
    ("América Latina", [
        ("Merval (Argentina)", "^MERV", "pts", 0), ("IPC (México)", "^MXX", "pts", 0),
        ("IPSA (Chile)", "^IPSA", "pts", 0),
    ]),
    ("Juros dos títulos americanos", [
        ("Treasury 3 meses", "^IRX", "%", 2), ("Treasury 5 anos", "^FVX", "%", 2),
        ("Treasury 10 anos", "^TNX", "%", 2), ("Treasury 30 anos", "^TYX", "%", 2),
    ]),
    ("Commodities", [
        ("Petróleo Brent (US$/barril)", "BZ=F", "US$", 2), ("Petróleo WTI (US$/barril)", "CL=F", "US$", 2),
        ("Ouro (US$/onça)", "GC=F", "US$", 2), ("Prata (US$/onça)", "SI=F", "US$", 2),
        ("Cobre (US$/libra)", "HG=F", "US$", 3), ("Minério de ferro (US$/t)", "TIO=F", "US$", 2),
        ("Soja (US¢/bushel)", "ZS=F", "US¢", 2), ("Milho (US¢/bushel)", "ZC=F", "US¢", 2),
        ("Café arábica (US¢/libra)", "KC=F", "US¢", 2),
    ]),
    ("Moedas", [
        ("Índice do dólar (DXY)", "DX-Y.NYB", "pts", 2), ("Euro / dólar", "EURUSD=X", "US$", 4),
        ("Dólar / iene", "JPY=X", "¥", 2), ("Dólar / yuan", "CNY=X", "CN¥", 4),
        ("Dólar / peso argentino", "ARS=X", "ARS", 2), ("Dólar / peso mexicano", "MXN=X", "MXN", 4),
    ]),
    ("Criptomoedas", [
        ("Bitcoin", "BTC-USD", "US$", 0), ("Ethereum", "ETH-USD", "US$", 0),
    ]),
]
# Séries do FRED usadas quando o Yahoo não responde
FRED_RESERVA = {
    "^GSPC": "SP500", "^IXIC": "NASDAQCOM", "^DJI": "DJIA", "^N225": "NIKKEI225", "^VIX": "VIXCLS",
    "BZ=F": "DCOILBRENTEU", "CL=F": "DCOILWTICO", "^TNX": "DGS10", "^FVX": "DGS5", "^TYX": "DGS30",
    "^IRX": "DTB3", "DX-Y.NYB": "DTWEXBGS",
}
NAVEGADOR = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Preços e taxas do Tesouro Direto (Tesouro Transparente, CSV oficial atualizado todo dia útil)
TESOURO_CSV = ("https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/"
               "resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv")

FEEDS = {
    "Google Notícias": "https://news.google.com/rss/search?q=economia+brasil&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "InfoMoney": "https://www.infomoney.com.br/feed/",
    "Agência Brasil": "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml",
}
NOTICIAS_POR_FONTE = 10


def log(msg):
    print(msg, file=sys.stderr)


# ================================================================ Banco Central

def sgs(codigo, meses=27):
    """Série do SGS entre hoje e N meses atrás, como lista de {d, v}."""
    inicio = (HOJE - dt.timedelta(days=31 * meses)).strftime("%d/%m/%Y")
    fim = HOJE.strftime("%d/%m/%Y")
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
    r = HTTP.get(url, params={"formato": "json", "dataInicial": inicio, "dataFinal": fim}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for p in r.json():
        try:
            d = dt.datetime.strptime(p["data"], "%d/%m/%Y").date()
            out.append({"d": d.isoformat(), "v": float(p["valor"])})
        except (ValueError, KeyError):
            continue
    if not out:
        raise ValueError(f"série {codigo} veio vazia")
    return out


def mensal(serie):
    """Fica com o último valor de cada mês."""
    por_mes = {}
    for p in serie:
        por_mes[p["d"][:7]] = p
    return [por_mes[m] for m in sorted(por_mes)]


def acumulado_12m(serie_mensal):
    out = []
    for i in range(11, len(serie_mensal)):
        fator = 1.0
        for p in serie_mensal[i - 11:i + 1]:
            fator *= 1 + p["v"] / 100
        out.append({"d": serie_mensal[i]["d"], "v": round((fator - 1) * 100, 2)})
    return out


def ultima_mudanca(serie):
    """Data em que o valor atual da série passou a valer."""
    atual = serie[-1]["v"]
    for i in range(len(serie) - 1, 0, -1):
        if serie[i - 1]["v"] != atual:
            return serie[i]["d"]
    return None


# ---------------------------------------------------------------- Tabelas mês a mês

def tabela_inflacao(s, linhas=13):
    """Mês | No mês | No ano | 12 meses, a partir da variação mensal."""
    out = []
    for i in range(max(11, len(s) - linhas), len(s)):
        mes = s[i]["d"][:7]
        f_ano = 1.0
        for p in s[:i + 1]:
            if p["d"][:4] == mes[:4]:
                f_ano *= 1 + p["v"] / 100
        f_12 = 1.0
        for p in s[i - 11:i + 1]:
            f_12 *= 1 + p["v"] / 100
        out.append([mes, s[i]["v"], round((f_ano - 1) * 100, 2), round((f_12 - 1) * 100, 2)])
    return {"colunas": ["Mês", "No mês (%)", "No ano (%)", "12 meses (%)"], "casas": [None, 2, 2, 2], "linhas": out}


def tabela_taxa(s, nome, casas=2, linhas=13):
    """Mês | Taxa | Variação no mês | Variação em 12 meses (em pontos percentuais)."""
    out = []
    for i in range(max(1, len(s) - linhas), len(s)):
        v = s[i]["v"]
        a12 = round(v - s[i - 12]["v"], 2) if i >= 12 else None
        out.append([s[i]["d"][:7], v, round(v - s[i - 1]["v"], 2), a12])
    return {"colunas": ["Mês", nome, "Variação no mês (p.p.)", "Variação em 12 meses (p.p.)"],
            "casas": [None, casas, 2, 2], "linhas": out}


def tabela_cambio(serie_diaria, linhas=13):
    """Mês | Cotação no fim do mês | No mês | No ano | 12 meses."""
    s = mensal(serie_diaria)
    out = []
    for i in range(max(1, len(s) - linhas), len(s)):
        v, mes = s[i]["v"], s[i]["d"][:7]
        dez = [p for p in s[:i] if p["d"][:7] == f"{int(mes[:4]) - 1}-12"]
        no_ano = round((v / dez[-1]["v"] - 1) * 100, 2) if dez else None
        a12 = round((v / s[i - 12]["v"] - 1) * 100, 2) if i >= 12 else None
        out.append([mes, v, round((v / s[i - 1]["v"] - 1) * 100, 2), no_ano, a12])
    return {"colunas": ["Mês", "Cotação no fim do mês (R$)", "No mês (%)", "No ano (%)", "12 meses (%)"],
            "casas": [None, 4, 2, 2, 2], "linhas": out}


def tabela_indice(s, nome, casas=2, linhas=13):
    """Mês | Valor | No mês (%) | 12 meses (%), para índices e saldos."""
    out = []
    for i in range(max(1, len(s) - linhas), len(s)):
        v = s[i]["v"]
        a12 = round((v / s[i - 12]["v"] - 1) * 100, 2) if i >= 12 and s[i - 12]["v"] else None
        m1 = round((v / s[i - 1]["v"] - 1) * 100, 2) if s[i - 1]["v"] else None
        out.append([s[i]["d"][:7], round(v, casas), m1, a12])
    return {"colunas": ["Mês", nome, "No mês (%)", "12 meses (%)"], "casas": [None, casas, 2, 2], "linhas": out}


def escala(serie, fator):
    return [{"d": p["d"], "v": p["v"] / fator} for p in serie]


def indicador(nome, serie, unidade, casas=2, freq="m", pontos=12, tabela=None, fonte="", grupo=""):
    s = serie[-pontos:]
    ult, ant = s[-1], (s[-2] if len(s) > 1 else None)
    return {
        "nome": nome, "grupo": grupo, "valor": ult["v"], "unidade": unidade, "casas": casas,
        "data": ult["d"], "freq": freq, "anterior": ant["v"] if ant else None,
        "serie": [p["v"] for p in s], "tabela": tabela, "fonte": fonte,
    }


def var_pct(serie):
    if len(serie) < 2 or not serie[-2]["v"]:
        return None
    return round((serie[-1]["v"] / serie[-2]["v"] - 1) * 100, 2)


# ================================================================ Seções

def coletar_sgs():
    dados = {}
    for nome, codigo in SGS.items():
        try:
            dados[nome] = sgs(codigo)
        except Exception as e:  # noqa: BLE001
            log(f"[SGS] {nome} ({codigo}) falhou: {e}")
    return dados


def secao_juros(S):
    selic = mensal(S["selic"])[-24:]
    ipca = {p["d"][:7]: p["v"] for p in mensal(S["ipca_12m"])}
    meses = [p["d"][:7] for p in selic]
    return {
        "meses": meses,
        "selic": [p["v"] for p in selic],
        "ipca12": [ipca.get(m) for m in meses],
        "selic_desde": ultima_mudanca(S["selic"]),
        "tr_mes": S["tr"][-1]["v"] if "tr" in S else None,
    }


def secao_indicadores(S):
    m = lambda chave: mensal(S[chave])  # noqa: E731
    a12 = lambda chave: acumulado_12m(m(chave))  # noqa: E731
    infl = lambda chave: (lambda: tabela_inflacao(m(chave)))  # noqa: E731
    BC, IBGE, FGV = "Banco Central do Brasil", "IBGE", "FGV"
    # (grupo, nome, chave da série, unidade, casas, frequência, série do card, tabela, fonte)
    regras = [
        ("Juros", "Selic meta", "selic", "%", 2, "m", lambda: m("selic"),
         lambda: tabela_taxa(m("selic"), "Selic (% ao ano)"), BC),
        ("Juros", "CDI (ao ano)", "cdi", "%", 2, "m", lambda: m("cdi"),
         lambda: tabela_taxa(m("cdi"), "CDI (% ao ano)"), BC),
        ("Juros", "TJLP", "tjlp", "%", 2, "m", lambda: m("tjlp"),
         lambda: tabela_taxa(m("tjlp"), "TJLP (% ao ano)"), BC),
        ("Inflação", "IPCA no mês", "ipca_mes", "%", 2, "m", lambda: m("ipca_mes"), infl("ipca_mes"), IBGE),
        ("Inflação", "IPCA em 12 meses", "ipca_12m", "%", 2, "m", lambda: m("ipca_12m"), infl("ipca_mes"), IBGE),
        ("Inflação", "IPCA-15 em 12 meses", "ipca15_mes", "%", 2, "m", lambda: a12("ipca15_mes"), infl("ipca15_mes"), IBGE),
        ("Inflação", "INPC em 12 meses", "inpc_mes", "%", 2, "m", lambda: a12("inpc_mes"), infl("inpc_mes"), IBGE),
        ("Inflação", "IGP-M em 12 meses", "igpm_mes", "%", 2, "m", lambda: a12("igpm_mes"), infl("igpm_mes"), FGV),
        ("Inflação", "IGP-DI em 12 meses", "igpdi_mes", "%", 2, "m", lambda: a12("igpdi_mes"), infl("igpdi_mes"), FGV),
        ("Inflação", "INCC em 12 meses", "incc_mes", "%", 2, "m", lambda: a12("incc_mes"), infl("incc_mes"), FGV),
        ("Inflação", "IPC-Fipe em 12 meses", "ipcfipe_mes", "%", 2, "m", lambda: a12("ipcfipe_mes"), infl("ipcfipe_mes"), "Fipe"),
        ("Atividade e emprego", "IBC-Br (prévia do PIB)", "ibcbr", "índice", 2, "m", lambda: m("ibcbr"),
         lambda: tabela_indice(m("ibcbr"), "Índice (dessazonalizado)"), BC),
        ("Atividade e emprego", "Desemprego", "desemprego", "%", 1, "m", lambda: m("desemprego"),
         lambda: tabela_taxa(m("desemprego"), "Taxa de desocupação (%)", casas=1), "IBGE, PNAD Contínua"),
        ("Crédito", "Saldo de crédito", "credito", "R$ tri", 2, "m", lambda: escala(m("credito"), 1e6),
         lambda: tabela_indice(escala(m("credito"), 1e6), "Saldo (R$ trilhões)"), BC),
        ("Crédito", "Inadimplência", "inadimplencia", "%", 2, "m", lambda: m("inadimplencia"),
         lambda: tabela_taxa(m("inadimplencia"), "Inadimplência (%)"), BC),
        ("Contas públicas", "Dívida bruta (% do PIB)", "divida_bruta", "%", 1, "m", lambda: m("divida_bruta"),
         lambda: tabela_taxa(m("divida_bruta"), "Dívida bruta (% do PIB)", casas=1), BC),
        ("Contas públicas", "Dívida líquida (% do PIB)", "dlsp", "%", 1, "m", lambda: m("dlsp"),
         lambda: tabela_taxa(m("dlsp"), "Dívida líquida (% do PIB)", casas=1), BC),
        ("Setor externo e câmbio", "Reservas internacionais", "reservas", "US$ bi", 1, "d",
         lambda: escala(S["reservas"], 1e3)[-30:],
         lambda: tabela_indice(escala(m("reservas"), 1e3), "Reservas (US$ bilhões)", casas=1), BC),
        ("Setor externo e câmbio", "Dólar (PTAX venda)", "dolar", "R$", 4, "d", lambda: S["dolar"][-30:],
         lambda: tabela_cambio(S["dolar"]), BC),
        ("Setor externo e câmbio", "Euro (PTAX venda)", "euro", "R$", 4, "d", lambda: S["euro"][-30:],
         lambda: tabela_cambio(S["euro"]), BC),
    ]
    out = []
    for grupo, nome, chave, un, casas, freq, serie, tabela, fonte in regras:
        if chave not in S or (chave == "ipca_12m" and "ipca_mes" not in S):
            continue
        try:
            try:
                tab = tabela()
            except Exception as e:  # noqa: BLE001
                log(f"[tabela] {nome}: {e}")
                tab = None
            out.append(indicador(nome, serie(), un, casas, freq, pontos=30 if freq == "d" else 12,
                                 tabela=tab, fonte=fonte, grupo=grupo))
        except Exception as e:  # noqa: BLE001
            log(f"[indicador] {nome}: {e}")
    if not out:
        raise ValueError("nenhum indicador disponível")
    return out


def secao_balanca(S):
    exp = {p["d"][:7]: p["v"] for p in mensal(S["exportacoes"])}
    imp = {p["d"][:7]: p["v"] for p in mensal(S["importacoes"])}
    meses = sorted(set(exp) & set(imp))[-12:]
    return {
        "meses": meses,
        "exportacoes": [round(exp[m] / 1000, 2) for m in meses],  # US$ bilhões
        "importacoes": [round(imp[m] / 1000, 2) for m in meses],
    }


def olinda(recurso, filtro, top=60):
    url = (f"{OLINDA}/{recurso}?$top={top}&$format=json&$orderby=Data%20desc"
           f"&$filter={quote(filtro)}")
    r = HTTP.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("value", [])


def secao_focus():
    anos = {HOJE.year, HOJE.year + 1, HOJE.year + 2}
    out = []
    for ind in ["IPCA", "PIB Total", "Câmbio", "Selic"]:
        try:
            linhas = olinda("ExpectativasMercadoAnuais", f"Indicador eq '{ind}' and baseCalculo eq 0", top=30)
        except Exception as e:  # noqa: BLE001
            log(f"[Focus] {ind}: {e}")
            continue
        if not linhas:
            continue
        ultima = linhas[0]["Data"]
        for l in linhas:
            if l["Data"] == ultima and int(l["DataReferencia"]) in anos:
                out.append({"indicador": ind, "ano": int(l["DataReferencia"]),
                            "mediana": l["Mediana"], "data": ultima})
    if not out:
        raise ValueError("Focus sem dados")
    return out


def secao_selic_focus():
    """Mediana da Selic esperada para cada reunião do Copom, no boletim Focus mais recente."""
    linhas = olinda("ExpectativasMercadoSelic", "baseCalculo eq 0", top=80)
    if not linhas:
        raise ValueError("Focus Selic sem dados")
    ultima = linhas[0]["Data"]
    reunioes = []
    for l in linhas:
        if l.get("Data") != ultima:
            continue
        m = re.match(r"R(\d+)/(\d{4})", l.get("Reuniao", ""))
        if not m:
            continue
        reunioes.append({
            "reuniao": l["Reuniao"], "ordem": int(m.group(2)) * 10 + int(m.group(1)),
            "mediana": l.get("Mediana"), "minimo": l.get("Minimo"), "maximo": l.get("Maximo"),
            "respondentes": l.get("numeroRespondentes"),
        })
    reunioes.sort(key=lambda x: x["ordem"])
    return {"data": ultima, "reunioes": reunioes}


def brapi(ticker):
    params = {"token": BRAPI_TOKEN} if BRAPI_TOKEN else {}
    r = HTTP.get(f"https://brapi.dev/api/quote/{quote(ticker)}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["results"][0]


def fred(serie_id):
    url = "https://api.stlouisfed.org/fred/series/observations"
    r = HTTP.get(url, params={"series_id": serie_id, "api_key": FRED_KEY, "file_type": "json",
                              "sort_order": "desc", "limit": 10}, timeout=TIMEOUT)
    r.raise_for_status()
    obs = [o for o in r.json()["observations"] if o["value"] not in (".", "")]
    atual, anterior = float(obs[0]["value"]), float(obs[1]["value"])
    return atual, round((atual / anterior - 1) * 100, 2)


def yahoo(simbolo):
    """Preço, variação no dia, variação em 1 mês e fechamentos do último mês, pelo Yahoo Finance."""
    ultimo_erro = None
    for host in ("query1", "query2"):
        try:
            r = HTTP.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{quote(simbolo)}",
                         params={"range": "1mo", "interval": "1d"}, headers={"User-Agent": NAVEGADOR}, timeout=TIMEOUT)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            fech = [c for c in (res["indicators"]["quote"][0].get("close") or []) if c is not None]
            preco = res["meta"].get("regularMarketPrice") or (fech[-1] if fech else None)
            if preco is None:
                raise ValueError("sem preço")
            ant = fech[-2] if len(fech) >= 2 else res["meta"].get("chartPreviousClose")
            var = round((preco / ant - 1) * 100, 2) if ant else None
            var_mes = round((preco / fech[0] - 1) * 100, 2) if fech else None
            return preco, var, var_mes, [round(c, 4) for c in fech]
        except Exception as e:  # noqa: BLE001
            ultimo_erro = e
    raise ultimo_erro


def fred_serie(serie_id, n=25):
    url = "https://api.stlouisfed.org/fred/series/observations"
    r = HTTP.get(url, params={"series_id": serie_id, "api_key": FRED_KEY, "file_type": "json",
                              "sort_order": "desc", "limit": n * 2}, timeout=TIMEOUT)
    r.raise_for_status()
    obs = [float(o["value"]) for o in r.json()["observations"] if o["value"] not in (".", "")][:n]
    obs.reverse()
    preco = obs[-1]
    return preco, round((preco / obs[-2] - 1) * 100, 2), round((preco / obs[0] - 1) * 100, 2), obs


def cotacao(simbolo):
    try:
        return yahoo(simbolo)
    except Exception as e:  # noqa: BLE001
        if FRED_KEY and simbolo in FRED_RESERVA:
            log(f"[Yahoo] {simbolo}: {e}; usando o FRED")
            return fred_serie(FRED_RESERVA[simbolo])
        raise


def secao_mercado(S):
    grupos = []
    for grupo, itens in MERCADOS:
        linhas = []
        for nome, simbolo, un, casas in itens:
            try:
                preco, var, var_mes, serie = cotacao(simbolo)
                linhas.append({"nome": nome, "simbolo": simbolo, "valor": preco, "unidade": un, "casas": casas,
                               "var": var, "var_mes": var_mes, "serie": serie})
            except Exception as e:  # noqa: BLE001
                log(f"[mercado] {nome} ({simbolo}): {e}")
        if linhas:
            grupos.append({"nome": grupo, "itens": linhas})

    def pega(simbolo):
        for g in grupos:
            for x in g["itens"]:
                if x["simbolo"] == simbolo:
                    return x
        return None

    # Destaques usados no resumo: Ibovespa pela brapi, câmbio pela PTAX, o resto pelos mercados
    indices = []
    try:
        q = brapi("^BVSP")
        indices.append({"nome": "Ibovespa", "valor": q.get("regularMarketPrice"), "unidade": "pts",
                        "casas": 0, "var": q.get("regularMarketChangePercent")})
    except Exception as e:  # noqa: BLE001
        log(f"[brapi] Ibovespa: {e}")
        x = pega("^BVSP")
        if x:
            indices.append({k: x[k] for k in ("nome", "valor", "unidade", "casas", "var")})
    for nome, chave in [("Dólar", "dolar"), ("Euro", "euro")]:
        if chave in S:
            indices.append({"nome": nome, "valor": S[chave][-1]["v"], "unidade": "R$",
                            "casas": 4, "var": var_pct(S[chave])})
    for nome, simbolo in [("S&P 500", "^GSPC"), ("Petróleo Brent", "BZ=F"), ("Ouro", "GC=F"), ("Bitcoin", "BTC-USD")]:
        x = pega(simbolo)
        if x:
            indices.append({"nome": nome, "valor": x["valor"], "unidade": x["unidade"], "casas": x["casas"], "var": x["var"]})

    acoes = []
    for t in ACOES:
        try:
            q = brapi(t)
            nome = NOMES_ACOES.get(t) or q.get("longName") or q.get("shortName") or q["symbol"]
            acoes.append({"ticker": q["symbol"], "nome": nome,
                          "preco": q.get("regularMarketPrice"), "var": q.get("regularMarketChangePercent")})
        except Exception as e:  # noqa: BLE001
            log(f"[brapi] {t}: {e}")
    if not indices and not acoes and not grupos:
        raise ValueError("mercado sem dados")
    return {"indices": indices, "acoes": acoes, "grupos": grupos}


# ---------------------------------------------------------------- Mundo

def fmi(indicador):
    url = f"https://www.imf.org/external/datamapper/api/v1/{indicador}/" + "/".join(PAISES)
    r = HTTP.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("values", {}).get(indicador, {})


def banco_mundial(chave, ano):
    """Mesmo formato do fmi(), pelo Banco Mundial: {ISO3: {ano: valor}}."""
    codigo, div = INDICADORES_BM[chave]
    url = f"https://api.worldbank.org/v2/country/{';'.join(PAISES)}/indicator/{codigo}"
    r = HTTP.get(url, params={"format": "json", "per_page": 2000, "date": f"{ano - 10}:{ano}"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for o in (r.json()[1] or []):
        if o["value"] is not None:
            out.setdefault(o["countryiso3code"], {})[o["date"]] = o["value"] / div
    return out


@functools.lru_cache(maxsize=1)
def bis_historico():
    """Últimos 25 meses do juro básico de cada banco central (base WS_CBPOL do BIS)."""
    codigos = sorted(set(BANCOS_CENTRAIS) | {c for _, c in PAISES.values()})
    url = f"https://stats.bis.org/api/v1/data/WS_CBPOL/M.{'+'.join(codigos)}/all"
    r = HTTP.get(url, params={"lastNObservations": 25, "detail": "dataonly", "format": "csv"}, timeout=TIMEOUT)
    r.raise_for_status()
    hist = {}
    for linha in csv.DictReader(io.StringIO(r.text)):
        try:
            hist.setdefault(linha["REF_AREA"], []).append({"d": linha["TIME_PERIOD"][:7], "v": float(linha["OBS_VALUE"])})
        except (KeyError, ValueError):
            continue
    for k in hist:
        hist[k].sort(key=lambda p: p["d"])
    return hist


def secao_paises(S):
    ano = HOJE.year
    anos = {str(a) for a in range(ano - 10, ano + 5)}
    series = {cod: {} for cod in PAISES}
    fmi_ok = False
    for chave, ind in INDICADORES_FMI.items():
        try:
            dados = fmi(ind)
            fmi_ok = True
        except Exception as e:  # noqa: BLE001
            log(f"[FMI] {ind}: {e}; tentando o Banco Mundial")
            try:
                dados = banco_mundial(chave, ano)
            except Exception as e2:  # noqa: BLE001
                log(f"[Banco Mundial] {chave}: {e2}")
                continue
        for cod in PAISES:
            valores = dados.get(cod, {})
            series[cod][chave] = {a: round(float(v), 2) for a, v in valores.items() if a in anos and v is not None}
    if not any(series[c] for c in PAISES):
        raise ValueError("FMI e Banco Mundial sem dados")

    if fmi_ok:
        ref, proj, fonte = ano, ano, "FMI"
    else:  # só dados realizados: usa o último ano com PIB divulgado
        ref = max((int(a) for c in PAISES for a in series[c].get("pib", {})), default=ano)
        proj, fonte = ref + 1, "Banco Mundial"

    try:
        hist = bis_historico()
    except Exception as e:  # noqa: BLE001
        log(f"[BIS] juros: {e}")
        hist = {}

    lista = []
    for cod, (nome, iso2) in PAISES.items():
        h = hist.get(iso2)
        j, data = (h[-1]["v"], h[-1]["d"]) if h else (None, None)
        if cod == "BRA" and "selic" in S:  # a Selic do Banco Central é mais atual
            j, data = S["selic"][-1]["v"], S["selic"][-1]["d"][:7]
        lista.append({"codigo": cod, "nome": nome, "juros": j, "juros_data": data, "series": series[cod]})
    return {"ano": ref, "proj_desde": proj, "fonte": fonte, "lista": lista}


def secao_juros_mundo(S, paises):
    """Juro básico de cada banco central, com variação em 12 meses e juro real aproximado."""
    hist = bis_historico()
    ano = str((paises or {}).get("ano") or HOJE.year)
    inflacao = {}
    for p in (paises or {}).get("lista", []):
        v = p.get("series", {}).get("inflacao", {}).get(ano)
        if v is not None:
            inflacao[p["codigo"]] = v
    lista = []
    for iso2, (nome, banco, iso3) in BANCOS_CENTRAIS.items():
        h = hist.get(iso2)
        if iso2 == "BR" and "selic" in S:
            h = [{"d": p["d"][:7], "v": p["v"]} for p in mensal(S["selic"])]
            desde = ultima_mudanca(S["selic"])
        else:
            desde = ultima_mudanca(h) if h else None
        if not h:
            continue
        taxa = h[-1]["v"]
        var12 = round(taxa - h[-13]["v"], 2) if len(h) >= 13 else None
        inf = inflacao.get(iso3) if iso3 else None
        real = round(((1 + taxa / 100) / (1 + inf / 100) - 1) * 100, 2) if inf is not None else None
        lista.append({"codigo": iso2, "pais": nome, "banco": banco, "taxa": taxa, "data": h[-1]["d"],
                      "desde": desde, "var12": var12, "inflacao": inf, "real": real})
    if not lista:
        raise ValueError("sem juros de outros países")
    lista.sort(key=lambda x: -x["taxa"])
    return {"ano_inflacao": int(ano), "lista": lista}


def _num_br(txt):
    txt = (txt or "").strip().replace(".", "").replace(",", ".")
    return float(txt) if txt else None


def _data_br(txt):
    return dt.datetime.strptime(txt.strip(), "%d/%m/%Y").date()


def secao_tesouro():
    """Taxas dos títulos do Tesouro Direto no último dia disponível, com a variação em uma semana."""
    r = HTTP.get(TESOURO_CSV, timeout=180)
    r.raise_for_status()
    try:
        texto = r.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = r.content.decode("latin-1")
    leitor = csv.reader(io.StringIO(texto), delimiter=";")
    cab = [c.strip().lower() for c in next(leitor)]

    def col(*partes):
        for i, c in enumerate(cab):
            if all(p in c for p in partes):
                return i
        raise KeyError(" ".join(partes))

    i_tipo, i_venc, i_base = col("tipo"), col("vencimento"), col("data base")
    i_tc, i_tv = col("taxa", "compra"), col("taxa", "venda")
    i_pc, i_pv = col("pu", "compra"), col("pu", "venda")
    linhas = []
    for l in leitor:
        try:
            linhas.append((l[i_tipo].strip(), _data_br(l[i_venc]), _data_br(l[i_base]),
                           _num_br(l[i_tc]), _num_br(l[i_tv]), _num_br(l[i_pc]), _num_br(l[i_pv])))
        except (ValueError, IndexError):
            continue
    if not linhas:
        raise ValueError("CSV do Tesouro sem linhas")
    ultima = max(x[2] for x in linhas)
    datas = sorted({x[2] for x in linhas if x[2] <= ultima - dt.timedelta(days=6)})
    ref = datas[-1] if datas else None
    antes = {(x[0], x[1]): x[4] for x in linhas if x[2] == ref}
    titulos = []
    for tipo, venc, base, tc, tv, pc, pv in linhas:
        if base != ultima or venc <= ultima:
            continue
        a = antes.get((tipo, venc))
        titulos.append({
            "tipo": tipo, "vencimento": venc.isoformat(), "anos": round((venc - ultima).days / 365.25, 2),
            "taxa_compra": tc or None, "taxa_venda": tv, "pu_compra": pc or None, "pu_venda": pv,
            "var_semana": round(tv - a, 2) if a is not None and tv is not None else None,
        })
    titulos.sort(key=lambda t: (t["tipo"], t["vencimento"]))
    return {"data": ultima.isoformat(), "data_comparacao": ref.isoformat() if ref else None, "titulos": titulos}


def secao_noticias():
    out = []
    for fonte, url in FEEDS.items():
        try:
            feed = feedparser.parse(url, agent=HTTP.headers["User-Agent"])
        except Exception as e:  # noqa: BLE001
            log(f"[RSS] {fonte}: {e}")
            continue
        for item in feed.entries[:NOTICIAS_POR_FONTE]:
            quando = item.get("published_parsed") or item.get("updated_parsed")
            data = dt.datetime(*quando[:6], tzinfo=dt.timezone.utc).isoformat() if quando else None
            titulo = (item.get("title") or "").strip()
            link = item.get("link") or ""
            if titulo and link.startswith("http"):
                out.append({"titulo": titulo, "fonte": fonte, "link": link, "data": data})
    if not out:
        raise ValueError("nenhuma notícia")
    out.sort(key=lambda n: n["data"] or "", reverse=True)
    return out[:40]


# ================================================================ Principal

def main():
    anterior = {}
    if SAIDA.exists():
        try:
            anterior = json.loads(SAIDA.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    S = coletar_sgs()
    hub = {"atualizado": dt.datetime.now(dt.timezone.utc).isoformat()}
    tarefas = {
        "juros": lambda: secao_juros(S),
        "indicadores": lambda: secao_indicadores(S),
        "mercado": lambda: secao_mercado(S),
        "balanca": lambda: secao_balanca(S),
        "focus": secao_focus,
        "selic_focus": secao_selic_focus,
        "tesouro": secao_tesouro,
        "paises": lambda: secao_paises(S),
        "juros_mundo": lambda: secao_juros_mundo(S, hub.get("paises")),
        "noticias": secao_noticias,
    }
    for chave, fn in tarefas.items():
        try:
            hub[chave] = fn()
        except Exception as e:  # noqa: BLE001
            log(f"[{chave}] falhou, mantendo o dado anterior: {e}")
            if chave in anterior:
                hub[chave] = anterior[chave]

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps(hub, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"OK: {SAIDA}")


if __name__ == "__main__":
    main()
