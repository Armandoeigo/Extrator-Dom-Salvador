# -*- coding: utf-8 -*-
"""
Acesso direto ao Diario Oficial do Municipio de Salvador (DOM).

Alternativa a API do Querido Diario, lendo direto da fonte:
http://www.dom.salvador.ba.gov.br

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

Uso tipico:

    import dom_salvador as dom
    from datetime import date

    blocos = dom.buscar_decretos(date(2025, 1, 1), date(2025, 12, 31))
    for b in blocos:
        print(b["edicao"], b["data"], b["texto"])

Requer: requests, pymupdf
"""

import re
import os
import json
import time
import hashlib
import tempfile
import requests
from datetime import date
from concurrent.futures import ThreadPoolExecutor

BASE = "http://www.dom.salvador.ba.gov.br"
POR_PAGINA = 20
WORKERS = 8          # conexoes simultaneas; mantenha modesto por educacao

# Cache em disco. Uma edicao publicada nunca muda, entao o que guardamos aqui
# nao expira. Guardamos o TEXTO ja extraido (poucos KB), nunca os PDFs.
CACHE_DIR = os.environ.get(
    "DOM_CACHE_DIR", os.path.join(tempfile.gettempdir(), "dom_salvador_cache"))
USAR_CACHE = True
CABECALHO = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

_RE_ITEM = re.compile(r'id=(\d+):dom-(\d+)', re.IGNORECASE)
_RE_PDF = re.compile(r'href="([^"]*?/pdf/[^"]*?\.pdf)"', re.IGNORECASE)
_RE_DATA = re.compile(r'dom-\d+-(\d{2})-(\d{2})-(\d{4})\.pdf', re.IGNORECASE)
_RE_SO_NUMERO = re.compile(r"^\s*(\d{1,3})\s*$")

_RE_CABECALHO_PAG = re.compile(
    r"DIÁRIO\s+OFICIAL\s+DO\s*\n?\s*SALVADOR\s*-?\s*BAHIA.*?"
    r"ANO\s+[IVXLCDM]+\s*\|\s*N\s*[º°oO]?\s*[\d.]+\s*\d*",
    re.IGNORECASE | re.DOTALL)

_PARADA = (r"\n\s*(?:SECRETARIA|GABINETE|PROCURADORIA|CONTROLADORIA|"
           r"SUPERINTEND[ÊE]NCIA|FUNDA[ÇC][ÃA]O|LICITA[ÇC][ÕO]ES|CONSELHO|"
           r"CASA\s+CIVIL|EMPRESA|INSTITUTO|AG[ÊE]NCIA|COMPANHIA)\b")


# ==========================================
# Cache em duas camadas
# ==========================================
# L1, memoria: vive enquanto o processo viver. E a unica camada que funciona
#     no Streamlit Cloud, onde o disco do container e descartado a cada
#     redeploy ou hibernacao. Serve buscas repetidas de todos os usuarios
#     enquanto o app estiver de pe.
# L2, disco: sobrevive ao processo onde o disco persiste (uso local, VM
#     propria). No Streamlit Cloud some junto com o container.
#
# Uma edicao publicada nunca muda, entao nada aqui expira.

_MEMORIA = {}
LIMITE_MEMORIA = 5000     # entradas; ~5 anos de metadados cabem folgados


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
        _MEMORIA[chave] = valor       # promove para a memoria
        return valor
    except Exception:
        return None   # cache ausente ou corrompido: segue pela rede


def _gravar_cache(chave, valor):
    if not USAR_CACHE:
        return
    if len(_MEMORIA) >= LIMITE_MEMORIA:
        _MEMORIA.clear()              # simples e suficiente: nada aqui e caro
    _MEMORIA[chave] = valor
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        caminho = _caminho_cache(chave)
        # grava em arquivo temporario e renomeia, para nunca deixar
        # um json pela metade se o processo morrer no meio
        tmp = caminho + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(valor, f, ensure_ascii=False)
        os.replace(tmp, caminho)
    except Exception:
        pass          # disco indisponivel (Cloud, permissao): a memoria basta


# --- API publica de cache, para as paginas guardarem os proprios resultados ---

def cache_ler(chave):
    """Le um valor do cache, ou None."""
    return _ler_cache(chave)


def cache_gravar(chave, valor):
    """Guarda um valor no cache (memoria + disco, quando houver)."""
    _gravar_cache(chave, valor)


def limpar_cache():
    """Apaga o cache das duas camadas. Devolve quantos arquivos removeu."""
    _MEMORIA.clear()
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
# Navegacao no site
# ==========================================

def _get(url, tentativas=3):
    erro = None
    for n in range(tentativas):
        try:
            r = requests.get(url, headers=CABECALHO, timeout=60)
            r.raise_for_status()
            return r.text
        except Exception as e:
            erro = e
            time.sleep(1 + n)
    raise RuntimeError(f"Falha ao acessar {url}: {erro}")


def listar_pagina(limitstart=0):
    """Devolve [(edicao, artigo_id)] de uma pagina da listagem."""
    html = _get(f"{BASE}/index.php?option=com_content&view=category"
                f"&id=1&limitstart={limitstart}")
    vistos, saida = set(), []
    for artigo_id, edicao in _RE_ITEM.findall(html):
        if edicao not in vistos:
            vistos.add(edicao)
            saida.append((int(edicao), int(artigo_id)))
    return saida


def obter_meta(edicao, artigo_id):
    """Devolve {'edicao', 'data', 'pdf_url'} de uma edicao, ou None."""
    chave = f"meta:{edicao}:{artigo_id}"
    guardado = _ler_cache(chave)
    if guardado:
        a, m, d = (int(x) for x in guardado["data"].split("-"))
        return {"edicao": guardado["edicao"], "data": date(a, m, d),
                "pdf_url": guardado["pdf_url"]}

    html = _get(f"{BASE}/index.php?option=com_content&view=article"
                f"&id={artigo_id}:dom-{edicao}&catid=1:dom")
    achado = _RE_PDF.search(html)
    if not achado:
        return None
    pdf_url = achado.group(1)
    if pdf_url.startswith("/"):
        pdf_url = BASE + pdf_url
    d = _RE_DATA.search(pdf_url)
    if not d:
        return None
    dia, mes, ano = (int(x) for x in d.groups())
    meta = {"edicao": edicao, "data": date(ano, mes, dia), "pdf_url": pdf_url}
    _gravar_cache(chave, {"edicao": edicao, "pdf_url": pdf_url,
                          "data": f"{ano:04d}-{mes:02d}-{dia:02d}"})
    return meta


def _meta_segura(par):
    try:
        return obter_meta(par[0], par[1])
    except Exception:
        return None


def _data_em(limitstart):
    for edicao, artigo_id in listar_pagina(limitstart):
        m = _meta_segura((edicao, artigo_id))
        if m:
            return m["data"]
    return None


def _achar_inicio(data_fim):
    """Busca binaria: menor limitstart cuja data ja seja <= data_fim."""
    if (_data_em(0) or date.min) <= data_fim:
        return 0
    baixo, alto = 0, 8000
    while baixo < alto:
        meio = (baixo + alto) // 2
        d = _data_em(meio - meio % POR_PAGINA)
        if d is None:
            alto = meio
        elif d <= data_fim:
            alto = meio
        else:
            baixo = meio + 1
    return max(0, baixo - POR_PAGINA)


def buscar_por_periodo(data_inicio, data_fim, workers=WORKERS):
    """Devolve as edicoes publicadas no periodo, em ordem cronologica."""
    limitstart = _achar_inicio(data_fim)
    achados, parar, rodadas = [], False, 0
    lote_paginas = max(1, workers // 2)

    while limitstart < 9000 and not parar and rodadas < 600:
        rodadas += 1
        alvos = range(limitstart, limitstart + lote_paginas * POR_PAGINA, POR_PAGINA)
        with ThreadPoolExecutor(workers) as ex:
            paginas = list(ex.map(listar_pagina, alvos))
        itens = [x for p in paginas for x in p]
        if not itens:
            break
        with ThreadPoolExecutor(workers) as ex:
            metas = list(ex.map(_meta_segura, itens))
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
# Recorte da secao dentro do PDF
# ==========================================

def _limpar(t):
    t = _RE_CABECALHO_PAG.sub("\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


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
    import fitz
    r = requests.get(pdf_url, headers=CABECALHO, timeout=120)
    r.raise_for_status()
    with fitz.open(stream=r.content, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc)


def _bloco(m, secao):
    import fitz
    # O cache guarda inclusive o resultado negativo ("esta edicao nao tem a
    # secao"), que tambem custou um PDF inteiro para ser descoberto.
    chave = f"bloco:{m['edicao']}:{secao}"
    guardado = _ler_cache(chave)
    if guardado is not None:
        if not guardado.get("texto"):
            return None
        return {**m, "texto": guardado["texto"], "metodo": guardado["metodo"]}

    try:
        r = requests.get(m["pdf_url"], headers=CABECALHO, timeout=120)
        r.raise_for_status()
        with fitz.open(stream=r.content, filetype="pdf") as doc:
            texto, metodo = extrair_secao(doc, secao)
        _gravar_cache(chave, {"texto": texto, "metodo": metodo})
        return {**m, "texto": texto, "metodo": metodo} if texto else None
    except Exception as e:
        # Falha de rede nao vai para o cache: da proxima vez tentamos de novo
        return {**m, "texto": None, "metodo": None, "erro": str(e)}


def buscar_decretos(data_inicio, data_fim, secao="DECRETOS SIMPLES",
                    progresso=None, workers=WORKERS):
    """
    Devolve SO os blocos da secao pedida, no periodo, em ordem cronologica:

        [{"edicao": 9339, "data": date(...), "texto": "...",
          "metodo": "sumario"|"regex", "pdf_url": "..."}]

    Edicoes sem a secao sao omitidas; falhas vem com a chave "erro".
    `progresso` e um callable opcional recebendo (feitos, total).
    """
    edicoes = buscar_por_periodo(data_inicio, data_fim, workers)
    total, feitos, saida = len(edicoes), 0, []

    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(lambda m: _bloco(m, secao), edicoes):
            feitos += 1
            if r:
                saida.append(r)
            if progresso:
                progresso(feitos, total)

    return sorted(saida, key=lambda m: m["data"])
