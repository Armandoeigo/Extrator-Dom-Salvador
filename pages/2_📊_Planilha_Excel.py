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

st.sidebar.title("⚙️ Configurações da IA")
st.sidebar.write("Gerador de Planilhas Perfeitas (Excel).")
chave_api = st.sidebar.text_input("🔑 Cole sua API Key aqui:", type="password")
st.sidebar.markdown("[Clique aqui para criar/ver sua API Key grátis](https://aistudio.google.com/)")

st.title("📊 Extrator Matemático: Cargos e Decretos")

st.write("Selecione o período abaixo. A IA vai ignorar textos soltos e gerar uma **planilha de Excel** perfeita com as tabelas de cargos acrescidos e suprimidos.")

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
                        Você é um robô extrator de dados estruturados.
                        Sua ÚNICA tarefa é extrair os quadros de cargos (Acrescidos e Suprimidos) da seção "DECRETOS NUMERADOS".
                        
                        REGRAS RÍGIDAS DE FORMATAÇÃO:
                        1. Ignore todo o texto explicativo, organogramas, cabeçalhos, introduções e assinaturas.
                        2. Retorne os dados ESTRITAMENTE em formato CSV, usando ponto e vírgula (;) como separador.
                        3. A estrutura de cada linha DEVE ser obrigatoriamente:
                           Numero_do_Decreto;Nome_do_Cargo;Grau;Qtd_Acrescida;Qtd_Suprimida
                        4. Se a coluna Acrescido ou Suprimido estiver em branco, coloque 0.
                        5. NÃO use blocos de código Markdown (como ```csv). Retorne apenas o texto puro do CSV.
                        6. Se não houver NENHUMA tabela de cargos neste texto, retorne EXATAMENTE a palavra "NADA".
                        
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
                                se_cabeçalho = any("Decreto" in str(item) or "Cargo" in str(item) for item in linha)
                                
                                if len(linha) >= 2 and not se_cabeçalho:
                                    # Formata a data para padrão brasileiro
                                    data_formatada = datetime.strptime(data_pub, "%Y-%m-%d").strftime("%d/%m/%Y")
                                    
                                    # Junta a Data e o DOM (gerados pelo sistema) com os 5 dados (gerados pela IA)
                                    linha_completa = [data_formatada, num_dom] + linha
                                    dados_para_excel.append(linha_completa)
                        
                        time.sleep(8)
                        
                    except Exception as e:
                        st.error(f"Erro no diário de {data_pub}: {e}")
                    
                    progresso.progress((i + 1) / total)

                st.success(f"✅ Análise concluída! Diários processados: {total}")
                
                # ==========================================
                # 3. GERAÇÃO DA PLANILHA EXCEL (.XLSX)
                # ==========================================
                if dados_para_excel:
                    colunas = ["Data da Publicação", "Nº do DOM", "Nº do Decreto", "Cargo / Função", "Grau", "Qtd Acrescida", "Qtd Suprimida"]
                    
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
