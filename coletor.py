"""
Coletor do Hub Econômico.

Busca dados públicos (Banco Central, Focus, brapi, FRED, FMI, Banco Mundial, BIS
e feeds RSS) e grava tudo em data/hub.json, que é o arquivo lido pela página.

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
    "desemprego": 24369,   # Taxa de desocupação PNAD Contínua (%)
    "dolar": 1,            # Dólar PTAX venda (R$, diária)
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


def indicador(nome, serie, unidade, casas=2, freq="m", pontos=12, tabela=None, fonte=""):
    s = serie[-pontos:]
    ult, ant = s[-1], (s[-2] if len(s) > 1 else None)
    return {
        "nome": nome, "valor": ult["v"], "unidade": unidade, "casas": casas,
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
    }


def secao_indicadores(S):
    m = lambda chave: mensal(S[chave])  # noqa: E731
    regras = [
        ("Selic meta", "selic", "%", 2, "m", lambda: m("selic"),
         lambda: tabela_taxa(m("selic"), "Selic (% ao ano)"), "Banco Central do Brasil"),
        ("CDI (ao ano)", "cdi", "%", 2, "m", lambda: m("cdi"),
         lambda: tabela_taxa(m("cdi"), "CDI (% ao ano)"), "Banco Central do Brasil"),
        ("IPCA no mês", "ipca_mes", "%", 2, "m", lambda: m("ipca_mes"),
         lambda: tabela_inflacao(m("ipca_mes")), "IBGE"),
        ("IPCA em 12 meses", "ipca_12m", "%", 2, "m", lambda: m("ipca_12m"),
         lambda: tabela_inflacao(m("ipca_mes")), "IBGE"),
        ("INPC em 12 meses", "inpc_mes", "%", 2, "m", lambda: acumulado_12m(m("inpc_mes")),
         lambda: tabela_inflacao(m("inpc_mes")), "IBGE"),
        ("IGP-M em 12 meses", "igpm_mes", "%", 2, "m", lambda: acumulado_12m(m("igpm_mes")),
         lambda: tabela_inflacao(m("igpm_mes")), "FGV"),
        ("IGP-DI em 12 meses", "igpdi_mes", "%", 2, "m", lambda: acumulado_12m(m("igpdi_mes")),
         lambda: tabela_inflacao(m("igpdi_mes")), "FGV"),
        ("Desemprego", "desemprego", "%", 1, "m", lambda: m("desemprego"),
         lambda: tabela_taxa(m("desemprego"), "Taxa de desocupação (%)", casas=1), "IBGE, PNAD Contínua"),
        ("Dólar (PTAX venda)", "dolar", "R$", 4, "d", lambda: S["dolar"][-30:],
         lambda: tabela_cambio(S["dolar"]), "Banco Central do Brasil"),
        ("Euro (PTAX venda)", "euro", "R$", 4, "d", lambda: S["euro"][-30:],
         lambda: tabela_cambio(S["euro"]), "Banco Central do Brasil"),
    ]
    out = []
    for nome, chave, un, casas, freq, serie, tabela, fonte in regras:
        if chave not in S or (chave == "ipca_12m" and "ipca_mes" not in S):
            continue
        try:
            try:
                tab = tabela()
            except Exception as e:  # noqa: BLE001
                log(f"[tabela] {nome}: {e}")
                tab = None
            out.append(indicador(nome, serie(), un, casas, freq, pontos=30 if freq == "d" else 12,
                                 tabela=tab, fonte=fonte))
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


def secao_mercado(S):
    indices, acoes = [], []
    try:
        q = brapi("^BVSP")
        indices.append({"nome": "Ibovespa", "valor": q.get("regularMarketPrice"), "unidade": "pts",
                        "casas": 0, "var": q.get("regularMarketChangePercent")})
    except Exception as e:  # noqa: BLE001
        log(f"[brapi] Ibovespa: {e}")
    for nome, chave in [("Dólar", "dolar"), ("Euro", "euro")]:
        if chave in S:
            indices.append({"nome": nome, "valor": S[chave][-1]["v"], "unidade": "R$",
                            "casas": 4, "var": var_pct(S[chave])})
    if FRED_KEY:
        for nome, sid, un, casas in [("S&P 500", "SP500", "pts", 0), ("Petróleo Brent", "DCOILBRENTEU", "US$", 2)]:
            try:
                v, var = fred(sid)
                indices.append({"nome": nome, "valor": v, "unidade": un, "casas": casas, "var": var})
            except Exception as e:  # noqa: BLE001
                log(f"[FRED] {sid}: {e}")
    for t in ACOES:
        try:
            q = brapi(t)
            nome = NOMES_ACOES.get(t) or q.get("longName") or q.get("shortName") or q["symbol"]
            acoes.append({"ticker": q["symbol"], "nome": nome,
                          "preco": q.get("regularMarketPrice"), "var": q.get("regularMarketChangePercent")})
        except Exception as e:  # noqa: BLE001
            log(f"[brapi] {t}: {e}")
    if not indices and not acoes:
        raise ValueError("mercado sem dados")
    return {"indices": indices, "acoes": acoes}


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
