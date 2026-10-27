import streamlit as st
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
st.sidebar.caption("Site oficial do DOM. Se ele não responder, o Querido Diário "
                   "entra automaticamente e o resultado avisa.")
st.sidebar.markdown("---")

# --- INFORMAÇÕES DA FERRAMENTA ---
st.sidebar.write("⚙️ **Modo: Texto Rápido**")
st.sidebar.write("Esta ferramenta utiliza extração estruturada simples. Como ela não utiliza a Inteligência Artificial, **não é necessário inserir nenhuma API Key** nesta página.")
st.sidebar.success("Pronto para uso imediato!")
st.sidebar.markdown("---")
st.sidebar.caption(f"Versão: {dom.VERSAO}")

st.title("🔍 Extrator de Decretos Simples")
st.write("Selecione o período abaixo para buscar os Decretos Simples no Diário Oficial de Salvador.")

data_minima = date(2013, 1, 1)
st.markdown("<span style='color:red'>**Atenção: Base de dados disponível desde 01/2013** "
            "(antes disso o DOM era digitalizado, sem texto confiável)</span>",
            unsafe_allow_html=True)

data_maxima = date.today()

col1, col2 = st.columns(2)
with col1:
    data_inicio = st.date_input("Data de Início", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")
with col2:
    data_fim = st.date_input("Data Final", min_value=data_minima, max_value=data_maxima, format="DD/MM/YYYY")

# Aviso de tempo para períodos longos
dias = (data_fim - data_inicio).days
if dias > 120:
    st.info(f"⏳ Período de {dias} dias. Como referência: 1 mês leva ~12s e 1 ano ~1min15s.")

# ==========================================
# 2. AÇÃO DO BOTÃO
# ==========================================
if st.button("🚀 Buscar e Gerar Relatório"):

    st.warning("🚨 **NÃO MUDE DE PÁGINA!** O robô começou a trabalhar. Se você clicar no menu lateral ou fechar esta aba, a extração será cancelada e o progresso será perdido.")

    str_inicio = data_inicio.strftime("%Y-%m-%d")
    str_fim = data_fim.strftime("%Y-%m-%d")

    # {"data": date, "edicao": str, "conteudo": str}
    achados = []
    sem_bloco = 0
    falha_api = False
    fonte_real = "DOM (site oficial)"   # muda se o módulo precisar usar o Querido Diário

    with st.spinner(f"Buscando diários de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}..."):
        progresso = st.progress(0)
        try:
            # Lê do site do DOM; se ele falhar, o módulo usa o Querido Diário sozinho
            blocos = dom.buscar_decretos(
                data_inicio, data_fim, secao="DECRETOS SIMPLES",
                progresso=lambda feitos, total: progresso.progress(feitos / max(total, 1))
            )
        except Exception as e:
            falha_api = True
            blocos = []
            st.error(f"Não foi possível buscar os diários: {e}")
            st.info("Nem o site do DOM nem o Querido Diário responderam. Tente de novo em alguns minutos.")

        motivo = next((b["falha_dom"] for b in blocos if b.get("falha_dom")), None)
        if motivo:
            fonte_real = "Querido Diário (o site do DOM não respondeu)"
            st.warning("O site do DOM não respondeu; os dados vieram do Querido Diário.")
            st.caption(f"Motivo técnico: {motivo}")

        for b in blocos:
            if b.get("erro"):
                sem_bloco += 1
                continue
            achados.append({
                "data": b["data"],
                "edicao": str(b["edicao"] or "Não identificado"),
                "conteudo": b["texto"],
            })

    # ==========================================
    # 3. MONTAGEM DO RELATÓRIO
    # ==========================================
    if achados:
        achados = sorted(achados, key=lambda x: x["data"])

        texto_para_salvar = "RELATÓRIO DE DECRETOS SIMPLES - SALVADOR\n"
        texto_para_salvar += f"PERÍODO: {data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}\n"
        texto_para_salvar += f"FONTE: {fonte_real}\n"
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
