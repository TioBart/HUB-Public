"""
Coletor do Hub Econômico.

Busca dados públicos (Banco Central, Focus, brapi, FRED, FMI, BIS e feeds RSS)
e grava tudo em data/hub.json, que é o arquivo lido pela página.

Se uma fonte falhar, o coletor mantém o último dado salvo daquela seção,
então a página nunca fica em branco por causa de uma API fora do ar.

Variáveis de ambiente (opcionais):
  BRAPI_TOKEN  token da brapi.dev (cotações da B3)
  FRED_KEY     chave da API do FRED (S&P 500 e petróleo Brent)
"""
import csv
import datetime as dt
import io
import json
import os
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

# Códigos das séries no SGS do Banco Central.
# Confira ou troque em https://www3.bcb.gov.br/sgspub
SGS = {
    "selic": 432,          # Selic meta (% a.a., diária)
    "cdi": 4389,           # CDI anualizado base 252 (% a.a., diária)
    "ipca_mes": 433,       # IPCA variação mensal (%)
    "ipca_12m": 13522,     # IPCA acumulado em 12 meses (%)
    "igpm_mes": 189,       # IGP-M variação mensal (%)
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

# Países da página "Países": código ISO de 3 letras (FMI) -> (nome, código de 2 letras do BIS)
PAISES = {
    "BRA": ("Brasil", "BR"), "USA": ("Estados Unidos", "US"), "CHN": ("China", "CN"),
    "DEU": ("Alemanha", "DE"), "JPN": ("Japão", "JP"), "GBR": ("Reino Unido", "GB"),
    "FRA": ("França", "FR"), "ITA": ("Itália", "IT"), "CAN": ("Canadá", "CA"),
    "IND": ("Índia", "IN"), "MEX": ("México", "MX"), "ARG": ("Argentina", "AR"),
    "CHL": ("Chile", "CL"), "COL": ("Colômbia", "CO"), "KOR": ("Coreia do Sul", "KR"),
    "ZAF": ("África do Sul", "ZA"),
}
# Indicadores do FMI (World Economic Outlook, API DataMapper)
INDICADORES_FMI = {
    "pib": "NGDP_RPCH",            # crescimento real do PIB (%)
    "inflacao": "PCPIPCH",         # inflação média ao consumidor (%)
    "desemprego": "LUR",           # taxa de desemprego (%)
    "divida": "GGXWDG_NGDP",       # dívida bruta do governo (% do PIB)
    "conta_corrente": "BCA_NGDPD", # saldo em conta corrente (% do PIB)
    "pib_usd": "NGDPD",            # PIB nominal (US$ bilhões)
    "pib_pc": "NGDPDPC",           # PIB per capita (US$)
}

# Plano B: Banco Mundial (API aberta). Só tem dados já realizados, sem projeções.
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

FEEDS = {
    "Google Notícias": "https://news.google.com/rss/search?q=economia+brasil&hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "InfoMoney": "https://www.infomoney.com.br/feed/",
    "Agência Brasil": "https://agenciabrasil.ebc.com.br/rss/economia/feed.xml",
}
NOTICIAS_POR_FONTE = 10


def log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- Banco Central

def sgs(codigo, meses=26):
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


def indicador(nome, serie, unidade, casas=2, freq="m", pontos=12):
    s = serie[-pontos:]
    ult, ant = s[-1], (s[-2] if len(s) > 1 else None)
    return {
        "nome": nome, "valor": ult["v"], "unidade": unidade, "casas": casas,
        "data": ult["d"], "freq": freq, "anterior": ant["v"] if ant else None,
        "serie": [p["v"] for p in s],
    }


def var_pct(serie):
    if len(serie) < 2 or not serie[-2]["v"]:
        return None
    return round((serie[-1]["v"] / serie[-2]["v"] - 1) * 100, 2)


# ---------------------------------------------------------------- Seções

def coletar_sgs():
    """Baixa todas as séries de uma vez para reaproveitar entre as seções."""
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
    }


def secao_indicadores(S):
    out = []
    regras = [
        ("Selic meta", "selic", "%", 2, "m", mensal),
        ("CDI (ao ano)", "cdi", "%", 2, "m", mensal),
        ("IPCA no mês", "ipca_mes", "%", 2, "m", mensal),
        ("IPCA em 12 meses", "ipca_12m", "%", 2, "m", mensal),
        ("IGP-M em 12 meses", "igpm_mes", "%", 2, "m", lambda s: acumulado_12m(mensal(s))),
        ("Desemprego", "desemprego", "%", 1, "m", mensal),
        ("Dólar (PTAX venda)", "dolar", "R$", 4, "d", lambda s: s[-30:]),
        ("Euro (PTAX venda)", "euro", "R$", 4, "d", lambda s: s[-30:]),
    ]
    for nome, chave, un, casas, freq, prep in regras:
        if chave in S:
            try:
                out.append(indicador(nome, prep(S[chave]), un, casas, freq, pontos=30 if freq == "d" else 12))
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


def secao_focus():
    base = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
    anos = {HOJE.year, HOJE.year + 1, HOJE.year + 2}
    out = []
    for ind in ["IPCA", "PIB Total", "Câmbio", "Selic"]:
        filtro = quote(f"Indicador eq '{ind}' and baseCalculo eq 0")
        url = (f"{base}?$top=30&$format=json&$orderby=Data%20desc"
               f"&$select=Indicador,Data,DataReferencia,Mediana&$filter={filtro}")
        try:
            r = HTTP.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            linhas = r.json().get("value", [])
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


def brapi(ticker):
    params = {"token": BRAPI_TOKEN} if BRAPI_TOKEN else {}
    r = HTTP.get(f"https://brapi.dev/api/quote/{quote(ticker)}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    q = r.json()["results"][0]
    return q


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


def fmi(indicador):
    url = f"https://www.imf.org/external/datamapper/api/v1/{indicador}/" + "/".join(PAISES)
    r = HTTP.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("values", {}).get(indicador, {})


def banco_mundial(chave, ano):
    """Mesma ideia do fmi(), pelo Banco Mundial: {ISO3: {ano: valor}}."""
    codigo, div = INDICADORES_BM[chave]
    url = f"https://api.worldbank.org/v2/country/{';'.join(PAISES)}/indicator/{codigo}"
    r = HTTP.get(url, params={"format": "json", "per_page": 2000, "date": f"{ano - 10}:{ano}"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for o in (r.json()[1] or []):
        if o["value"] is not None:
            out.setdefault(o["countryiso3code"], {})[o["date"]] = o["value"] / div
    return out


def bis_juros():
    """Último juro básico de cada banco central, pela base de política monetária do BIS."""
    codigos = "+".join(c for _, c in PAISES.values())
    url = f"https://stats.bis.org/api/v1/data/WS_CBPOL/M.{codigos}/all"
    r = HTTP.get(url, params={"lastNObservations": 1, "detail": "dataonly", "format": "csv"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = {}
    for linha in csv.DictReader(io.StringIO(r.text)):
        try:
            out[linha["REF_AREA"]] = (float(linha["OBS_VALUE"]), linha["TIME_PERIOD"][:7])
        except (KeyError, ValueError):
            continue
    return out


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
        juros = bis_juros()
    except Exception as e:  # noqa: BLE001
        log(f"[BIS] juros: {e}")
        juros = {}

    lista = []
    for cod, (nome, iso2) in PAISES.items():
        j, data = juros.get(iso2, (None, None))
        if cod == "BRA" and "selic" in S:  # a Selic do Banco Central é mais atual
            j, data = S["selic"][-1]["v"], S["selic"][-1]["d"][:7]
        lista.append({"codigo": cod, "nome": nome, "juros": j, "juros_data": data, "series": series[cod]})
    return {"ano": ref, "proj_desde": proj, "fonte": fonte, "lista": lista}


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


# ---------------------------------------------------------------- Principal

def main():
    anterior = {}
    if SAIDA.exists():
        try:
            anterior = json.loads(SAIDA.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    S = coletar_sgs()
    tarefas = {
        "juros": lambda: secao_juros(S),
        "indicadores": lambda: secao_indicadores(S),
        "mercado": lambda: secao_mercado(S),
        "balanca": lambda: secao_balanca(S),
        "focus": secao_focus,
        "paises": lambda: secao_paises(S),
        "noticias": secao_noticias,
    }
    hub = {"atualizado": dt.datetime.now(dt.timezone.utc).isoformat()}
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
