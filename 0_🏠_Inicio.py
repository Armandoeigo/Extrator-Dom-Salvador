import streamlit as st

st.set_page_config(page_title="Painel do Extrator", page_icon="🏛️")

st.title("🏛️ Bem-vindo ao Painel do Extrator")
st.write("Prefeitura Municipal de Salvador")
st.markdown("---")
st.write("👈 **Selecione uma das ferramentas no menu lateral esquerdo para começar:**")
st.write("- 📄 **Relatórios em Texto:** Gera um arquivo `.txt` com os Decretos Simples "
         "de cada diário do período, em ordem cronológica. Não usa Inteligência "
         "Artificial e não pede API Key.")
st.write("- 📊 **Planilha Excel:** Gera uma planilha `.xlsx` com os atos de pessoal "
         "(nomeações, exonerações, designações, vacâncias), trazendo nome, matrícula, "
         "cargo e secretaria. Usa Inteligência Artificial e exige uma API Key do Gemini.")

st.markdown("---")
st.caption(
    "As duas ferramentas leem o Diário Oficial do Município direto do site da "
    "Prefeitura, com a API do Querido Diário como fonte alternativa. A base tem "
    "texto confiável a partir de 2013 — antes disso o DOM era digitalizado."
)
