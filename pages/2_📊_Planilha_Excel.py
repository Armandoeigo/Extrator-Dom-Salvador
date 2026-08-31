import streamlit as st
import requests
import google.generativeai as genai
import re
import time
import pandas as pd
import io
import csv
from datetime import datetime, date

import dom_salvador as dom

# ==========================================
# 1. INTERFACE DO SITE E CONFIGURAÇÃO DA IA
# ==========================================
st.set_page_config(page_title="Extrator DOM com IA", page_icon="📊")

# --- FONTE DOS DADOS ---
st.sidebar.write("📡 **Fonte dos dados**")
fonte = st.sidebar.radio(
    "De onde buscar os diários:",
    ["DOM direto (site oficial)", "Querido Diário (API)"],
    help="O DOM direto lê do site da Prefeitura e já traz o número da edição, "
         "o que dispensa uma das chamadas de IA por diário."
)
usar_dom = fonte.startswith("DOM")
st.sidebar.markdown("---")

# --- CONFIGURAÇÕES DA API ---
st.sidebar.write("⚙️ **Motor de Inteligência**")
st.sidebar.write("O sistema utiliza a IA do Google Gemini para processamento.")

chave_api = st.sidebar.text_input("🔑 Cole sua API Key aqui:", type="password", autocomplete="off")

# --- TUTORIAL PASSO A PASSO ---
with st.sidebar.expander("❓ Como criar minha API Key grátis?"):
    st.markdown("""
    **Passo a passo rápido:**
    1. Acesse o site [Google AI Studio](https://aistudio.google.com/).
    2. Faça login com a sua conta do Google (a mesma do Gmail).
    3. No menu lateral esquerdo, clique na opção **"Get API key"**.
    4. Clique no botão azul **"Create API key"**.
    5. Copie a sequência de letras e números gerada e cole no campo acima!
    """)

st.sidebar.markdown("---")

# --- RITMO DAS CHAMADAS DE IA ---
st.sidebar.write("🐢 **Ritmo da IA**")
pausa_ia = st.sidebar.slider(
    "Pausa entre diários (segundos)", 0, 20, 12,
    help="A conta gratuita do Gemini limita as chamadas por minuto. Se você tem "
         "cota paga, pode reduzir bastante e acelerar períodos longos."
)

st.title("📊 Extrator Matemático: Cargos e Decretos")

st.write("Selecione o período abaixo. A IA vai ignorar textos soltos e gerar uma **planilha de Excel** perfeita com os atos de pessoal (nomeações, exonerações, designações).")

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

# A IA é o gargalo desta página: avisa antes de o usuário esperar à toa
dias = (data_fim - data_inicio).days
if dias > 60:
    estimativa = int(dias * 0.7 * (pausa_ia + 3) / 60)
    st.warning(f"⏳ Período de {dias} dias. Com a pausa de {pausa_ia}s entre diários, "
               f"isso deve levar cerca de **{estimativa} min**. O tempo aqui é da IA, não da busca.")

# ==========================================
# RECORTE DO TRECHO QUE VAI PARA A IA
# ==========================================
# ATENÇÃO: este recorte é LARGO de propósito (pega ~60% do diário).
# Os atos de pessoal ficam espalhados: uns em DECRETOS SIMPLES, outros dentro
# da seção de cada Secretaria (SMED, SMS...). Recortar apenas a seção
# "DECRETOS NUMERADOS" descarta a maior parte deles. Não estreite esta lista
# de parada sem antes conferir que os nomes esperados continuam aparecendo.
PARADA_PESSOAL = r"\n\s*(?:DECRETOS FINANCEIROS|CONTRATOS|LICITAÇÕES|EDITAIS|ATOS|AVISOS)\b"


def cortar_trecho(texto_completo):
    """Do último 'DECRETOS NUMERADOS' até uma das seções finais do diário."""
    ocorrencias = list(re.finditer(r"DECRETOS\s+NUMERADOS", texto_completo, re.IGNORECASE))
    if not ocorrencias:
        return None
    texto_restante = texto_completo[ocorrencias[-1].start():]
    match_fim = re.search(PARADA_PESSOAL, texto_restante, re.IGNORECASE)
    return texto_restante[:match_fim.start()] if match_fim else texto_restante


# ==========================================
# 2. AÇÃO DO BOTÃO
# ==========================================
if st.button("🚀 Buscar e Gerar Planilha Excel"):

    if not chave_api:
        st.error("⚠️ Por favor, cole a sua API Key no menu lateral esquerdo antes de clicar em buscar.")
    else:
        tempo_inicio = time.time()

        st.warning("🚨 **NÃO MUDE DE PÁGINA!** O robô começou a trabalhar. Se você clicar no menu lateral ou fechar esta aba, a extração será cancelada e o progresso será perdido.")

        genai.configure(api_key=chave_api)

        modelo_ia = genai.GenerativeModel('gemini-3.1-flash-lite')

        str_inicio = data_inicio.strftime("%Y-%m-%d")
        str_fim = data_fim.strftime("%Y-%m-%d")

        # Lista comum às duas fontes. O texto do diário é baixado só na hora de
        # processar, para não segurar centenas de PDFs na memória.
        # {"data": date, "edicao": str|None, "pdf_url": str|None, "txt_url": str|None}
        a_processar = []
        falha_api = False

        # ------------------------------------------
        # CAMINHO A: direto do site do DOM
        # ------------------------------------------
        if usar_dom:
            with st.spinner("Buscando diários no site do DOM..."):
                try:
                    edicoes = dom.buscar_por_periodo(data_inicio, data_fim)
                except Exception as e:
                    falha_api = True
                    edicoes = []
                    st.error(f"Erro ao acessar o site do DOM: {e}")
                    st.info("Confira se http://www.dom.salvador.ba.gov.br está no ar.")

                for m in edicoes:
                    a_processar.append({
                        "data": m["data"],
                        "edicao": str(m["edicao"]),   # vem de graça no nome do PDF
                        "pdf_url": m["pdf_url"],
                        "txt_url": None,
                    })

        # ------------------------------------------
        # CAMINHO B: API do Querido Diário
        # ------------------------------------------
        else:
            with st.spinner("Buscando diários no servidor..."):
                url_api = "https://api.queridodiario.ok.org.br/gazettes"
                lista_diarios = []
                offset = 0

                while True:
                    parametros = {
                        "territory_ids": "2927408",
                        "querystring": '"DECRETOS NUMERADOS"',
                        "published_since": str_inicio,
                        "published_until": str_fim,
                        "size": 50,
                        "offset": offset
                    }
                    try:
                        resposta_api = requests.get(url_api, params=parametros, timeout=60)
                        resposta_api.raise_for_status()
                        dados = resposta_api.json()
                        if "gazettes" in dados and len(dados["gazettes"]) > 0:
                            lista_diarios.extend(dados["gazettes"])
                            offset += 50
                            # Para assim que já baixamos tudo que a API declara ter
                            if offset >= dados.get("total_gazettes", 0):
                                break
                            time.sleep(1)  # Freio ABS para o servidor do Querido Diário não bloquear
                        else:
                            break
                    except Exception as e:
                        falha_api = True
                        st.error(f"Erro ao consultar a API do Querido Diário: {e}")
                        st.info("Se o erro persistir, troque a fonte para **DOM direto** na barra lateral — "
                                "a API do Querido Diário sai do ar com alguma frequência.")
                        break

                for diario in sorted(lista_diarios, key=lambda x: x["date"]):
                    a_processar.append({
                        "data": datetime.strptime(diario["date"], "%Y-%m-%d").date(),
                        "edicao": diario.get("edition"),  # pode vir vazio; a IA resolve
                        "pdf_url": None,
                        "txt_url": diario["txt_url"],
                    })

        # ==========================================
        # 3. PROCESSAMENTO PELA IA
        # ==========================================
        if a_processar:
            dados_para_excel = []
            sem_trecho = 0
            progresso = st.progress(0)
            total = len(a_processar)

            st.info("🧠 A IA está garimpando os dados numéricos e montando as colunas...")

            for i, item in enumerate(a_processar):
                try:
                    # Baixa o texto do diário agora (não antes, para poupar memória)
                    if item["pdf_url"]:
                        texto_completo = dom.baixar_texto(item["pdf_url"])
                    else:
                        texto_completo = requests.get(item["txt_url"], timeout=60).text

                    texto_secao = cortar_trecho(texto_completo)
                    if not texto_secao:
                        sem_trecho += 1
                        progresso.progress((i + 1) / total)
                        continue

                    num_dom = item.get("edicao")

                    # Só chama a IA para descobrir a edição quando ela não veio na fonte.
                    # Pelo DOM direto o número está no nome do arquivo, o que corta
                    # metade das chamadas de IA (e metade do custo).
                    if not num_dom:
                        prompt_capa = f"""
                        Analise o começo deste Diário Oficial de Salvador e identifique o número da edição.
                        Retorne APENAS o número (ex: 8.542). Se não encontrar, retorne S/N.
                        Texto: {texto_completo[:2000]}
                        """
                        num_dom = modelo_ia.generate_content(prompt_capa).text.strip()

                    # A ORDEM NOVA: GERAR DADOS PUROS (CSV)
                    prompt_decretos = f"""
                    Você é um especialista em extração de dados de Diários Oficiais.
                    Leia o texto abaixo, que contém Decretos de Pessoal em parágrafos corridos.
                    Sua missão é procurar nomeações, exonerações, demissões, transferências e outros atos de pessoal.

                    Extraia os dados desses textos e monte uma tabela estrita no formato CSV, separada por ponto e vírgula (;).

                    O cabeçalho obrigatório deve ser exatamente este:
                    Ato;Nome;Matricula;Cargo;Secretaria

                    Exemplo de como você deve montar a linha com base no texto lido:
                    Demissão;ANNE GABRIELA COSTA NASCIMENTO SANTOS;813672;Agente de Salvamento Aquático;Secretaria Municipal de Ordem Pública

                    Retorne APENAS o CSV. Não escreva mais nada.
                    Se não encontrar nenhum ato de pessoal no texto, responda EXATAMENTE a palavra: NADA

                    Texto para análise:
                    {texto_secao}
                    """

                    resposta_decretos = modelo_ia.generate_content(prompt_decretos)
                    conteudo_csv = resposta_decretos.text.strip()

                    # Limpando blocos de código indesejados da IA
                    conteudo_csv = re.sub(r'```(?:csv|text)?', '', conteudo_csv).strip()

                    if conteudo_csv != "NADA" and conteudo_csv != "":
                        # Transforma a resposta da IA em linhas de código
                        leitor_csv = csv.reader(io.StringIO(conteudo_csv), delimiter=';')

                        for linha in leitor_csv:
                            # Pula possíveis cabeçalhos de coluna que a IA tenha gerado sozinha
                            se_cabeçalho = any("Ato" in str(campo) or "Nome" in str(campo) for campo in linha)

                            if len(linha) >= 2 and not se_cabeçalho:
                                data_formatada = item["data"].strftime("%d/%m/%Y")
                                linha_completa = [data_formatada, num_dom] + linha
                                dados_para_excel.append(linha_completa)

                    if pausa_ia:
                        time.sleep(pausa_ia)

                except Exception as e:
                    st.error(f"Erro no diário de {item['data'].strftime('%d/%m/%Y')}: {e}")

                progresso.progress((i + 1) / total)

            duracao_total = int(time.time() - tempo_inicio)
            minutos = duracao_total // 60
            segundos = duracao_total % 60

            # Exibe o tempo formatado na tela
            if minutos > 0:
                st.metric(label="⏱️ Tempo Total da Consulta", value=f"{minutos} min e {segundos} seg")
            else:
                st.metric(label="⏱️ Tempo Total da Consulta", value=f"{segundos} seg")

            st.success(f"✅ Análise concluída! Diários processados: {total}")
            if sem_trecho:
                st.info(f"ℹ️ {sem_trecho} diário(s) não traziam a seção de Decretos Numerados e foram pulados.")

            # ==========================================
            # 4. GERAÇÃO DA PLANILHA EXCEL (.XLSX)
            # ==========================================
            if dados_para_excel:
                # ATENÇÃO: As colunas novas para o Excel!
                colunas = ["Data da Publicação", "Nº do DOM", "Ato", "Nome", "Matrícula", "Cargo", "Secretaria"]

                # Padroniza as linhas para garantir que todas tenham 7 colunas (evita erros no Excel)
                dados_padronizados = [linha[:7] + [""] * (7 - len(linha[:7])) for linha in dados_para_excel]

                df = pd.DataFrame(dados_padronizados, columns=colunas)

                # Salva os dados do Pandas em um arquivo Excel virtual
                buffer = io.BytesIO()
                with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                    df.to_excel(writer, index=False, sheet_name='Cargos Extraidos')

                nome_arquivo = f"Decretos_Matematico_{str_inicio}_a_{str_fim}.xlsx"

                st.download_button(
                    label="📥 Baixar Planilha Excel Perfeita (.xlsx)",
                    data=buffer.getvalue(),
                    file_name=nome_arquivo,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            else:
                st.warning("A IA processou os diários, mas não encontrou nenhuma tabela de cargos no período selecionado.")

        elif falha_api:
            st.error("A busca foi interrompida por causa do erro acima. Nenhuma planilha foi gerada.")

        else:
            st.warning("Nenhum diário encontrado no período.")
