import streamlit as st
import requests
import re
from datetime import datetime, date

# ==========================================
# 1. INTERFACE DO SITE
# ==========================================
st.set_page_config(page_title="Extrator DOM Salvador", page_icon="📑")

st.title("🔍 Extrator de Decretos Simples")
st.write("Selecione o período abaixo para buscar os Decretos Simples no Diário Oficial de Salvador.")

# Definindo os limites do calendário (de 2001 até hoje)
data_minima = date(2001, 1, 1)
data_maxima = date.today()

col1, col2 = st.columns(2)
with col1:
    data_inicio = st.date_input("Data de Início", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")
with col2:
    data_fim = st.date_input("Data Final", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")

# ==========================================
# 2. AÇÃO DO BOTÃO
# ==========================================
if st.button("🚀 Buscar e Gerar Relatório"):
    
    str_inicio = data_inicio.strftime("%Y-%m-%d")
    str_fim = data_fim.strftime("%Y-%m-%d")
    
    with st.spinner(f"Buscando diários de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}..."):
        
        url_api = "https://api.queridodiario.ok.org.br/api/gazettes/"
        
        # Paginação: Garante que pegamos todos os diários do período
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
                resposta_api = requests.get(url_api, params=parametros)
                resposta_api.raise_for_status() 
                dados = resposta_api.json()

                if "gazettes" in dados and len(dados["gazettes"]) > 0:
                    lista_diarios.extend(dados["gazettes"])
                    offset += 50 
                else:
                    break 
                    
            except Exception as e:
                st.error(f"Erro de conexão com a API: {e}")
                break

        # ==========================================
        # 3. PROCESSAMENTO E ORDENAÇÃO
        # ==========================================
        if len(lista_diarios) > 0:
            
            # Ordenação Cronológica (do mais antigo para o mais novo)
            lista_diarios = sorted(lista_diarios, key=lambda x: x["date"])
            
            texto_para_salvar = f"RELATÓRIO DE DECRETOS SIMPLES - SALVADOR\n"
            texto_para_salvar += f"PERÍODO: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}\n"
            texto_para_salvar += f"GERADO EM: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
            texto_para_salvar += "="*60 + "\n\n"

            progresso = st.progress(0)
            total = len(lista_diarios)
            
            for i, diario in enumerate(lista_diarios):
                data_pub = diario["date"]
                url_txt = diario["txt_url"]
                
                try:
                    texto_completo = requests.get(url_txt).text
                    
                    # 1. Identifica o número da Edição do DOM
                    padrao_numero = r"N\s*[º°oO]\s*(\d{1,3}(?:\.\d{3})?|\d+)"
                    busca_numero = re.search(padrao_numero, texto_completo[:1000], re.IGNORECASE)
                    numero_dom = busca_numero.group(1) if busca_numero else "Não identificado"
                    
                    # 2. Recorta apenas o bloco de Decretos Simples
                    # Parada inteligente: busca palavras em MAIÚSCULO no início da linha
                    parada = r"\n\s*(?:SECRETARIA|GABINETE|PROCURADORIA|CONTROLADORIA|SUPERINTENDÊNCIA|FUNDAÇÃO|LICITAÇÕES|CONSELHO)\b"
                    padrao = rf"DECRETOS SIMPLES(.*?)({parada})"
                    
                    blocos = re.findall(padrao, texto_completo, re.DOTALL)
                    
                    if blocos:
                        maior_bloco = max(blocos, key=lambda x: len(x[0]))
                        conteudo = maior_bloco[0].strip()
                        
                        # --- FORMATAÇÃO COM A BARRA VERMELHA (OPÇÃO 1) ---
                        texto_para_salvar += "🟥"*30 + "\n"
                        texto_para_salvar += f"📅 DATA: {data_pub} | EDIÇÃO Nº: {numero_dom}\n"
                        texto_para_salvar += "🟥"*30 + "\n\n"
                        
                        texto_para_salvar += conteudo + "\n\n"
                        texto_para_salvar += "\n\n\n" # Espaço extra entre diários
                except:
                    pass 
                
                progresso.progress((i + 1) / total)

            st.success(f"✅ Relatório gerado com sucesso! Encontrados {total} diários.")
            
            # 4. BOTÃO DE DOWNLOAD
            nome_arquivo = f"Decretos_Salvador_{str_inicio}_a_{str_fim}.txt"
            st.download_button(
                label="📥 Baixar Arquivo .TXT",
                data=texto_para_salvar,
                file_name=nome_arquivo,
                mime="text/plain"
            )

        else:
            st.warning("Nenhum diário encontrado para este período.")
