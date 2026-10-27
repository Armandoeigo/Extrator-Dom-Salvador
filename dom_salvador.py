# -*- coding: utf-8 -*-
"""
Acesso ao Diario Oficial do Municipio de Salvador (DOM).

Fonte principal: o proprio site, https://www.dom.salvador.ba.gov.br
Fonte reserva:   a API do Querido Diario (https://api.queridodiario.org.br),
                 usada automaticamente quando o site do DOM bloqueia o acesso.

O site e um Joomla. A categoria 1 lista todas as edicoes, da mais recente
para a mais antiga, 20 por pagina. A pagina de cada edicao traz o link do
PDF, e o nome do arquivo carrega o numero da edicao e a data:

    /images/stories/pdf/2026/agosto/dom-9339-28-08-2026.pdf

Como a listagem nao expoe datas, usamos busca binaria sobre a paginacao
(as edicoes sao cronologicas) para achar o inicio do periodo.

Para recortar a secao pedida usamos o SUMARIO da 1a pagina, que informa a
pagina inicial de cada secao -- inclusive da seguinte, o que da o fim do
bloco sem depender de lista de palavras-chave. Edicoes antigas (ate ~2012)
e EDICOES EXTRA nao tem sumario; nesses casos caimos no recorte por regex.

O que mudou nesta versao (out/2026)
-----------------------------------
* HTTP: usa curl_cffi quando instalado. Ele imita o "aperto de mao" TLS do
  Chrome, que e o que os firewalls (Cloudflare e afins) olham hoje; o
  cloudscraper ja nao passa por eles. Cada thread tem a propria sessao.
  O User-Agent nao e mais sobrescrito (sobrescrever denunciava o robo).
* Educacao: menos conexoes simultaneas (4), pausa curta entre requisicoes
  e respeito ao Retry-After quando o site pede calma (HTTP 429/503).
* Falhas deixaram de ser silenciosas: bloqueio vira SiteBloqueado, HTML
  irreconhecivel vira LayoutMudou, em vez de devolver lista vazia.
* Parsing tolerante: links de edicao no formato antigo (?id=123:dom-9339)
  e no SEF (/123-dom-9339); PDF em href/src com aspas simples ou duplas,
  dentro de visualizador (?file=...pdf) ou ja na propria listagem.
* Querido Diario: a API mudou de api.queridodiario.ok.org.br para
  api.queridodiario.org.br (ago/2026). Os PDFs continuam em
  data.queridodiario.ok.org.br.
* diagnosticar(): roda passo a passo e diz onde quebra. Na linha de
  comando: `python dom_salvador.py`.
* https -> http: quando o https do site recusa a conexao, o modulo passa
  a usar http:// (o navegador faz o mesmo e mostra "Nao seguro").
* Edicao: o Querido Diario costuma vir sem numero de edicao para Salvador.
  Agora o numero sai do cabecalho da capa ("ANO XXXIX | Nº 9.339"), via
  edicao_do_texto(); antes saia um codigo provisorio tipo "83131c10f1".
  Quando o DOM falha e o modulo usa o QD, cada item traz "falha_dom" com o
  motivo.

Uso tipico:

    import dom_salvador as dom
    from datetime import date

    blocos = dom.buscar_decretos(date(2025, 1, 1), date(2025, 12, 31))
    for b in blocos:
        print(b["edicao"], b["data"], b["fonte"], b["texto"])

Configuracao por variavel de ambiente (todas opcionais):

    DOM_FONTE         auto (padrao) | dom | qd
    DOM_MOTOR         auto (padrao) | curl_cffi | cloudscraper | requests
    DOM_WORKERS       conexoes simultaneas (padrao 4)
    DOM_PAUSA         segundos de pausa media por requisicao (padrao 0.3)
    DOM_VERIFICAR_SSL 1 para exigir certificado valido (padrao: tenta e,
                      se o certificado do site falhar, segue sem verificar)
    DOM_PROXY         proxy HTTP(S) para o site do DOM, ex. de um servidor
                      no Brasil, se o site barrar IPs estrangeiros
    QD_API_URL        endereco da API do Querido Diario
    DOM_CACHE_DIR     pasta do cache em disco

Requer:      requests, pymupdf
Recomendado: curl_cffi
Opcional:    cloudscraper
"""

import re
import os
import sys
import json
import html as _html
import time
import random
import hashlib
import logging
import tempfile
import threading
from datetime import date
from urllib.parse import urljoin, unquote, urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from curl_cffi import requests as _cffi
except Exception:            # nao instalado, ou plataforma sem wheel
    _cffi = None
try:
    import cloudscraper as _cloudscraper
except Exception:
    _cloudscraper = None

log = logging.getLogger("dom_salvador")

BASE = os.environ.get("DOM_BASE", "https://www.dom.salvador.ba.gov.br").rstrip("/")
POR_PAGINA = 20
WORKERS = int(os.environ.get("DOM_WORKERS", "4"))   # mantenha modesto por educacao
PAUSA = float(os.environ.get("DOM_PAUSA", "0.3"))
FONTE = os.environ.get("DOM_FONTE", "auto")          # auto | dom | qd
MOTOR = os.environ.get("DOM_MOTOR", "auto")          # auto | curl_cffi | cloudscraper | requests
PROXY = os.environ.get("DOM_PROXY") or None

QD_API = os.environ.get("QD_API_URL", "https://api.queridodiario.org.br").rstrip("/")
QD_SALVADOR = "2927408"                               # codigo IBGE

# Cache em disco. Uma edicao publicada nunca muda, entao o que guardamos aqui
# nao expira. Guardamos o TEXTO ja extraido (poucos KB), nunca os PDFs.
CACHE_DIR = os.environ.get(
    "DOM_CACHE_DIR", os.path.join(tempfile.gettempdir(), "dom_salvador_cache"))
USAR_CACHE = True

# Usado so pelo motor "requests". curl_cffi e cloudscraper montam o proprio
# User-Agent coerente com o TLS que imitam; trocar o UA deles denuncia o robo.
CABECALHO = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_CABECALHO_COMUM = {
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
    "Referer": BASE + "/",
}


class SiteBloqueado(RuntimeError):
    """O site respondeu com bloqueio, desafio anti-robo ou limite de taxa."""


class LayoutMudou(RuntimeError):
    """A pagina veio, mas nao tem o que o parser procura."""


class FonteIndisponivel(RuntimeError):
    """Nenhuma fonte conseguiu responder."""


# ==========================================
# Cache em duas camadas
# ==========================================
# L1, memoria: vive enquanto o processo viver. E a unica camada que funciona
#     no Streamlit Cloud, onde o disco do container e descartado a cada
#     redeploy ou hibernacao.
# L2, disco: sobrevive ao processo onde o disco persiste (uso local, VM).

_MEMORIA = {}
LIMITE_MEMORIA = 5000


def _caminho_cache(chave):
    nome = hashlib.sha1(chave.encode("utf-8")).hexdigest() + ".json"
    return os.path.join(CACHE_DIR, nome)


def _ler_cache(chave):
    if not USAR_CACHE:
        return None
    if chave in _MEMORIA:
        return _MEMORIA[chave]
    try:
        with open(_caminho_cache(chave), encoding="utf-8") as f:
            valor = json.load(f)
        _MEMORIA[chave] = valor
        return valor
    except Exception:
        return None


def _gravar_cache(chave, valor):
    if not USAR_CACHE:
        return
    if len(_MEMORIA) >= LIMITE_MEMORIA:
        _MEMORIA.clear()
    _MEMORIA[chave] = valor
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        caminho = _caminho_cache(chave)
        tmp = caminho + f".{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(valor, f, ensure_ascii=False)
        os.replace(tmp, caminho)
    except Exception:
        pass


def cache_ler(chave):
    """Le um valor do cache, ou None."""
    return _ler_cache(chave)


def cache_gravar(chave, valor):
    """Guarda um valor no cache (memoria + disco, quando houver)."""
    _gravar_cache(chave, valor)


def limpar_cache():
    """Apaga o cache das duas camadas. Devolve quantos arquivos removeu."""
    _MEMORIA.clear()
    _PDF_DA_LISTAGEM.clear()
    n = 0
    try:
        for nome in os.listdir(CACHE_DIR):
            if nome.endswith(".json"):
                os.remove(os.path.join(CACHE_DIR, nome))
                n += 1
    except Exception:
        pass
    return n


def tamanho_cache():
    """Devolve (entradas em memoria, arquivos em disco, bytes em disco)."""
    arq = tam = 0
    try:
        for nome in os.listdir(CACHE_DIR):
            if nome.endswith(".json"):
                arq += 1
                tam += os.path.getsize(os.path.join(CACHE_DIR, nome))
    except Exception:
        pass
    return len(_MEMORIA), arq, tam


# ==========================================
# HTTP
# ==========================================

_local = threading.local()
_SSL_EXIGIDO = os.environ.get("DOM_VERIFICAR_SSL", "") == "1"
_verificar_ssl = True              # tenta verificar; relaxa so se o certificado falhar

_STATUS_REPETIR = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_STATUS_BLOQUEIO = {401, 403, 406, 451}

# Sinais de pagina de desafio/bloqueio. So os inequivocos: "captcha" pode
# aparecer numa pagina normal, e o Cloudflare injeta "challenge-platform" ate
# em paginas que deixou passar.
_TITULOS_DESAFIO = ("just a moment", "um momento", "attention required",
                    "access denied", "acesso negado", "ddos-guard",
                    "request rejected", "403 forbidden", "blocked")
_SINAIS_DESAFIO = (
    "cf_chl_opt", "cf-chl-widget", "checking your browser",
    "verifying you are human", "verifique se voc", "_incapsula_resource",
    "the requested url was rejected", "sucuri website firewall",
    "ddos-guard.net",
)


def motores_disponiveis():
    m = []
    if _cffi:
        m.append("curl_cffi")
    if _cloudscraper:
        m.append("cloudscraper")
    m.append("requests")
    return m


def _motor_escolhido():
    if MOTOR != "auto":
        if MOTOR not in motores_disponiveis():
            raise RuntimeError(f"DOM_MOTOR={MOTOR}, mas a biblioteca nao esta instalada")
        return MOTOR
    return motores_disponiveis()[0]


def _nova_sessao(motor):
    if motor == "curl_cffi":
        s = _cffi.Session(impersonate="chrome")
    elif motor == "cloudscraper":
        s = _cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False})
    else:
        s = requests.Session()
        s.headers.update(CABECALHO)
    if PROXY:
        s.proxies = {"http": PROXY, "https": PROXY}
    return s


def _sessao():
    s = getattr(_local, "sessao", None)
    if s is None:
        s = _local.sessao = _nova_sessao(_motor_escolhido())
    return s


def _cab(r, nome):
    try:
        return r.headers.get(nome) or r.headers.get(nome.lower()) or ""
    except Exception:
        return ""


def _titulo(texto):
    m = re.search(r"<title[^>]*>(.*?)</title>", texto or "", re.I | re.S)
    return re.sub(r"\s+", " ", _html.unescape(m.group(1))).strip()[:120] if m else ""


def _tem_desafio(texto):
    if not texto:
        return False
    titulo = _titulo(texto).lower()
    if titulo and any(titulo.startswith(t) for t in _TITULOS_DESAFIO):
        return True
    if len(texto) > 150000:         # pagina "de verdade", grande demais para desafio
        return False
    baixo = texto.lower()
    return any(s in baixo for s in _SINAIS_DESAFIO)


def _descrever(url, r, texto=None):
    texto = texto if texto is not None else _texto(r)
    partes = [f"HTTP {r.status_code} em {url}"]
    for nome in ("server", "cf-ray", "x-cdn", "x-iinfo", "x-sucuri-id"):
        v = _cab(r, nome)
        if v:
            partes.append(f"{nome}={v}")
    t = _titulo(texto)
    if t:
        partes.append(f'titulo="{t}"')
    return ", ".join(partes)


def _texto(r):
    try:
        return r.text
    except Exception:
        return ""


_HOST_DOM = urlsplit(BASE).netloc.lower()
_usar_http = False      # liga quando o https do DOM nao conecta


def _para_http(url):
    """A mesma URL do DOM em http://, ou None se ela nao for https do DOM."""
    p = urlsplit(url)
    if p.scheme == "https" and p.netloc.lower() == _HOST_DOM:
        return urlunsplit(p._replace(scheme="http"))
    return None


_SINAIS_SEM_CONEXAO = (
    "curl: (7)", "curl: (28)", "curl: (35)", "failed to connect", "could not connect",
    "connection refused", "newconnectionerror", "connecttimeout", "connect timeout",
    "connection timed out", "max retries exceeded",
)


def _eh_falha_conexao(e):
    s = str(e).lower()
    return any(x in s for x in _SINAIS_SEM_CONEXAO)


def _eh_erro_ssl(e):
    s = str(e).lower()
    return ("ssl" in s or "certificate" in s or "curl: (60)" in s
            or "self signed" in s or "self-signed" in s)


def _espera_retry_after(r, padrao):
    v = _cab(r, "retry-after")
    try:
        return min(60.0, max(1.0, float(v)))
    except (TypeError, ValueError):
        return padrao


def _requisitar(url, timeout=(15, 60), tentativas=3, binario=False, sessao=None):
    """GET com tentativas, backoff e deteccao de bloqueio. Devolve a resposta."""
    global _verificar_ssl, _usar_http
    s = sessao or _sessao()
    erro, erro_https, n = None, None, 0
    while n < tentativas:
        if _usar_http:
            url = _para_http(url) or url
        if PAUSA:
            time.sleep(PAUSA * random.uniform(0.5, 1.5))
        try:
            r = s.get(url, headers=_CABECALHO_COMUM, timeout=timeout,
                      verify=_verificar_ssl, allow_redirects=True)
        except Exception as e:
            if _verificar_ssl and not _SSL_EXIGIDO and _eh_erro_ssl(e):
                # o certificado do site ja deu problema antes (por isso o
                # verify=False da versao anterior); segue sem verificar
                _verificar_ssl = False
                log.warning("certificado de %s nao validou; seguindo sem verificar SSL", url)
                continue
            if (not _usar_http and not _SSL_EXIGIDO and _para_http(url)
                    and (_eh_falha_conexao(e) or _eh_erro_ssl(e))):
                # O https do DOM as vezes recusa a conexao (porta 443) enquanto
                # o http responde -- e o "Nao seguro" que o navegador mostra.
                # Passa a usar http para todas as requisicoes do DOM.
                _usar_http, erro_https = True, e
                log.warning("https de %s nao conectou (%s); tentando http", _HOST_DOM, str(e)[:160])
                continue
            erro = e
            n += 1
            if n < tentativas:
                time.sleep(min(20, 2 ** n) + random.random())
            continue
        n += 1

        st = r.status_code
        if st == 200:
            if binario:
                return r
            t = _texto(r)
            if _tem_desafio(t):
                raise SiteBloqueado("pagina de desafio anti-robo: " + _descrever(url, r, t))
            return r
        if st in (403, 503) and _tem_desafio(_texto(r)):
            raise SiteBloqueado("pagina de desafio anti-robo: " + _descrever(url, r))
        if st in _STATUS_BLOQUEIO:
            raise SiteBloqueado(_descrever(url, r))
        if st in _STATUS_REPETIR:
            erro = SiteBloqueado(_descrever(url, r)) if st == 429 else \
                RuntimeError(_descrever(url, r))
            if n < tentativas:
                time.sleep(_espera_retry_after(r, min(30, 2 ** (n + 1))) + random.random())
            continue
        if st == 404:
            raise LookupError(f"HTTP 404 em {url}")
        raise RuntimeError(_descrever(url, r))

    if isinstance(erro, SiteBloqueado):
        raise SiteBloqueado(f"{erro} (persistiu apos {tentativas} tentativas)")
    antes = f" (antes, pelo https: {erro_https})" if erro_https else ""
    raise RuntimeError(f"Falha ao acessar {url}: {erro}{antes}")


def _get(url, tentativas=3):
    """Devolve o HTML de uma pagina do site."""
    return _texto(_requisitar(url, tentativas=tentativas))


def _baixar_pdf(url):
    """Baixa um PDF e confere que e PDF mesmo (e nao uma pagina de bloqueio)."""
    if "queridodiario" in url:
        r = requests.get(url, timeout=(15, 120), headers={"User-Agent": "dom_salvador/2"})
        r.raise_for_status()
    else:
        r = _requisitar(url, timeout=(15, 120), binario=True)
    dados = r.content
    if not dados[:1024].lstrip().startswith(b"%PDF"):
        texto = dados[:60000].decode("utf-8", "replace")
        if _tem_desafio(texto):
            raise SiteBloqueado("PDF trocado por pagina de desafio: " + _descrever(url, r, texto))
        raise LayoutMudou(f"{url} nao devolveu um PDF (content-type="
                          f"{_cab(r, 'content-type')}, titulo=\"{_titulo(texto)}\")")
    return dados


def _abrir_pdf(dados):
    try:
        import pymupdf as fitz
    except ImportError:          # versoes antigas so tem o nome "fitz"
        import fitz
    return fitz.open(stream=dados, filetype="pdf")


# ==========================================
# Leitura do site do DOM
# ==========================================

# Link de edicao: formato antigo (...&id=12345:dom-9339) e SEF (/12345-dom-9339)
_RE_ITEM = (
    re.compile(r"id=(\d+)(?::|%3A)dom-(\d+)", re.I),
    re.compile(r"/(\d+)-dom-(\d+)(?=[/?#\"'.&]|$)", re.I),
)
# Qualquer .pdf em atributo, com aspas simples ou duplas
_RE_PDF_ATTR = re.compile(
    r"""(?:href|src|data|data-src|data-url|data-file|value)\s*=\s*["']([^"'<>]+?\.pdf(?:[?#][^"'<>]*)?)["']""",
    re.I)
# Visualizador: ...viewer.html?file=/images/.../dom-9339-28-08-2026.pdf
_RE_PDF_PARAM = re.compile(r"[?&](?:file|url|pdf|arquivo)=([^\"'&\s<>]+?\.pdf)", re.I)
_RE_PDF_SOLTO = re.compile(r"""((?:https?:)?//[^\s"'<>]+?\.pdf|/[^\s"'<>]+?\.pdf)""", re.I)
_RE_DATA = re.compile(r"dom[-_]?(\d+)[-_](\d{1,2})[-_](\d{1,2})[-_](\d{4})", re.I)

_RE_SO_NUMERO = re.compile(r"^\s*(\d{1,3})\s*$")

_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4,
          "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
          "outubro": 10, "novembro": 11, "dezembro": 12}

# PDFs que ja vieram na propria listagem, para nao abrir a pagina da edicao
_PDF_DA_LISTAGEM = {}


def _url_categoria(limitstart):
    return (f"{BASE}/index.php?option=com_content&view=category"
            f"&id=1&limitstart={limitstart}")


def _url_artigo(edicao, artigo_id):
    return (f"{BASE}/index.php?option=com_content&view=article"
            f"&id={artigo_id}:dom-{edicao}&catid=1:dom")


def _normalizar_pdf(u):
    u = _html.unescape(u.strip())
    m = _RE_PDF_PARAM.search(u)
    if m:                       # era o visualizador; queremos o arquivo
        u = unquote(m.group(1))
    if u.startswith("//"):
        u = "https:" + u
    return urljoin(BASE + "/", u)


def _pdfs_no_html(texto):
    """Todos os links de PDF da pagina, sem repeticao, na ordem em que aparecem."""
    achados = []
    for rx in (_RE_PDF_ATTR, _RE_PDF_PARAM, _RE_PDF_SOLTO):
        for m in rx.finditer(texto or ""):
            u = _normalizar_pdf(m.group(1))
            if u not in achados:
                achados.append(u)
    return achados


def _data_do_nome(pdf_url):
    """(edicao, date) a partir do nome do arquivo, ou (None, None)."""
    nome = unquote(pdf_url.rsplit("/", 1)[-1])
    m = _RE_DATA.search(nome)
    if not m:
        return None, None
    ed, dia, mes, ano = (int(x) for x in m.groups())
    try:
        return ed, date(ano, mes, dia)
    except ValueError:
        return ed, None


def _data_do_html(texto):
    """Data da edicao lida do titulo/cabecalho da pagina, como ultimo recurso."""
    trechos = re.findall(r"<(?:title|h1|h2)[^>]*>(.*?)</(?:title|h1|h2)>", texto or "", re.I | re.S)
    for t in trechos:
        t = _html.unescape(re.sub(r"<[^>]+>", " ", t)).lower()
        m = re.search(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})", t)
        if m:
            d, mth, a = (int(x) for x in m.groups())
        else:
            m = re.search(r"(\d{1,2})\s+de\s+([a-zç]+)\s+de\s+(\d{4})", t)
            if not m or m.group(2) not in _MESES:
                continue
            d, mth, a = int(m.group(1)), _MESES[m.group(2)], int(m.group(3))
        try:
            return date(a, mth, d)
        except ValueError:
            continue
    return None


def _escolher_pdf(pdfs, edicao=None):
    """Prefere o PDF que tem o numero da edicao no nome, depois o de /pdf/."""
    if edicao is not None:
        for u in pdfs:
            ed, _ = _data_do_nome(u)
            if ed == int(edicao):
                return u
    for u in pdfs:
        if _RE_DATA.search(unquote(u)):
            return u
    for u in pdfs:
        if "/pdf/" in u.lower():
            return u
    return pdfs[0] if pdfs else None


_RE_HREF = re.compile(r"""href\s*=\s*["']([^"'<>]+)["']""", re.I)
_URL_ARTIGO = {}     # edicao -> link da pagina da edicao, como veio na listagem


def _itens_no_html(texto):
    """[(edicao, artigo_id)] na ordem da pagina, sem repeticao."""
    vistos, saida = set(), []

    def poe(ed, aid, url=None):
        if url and ed not in _URL_ARTIGO:
            _URL_ARTIGO[ed] = url
        if ed not in vistos:
            vistos.add(ed)
            saida.append((ed, aid))

    for h in _RE_HREF.findall(texto or ""):
        h = _html.unescape(h)
        for rx in _RE_ITEM:
            m = rx.search(h)
            if m:
                poe(int(m.group(2)), int(m.group(1)), urljoin(BASE + "/", h))
                break
    for rx in _RE_ITEM:              # links fora de href (scripts, data-*)
        for aid, ed in rx.findall(texto or ""):
            poe(int(ed), int(aid))
    # Listagem que ja traz o PDF direto: guardamos e, se a edicao nao tinha
    # link de pagina, entra com artigo_id 0
    for u in _pdfs_no_html(texto):
        ed, _ = _data_do_nome(u)
        if ed is not None:
            _PDF_DA_LISTAGEM[ed] = u
            poe(ed, 0)
    return saida


def listar_pagina(limitstart=0):
    """Devolve [(edicao, artigo_id)] de uma pagina da listagem."""
    try:
        return _itens_no_html(_get(_url_categoria(limitstart)))
    except LookupError:              # 404: passou do fim da listagem
        return []


def obter_meta(edicao, artigo_id):
    """Devolve {'edicao', 'data', 'pdf_url', 'fonte'} de uma edicao, ou None."""
    chave = f"meta:{edicao}:{artigo_id}"
    guardado = _ler_cache(chave)
    if guardado:
        return {"edicao": guardado["edicao"], "data": date.fromisoformat(guardado["data"]),
                "pdf_url": guardado["pdf_url"], "fonte": "dom"}

    pdf_url = _PDF_DA_LISTAGEM.get(int(edicao))
    texto = None
    if not pdf_url:
        url = _URL_ARTIGO.get(int(edicao)) or (_url_artigo(edicao, artigo_id) if artigo_id else None)
        if not url:
            return None
        texto = _get(url)
        pdf_url = _escolher_pdf(_pdfs_no_html(texto), edicao)
        if not pdf_url:
            return None

    _, data = _data_do_nome(pdf_url)
    if data is None and texto is not None:
        data = _data_do_html(texto)
    if data is None:
        return None
    _gravar_cache(chave, {"edicao": edicao, "pdf_url": pdf_url, "data": data.isoformat()})
    return {"edicao": edicao, "data": data, "pdf_url": pdf_url, "fonte": "dom"}


_ERROS_META = []


def _meta_segura(par):
    try:
        return obter_meta(par[0], par[1])
    except SiteBloqueado:
        raise                   # bloqueio nao e problema de uma edicao so
    except Exception as e:
        _ERROS_META.append(f"edicao {par[0]}: {e}")
        log.warning("edicao %s: %s", par[0], e)
        return None


def _data_em(limitstart):
    """
    Data da edicao mais antiga da pagina. Usar a mais antiga (menor numero),
    e nao a primeira do HTML, protege contra blocos laterais do tipo
    "ultimas edicoes", que se repetem em todas as paginas.
    """
    for edicao, artigo_id in sorted(listar_pagina(limitstart)):
        m = _meta_segura((edicao, artigo_id))
        if m:
            return m["data"]
    return None


def _achar_inicio(data_fim):
    """Busca binaria: menor limitstart cuja data ja seja <= data_fim."""
    primeira = _data_em(0)
    if primeira is None:
        raise LayoutMudou(
            "a 1a pagina da listagem nao rendeu nenhuma edicao com PDF e data"
            + (f" (erros: {_ERROS_META[:3]})" if _ERROS_META else "")
            + ". Rode dom_salvador.diagnosticar() para ver o HTML que chegou.")
    if primeira <= data_fim:
        return 0

    # Teto adaptativo: dobra ate passar da data ou acabar a listagem
    alto = 1000
    while alto < 40000:
        d = _data_em(alto)
        if d is None or d <= data_fim:
            break
        alto *= 2
    baixo = 0
    while baixo < alto:
        meio = (baixo + alto) // 2
        d = _data_em(meio - meio % POR_PAGINA)
        if d is None or d <= data_fim:
            alto = meio
        else:
            baixo = meio + 1
    return max(0, baixo - baixo % POR_PAGINA - POR_PAGINA)


def _buscar_por_periodo_dom(data_inicio, data_fim, workers=WORKERS):
    _ERROS_META.clear()
    limitstart = _achar_inicio(data_fim)
    achados, parar, rodadas, vistas = [], False, 0, set()
    lote_paginas = max(1, workers // 2)

    while not parar and rodadas < 600:
        rodadas += 1
        alvos = range(limitstart, limitstart + lote_paginas * POR_PAGINA, POR_PAGINA)
        with ThreadPoolExecutor(workers) as ex:
            paginas = list(ex.map(listar_pagina, alvos))
        brutos = [x for p in paginas for x in p]
        if not brutos:
            break
        itens = []
        for x in brutos:             # tira repetidas entre paginas
            if x[0] not in vistas:
                vistas.add(x[0])
                itens.append(x)
        if not itens:
            break
        with ThreadPoolExecutor(workers) as ex:
            metas = list(ex.map(_meta_segura, itens))
        if rodadas == 1 and not any(metas):
            raise LayoutMudou(
                f"achei {len(itens)} edicoes na listagem mas nenhum PDF/data nelas. "
                f"Erros: {_ERROS_META[:3]}")
        for m in metas:
            if not m or m["data"] > data_fim:
                continue
            if m["data"] < data_inicio:
                parar = True
                break
            achados.append(m)
        limitstart += lote_paginas * POR_PAGINA

    return sorted(achados, key=lambda m: m["data"])


# ==========================================
# Querido Diario (fonte reserva)
# ==========================================

def _qd_get(caminho, params, tentativas=3):
    url = QD_API + caminho
    erro = None
    for n in range(tentativas):
        try:
            r = requests.get(url, params=params, timeout=(15, 60),
                             headers={"Accept": "application/json",
                                      "User-Agent": "dom_salvador/2 (python-requests)"})
            if r.status_code == 200:
                return r.json()
            erro = f"HTTP {r.status_code}: {r.text[:120]!r}"
            if r.status_code < 500 and r.status_code != 429:
                break
        except Exception as e:
            erro = str(e)
        time.sleep(2 * (n + 1))
    raise FonteIndisponivel(f"API do Querido Diario ({url}) nao respondeu: {erro}")


def buscar_por_periodo_qd(data_inicio, data_fim, territorio=QD_SALVADOR):
    """Edicoes do periodo segundo o Querido Diario, em ordem cronologica."""
    achados, offset, tamanho = [], 0, 100
    while True:
        dados = _qd_get("/gazettes", {
            "territory_ids": territorio,
            "published_since": data_inicio.isoformat(),
            "published_until": data_fim.isoformat(),
            "size": tamanho, "offset": offset, "sort_by": "ascending_date"})
        lote = dados.get("gazettes") or []
        for g in lote:
            # O QD costuma deixar "edition" vazio para Salvador; nesse caso o
            # numero sai do cabecalho do PDF, quando ele for aberto
            digitos = re.sub(r"\D", "", str(g.get("edition") or ""))
            ed = int(digitos) if digitos and _EDICAO_MIN <= int(digitos) <= _EDICAO_MAX else None
            achados.append({
                "edicao": ed,
                "data": date.fromisoformat(g["date"][:10]),
                "pdf_url": g["url"],
                "txt_url": g.get("txt_url"),
                "extra": bool(g.get("is_extra_edition")),
                "fonte": "qd",
            })
        offset += len(lote)
        if not lote or offset >= int(dados.get("total_gazettes") or 0):
            break
    return sorted(achados, key=lambda m: m["data"])


def buscar_por_periodo(data_inicio, data_fim, workers=WORKERS, fonte=None):
    """
    Devolve as edicoes publicadas no periodo, em ordem cronologica.
    fonte: "auto" (DOM e, se bloquear, Querido Diario), "dom" ou "qd".
    """
    fonte = fonte or FONTE
    if fonte == "qd":
        return buscar_por_periodo_qd(data_inicio, data_fim)

    # Periodo encerrado ha mais de uma semana nao ganha edicao nova: a lista
    # fica no cache e a busca repetida nem passa pela listagem do site.
    chave = f"periodo:{data_inicio}:{data_fim}"
    fechado = (date.today() - data_fim).days > 7
    guardado = _ler_cache(chave) if fechado else None
    if guardado is not None:
        return [{**m, "data": date.fromisoformat(m["data"])} for m in guardado]
    try:
        edicoes = _buscar_por_periodo_dom(data_inicio, data_fim, workers)
        if fechado:
            _gravar_cache(chave, [{**m, "data": m["data"].isoformat()} for m in edicoes])
        return edicoes
    except Exception as e:           # bloqueio, layout novo, site fora do ar
        if fonte == "dom":
            raise
        log.warning("site do DOM falhou (%s); tentando o Querido Diario", e)
        motivo = f"{type(e).__name__}: {e}"[:400]
        try:
            # cada item leva o motivo, para a pagina poder mostrar o porque
            return [{**m, "falha_dom": motivo}
                    for m in buscar_por_periodo_qd(data_inicio, data_fim)]
        except FonteIndisponivel as e2:
            raise FonteIndisponivel(
                f"As duas fontes falharam.\n  DOM: {e}\n  Querido Diario: {e2}") from e2


# ==========================================
# Recorte da secao dentro do PDF
# ==========================================

_RE_CABECALHO_PAG = re.compile(
    r"DIÁRIO\s+OFICIAL\s+DO\s*\n?\s*SALVADOR\s*-?\s*BAHIA.*?"
    r"ANO\s+[IVXLCDM]+\s*\|\s*N\s*[º°oO]?\s*[\d.]+\s*\d*",
    re.IGNORECASE | re.DOTALL)

_PARADA = (r"\n\s*(?:SECRETARIA|GABINETE|PROCURADORIA|CONTROLADORIA|"
           r"SUPERINTEND[ÊE]NCIA|FUNDA[ÇC][ÃA]O|LICITA[ÇC][ÕO]ES|CONSELHO|"
           r"CASA\s+CIVIL|EMPRESA|INSTITUTO|AG[ÊE]NCIA|COMPANHIA)\b")


def _limpar(t):
    t = _RE_CABECALHO_PAG.sub("\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# Numero da edicao no cabecalho da capa: "ANO XXXIX | Nº 9.339".
# Os padroes ficam presos ao cabecalho de proposito: um "Nº 42.453" solto no
# texto e numero de decreto, nao de edicao.
_EDICAO_MIN, _EDICAO_MAX = 1000, 99999
_NUM = r"N\s*[º°ºoO]?\s*\.?\s*(\d{1,2}\.?\d{3})(?!\d)"
_RE_EDICAO_TEXTO = (
    re.compile(r"ANO\s+[IVXLCDM]+\s*[|\-–—]?\s*" + _NUM, re.I),
    re.compile(r"SALVADOR\s*[-–]?\s*BAHIA[\s|,\-–—]{0,10}" + _NUM, re.I),
    re.compile(r"EDI[ÇC][ÃA]O\s+(?:EXTRA\s+)?" + _NUM, re.I),
)


def edicao_do_texto(texto):
    """Numero da edicao lido do cabecalho do diario (int), ou None."""
    inicio = (texto or "")[:3000]
    for rx in _RE_EDICAO_TEXTO:
        m = rx.search(inicio)
        if m:
            n = int(m.group(1).replace(".", ""))
            if _EDICAO_MIN <= n <= _EDICAO_MAX:
                return n
    return None


def _achar_titulo(texto, nome):
    """
    Procura um titulo de secao tolerando quebras de linha e espacos extras.
    Tenta o nome inteiro e vai encurtando ate 3 palavras (ou o nome todo,
    se ele for mais curto que isso -- caso de "DECRETOS SIMPLES").
    """
    toks = [t for t in nome.split() if t]
    if not toks:
        return None
    minimo = min(3, len(toks))
    for corte in range(len(toks), minimo - 1, -1):
        padrao = r"\s+".join(re.escape(t) for t in toks[:corte])
        m = re.search(padrao, texto, re.IGNORECASE)
        if m:
            return m
    return None


def ler_sumario(doc):
    """Devolve [(nome_secao, pagina)] lido do sumario da 1a pagina."""
    txt = doc[0].get_text()
    i = txt.find("S U M")
    if i < 0:
        return []
    linhas = [l.replace("\x08", "").strip() for l in txt[i:].split("\n")]
    entradas, nome = [], None
    for l in linhas:
        if not l:
            continue
        m = _RE_SO_NUMERO.match(l)
        if m and nome:
            entradas.append((nome, int(m.group(1))))
            nome = None
        elif not m:
            nome = l
    return entradas


def _por_sumario(doc, secao):
    entradas = ler_sumario(doc)
    alvo = next((n for n, (nome, _) in enumerate(entradas)
                 if nome.upper().startswith(secao.upper())), None)
    if alvo is None:
        return None

    nome_alvo, pag_ini = entradas[alvo]
    nome_prox, pag_fim = (entradas[alvo + 1] if alvo + 1 < len(entradas)
                          else (None, doc.page_count))

    ini = max(0, pag_ini - 1)
    fim = min(doc.page_count, max(pag_fim, pag_ini))
    trecho = "\n".join(doc[p].get_text() for p in range(ini, fim))

    m = _achar_titulo(trecho, nome_alvo)
    if m:
        trecho = trecho[m.end():]
    if nome_prox:
        m2 = _achar_titulo(trecho, nome_prox)
        if m2:
            trecho = trecho[:m2.start()]
        else:
            m3 = re.search(_PARADA, trecho)
            if m3:
                trecho = trecho[:m3.start()]
    return _limpar(trecho) or None


def _por_regex(doc, secao):
    txt = "\n".join(p.get_text() for p in doc)
    blocos = re.findall(re.escape(secao) + r"(.*?)(" + _PARADA + ")",
                        txt, re.DOTALL | re.IGNORECASE)
    if not blocos:
        return None
    return _limpar(max(blocos, key=lambda x: len(x[0]))[0]) or None


def extrair_secao(doc, secao="DECRETOS SIMPLES"):
    """
    Devolve (texto, metodo); texto e None se a edicao nao tiver a secao.
    metodo e "sumario" ou "regex", util para auditar o resultado.
    """
    t = _por_sumario(doc, secao)
    if t:
        return t, "sumario"
    t = _por_regex(doc, secao)
    if t:
        return t, "regex"
    return None, None


# ==========================================
# API de alto nivel
# ==========================================

def baixar_texto(pdf_url):
    """Baixa o PDF e devolve o texto completo."""
    with _abrir_pdf(_baixar_pdf(pdf_url)) as doc:
        return "\n".join(p.get_text() for p in doc)


def _bloco(m, secao):
    # O cache guarda inclusive o resultado negativo ("esta edicao nao tem a
    # secao"), que tambem custou um PDF inteiro para ser descoberto.
    # Sem numero de edicao (Querido Diario), a chave e o proprio PDF
    chave = f"bloco:{m['edicao'] or m['pdf_url']}:{secao}"
    guardado = _ler_cache(chave)
    if guardado is not None:
        if not guardado.get("texto"):
            return None
        return {**m, "edicao": m["edicao"] or guardado.get("edicao"),
                "texto": guardado["texto"], "metodo": guardado["metodo"]}

    try:
        with _abrir_pdf(_baixar_pdf(m["pdf_url"])) as doc:
            texto, metodo = extrair_secao(doc, secao)
            edicao = m["edicao"] or edicao_do_texto(doc[0].get_text() if doc.page_count else "")
        _gravar_cache(chave, {"texto": texto, "metodo": metodo, "edicao": edicao})
        return {**m, "edicao": edicao, "texto": texto, "metodo": metodo} if texto else None
    except Exception as e:
        # Falha de rede nao vai para o cache: da proxima vez tentamos de novo
        return {**m, "texto": None, "metodo": None,
                "erro": f"{type(e).__name__}: {e}"}


def buscar_decretos(data_inicio, data_fim, secao="DECRETOS SIMPLES",
                    progresso=None, workers=WORKERS, fonte=None):
    """
    Devolve SO os blocos da secao pedida, no periodo, em ordem cronologica:

        [{"edicao": 9339, "data": date(...), "texto": "...",
          "metodo": "sumario"|"regex", "pdf_url": "...", "fonte": "dom"|"qd"}]

    Edicoes sem a secao sao omitidas; falhas vem com a chave "erro".
    `progresso` e um callable opcional recebendo (feitos, total).
    Levanta SiteBloqueado, LayoutMudou ou FonteIndisponivel quando nao da
    para nem listar as edicoes -- em vez de devolver lista vazia calada.
    """
    edicoes = buscar_por_periodo(data_inicio, data_fim, workers, fonte)
    total, feitos, saida = len(edicoes), 0, []

    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(lambda m: _bloco(m, secao), edicoes):
            feitos += 1
            if r:
                saida.append(r)
            if progresso:
                progresso(feitos, total)

    return sorted(saida, key=lambda m: m["data"])


# ==========================================
# Diagnostico
# ==========================================

def diagnosticar(pasta=None, imprimir=True):
    """
    Testa cada etapa e diz onde quebra. Salva o HTML de paginas problematicas
    em `pasta` (padrao: diretorio atual) para inspecao. Devolve um dict.
    """
    pasta = pasta or os.getcwd()
    rel = {"motores": {}, "etapas": []}

    def diz(msg):
        if imprimir:
            print(msg, flush=True)

    def salva(nome, texto):
        try:
            caminho = os.path.join(pasta, nome)
            with open(caminho, "w", encoding="utf-8") as f:
                f.write(texto or "")
            diz(f"     HTML salvo em {caminho}")
        except Exception:
            pass

    def etapa(nome, ok, detalhe=""):
        rel["etapas"].append({"etapa": nome, "ok": ok, "detalhe": detalhe})
        diz(f"[{'OK ' if ok else 'ERRO'}] {nome}" + (f": {detalhe}" if detalhe else ""))

    diz(f"Python {sys.version.split()[0]} | motores disponiveis: {', '.join(motores_disponiveis())}"
        f" | motor em uso: {_motor_escolhido()} | proxy: {PROXY or 'nenhum'}")
    if not _cffi:
        diz("     dica: `pip install curl_cffi` costuma resolver bloqueio por firewall")

    # 1. Listagem com cada motor, para saber qual passa
    url_lista = _url_categoria(0)
    melhor = None
    for motor in motores_disponiveis():
        try:
            r = _requisitar(url_lista, tentativas=2, sessao=_nova_sessao(motor))
            t = _texto(r)
            itens = _itens_no_html(t)
            rel["motores"][motor] = {"status": r.status_code, "itens": len(itens)}
            etapa(f"listagem via {motor}", bool(itens),
                  f"HTTP {r.status_code}, {len(t)} bytes, {len(itens)} edicoes, "
                  f'titulo="{_titulo(t)}"')
            if itens and not melhor:
                melhor = (motor, itens)
                diz(f"     protocolo: {'http (o https nao conectou)' if _usar_http else 'https'}")
            if not itens:
                amostra = sorted(set(re.findall(r"""href=["']([^"']*(?:dom|pdf|id=)[^"']*)["']""", t, re.I)))[:12]
                diz("     links com 'dom'/'pdf'/'id=' na pagina: " + (", ".join(amostra) or "nenhum"))
                salva(f"diag_listagem_{motor}.html", t)
        except Exception as e:
            rel["motores"][motor] = {"erro": str(e)}
            etapa(f"listagem via {motor}", False, f"{type(e).__name__}: {e}")

    if melhor:
        motor, itens = melhor
        ed, art = itens[0]
        diz(f"     use DOM_MOTOR={motor} se o automatico nao for este")
        # 2. Pagina da edicao
        try:
            if ed in _PDF_DA_LISTAGEM:
                pdf = _PDF_DA_LISTAGEM[ed]
                etapa("PDF direto na listagem", True, pdf)
            else:
                t = _texto(_requisitar(_url_artigo(ed, art), tentativas=2,
                                       sessao=_nova_sessao(motor)))
                pdfs = _pdfs_no_html(t)
                pdf = _escolher_pdf(pdfs, ed)
                etapa(f"pagina da edicao {ed}", bool(pdf), pdf or f"{len(pdfs)} PDFs achados")
                if not pdf:
                    salva(f"diag_edicao_{ed}.html", t)
            if pdf:
                _, d = _data_do_nome(pdf)
                etapa("data no nome do PDF", d is not None, str(d) if d else unquote(pdf.rsplit("/", 1)[-1]))
                # 3. PDF
                r = _requisitar(pdf, timeout=(15, 120), tentativas=2, binario=True,
                                sessao=_nova_sessao(motor))
                dados = r.content
                eh_pdf = dados[:1024].lstrip().startswith(b"%PDF")
                etapa("download do PDF", eh_pdf,
                      f"HTTP {r.status_code}, {len(dados)} bytes, content-type={_cab(r, 'content-type')}")
                if eh_pdf:
                    with _abrir_pdf(dados) as doc:
                        sec = ler_sumario(doc)
                        etapa("leitura do PDF", True, f"{doc.page_count} paginas, "
                              f"{len(sec)} entradas no sumario")
                else:
                    salva("diag_pdf.html", dados[:200000].decode("utf-8", "replace"))
        except Exception as e:
            etapa("edicao/PDF", False, f"{type(e).__name__}: {e}")

    # 4. Querido Diario
    try:
        dados = _qd_get("/gazettes", {"territory_ids": QD_SALVADOR, "size": 1,
                                      "sort_by": "descending_date"}, tentativas=1)
        g = (dados.get("gazettes") or [{}])[0]
        etapa("API do Querido Diario", True,
              f"{dados.get('total_gazettes')} edicoes de Salvador, mais recente {g.get('date')}")
    except Exception as e:
        etapa("API do Querido Diario", False, str(e))

    # Veredito
    dom_ok = bool(melhor)
    qd_ok = rel["etapas"][-1]["ok"]
    if dom_ok:
        diz("\n=> O site do DOM respondeu. Se o app ainda falha, compare com o ambiente "
            "onde ele roda (ex.: Streamlit Cloud fica nos EUA; o site pode barrar IP de fora).")
    elif qd_ok:
        diz("\n=> O DOM nao passou, mas o Querido Diario sim: com DOM_FONTE=auto o modulo usa ele.")
    else:
        diz("\n=> Nenhuma fonte passou. Me mande esta saida (e os arquivos diag_*.html).")
    rel["dom_ok"], rel["qd_ok"] = dom_ok, qd_ok
    return rel


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if len(sys.argv) == 3:
        ini, fim = (date.fromisoformat(x) for x in sys.argv[1:3])
        res = buscar_decretos(ini, fim,
                              progresso=lambda f, t: print(f"\r{f}/{t} edicoes", end="", flush=True))
        print()
        for b in res:
            if b.get("erro"):
                print(f"{b['data']} ed. {b['edicao']} [{b['fonte']}] ERRO: {b['erro']}")
            else:
                print(f"{b['data']} ed. {b['edicao']} [{b['fonte']}/{b['metodo']}] "
                      f"{len(b['texto'])} caracteres")
        print(f"{len(res)} blocos")
    else:
        print("Uso: python dom_salvador.py             -> diagnostico")
        print("     python dom_salvador.py 2026-10-01 2026-10-20  -> busca no periodo\n")
        diagnosticar()
