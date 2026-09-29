import streamlit as st
import requests
import re
import time
import cloudscraper
from datetime import datetime, date

import dom_salvador as dom

# ==========================================
# 1. INTERFACE DO SITE E CONFIGURAÇÃO
# ==========================================
st.set_page_config(page_title="Extrator DOM Salvador", page_icon="🏛️")

# --- CABEÇALHO DA BARRA LATERAL ---
st.sidebar.markdown("### 🏛️ Painel do Extrator")
st.sidebar.caption("Prefeitura Municipal de Salvador")
st.sidebar.markdown("---")

# --- FONTE DOS DADOS ---
st.sidebar.write("📡 **Fonte dos dados**")
fonte = st.sidebar.radio(
    "De onde buscar os diários:",
    ["DOM direto (site oficial)", "Querido Diário (API)"],
    help="O DOM direto lê do site da Prefeitura. O Querido Diário depende da API "
         "de terceiros, que pode estar fora do ar."
)
usar_dom = fonte.startswith("DOM")

if usar_dom:
    st.sidebar.success("Lendo direto do site oficial do DOM.")
else:
    st.sidebar.info("Usando a API pública do Querido Diário.")
st.sidebar.markdown("---")

# --- INFORMAÇÕES DA FERRAMENTA ---
st.sidebar.write("⚙️ **Modo: Texto Rápido**")
st.sidebar.write("Esta ferramenta utiliza extração estruturada simples. Como ela não utiliza a Inteligência Artificial, **não é necessário inserir nenhuma API Key** nesta página.")
st.sidebar.success("Pronto para uso imediato!")
st.sidebar.markdown("---")

st.title("🔍 Extrator de Decretos Simples")
st.write("Selecione o período abaixo para buscar os Decretos Simples no Diário Oficial de Salvador.")

# A base de texto confiável começa em momentos diferentes conforme a fonte
data_minima = date(2013, 1, 1) if usar_dom else date(2012, 6, 1)
if usar_dom:
    st.markdown("<span style='color:red'>**Atenção: Base de dados disponível desde 01/2013** "
                "(antes disso o DOM era digitalizado, sem texto confiável)</span>",
                unsafe_allow_html=True)
else:
    st.markdown("<span style='color:red'>**Atenção: Base de dados disponível desde 06/2012**</span>",
                unsafe_allow_html=True)

data_maxima = date.today()

col1, col2 = st.columns(2)
with col1:
    data_inicio = st.date_input("Data de Início", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")
with col2:
    data_fim = st.date_input("Data Final", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")

# Aviso de tempo para períodos longos
dias = (data_fim - data_inicio).days
if usar_dom and dias > 120:
    st.info(f"⏳ Período de {dias} dias. Como referência: 1 mês leva ~12s e 1 ano ~1min15s.")

# ==========================================
# 2. AÇÃO DO BOTÃO
# ==========================================
if st.button("🚀 Buscar e Gerar Relatório"):

    st.warning("🚨 **NÃO MUDE DE PÁGINA!** O robô começou a trabalhar. Se você clicar no menu lateral ou fechar esta aba, a extração será cancelada e o progresso será perdido.")

    str_inicio = data_inicio.strftime("%Y-%m-%d")
    str_fim = data_fim.strftime("%Y-%m-%d")

    # Cabeçalho de navegador, para não ser barrado por proteções do servidor
    cabecalho = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive"
    }

    # Lista comum às duas fontes: {"data": date, "edicao": str, "conteudo": str}
    achados = []
    sem_bloco = 0
    falha_api = False

    # ------------------------------------------
    # CAMINHO A: direto do site do DOM
    # ------------------------------------------
    if usar_dom:
        with st.spinner(f"Buscando diários de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')} no site do DOM..."):
            progresso = st.progress(0)
            try:
                blocos = dom.buscar_decretos(
                    data_inicio, data_fim, secao="DECRETOS SIMPLES",
                    progresso=lambda feitos, total: progresso.progress(feitos / max(total, 1))
                )
            except Exception as e:
                falha_api = True
                blocos = []
                st.error(f"Erro ao acessar o site do DOM: {e}")
                st.info("Confira se http://www.dom.salvador.ba.gov.br está no ar.")

            for b in blocos:
                if b.get("erro"):
                    sem_bloco += 1
                    continue
                achados.append({
                    "data": b["data"],
                    "edicao": str(b["edicao"]),
                    "conteudo": b["texto"],
                })

    # ------------------------------------------
    # CAMINHO B: API do Querido Diário
    # ------------------------------------------
    else:
        with st.spinner(f"Buscando diários de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}..."):
            url_api = "https://api.queridodiario.ok.org.br/gazettes"
            scraper = cloudscraper.create_scraper()
            lista_diarios = []
            offset = 0

            while True:
                parametros = {
                    "territory_ids": "2927408",
                    "querystring": '"DECRETOS SIMPLES"',
                    "published_since": str_inicio,
                    "published_until": str_fim,
                    "size": 50,
                    "offset": offset
                }

                try:
                    resposta_api = scraper.get(url_api, params=parametros, headers=cabecalho, timeout=60)
                    resposta_api.raise_for_status()
                    dados = resposta_api.json()

                    if "gazettes" in dados and len(dados["gazettes"]) > 0:
                        lista_diarios.extend(dados["gazettes"])
                        offset += 50
                        # Para assim que já baixamos tudo que a API declara ter
                        if offset >= dados.get("total_gazettes", 0):
                            break
                        time.sleep(1)
                    else:
                        break

                except Exception as e:
                    falha_api = True
                    st.error(f"Erro ao consultar a API do Querido Diário: {e}")
                    st.info("Se o erro persistir, troque a fonte para **DOM direto** na barra lateral — "
                            "a API do Querido Diário sai do ar com alguma frequência.")
                    break

            if lista_diarios:
                lista_diarios = sorted(lista_diarios, key=lambda x: x["date"])
                progresso = st.progress(0)
                total = len(lista_diarios)

                # Parada inteligente: seções que costumam suceder os decretos
                parada = (r"\n\s*(?:SECRETARIA|GABINETE|PROCURADORIA|CONTROLADORIA|"
                          r"SUPERINTEND[ÊE]NCIA|FUNDA[ÇC][ÃA]O|LICITA[ÇC][ÕO]ES|CONSELHO|"
                          r"CASA\s+CIVIL|EMPRESA|INSTITUTO|AG[ÊE]NCIA|COMPANHIA)\b")
                padrao = rf"DECRETOS SIMPLES(.*?)({parada})"

                for i, diario in enumerate(lista_diarios):
                    try:
                        texto_completo = scraper.get(diario["txt_url"], headers=cabecalho, timeout=60).text

                        numero_dom = diario.get("edition") or "Não identificado"

                        blocos = re.findall(padrao, texto_completo, re.DOTALL)
                        if blocos:
                            conteudo = max(blocos, key=lambda x: len(x[0]))[0].strip()
                            achados.append({
                                "data": datetime.strptime(diario["date"], "%Y-%m-%d").date(),
                                "edicao": str(numero_dom),
                                "conteudo": conteudo,
                            })
                        else:
                            sem_bloco += 1
                    except Exception:
                        sem_bloco += 1

                    progresso.progress((i + 1) / total)

    # ==========================================
    # 3. MONTAGEM DO RELATÓRIO
    # ==========================================
    if achados:
        achados = sorted(achados, key=lambda x: x["data"])

        texto_para_salvar = "RELATÓRIO DE DECRETOS SIMPLES - SALVADOR\n"
        texto_para_salvar += f"PERÍODO: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}\n"
        texto_para_salvar += f"FONTE: {fonte}\n"
        texto_para_salvar += f"GERADO EM: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        texto_para_salvar += "=" * 60 + "\n\n"

        for item in achados:
            texto_para_salvar += "🟥" * 30 + "\n"
            texto_para_salvar += f"📅 DATA: {item['data'].strftime('%d/%m/%Y')} | EDIÇÃO Nº: {item['edicao']}\n"
            texto_para_salvar += "🟥" * 30 + "\n\n"
            texto_para_salvar += item["conteudo"] + "\n\n"
            texto_para_salvar += "\n\n\n"  # Espaço extra entre diários

        st.success(f"✅ Relatório gerado! {len(achados)} diários com Decretos Simples.")
        if sem_bloco:
            st.info(f"ℹ️ {sem_bloco} diário(s) do período não traziam a seção (ou falharam) e ficaram de fora.")

        # 4. BOTÃO DE DOWNLOAD
        nome_arquivo = f"Decretos_Salvador_{str_inicio}_a_{str_fim}.txt"
        st.download_button(
            label="📥 Baixar Arquivo .TXT",
            data=texto_para_salvar,
            file_name=nome_arquivo,
            mime="text/plain"
        )

    elif falha_api:
        st.error("A busca foi interrompida por causa do erro acima. Nenhum relatório foi gerado.")

    else:
        st.warning("Nenhum diário com Decretos Simples encontrado para este período.")
