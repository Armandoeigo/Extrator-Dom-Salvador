import streamlit as st
import requests
import google.generativeai as genai
import re
import time
import pandas as pd
import io
import csv
from datetime import datetime, date

# ==========================================
# 1. INTERFACE DO SITE E CONFIGURAÇÃO DA IA
# ==========================================
st.set_page_config(page_title="Extrator DOM com IA", page_icon="📊")

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

st.title("📊 Extrator Matemático: Cargos e Decretos")

st.write("Selecione o período abaixo. A IA vai ignorar textos soltos e gerar uma **planilha de Excel** perfeita com as tabelas de cargos acrescidos e suprimidos.")

st.markdown("<span style='color:red'>**Atenção: Base de dados disponível desde 06/2012**</span>", unsafe_allow_html=True)

data_minima = date(2012, 6, 1)
data_maxima = date.today()

col1, col2 = st.columns(2)
with col1:
    data_inicio = st.date_input("Data de Início", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")
with col2:
    data_fim = st.date_input("Data Final", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")

# ==========================================
# 2. AÇÃO DO BOTÃO
# ==========================================
if st.button("🚀 Buscar e Gerar Planilha Excel"):
    
    if not chave_api:
        st.error("⚠️ Por favor, cole a sua API Key no menu lateral esquerdo antes de clicar em buscar.")
    else:
        st.warning("🚨 **NÃO MUDE DE PÁGINA!** O robô começou a trabalhar. Se você clicar no menu lateral ou fechar esta aba, a extração será cancelada e o progresso será perdido.")
        
        genai.configure(api_key=chave_api)
        
        modelo_ia = genai.GenerativeModel('gemini-3.1-flash-lite')
        
        str_inicio = data_inicio.strftime("%Y-%m-%d")
        str_fim = data_fim.strftime("%Y-%m-%d")
        
        with st.spinner("Buscando diários no servidor..."):
            url_api = "https://api.queridodiario.ok.org.br/api/gazettes/"
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
                    resposta_api = requests.get(url_api, params=parametros)
                    resposta_api.raise_for_status() 
                    dados = resposta_api.json()
                    if "gazettes" in dados and len(dados["gazettes"]) > 0:
                        lista_diarios.extend(dados["gazettes"])
                        offset += 50 
                        time.sleep(1) # Freio ABS para o servidor do Querido Diário não bloquear
                    else:
                        break 
                except Exception as e:
                    st.error(f"Erro de conexão com o Querido Diário: {e}")
                    break

            if len(lista_diarios) > 0:
                lista_diarios = sorted(lista_diarios, key=lambda x: x["date"])
                
                # Lista mestre onde vamos guardar as linhas puras da planilha
                dados_para_excel = []
                
                progresso = st.progress(0)
                total = len(lista_diarios)
                
                st.info("🧠 A IA está garimpando os dados numéricos e montando as colunas...")
                
                for i, diario in enumerate(lista_diarios):
                    data_pub = diario["date"]
                    url_txt = diario["txt_url"]
                    
                    try:
                        texto_completo = requests.get(url_txt).text
                        
                        # IA identificando o DOM na capa
                        prompt_capa = f"""
                        Analise o começo deste Diário Oficial de Salvador e identifique o número da edição.
                        Retorne APENAS o número (ex: 8.542). Se não encontrar, retorne S/N.
                        Texto: {texto_completo[:2000]}
                        """
                        num_dom = modelo_ia.generate_content(prompt_capa).text.strip()
                        
                        # O Corte Dinâmico
                        ocorrencias = list(re.finditer(r"DECRETOS\s+NUMERADOS", texto_completo, re.IGNORECASE))
                        if not ocorrencias:
                            continue 
                            
                        inicio_idx = ocorrencias[-1].start()
                        texto_restante = texto_completo[inicio_idx:]
                        
                        match_fim = re.search(r"\n\s*(?:DECRETOS FINANCEIROS|CONTRATOS|LICITAÇÕES|EDITAIS|ATOS|AVISOS)\b", texto_restante, re.IGNORECASE)
                        texto_secao = texto_restante[:match_fim.start()] if match_fim else texto_restante
                        
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
                                se_cabeçalho = any("Ato" in str(item) or "Nome" in str(item) for item in linha)
                                
                                if len(linha) >= 2 and not se_cabeçalho:
                                    # Formata a data para padrão brasileiro
                                    data_formatada = datetime.strptime(data_pub, "%Y-%m-%d").strftime("%d/%m/%Y")
                                    
                                    # Junta a Data e o DOM com os dados gerados pela IA
                                    linha_completa = [data_formatada, num_dom] + linha
                                    dados_para_excel.append(linha_completa)
                        
                        time.sleep(12)
                        
                    except Exception as e:
                        st.error(f"Erro no diário de {data_pub}: {e}")
                    
                    progresso.progress((i + 1) / total)

                st.success(f"✅ Análise concluída! Diários processados: {total}")
                
                # ==========================================
                # 3. GERAÇÃO DA PLANILHA EXCEL (.XLSX)
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

            else:
                st.warning("Nenhum diário encontrado no período.")
