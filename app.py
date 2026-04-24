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

data_minima = date(2001, 1, 1) # Bloqueia antes de 2001
data_maxima = date.today()     # Trava no dia de hoje (não permite futuro)

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
    
    # Mensagem de carregamento com data no padrão brasileiro
    with st.spinner(f"Buscando diários de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}..."):
        
        url_api = "https://api.queridodiario.ok.org.br/api/gazettes/"
        
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
                st.error(f"Ocorreu um erro ao conectar com o sistema: {e}")
                break

        # ==========================================
        # 3. PROCESSAMENTO E ORDENAÇÃO
        # ==========================================
        if len(lista_diarios) > 0:
            
            # Força a ordem cronológica (do mais antigo pro mais novo)
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
                    
                    # ----------------------------------------------------
                    
                    padrao_numero = r"N\s*[º°oO]\s*(\d{1,3}(?:\.\d{3})?|\d+)"
                    busca_numero = re.search(padrao_numero, texto_completo[:1000], re.IGNORECASE)
                    
                    # Se achar, guarda o número. Se não achar, avisa que não identificou.
                    numero_dom = busca_numero.group(1) if busca_numero else "Não identificado"
                    # ----------------------------------------------------
                    
                    # Recorte dos Decretos Simples
                    parada = r"\n\s*(?:SECRETARIA|GABINETE|PROCURADORIA|CONTROLADORIA|SUPERINTENDÊNCIA|FUNDAÇÃO|LICITAÇÕES|CONSELHO)\b"
                    padrao = rf"DECRETOS SIMPLES(.*?)({parada})"
                    
                    blocos = re.findall(padrao, texto_completo, re.DOTALL)
                    
                    if blocos:
                        maior_bloco = max(blocos, key=lambda x: len(x[0]))
                        conteudo = maior_bloco[0].strip()
                        
                        # Adicionando o NÚMERO DO DOM na formatação do título do bloco
                        texto_para_salvar += f"📅 DATA: {data_pub} | EDIÇÃO Nº: {numero_dom}\n"
                        texto_para_salvar += "-"*60 + "\n"
                        texto_para_salvar += conteudo + "\n"
                        texto_para_salvar += "\n" + "="*60 + "\n\n"
                except:
                    pass 
                
                progresso.progress((i + 1) / total)

            st.success(f"✅ Relatório gerado com sucesso! Encontrados {total} diários.")
            
            nome_arquivo = f"Decretos_{str_inicio}_a_{str_fim}.txt"
            st.download_button(
                label="📥 Baixar Arquivo .TXT",
                data=texto_para_salvar,
                file_name=nome_arquivo,
                mime="text/plain"
            )

        else:
            st.warning("Nenhum diário encontrado para este período.")
