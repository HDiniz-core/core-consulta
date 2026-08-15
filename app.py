"""
CoRe — Registo de Consulta Cardio-Renal
Extração automática de dados clínicos via Claude → Google Sheets

Modelo longitudinal:
  Doentes  → 1 linha por doente   (dados basais, chave = N_Processo)
  Visitas  → 1 linha por consulta (clínica, chave = ID_Visita)
  Analises → 1 linha por consulta (laboratório, chave = ID_Visita)
  Eventos  → preenchimento manual
"""

import streamlit as st
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import re
from datetime import datetime, date
from docx import Document
import pdfplumber
import io
import pandas as pd

# ─── PAGE CONFIG ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CoRe — Registo Clínico",
    page_icon="🫀",
    layout="wide"
)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
SHEET_DOENTES  = "Doentes"
SHEET_VISITAS  = "Visitas"
SHEET_ANALISES = "Analises"
SHEET_EVENTOS  = "Eventos"

# Só dados basais — não mudam (ou raramente mudam) de consulta para consulta.
HEADERS_DOENTES = [
    "N_Processo", "Data_Nascimento", "Sexo", "Localidade",
    "Profissao", "Referenciacao",
    # FRCV
    "DM2", "Tabagismo", "HTA", "Dislipidemia", "Obesidade",
    "SAOS", "Sedentarismo", "HxFamiliar_DC",
    # Comorbilidades
    "DAP", "DPOC", "Doenca_hepatica", "HBP", "FA", "Outras_comorbilidades",
    # Etiologias (basais)
    "IC_Etiologia", "DRC_Etiologia",
    # Metadados longitudinais
    "Data_primeira_consulta", "Data_ultima_consulta", "N_Consultas", "Data_registo",
]

# Tudo o que varia no tempo: estadiamento, congestão, POCUS, sintomas,
# exame objectivo e medicação. Uma linha por consulta.
HEADERS_VISITAS = [
    "ID_Visita", "N_Processo", "N_Consulta", "Data_consulta", "Idade_na_consulta",
    "Frailty_CFS",
    # IC / DRC (estadiamento no momento da consulta)
    "IC_FE_tipo", "IC_FEVE_atual_pct", "IC_FEVE_trajetoria",
    "DRC_Grau", "DRC_Albuminuria",
    # Congestão + POCUS
    "Fenotipo_congestao",
    "POCUS_FE_pct", "POCUS_EE_ratio", "POCUS_LinhasB_N", "POCUS_VCI_mm",
    # Sintomas
    "NYHA", "CCS", "Ortopneia", "Bendopneia", "Edemas_MI",
    "Claudicacao_intermitente", "Palpitacoes",
    # Exame objectivo
    "Peso_kg", "Altura_m", "IMC", "TA_sist", "TA_diast", "FC", "SpO2",
    "Exame_objetivo_descricao",
    # Medicação (12 classes × 3 colunas)
    "RASi", "RASi_farmaco", "RASi_dose",
    "MRA", "MRA_farmaco", "MRA_dose",
    "iSGLT2", "iSGLT2_farmaco", "iSGLT2_dose",
    "GLP1RA", "GLP1RA_farmaco", "GLP1RA_dose",
    "Estatina", "Estatina_farmaco", "Estatina_dose",
    "Diuretico_ansa", "Diuretico_ansa_farmaco", "Diuretico_ansa_dose",
    "Diuretico_tiazida", "Diuretico_tiazida_farmaco", "Diuretico_tiazida_dose",
    "Acetazolamida", "Acetazolamida_farmaco", "Acetazolamida_dose",
    "BetaBloqueante", "BetaBloqueante_farmaco", "BetaBloqueante_dose",
    "Antiagregante", "Antiagregante_farmaco", "Antiagregante_dose",
    "Anticoagulante", "Anticoagulante_farmaco", "Anticoagulante_dose",
    "Ivabradina", "Ivabradina_farmaco", "Ivabradina_dose",
    "Data_registo",
]

# Só laboratório. Liga-se a Visitas por ID_Visita.
HEADERS_ANALISES = [
    "ID_Visita", "N_Processo", "N_Consulta", "Data_consulta",
    # Função renal
    "Ureia", "Creatinina", "Cistatina_C", "TFGe_CKD_EPI_CrCist",
    "RACu_mg_g", "RPC_mg_g", "Na_urinario",
    # Proteínas / Hepático
    "Albumina", "ALT", "AST", "GGT", "Bilirrubina_total",
    # Eletrólitos / Minerais
    "Na", "K", "Cl", "Ca", "P", "Mg",
    # Endócrino
    "PTH", "Vit_D",
    # Biomarcadores
    "NT_proBNP", "BNP", "CA125",
    # Hemograma (selecionado)
    "Hgb", "Leucocitos", "Plaquetas",
    # Gasimetria (selecionado)
    "HCO3", "Ca_ionizado",
    # Urina
    "Sumario_urina",
    "Data_registo",
]

HEADERS_EVENTOS = [
    "N_Processo", "Data_evento", "Tipo_evento", "Causa_descricao", "Data_registo"
]

MED_LABELS = {
    "rasi":             "RASi (IECA/ARA/ARNi)",
    "mra":              "MRA",
    "isglt2":           "iSGLT2",
    "glp1ra":           "GLP-1RA",
    "estatina":         "Estatina",
    "diuretico_ansa":   "Diurético de ansa",
    "diuretico_tiazida":"Diurético tiazida",
    "acetazolamida":    "Acetazolamida",
    "beta_bloqueante":  "Beta-bloqueante",
    "antiagregante":    "Antiagregante",
    "anticoagulante":   "Anticoagulante",
    "ivabradina":       "Ivabradina",
}

# Condições crónicas: uma vez documentadas, não são apagadas por uma nota
# posterior que simplesmente não as mencione.
STICKY_TRUE_COLS = {
    "DM2", "Tabagismo", "HTA", "Dislipidemia", "Obesidade", "SAOS",
    "Sedentarismo", "HxFamiliar_DC", "DAP", "DPOC", "Doenca_hepatica", "HBP", "FA",
}

# Colunas mostradas no histórico longitudinal do doente
HIST_VISITA_COLS  = ["N_Consulta", "Data_consulta", "NYHA", "Peso_kg",
                     "TA_sist", "TA_diast", "FC", "Fenotipo_congestao",
                     "IC_FEVE_atual_pct", "DRC_Grau"]
HIST_ANALISE_COLS = ["Creatinina", "TFGe_CKD_EPI_CrCist", "RACu_mg_g",
                     "NT_proBNP", "K", "Na", "Hgb", "Albumina"]


class SchemaMismatch(Exception):
    """A folha existe mas tem cabeçalhos diferentes dos esperados."""
    def __init__(self, sheet_name: str, found: list, expected: list):
        self.sheet_name = sheet_name
        self.found = found
        self.expected = expected
        super().__init__(sheet_name)


# ─── AUTENTICAÇÃO SIMPLES ─────────────────────────────────────────────────────
def check_password() -> bool:
    """Verifica palavra-passe simples. Devolve True se autenticado."""
    if st.session_state.get("authenticated"):
        return True
    st.title("🫀 CoRe — Registo Cardio-Renal")
    pwd = st.text_input("Palavra-passe de acesso", type="password")
    if st.button("Entrar"):
        if pwd == st.secrets.get("app_password", ""):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Palavra-passe incorrecta.")
    return False

# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────
@st.cache_resource
def get_gspread_client():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )
    return gspread.authorize(creds)

def get_spreadsheet():
    client = get_gspread_client()
    return client.open_by_key(st.secrets["spreadsheet_id"])

def ensure_sheet(spreadsheet, name: str, headers: list):
    """Devolve a folha, criando-a se necessário.

    Se a folha já existir com cabeçalhos diferentes, levanta SchemaMismatch em
    vez de escrever por cima — escrever numa folha com estrutura antiga
    desalinharia todas as colunas.
    """
    try:
        ws = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=2000, cols=len(headers) + 5)
        ws.update(values=[headers], range_name="A1")
        return ws

    current = [c.strip() for c in ws.row_values(1)]
    if not any(current):
        ws.update(values=[headers], range_name="A1")
        return ws
    if current[:len(headers)] != headers:
        raise SchemaMismatch(name, current, headers)
    return ws

def sheet_to_dataframe(ws) -> pd.DataFrame:
    values = ws.get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=values[0] if values else [])
    return pd.DataFrame(values[1:], columns=values[0])

def next_visit_number(ws_visitas, n_processo: str) -> int:
    """Próximo número de consulta para este doente (1 se for a primeira)."""
    df = sheet_to_dataframe(ws_visitas)
    if df.empty or "N_Processo" not in df.columns:
        return 1
    existentes = df[df["N_Processo"].astype(str).str.strip() == n_processo]
    if existentes.empty:
        return 1
    nums = pd.to_numeric(existentes.get("N_Consulta"), errors="coerce").dropna()
    return int(nums.max()) + 1 if len(nums) else len(existentes) + 1

def find_visit_by_date(ws_visitas, n_processo: str, data_consulta: str):
    """Devolve o ID_Visita já registado para este doente nesta data, ou None."""
    if not data_consulta:
        return None
    df = sheet_to_dataframe(ws_visitas)
    if df.empty or "N_Processo" not in df.columns:
        return None
    match = df[
        (df["N_Processo"].astype(str).str.strip() == n_processo)
        & (df["Data_consulta"].astype(str).str.strip() == data_consulta)
    ]
    if match.empty:
        return None
    return match.iloc[0]["ID_Visita"]

def make_visit_id(n_processo: str, n_consulta: int) -> str:
    return f"{n_processo}-V{n_consulta:02d}"

def upsert_doente(ws, n_processo: str, novo: list, data_consulta: str, n_consulta: int):
    """Funde a linha do doente com a existente, sem perder dados basais.

    Um valor novo só substitui o antigo se não estiver vazio, e as condições
    crónicas já documentadas nunca são revertidas.
    """
    col_a = ws.col_values(1)
    idx = {h: i for i, h in enumerate(HEADERS_DOENTES)}
    hoje = datetime.now().isoformat(timespec="seconds")

    if n_processo in col_a:
        row_idx = col_a.index(n_processo) + 1
        antigo = ws.row_values(row_idx)
        antigo += [""] * (len(HEADERS_DOENTES) - len(antigo))

        fundido = []
        for i, header in enumerate(HEADERS_DOENTES):
            velho = (antigo[i] or "").strip()
            recente = (novo[i] or "").strip()
            if header in STICKY_TRUE_COLS and velho == "Sim":
                fundido.append("Sim")
            elif recente:
                fundido.append(recente)
            else:
                fundido.append(velho)

        primeira = (antigo[idx["Data_primeira_consulta"]] or "").strip()
        fundido[idx["Data_primeira_consulta"]] = primeira or data_consulta
        fundido[idx["Data_ultima_consulta"]] = data_consulta
        fundido[idx["N_Consultas"]] = str(n_consulta)
        fundido[idx["Data_registo"]] = hoje

        ws.update(values=[fundido], range_name=f"A{row_idx}")
    else:
        novo[idx["Data_primeira_consulta"]] = data_consulta
        novo[idx["Data_ultima_consulta"]] = data_consulta
        novo[idx["N_Consultas"]] = str(n_consulta)
        novo[idx["Data_registo"]] = hoje
        ws.append_row(novo)

# ─── PARSERS DE FICHEIRO ──────────────────────────────────────────────────────
def parse_docx(file_bytes: bytes) -> str:
    doc = Document(io.BytesIO(file_bytes))
    lines = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            lines.append(t)
    # Incluir texto em tabelas, se existirem
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
            if row_text:
                lines.append(row_text)
    return "\n".join(lines)

def parse_pdf(file_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts).strip()

# ─── PROMPT & EXTRAÇÃO LLM ───────────────────────────────────────────────────
EXTRACTION_PROMPT = """
És um assistente especializado em extração de dados clínicos de consultas de Cardiologia-Nefrologia (síndrome cardiorrenal).
Analisa o registo clínico em Português e devolve APENAS um objeto JSON válido com a estrutura indicada abaixo.

REGRAS DE EXTRAÇÃO:
- Booleanos: true ou false (nunca "Sim"/"Não")
- Números: só o valor numérico, sem unidades
- Datas: formato "YYYY-MM-DD"  |  null se ausente
- Strings não encontradas: null
- Sexo: "M" ou "F"
- NYHA: inteiro 1–4  |  CCS: inteiro 0–4  |  Frailty CFS: inteiro 1–9
- IC tipo FE: "FEr" (<40%), "FEp" (≥50%), "FEmr" (40–49%), ou null
- DRC Grau: "G1"/"G2"/"G3a"/"G3b"/"G4"/"G5" ou null
- DRC Albuminúria: "A1" (<30 mg/g), "A2" (30–300 mg/g), "A3" (>300 mg/g) ou null
- Fenótipo congestão: "Tecidular"/"Vascular"/"Misto"/"Ausente" ou null
- Referenciação: "Cardio"/"Nefro"/"Outra"/"Pós-internamento" ou null
- TFGe: valor numérico (preferencialmente CKD-EPI Cr-Cist se disponível)
- RACu/RAC: valor em mg/g como número
- NT-proBNP: valor em pg/mL como número
- Linhas B: número de campos pulmonares com linhas B (ex: "7/8" → 7)
- VCI: diâmetro em mm como número
- FE: percentagem como número (ex: "40%" → 40)

SEPARAÇÃO ENTRE EXAME OBJECTIVO E ANÁLISES — MUITO IMPORTANTE:
- "visita.exame_objetivo.descricao" é o ÚNICO sítio para a descrição narrativa do
  exame objectivo/físico: auscultação cardíaca e pulmonar, pressão venosa jugular,
  edemas, palpação abdominal, pulsos periféricos, estado geral, etc.
  Copia essa descrição de forma resumida mas fiel, em texto corrido.
- "visita.analises.sumario_urina" é EXCLUSIVAMENTE para o resultado do exame
  sumário de urina / tira-teste / sedimento urinário (ex: "proteínas +, sangue ++,
  leucócitos negativo"). NUNCA coloques aqui a descrição do exame objectivo,
  sintomas, nem qualquer outro texto clínico. Se não houver sumário de urina
  no documento, devolve null.
- Os valores numéricos do exame (peso, altura, IMC, TA, FC, SpO2) vão nos campos
  próprios e NÃO precisam de ser repetidos na descrição.

DADOS BASAIS vs. DADOS DA CONSULTA:
- "doente" contém apenas o que não muda: nascimento, sexo, localidade, profissão,
  referenciação, factores de risco, comorbilidades e etiologias da IC e da DRC.
- "visita" contém tudo o que varia no tempo: estadiamento, congestão, POCUS,
  sintomas, exame objectivo, medicação actual e análises.

MEDICAÇÃO:
- "presente" = true se o fármaco constar na medicação actual do doente nesta consulta
- RASi inclui: IECA (lisinopril, ramipril, enalapril, perindopril...), ARA (valsartan, losartan, olmesartan...), ARNi (sacubitril/valsartan = Entresto)
- MRA inclui: espironolactona, finerenona, eplerenona
- iSGLT2 inclui: dapagliflozina, empagliflozina, canagliflozina
- GLP-1RA inclui: semaglutido, liraglutido, dulaglutido, exenatido
- Antiagregante inclui: AAS/ácido acetilsalicílico, clopidogrel, ticagrelor, prasugrel
- Anticoagulante inclui: apixabano, rivaroxabano, dabigatrano, edoxabano, varfarina
- Beta-bloqueante inclui: carvedilol, bisoprolol, nebivolol, metoprolol, atenolol

TEXTO CLÍNICO:
{texto}

Responde EXCLUSIVAMENTE com o JSON abaixo preenchido (sem markdown, sem texto extra):

{
  "doente": {
    "data_nascimento": null,
    "sexo": null,
    "localidade": null,
    "profissao": null,
    "referenciacao": null,
    "frcv": {
      "dm2": null,
      "tabagismo": null,
      "hta": null,
      "dislipidemia": null,
      "obesidade": null,
      "saos": null,
      "sedentarismo": null,
      "hx_familiar_dc": null
    },
    "comorbilidades": {
      "dap": null,
      "dpoc": null,
      "doenca_hepatica": null,
      "hbp": null,
      "fa": null,
      "outras": null
    },
    "ic_etiologia": null,
    "drc_etiologia": null
  },
  "visita": {
    "data_consulta": null,
    "frailty_cfs": null,
    "ic": {
      "tipo_fe": null,
      "feve_atual": null,
      "feve_trajetoria": null
    },
    "drc": {
      "grau": null,
      "albuminuria": null
    },
    "fenotipo_congestao": null,
    "pocus": {
      "fe_pct": null,
      "ee_ratio": null,
      "linhas_b_n": null,
      "vci_mm": null
    },
    "sintomas": {
      "nyha": null,
      "ccs": null,
      "ortopneia": null,
      "bendopneia": null,
      "edemas_mi": null,
      "claudicacao_intermitente": null,
      "palpitacoes": null
    },
    "exame_objetivo": {
      "peso_kg": null,
      "altura_m": null,
      "imc": null,
      "ta_sist": null,
      "ta_diast": null,
      "fc": null,
      "spo2": null,
      "descricao": null
    },
    "medicacao": {
      "rasi":              {"presente": null, "farmaco": null, "dose": null},
      "mra":               {"presente": null, "farmaco": null, "dose": null},
      "isglt2":            {"presente": null, "farmaco": null, "dose": null},
      "glp1ra":            {"presente": null, "farmaco": null, "dose": null},
      "estatina":          {"presente": null, "farmaco": null, "dose": null},
      "diuretico_ansa":    {"presente": null, "farmaco": null, "dose": null},
      "diuretico_tiazida": {"presente": null, "farmaco": null, "dose": null},
      "acetazolamida":     {"presente": null, "farmaco": null, "dose": null},
      "beta_bloqueante":   {"presente": null, "farmaco": null, "dose": null},
      "antiagregante":     {"presente": null, "farmaco": null, "dose": null},
      "anticoagulante":    {"presente": null, "farmaco": null, "dose": null},
      "ivabradina":        {"presente": null, "farmaco": null, "dose": null}
    },
    "analises": {
      "ureia": null,
      "creatinina": null,
      "cistatina_c": null,
      "tfge_ckd_epi_crcist": null,
      "racu": null,
      "rpc": null,
      "na_urinario": null,
      "albumina": null,
      "alt": null,
      "ast": null,
      "ggt": null,
      "bilirrubina_total": null,
      "na": null,
      "k": null,
      "cl": null,
      "ca": null,
      "p": null,
      "mg": null,
      "pth": null,
      "vit_d": null,
      "nt_probnp": null,
      "bnp": null,
      "ca125": null,
      "hgb": null,
      "leucocitos": null,
      "plaquetas": null,
      "hco3": null,
      "ca_ionizado": null,
      "sumario_urina": null
    }
  }
}
"""

def get_api_key() -> str:
    """Aceita o nome novo ou o antigo, para não partir os secrets já configurados."""
    return st.secrets.get("anthropic_api_key") or st.secrets["gemini_api_key"]

def extract_with_claude(texto: str) -> dict:
    client = anthropic.Anthropic(api_key=get_api_key())
    prompt = EXTRACTION_PROMPT.replace("{texto}", texto)
    message = client.messages.create(
        model="claude-sonnet-5",
        # O Sonnet 5 raciocina por omissão, e o max_tokens limita raciocínio +
        # resposta em conjunto — 8192 (o valor do Sonnet 4.5) arriscava truncar
        # o JSON a meio. O effort "medium" chega para extração estruturada;
        # sobe para "high" se notares campos a ficar por preencher.
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        messages=[{"role": "user", "content": prompt}]
    )

    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "A resposta foi cortada por falta de espaço (max_tokens). "
            "O documento é provavelmente demasiado longo — divide-o ou aumenta o limite."
        )

    # Com raciocínio ativo, o primeiro bloco é o pensamento e não o texto:
    # é preciso procurar o bloco de texto em vez de assumir content[0].
    raw = next((b.text for b in message.content if b.type == "text"), "").strip()
    if not raw:
        raise RuntimeError("O Claude não devolveu texto — nada a extrair.")

    # Limpar eventual markdown
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)

# ─── HELPERS DE VALOR ──────────────────────────────────────────────────────────
def sv(val) -> str:
    """safe value → string"""
    if val is None:
        return ""
    if val is True:
        return "Sim"
    if val is False:
        return "Não"
    return str(val)

def calculate_age(dob_str: str, ref_str: str = ""):
    """Idade à data de referência (por omissão, hoje)."""
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d").date()
    except Exception:
        return None
    try:
        ref = datetime.strptime(ref_str, "%Y-%m-%d").date()
    except Exception:
        ref = date.today()
    return ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))

# Sufixos de consulta do esquema manual antigo: "123456-2a", "123456 3ª", "1234562a"
SUFIXO_SEP   = re.compile(r"^(?P<base>.+?)[\s\-_/]+\d{1,2}\s*[aª°º]?$", re.IGNORECASE)
SUFIXO_JUNTO = re.compile(r"^(?P<base>\d{5,})\d\s*[aª°º]$", re.IGNORECASE)

def clean_processo(raw: str) -> str:
    """Normaliza o nº de processo, removendo sufixos de consulta (1a, -2a, _3ª…).

    O sufixo deixou de ser necessário: cada consulta gera a sua própria linha,
    numerada automaticamente. Qualquer remoção é sinalizada ao utilizador.
    """
    s = (raw or "").strip()
    for padrao in (SUFIXO_SEP, SUFIXO_JUNTO):
        m = padrao.match(s)
        if m:
            base = m.group("base").strip(" -_/")
            if base:
                return base
    return s

# ─── CONSTRUTORES DE LINHAS PARA O SHEET ─────────────────────────────────────
def build_doentes_row(n_processo: str, extracted: dict) -> list:
    d    = extracted["doente"]
    frcv = d.get("frcv", {})
    co   = d.get("comorbilidades", {})

    return [
        n_processo,
        sv(d.get("data_nascimento")),
        sv(d.get("sexo")), sv(d.get("localidade")),
        sv(d.get("profissao")), sv(d.get("referenciacao")),
        # FRCV
        sv(frcv.get("dm2")), sv(frcv.get("tabagismo")), sv(frcv.get("hta")),
        sv(frcv.get("dislipidemia")), sv(frcv.get("obesidade")),
        sv(frcv.get("saos")), sv(frcv.get("sedentarismo")), sv(frcv.get("hx_familiar_dc")),
        # Comorbilidades
        sv(co.get("dap")), sv(co.get("dpoc")), sv(co.get("doenca_hepatica")),
        sv(co.get("hbp")), sv(co.get("fa")), sv(co.get("outras")),
        # Etiologias
        sv(d.get("ic_etiologia")), sv(d.get("drc_etiologia")),
        # Metadados (preenchidos em upsert_doente)
        "", "", "", "",
    ]

def build_visitas_row(id_visita: str, n_processo: str, n_consulta: int,
                      extracted: dict) -> list:
    d   = extracted["doente"]
    v   = extracted["visita"]
    ic  = v.get("ic", {})
    drc = v.get("drc", {})
    poc = v.get("pocus", {})
    s   = v.get("sintomas", {})
    eo  = v.get("exame_objetivo", {})
    med = v.get("medicacao", {})

    data_consulta = sv(v.get("data_consulta"))
    idade = calculate_age(d.get("data_nascimento") or "", data_consulta)

    def med3(key):
        m = med.get(key) or {}
        return [sv(m.get("presente")), sv(m.get("farmaco")), sv(m.get("dose"))]

    return [
        id_visita, n_processo, str(n_consulta), data_consulta, sv(idade),
        sv(v.get("frailty_cfs")),
        # IC / DRC
        sv(ic.get("tipo_fe")), sv(ic.get("feve_atual")), sv(ic.get("feve_trajetoria")),
        sv(drc.get("grau")), sv(drc.get("albuminuria")),
        # Congestão + POCUS
        sv(v.get("fenotipo_congestao")),
        sv(poc.get("fe_pct")), sv(poc.get("ee_ratio")),
        sv(poc.get("linhas_b_n")), sv(poc.get("vci_mm")),
        # Sintomas
        sv(s.get("nyha")), sv(s.get("ccs")),
        sv(s.get("ortopneia")), sv(s.get("bendopneia")), sv(s.get("edemas_mi")),
        sv(s.get("claudicacao_intermitente")), sv(s.get("palpitacoes")),
        # Exame objectivo
        sv(eo.get("peso_kg")), sv(eo.get("altura_m")), sv(eo.get("imc")),
        sv(eo.get("ta_sist")), sv(eo.get("ta_diast")), sv(eo.get("fc")), sv(eo.get("spo2")),
        sv(eo.get("descricao")),
        # Medicação (12 classes × 3)
        *med3("rasi"), *med3("mra"), *med3("isglt2"), *med3("glp1ra"),
        *med3("estatina"), *med3("diuretico_ansa"), *med3("diuretico_tiazida"),
        *med3("acetazolamida"), *med3("beta_bloqueante"),
        *med3("antiagregante"), *med3("anticoagulante"), *med3("ivabradina"),
        datetime.now().isoformat(timespec="seconds"),
    ]

def build_analises_row(id_visita: str, n_processo: str, n_consulta: int,
                       extracted: dict) -> list:
    v = extracted["visita"]
    a = v.get("analises", {})
    return [
        id_visita, n_processo, str(n_consulta), sv(v.get("data_consulta")),
        sv(a.get("ureia")), sv(a.get("creatinina")), sv(a.get("cistatina_c")),
        sv(a.get("tfge_ckd_epi_crcist")),
        sv(a.get("racu")), sv(a.get("rpc")), sv(a.get("na_urinario")),
        sv(a.get("albumina")),
        sv(a.get("alt")), sv(a.get("ast")), sv(a.get("ggt")), sv(a.get("bilirrubina_total")),
        sv(a.get("na")), sv(a.get("k")), sv(a.get("cl")), sv(a.get("ca")),
        sv(a.get("p")), sv(a.get("mg")),
        sv(a.get("pth")), sv(a.get("vit_d")),
        sv(a.get("nt_probnp")), sv(a.get("bnp")), sv(a.get("ca125")),
        sv(a.get("hgb")), sv(a.get("leucocitos")), sv(a.get("plaquetas")),
        sv(a.get("hco3")), sv(a.get("ca_ionizado")),
        sv(a.get("sumario_urina")),
        datetime.now().isoformat(timespec="seconds"),
    ]

# ─── COMPONENTES UI ───────────────────────────────────────────────────────────
def render_sidebar():
    with st.sidebar:
        st.header("ℹ️ Instruções")
        st.markdown("""
**1.** Introduz o N° de processo — **sem sufixo** de consulta
**2.** Faz upload da nota de consulta (`.docx`)
**3.** *(Opcional)* Faz upload das análises (`.pdf`)
**4.** Clica **Processar Consulta**
**5.** Revê os dados extraídos
**6.** Clica **Guardar no Google Sheets**

---
📋 **Doentes** → dados basais (1 linha/doente)
🩺 **Visitas** → clínica da consulta (1 linha/consulta)
🧪 **Analises** → laboratório (1 linha/consulta)
🏥 **Eventos** → preenchimento manual

`Visitas` e `Analises` ligam-se por **ID_Visita**.
Para a evolução de um doente, filtra por `N_Processo` e ordena por `Data_consulta`.
        """)
        st.divider()
        sheet_id = st.secrets.get("spreadsheet_id", "")
        if sheet_id:
            url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
            st.link_button("📊 Abrir Google Sheet", url)

def render_schema_error(err: SchemaMismatch):
    st.error(f"A folha **{err.sheet_name}** tem uma estrutura antiga.")
    st.markdown(f"""
Para não desalinhar as colunas, a app não escreve por cima. No Google Sheet:

1. Clica com o botão direito no separador **{err.sheet_name}** → **Renomear**
2. Muda para **{err.sheet_name}_arquivo** (os dados antigos ficam intactos)
3. Volta aqui e guarda outra vez — a folha nova é criada automaticamente
    """)
    with st.expander("Ver diferença de cabeçalhos"):
        st.write("**Encontrado:**")
        st.code(", ".join(err.found) or "(vazio)")
        st.write("**Esperado:**")
        st.code(", ".join(err.expected))

def render_historico(n_processo: str):
    """Mostra as consultas anteriores deste doente."""
    try:
        ss = get_spreadsheet()
        ws_v = ensure_sheet(ss, SHEET_VISITAS, HEADERS_VISITAS)
        ws_a = ensure_sheet(ss, SHEET_ANALISES, HEADERS_ANALISES)
    except SchemaMismatch as e:
        render_schema_error(e)
        return
    except Exception as e:
        st.error(f"Não foi possível ler o Google Sheet: {e}")
        return

    df_v = sheet_to_dataframe(ws_v)
    df_a = sheet_to_dataframe(ws_a)

    if df_v.empty or "N_Processo" not in df_v.columns:
        st.info("Ainda não há consultas registadas.")
        return

    vis = df_v[df_v["N_Processo"].astype(str).str.strip() == n_processo].copy()
    if vis.empty:
        st.info(f"Sem consultas anteriores para o processo **{n_processo}**. Esta será a 1ª.")
        return

    if not df_a.empty and "ID_Visita" in df_a.columns:
        cols_a = [c for c in HIST_ANALISE_COLS if c in df_a.columns]
        vis = vis.merge(
            df_a[["ID_Visita"] + cols_a], on="ID_Visita", how="left"
        )

    vis = vis.sort_values("Data_consulta")
    mostrar = [c for c in HIST_VISITA_COLS + HIST_ANALISE_COLS if c in vis.columns]
    st.caption(f"{len(vis)} consulta(s) registada(s) para o processo {n_processo}")
    st.dataframe(vis[mostrar], use_container_width=True, hide_index=True)

def render_review(extracted: dict):
    """Mostra resumo dos dados extraídos para revisão."""
    d   = extracted["doente"]
    v   = extracted["visita"]
    ic  = v.get("ic", {})
    drc = v.get("drc", {})
    poc = v.get("pocus", {})
    med = v.get("medicacao", {})
    a   = v.get("analises", {})
    s   = v.get("sintomas", {})
    eo  = v.get("exame_objetivo", {})

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("#### 👤 Identificação (basal)")
        st.write(f"**Data nasc.:** {sv(d.get('data_nascimento')) or '—'} &nbsp;|&nbsp; **Sexo:** {sv(d.get('sexo')) or '—'}")
        st.write(f"**Localidade:** {sv(d.get('localidade')) or '—'} &nbsp;|&nbsp; **Profissão:** {sv(d.get('profissao')) or '—'}")
        st.write(f"**Referenciação:** {sv(d.get('referenciacao')) or '—'}")
        st.write(f"**Etiologia IC:** {sv(d.get('ic_etiologia')) or '—'} &nbsp;|&nbsp; **Etiologia DRC:** {sv(d.get('drc_etiologia')) or '—'}")

        st.markdown("#### 🫀 Estado nesta consulta")
        st.write(f"**Data da consulta:** {sv(v.get('data_consulta')) or '—'} &nbsp;|&nbsp; **Frailty CFS:** {sv(v.get('frailty_cfs')) or '—'}")
        st.write(f"**IC:** {sv(ic.get('tipo_fe')) or '—'} | FEVE {sv(ic.get('feve_atual')) or '—'}% | {sv(ic.get('feve_trajetoria')) or '—'}")
        st.write(f"**DRC:** {sv(drc.get('grau')) or '—'} {sv(drc.get('albuminuria')) or '—'}")
        st.write(f"**Congestão:** {sv(v.get('fenotipo_congestao')) or '—'}")

        st.markdown("#### 🔬 POCUS")
        st.write(f"FE **{sv(poc.get('fe_pct')) or '—'}%** | E/E' **{sv(poc.get('ee_ratio')) or '—'}** | Linhas B **{sv(poc.get('linhas_b_n')) or '—'}** campos | VCI **{sv(poc.get('vci_mm')) or '—'} mm**")

        st.markdown("#### 🩺 Sintomas")
        st.write(f"**NYHA:** {sv(s.get('nyha')) or '—'} | **CCS:** {sv(s.get('ccs')) or '—'}")
        st.write(f"**Ortopneia:** {sv(s.get('ortopneia')) or '—'} | **Bendopneia:** {sv(s.get('bendopneia')) or '—'} | **Edemas MI:** {sv(s.get('edemas_mi')) or '—'}")

        st.markdown("#### 🧍 Exame objectivo")
        st.write(f"**Peso:** {sv(eo.get('peso_kg')) or '—'} kg | **IMC:** {sv(eo.get('imc')) or '—'}")
        st.write(f"**TA:** {sv(eo.get('ta_sist')) or '—'}/{sv(eo.get('ta_diast')) or '—'} mmHg | **FC:** {sv(eo.get('fc')) or '—'} bpm | **SpO₂:** {sv(eo.get('spo2')) or '—'}%")
        st.write(f"**Descrição:** {sv(eo.get('descricao')) or '—'}")

    with col2:
        st.markdown("#### 💊 Medicação")
        for key, label in MED_LABELS.items():
            m = med.get(key) or {}
            if m.get("presente") is True:
                farmaco = sv(m.get("farmaco")) or "—"
                dose    = sv(m.get("dose")) or ""
                st.write(f"✅ **{label}:** {farmaco} {dose}".strip())
            elif m.get("presente") is False:
                st.write(f"❌ **{label}**")
            else:
                st.write(f"❓ **{label}:** não identificado")

        st.markdown("#### 🧪 Análises (principais)")
        def lab(label, val, unit=""):
            v_str = sv(val) or "—"
            st.write(f"**{label}:** {v_str}{' ' + unit if v_str != '—' and unit else ''}")

        lab("TFGe (CKD-EPI Cr-Cist)", a.get("tfge_ckd_epi_crcist"), "mL/min")
        lab("Creatinina", a.get("creatinina"), "mg/dL")
        lab("Cistatina C", a.get("cistatina_c"), "mg/L")
        lab("RACu", a.get("racu"), "mg/g")
        lab("NT-proBNP", a.get("nt_probnp"), "pg/mL")
        lab("K", a.get("k"), "mEq/L")
        lab("Na", a.get("na"), "mEq/L")
        lab("Hgb", a.get("hgb"), "g/dL")
        lab("Albumina", a.get("albumina"), "g/dL")
        lab("HCO₃⁻", a.get("hco3"), "mmol/L")
        lab("Sumário de urina", a.get("sumario_urina"))

# ─── GUARDAR ──────────────────────────────────────────────────────────────────
def guardar(n_processo: str, extracted: dict, forcar: bool = False):
    ss = get_spreadsheet()
    ws_d = ensure_sheet(ss, SHEET_DOENTES,  HEADERS_DOENTES)
    ws_v = ensure_sheet(ss, SHEET_VISITAS,  HEADERS_VISITAS)
    ws_a = ensure_sheet(ss, SHEET_ANALISES, HEADERS_ANALISES)
    ensure_sheet(ss, SHEET_EVENTOS, HEADERS_EVENTOS)

    data_consulta = sv(extracted["visita"].get("data_consulta"))

    duplicada = find_visit_by_date(ws_v, n_processo, data_consulta)
    if duplicada and not forcar:
        return {"duplicada": duplicada, "data": data_consulta}

    n_consulta = next_visit_number(ws_v, n_processo)
    id_visita  = make_visit_id(n_processo, n_consulta)

    ws_v.append_row(build_visitas_row(id_visita, n_processo, n_consulta, extracted))
    ws_a.append_row(build_analises_row(id_visita, n_processo, n_consulta, extracted))
    upsert_doente(ws_d, n_processo,
                  build_doentes_row(n_processo, extracted),
                  data_consulta, n_consulta)

    return {"id_visita": id_visita, "n_consulta": n_consulta}

def executar_guardar(n_processo: str, extracted: dict, forcar: bool = False):
    """Corre o guardar e regista o resultado em session_state.

    O resultado tem de sobreviver ao rerun: em Streamlit, um botão dentro do
    bloco de outro botão nunca chega a ser accionado.
    """
    try:
        res = guardar(n_processo, extracted, forcar=forcar)
    except SchemaMismatch as e:
        st.session_state["erro_schema"] = e
        return
    except Exception as e:
        st.session_state["erro_guardar"] = str(e)
        return

    st.session_state["erro_schema"]  = None
    st.session_state["erro_guardar"] = None

    if "duplicada" in res:
        st.session_state["dup_aviso"] = res
    else:
        res["n_processo"] = n_processo
        st.session_state["dup_aviso"]     = None
        st.session_state["guardado"]      = res
        st.session_state["ready_to_save"] = False
        st.session_state.pop("mostrar_historico", None)

def limpar_estado_guardar():
    for k in ("dup_aviso", "erro_schema", "erro_guardar", "guardado"):
        st.session_state[k] = None

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not check_password():
        return

    render_sidebar()

    st.title("🫀 CoRe — Registo de Consulta Cardio-Renal")
    st.caption("Upload da nota de consulta → extração automática por IA → Google Sheets")

    # Inicializar session state
    if "ready_to_save" not in st.session_state:
        st.session_state["ready_to_save"] = False

    # Confirmação da última gravação (o bloco de revisão já desapareceu)
    guardado = st.session_state.get("guardado")
    if guardado:
        st.success(
            f"✅ Consulta **{guardado['n_consulta']}ª** do processo "
            f"**{guardado['n_processo']}** guardada (`{guardado['id_visita']}`)."
        )
        st.session_state["guardado"] = None

    # ── INPUTS ────────────────────────────────────────────────────────────────
    n_processo_raw = st.text_input(
        "N° de Processo *",
        placeholder="Ex: 123456",
        help="Só o número do processo. Não acrescentes 1a/2a/3a — "
             "cada consulta passa a ser uma linha própria, numerada automaticamente."
    )
    n_processo = clean_processo(n_processo_raw)

    if n_processo_raw.strip() and n_processo != n_processo_raw.strip():
        st.warning(
            f"Removi o sufixo de consulta: vou registar como **{n_processo}**. "
            "O número da consulta é agora atribuído automaticamente."
        )

    if n_processo:
        if st.button("📈 Ver consultas anteriores"):
            st.session_state["mostrar_historico"] = n_processo
        if st.session_state.get("mostrar_historico") == n_processo:
            render_historico(n_processo)

    col_up1, col_up2 = st.columns(2)
    with col_up1:
        docx_file = st.file_uploader(
            "📋 Nota de consulta (.docx) *", type=["docx"]
        )
    with col_up2:
        pdf_file = st.file_uploader(
            "🧪 Análises laboratoriais (.pdf) — opcional", type=["pdf"]
        )

    # ── PROCESSAR ─────────────────────────────────────────────────────────────
    can_process = bool(n_processo and docx_file)
    if st.button("⚡ Processar Consulta", type="primary", disabled=not can_process):
        texto_total = ""

        with st.spinner("A extrair texto dos ficheiros…"):
            try:
                texto_total += parse_docx(docx_file.read())
            except Exception as e:
                st.error(f"Erro a ler o .docx: {e}")
                return
            if pdf_file:
                try:
                    texto_total += "\n\n=== ANÁLISES LABORATORIAIS ===\n"
                    texto_total += parse_pdf(pdf_file.read())
                except Exception as e:
                    st.warning(f"Não foi possível ler o PDF das análises: {e}")

        with st.spinner("A enviar para o Claude e a extrair dados estruturados…"):
            try:
                extracted = extract_with_claude(texto_total)
                st.session_state["extracted"]     = extracted
                st.session_state["n_processo"]    = n_processo
                st.session_state["ready_to_save"] = True
                limpar_estado_guardar()
                st.success("✅ Extração concluída! Revê os dados abaixo antes de guardar.")
            except json.JSONDecodeError as e:
                st.error(f"O Claude devolveu uma resposta que não é JSON válido: {e}")
                return
            except Exception as e:
                st.error(f"Erro na extração: {e}")
                return

    # ── REVISÃO & GUARDAR ────────────────────────────────────────────────────
    if st.session_state.get("ready_to_save") and "extracted" in st.session_state:
        extracted  = st.session_state["extracted"]
        n_processo = st.session_state["n_processo"]

        st.divider()
        st.subheader("📋 Revisão dos dados extraídos")
        st.caption("Verifica antes de guardar. Podes corrigir directamente no Google Sheet após guardar.")
        render_review(extracted)

        # JSON bruto (debug)
        with st.expander("🔍 Ver JSON completo (debug)"):
            st.json(extracted)

        st.divider()
        col_ok, col_cancel, _ = st.columns([1, 1, 4])

        with col_ok:
            if st.button("💾 Guardar no Google Sheets", type="primary"):
                with st.spinner("A guardar…"):
                    executar_guardar(n_processo, extracted, forcar=False)
                st.rerun()

        with col_cancel:
            if st.button("🗑️ Cancelar"):
                st.session_state["ready_to_save"] = False
                st.session_state.pop("extracted", None)
                limpar_estado_guardar()
                st.rerun()

        # Avisos e confirmações vivem fora do bloco do botão, para
        # sobreviverem ao rerun que o clique provoca.
        if st.session_state.get("erro_schema"):
            render_schema_error(st.session_state["erro_schema"])

        if st.session_state.get("erro_guardar"):
            st.error(f"Erro ao guardar no Google Sheets: {st.session_state['erro_guardar']}")

        dup = st.session_state.get("dup_aviso")
        if dup:
            st.warning(
                f"Já existe uma consulta registada para o processo "
                f"**{n_processo}** na data **{dup['data']}** "
                f"(`{dup['duplicada']}`). Confirma se não é um upload repetido."
            )
            if st.button("➕ Guardar mesmo assim como nova consulta"):
                with st.spinner("A guardar…"):
                    executar_guardar(n_processo, extracted, forcar=True)
                st.rerun()


if __name__ == "__main__":
    main()
