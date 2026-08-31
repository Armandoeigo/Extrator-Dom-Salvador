import streamlit as st
import requests
from google import genai
from google.genai import errors as erros_ia
import re
import time
import random
import pandas as pd
import io
import csv
from datetime import datetime, date

import dom_salvador as dom

# Modelo do Gemini usado na extração. Alternativa mais recente da mesma
# familia: "gemini-3.5-flash-lite".
MODELO_IA = "gemini-3.1-flash-lite"

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

# A IA é o gargalo desta página: avisa antes de o usuário esperar à toa.
# Medido em agosto/2026: cada diário rende, em média, ~5 pedaços de texto,
# e cada pedaço é uma chamada à IA.
dias = (data_fim - data_inicio).days
if dias > 14:
    diarios = max(1, int(dias * 0.7))
    chamadas_previstas = diarios * 5
    estimativa = int(chamadas_previstas * (pausa_ia + 3) / 60)
    st.warning(f"⏳ Período de {dias} dias ≈ {diarios} diários e ~{chamadas_previstas} chamadas "
               f"à IA. Com a pausa de {pausa_ia}s, isso deve levar cerca de **{estimativa} min**. "
               f"O texto é fatiado para a IA não perder atos — o que custa tempo e cota.")

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
# FATIAMENTO PARA A IA
# ==========================================
# Um diário pode render centenas de milhares de caracteres. Mandados de uma
# vez só, o modelo satura e devolve uma amostra dos atos em vez de todos.
# Fatiar custa mais chamadas, mas não perde registro.
TAMANHO_PEDACO = 30000
SOBREPOSICAO = 2000      # evita cortar um ato exatamente na emenda


def fatiar(texto, tamanho=TAMANHO_PEDACO, sobreposicao=SOBREPOSICAO):
    """Quebra o texto em pedaços, preferindo cortar em quebra de linha."""
    if len(texto) <= tamanho:
        return [texto]
    pedacos, ini = [], 0
    while ini < len(texto):
        fim = min(ini + tamanho, len(texto))
        if fim < len(texto):
            quebra = texto.rfind("\n", ini + tamanho // 2, fim)
            if quebra > 0:
                fim = quebra
        pedacos.append(texto[ini:fim])
        if fim >= len(texto):
            break
        ini = max(fim - sobreposicao, ini + 1)
    return pedacos


def chave_linha(linha):
    """Identidade de um ato, para não repetir o que cai na sobreposição."""
    campos = [str(c).strip().upper() for c in linha[:3]]
    return tuple(re.sub(r"\s+", " ", c) for c in campos)


# ==========================================
# 2. AÇÃO DO BOTÃO
# ==========================================
if st.button("🚀 Buscar e Gerar Planilha Excel"):

    if not chave_api:
        st.error("⚠️ Por favor, cole a sua API Key no menu lateral esquerdo antes de clicar em buscar.")
    else:
        tempo_inicio = time.time()

        st.warning("🚨 **NÃO MUDE DE PÁGINA!** O robô começou a trabalhar. Se você clicar no menu lateral ou fechar esta aba, a extração será cancelada e o progresso será perdido.")

        # SDK novo (google-genai): um Client no lugar da configuração global
        cliente_ia = genai.Client(api_key=chave_api)

        repeticoes = {"n": 0}

        def perguntar_ia(texto, tentativas=4):
            """
            Uma pergunta ao Gemini, devolvendo só o texto da resposta.

            Erros temporários são refeitos com espera crescente: o 503
            ("modelo sobrecarregado") e o 429 (cota por minuto estourada)
            são frequentes e passageiros. Sem isso, um soluço momentâneo do
            servidor custa o diário inteiro.
            """
            espera = 8
            for n in range(tentativas):
                try:
                    resposta = cliente_ia.models.generate_content(
                        model=MODELO_IA, contents=texto)
                    return (resposta.text or "").strip()
                except erros_ia.APIError as e:
                    passageiro = (isinstance(e, erros_ia.ServerError)
                                  or getattr(e, "code", None) == 429)
                    if not passageiro or n == tentativas - 1:
                        raise
                    repeticoes["n"] += 1
                    # jitter: evita que várias tentativas caiam no mesmo instante
                    time.sleep(espera + random.uniform(0, 3))
                    espera *= 2   # 8s, 16s, 32s

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
            perdidos = []   # diários que a IA não conseguiu processar
            parciais = []   # diários que vieram incompletos: (data, falhos, total)
            chamadas = {"n": 0}
            progresso = st.progress(0)
            situacao = st.empty()
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
                        num_dom = perguntar_ia(prompt_capa)

                    # Fatia o texto: mandado inteiro, o modelo satura e devolve
                    # só uma amostra dos atos.
                    pedacos = fatiar(texto_secao)
                    vistos = set()          # dedup do que cai na sobreposição
                    falhos_no_diario = 0
                    ultimo_erro = None

                    for n_ped, pedaco in enumerate(pedacos, 1):
                        situacao.write(f"Diário {i+1}/{total} "
                                       f"({item['data'].strftime('%d/%m/%Y')}) — "
                                       f"pedaço {n_ped}/{len(pedacos)}")

                        # A ORDEM NOVA: GERAR DADOS PUROS (CSV)
                        prompt_decretos = f"""
                        Você é um especialista em extração de dados de Diários Oficiais.
                        Leia o texto abaixo, que contém Decretos de Pessoal em parágrafos corridos.
                        Sua missão é procurar nomeações, exonerações, demissões, transferências e outros atos de pessoal.

                        O texto pode conter atos escritos de formas variadas ("Nomear", "Exonerar",
                        "Considerar exonerado", "Declarar a Vacância", "Designar", "Dispensar") e
                        também TABELAS com colunas como SERVIDOR, MATRÍCULA, CÓDIGO/ESCOLA.
                        Extraia os atos nos dois formatos. Nas tabelas, cada linha é um ato.

                        Extraia os dados desses textos e monte uma tabela estrita no formato CSV, separada por ponto e vírgula (;).

                        O cabeçalho obrigatório deve ser exatamente este:
                        Ato;Nome;Matricula;Cargo;Secretaria

                        Exemplo de como você deve montar a linha com base no texto lido:
                        Demissão;ANNE GABRIELA COSTA NASCIMENTO SANTOS;813672;Agente de Salvamento Aquático;Secretaria Municipal de Ordem Pública

                        Retorne APENAS o CSV. Não escreva mais nada.
                        Se não encontrar nenhum ato de pessoal no texto, responda EXATAMENTE a palavra: NADA

                        Texto para análise:
                        {pedaco}
                        """

                        # A falha é tratada AQUI, por pedaço. Se um pedaço não
                        # vier, os outros do mesmo diário continuam valendo —
                        # perder 1 de 17 é muito melhor que perder o dia todo.
                        try:
                            conteudo_csv = perguntar_ia(prompt_decretos)
                            chamadas["n"] += 1
                        except erros_ia.APIError as e:
                            falhos_no_diario += 1
                            ultimo_erro = e
                            continue

                        # Limpando blocos de código indesejados da IA
                        conteudo_csv = re.sub(r'```(?:csv|text)?', '', conteudo_csv).strip()

                        if conteudo_csv != "NADA" and conteudo_csv != "":
                            # Transforma a resposta da IA em linhas de código
                            leitor_csv = csv.reader(io.StringIO(conteudo_csv), delimiter=';')

                            for linha in leitor_csv:
                                # Pula possíveis cabeçalhos de coluna que a IA tenha gerado sozinha
                                se_cabeçalho = any("Ato" in str(campo) or "Nome" in str(campo) for campo in linha)

                                if len(linha) >= 2 and not se_cabeçalho:
                                    chave = chave_linha(linha)
                                    if chave in vistos:
                                        continue
                                    vistos.add(chave)
                                    data_formatada = item["data"].strftime("%d/%m/%Y")
                                    linha_completa = [data_formatada, num_dom] + linha
                                    dados_para_excel.append(linha_completa)

                        if pausa_ia:
                            time.sleep(pausa_ia)

                    # Balanço do diário: nada, tudo ou parte?
                    if falhos_no_diario:
                        codigo = getattr(ultimo_erro, "code", "?")
                        if falhos_no_diario >= len(pedacos):
                            perdidos.append(item["data"])
                        else:
                            parciais.append((item["data"], falhos_no_diario, len(pedacos)))
                        if codigo == 429:
                            st.warning(f"{item['data'].strftime('%d/%m/%Y')}: {falhos_no_diario} de "
                                       f"{len(pedacos)} pedaços travaram na cota do Gemini. "
                                       f"Aumente a pausa na barra lateral.")
                        else:
                            st.warning(f"{item['data'].strftime('%d/%m/%Y')}: {falhos_no_diario} de "
                                       f"{len(pedacos)} pedaços falharam (erro {codigo}). "
                                       f"É passageiro — rode o período de novo para completar.")

                # Erros de pedaço já foram tratados acima. Aqui só cai o que
                # impede o diário inteiro: download do PDF, leitura, edição.
                except erros_ia.APIError as e:
                    perdidos.append(item["data"])
                    st.error(f"Diário de {item['data'].strftime('%d/%m/%Y')}: erro "
                             f"{getattr(e, 'code', '?')} da IA ao identificar a edição — {e}")

                except Exception as e:
                    perdidos.append(item["data"])
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

            situacao.empty()
            st.success(f"✅ Análise concluída! Diários processados: {total} "
                       f"({chamadas['n']} chamadas à IA)")
            if sem_trecho:
                st.info(f"ℹ️ {sem_trecho} diário(s) não traziam a seção de Decretos Numerados e foram pulados.")
            if repeticoes["n"]:
                st.info(f"🔁 {repeticoes['n']} chamada(s) à IA falharam por sobrecarga/cota e "
                        f"foram refeitas automaticamente.")
            if parciais:
                det = ", ".join(f"{d.strftime('%d/%m/%Y')} ({f} de {t} pedaços)"
                                for d, f, t in parciais)
                st.warning(f"⚠️ **{len(parciais)} diário(s) vieram incompletos:** {det}. "
                           f"O que foi extraído está na planilha; rode o período de novo "
                           f"para completar o que faltou.")
            if perdidos:
                dias = ", ".join(d.strftime("%d/%m/%Y") for d in perdidos)
                st.warning(f"⚠️ **{len(perdidos)} diário(s) ficaram de fora da planilha:** {dias}. "
                           f"Rode este período novamente para completá-los — a busca já está em "
                           f"cache, então será rápido.")

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
