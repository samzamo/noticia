# -*- coding: utf-8 -*-
"""
Núcleo do Monitor de Notícias (sem interface): coleta, filtro, Telegram
e o Monitor (thread). Usado por monitor_noticias.py (janela) e por
servidor.py (sem interface, para rodar 24h).

Dependências: pip install requests feedparser beautifulsoup4
"""
import calendar
import copy
import html
import json
import os
import queue
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime
from urllib.parse import quote, urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# Arquivos e constantes
# ----------------------------------------------------------------------
if getattr(sys, "frozen", False):          # rodando como .exe (PyInstaller)
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
ENVIADOS_FILE = os.path.join(APP_DIR, "enviados.json")
HEADERS = {"User-Agent": "Mozilla/5.0"}
INTERVALO_MINIMO = 5  # minutos (evita sobrecarregar os sites)

ESTADOS = {
    "Acre": "acre", "Alagoas": "alagoas", "Amapá": "amapa",
    "Amazonas": "amazonas", "Bahia": "bahia", "Ceará": "ceara",
    "Distrito Federal": "distrito-federal", "Espírito Santo": "espirito-santo",
    "Goiás": "goias", "Maranhão": "maranhao", "Mato Grosso": "mato-grosso",
    "Mato Grosso do Sul": "mato-grosso-do-sul", "Minas Gerais": "minas-gerais",
    "Pará": "para", "Paraíba": "paraiba", "Paraná": "parana",
    "Pernambuco": "pernambuco", "Piauí": "piaui",
    "Rio de Janeiro": "rio-de-janeiro",
    "Rio Grande do Norte": "rio-grande-do-norte",
    "Rio Grande do Sul": "rio-grande-do-sul", "Rondônia": "rondonia",
    "Roraima": "roraima", "Santa Catarina": "santa-catarina",
    "São Paulo": "sao-paulo", "Sergipe": "sergipe", "Tocantins": "tocantins",
}

ROTULO_TIPO = {
    "site": "Site (domínio)",
    "rss": "RSS (URL)",
    "google": "Busca Google News",
    "gcmais": "GC+ (scraper)",
}
TIPOS_MANUAIS = {
    "Site (domínio)": "site",
    "RSS (URL)": "rss",
    "Busca Google News": "google",
}

CONFIG_PADRAO = {
    "token": "",
    "chat_id": "",
    "palavras": [],
    "locais": [],
    "intervalo": 20,
    "ignorar_antigas": True,
    "extrair_detalhes": True,
    "fontes": [],
}

AJUDA_TELEGRAM = (
    "1) No Telegram, converse com @BotFather, envie /newbot e siga os passos. "
    "Ele vai entregar o TOKEN do seu bot.\n"
    "2) Abra a conversa com o bot que você criou e envie qualquer mensagem "
    "(ex.: oi). Para receber em um grupo, adicione o bot ao grupo e envie uma "
    "mensagem lá.\n"
    "3) Cole o token abaixo e clique em “Descobrir chat_id”. Depois use "
    "“Enviar teste” para conferir."
)


# ----------------------------------------------------------------------
# Configuração e histórico
# ----------------------------------------------------------------------
def carregar_config():
    cfg = copy.deepcopy(CONFIG_PADRAO)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def salvar_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def carregar_enviados():
    if os.path.exists(ENVIADOS_FILE):
        try:
            with open(ENVIADOS_FILE, "r", encoding="utf-8") as f:
                return dict.fromkeys(json.load(f))
        except Exception:
            pass
    return {}


def salvar_enviados(enviados):
    with open(ENVIADOS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(enviados)[-5000:], f)


# ----------------------------------------------------------------------
# Utilidades de texto
# ----------------------------------------------------------------------
def limpar(txt):
    txt = re.sub(r"<[^>]+>", " ", txt or "")
    return re.sub(r"\s+", " ", txt).strip()


def resumir(txt, limite=250):
    txt = limpar(txt)
    if len(txt) <= limite:
        return txt
    return txt[:limite].rsplit(" ", 1)[0] + "..."


def sem_acento(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def linhas(texto):
    return [l.strip() for l in texto.splitlines() if l.strip()]


def pertinente(texto, palavras, locais):
    """Qualquer palavra-chave E (se houver) qualquer termo de localidade."""
    t = sem_acento(texto)
    if not any(p in t for p in palavras):
        return False
    if locais and not any(l in t for l in locais):
        return False
    return True


def resumo_util(titulo, resumo):
    """Descarta resumos que são só o título repetido (comum no Google News)."""
    if not resumo:
        return ""
    if sem_acento(resumo).startswith(sem_acento(titulo)[:40]):
        return ""
    return resumo


def montar_mensagem(nome, titulo, resumo, data, link, bairro="", nomes=None):
    """Mensagem em HTML do Telegram (negrito nos destaques)."""
    e = lambda x: html.escape(x or "", quote=False)
    partes = [f"📅 {e(data)}", f"📰 {e(titulo)}", f"🏷️ {e(nome)}"]
    if bairro:
        partes.append(f"📍 Bairro: <b>{e(bairro)}</b>")
    if nomes:
        partes.append(f"👤 Preso(s)/suspeito(s): <b>{e('; '.join(nomes))}</b>")
    if resumo:
        partes += ["", e(resumir(resumo))]
    if nomes:
        partes += ["", "⚠️ <i>Nomes extraídos automaticamente: confira na matéria.</i>"]
    partes += ["", f'🔗 <a href="{html.escape(link, quote=True)}">Ler matéria completa</a>']
    return "\n".join(partes)


# ---------------- extração de bairro e nomes (heurística) ----------------
_MAI = "A-ZÁÉÍÓÚÂÊÔÃÕÇÀ"
_MIN = "a-záéíóúâêôãõçà"
_P = rf"[{_MAI}][{_MIN}]+"
_NOME = rf"{_P}(?:\s+(?:(?:d[aeo]s?|e)\s+)?{_P}){{1,4}}"
_ANOS = r"(?:,\s*(?:de\s+|com\s+)?\d{1,2}\s+anos,?)?"
_SUSP = (r"(?:preso|presa|presos|presas|detido|detida|detidos|detidas|suspeito|"
         r"suspeita|suspeitos|suspeitas|autuado|autuada|apreendido|apreendida|"
         r"acusado|acusada|investigado|investigada|indiciado|indiciada|"
         r"conduzido|conduzida|flagrante)")
_VITIMA = (r"(?:vítima|vitima|vítimas|morto|morta|mortos|mortas|morreu|"
           r"assassinad[oa]s?|balead[oa]s?|esfaquead[oa]s?|ferid[oa]s?)")

_BAIRRO_RE = re.compile(
    rf"(?i:\bbairro)\s+(?:d[aeo]s?\s+)?({_P}(?:\s+(?:d[aeo]s?\s+)?{_P}){{0,3}})")
_NOME_APOS = re.compile(
    rf"(?i:\b{_SUSP}\b)(?:\s+(?i:identificad[oa]s?\s+como|chamad[oa]|de\s+nome|"
    rf"conhecid[oa]\s+como))?[,:]?\s+({_NOME})")
_NOME_ANTES = re.compile(
    rf"({_NOME}){_ANOS},?\s+(?:também\s+|então\s+)?(?:(?:foi|foram|era|é)\s+)?"
    rf"(?i:preso|presa|detido|detida|autuado|autuada|apreendido|apreendida|"
    rf"conduzido|conduzida|indiciado|indiciada)\b")
_NOME_IDADE = re.compile(rf"({_NOME}),\s*(?:de\s+|com\s+)?\d{{1,2}}\s+anos")

_BLOQ = {
    "policia", "federal", "civil", "militar", "estadual", "ministerio",
    "publico", "tribunal", "justica", "secretaria", "delegacia", "operacao",
    "governo", "prefeitura", "corpo", "bombeiros", "batalhao", "ceara",
    "fortaleza", "juazeiro", "crato", "sobral", "caucaia", "maracanau",
    "brasil", "nordeste", "rodovia", "avenida", "rua", "bairro", "presidio",
    "unidade", "prisional", "segundo", "ontem", "hoje",
}


def _nome_valido(nome):
    return not any(sem_acento(t) in _BLOQ for t in nome.split())


def extrair_bairro(texto):
    m = _BAIRRO_RE.search(texto or "")
    return m.group(1).strip() if m else ""


def extrair_nomes(texto, maximo=5):
    """Nomes ligados a palavras como preso/suspeito/detido. É uma heurística:
    pode errar, por isso a mensagem avisa para conferir na matéria."""
    achados = []
    for frase in re.split(r"(?<=[.!?])\s+", texto or ""):
        if not re.search(rf"(?i:\b{_SUSP}\b)", frase):
            continue
        candidatos = _NOME_APOS.findall(frase) + _NOME_ANTES.findall(frase)
        if not re.search(rf"(?i:\b{_VITIMA}\b)", frase):
            candidatos += _NOME_IDADE.findall(frase)
        for c in candidatos:
            c = c.strip()
            if _nome_valido(c) and c not in achados:
                achados.append(c)
    return achados[:maximo]


def texto_completo(link, limite=8000):
    """Texto dos parágrafos da matéria (vazio se não for possível ler)."""
    try:
        r = requests.get(link, headers=HEADERS, timeout=20)
        if "google." in urlparse(r.url).netloc:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        raiz = soup.find("article") or soup
        paragrafos = [p.get_text(" ", strip=True) for p in raiz.find_all("p")]
        return " ".join(paragrafos)[:limite]
    except Exception:
        return ""


def formatar_data(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if t:
        try:
            return datetime.fromtimestamp(calendar.timegm(t)).strftime("%d/%m/%Y %H:%M")
        except Exception:
            pass
    return ""


# ----------------------------------------------------------------------
# Coleta de notícias
# ----------------------------------------------------------------------
def google_news_url(consulta):
    q = quote(f"{consulta} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def baixar_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    saida = []
    for e in feedparser.parse(r.content).entries:
        saida.append({
            "link": e.get("link"),
            "title": limpar(e.get("title")),
            "summary": limpar(e.get("summary")),
            "published": formatar_data(e),
        })
    return saida


def limpar_dominio(valor):
    v = valor.strip().replace("https://", "").replace("http://", "")
    return v.split("/")[0]


def descobrir_rss(dominio):
    try:
        r = requests.get(f"https://{dominio}", headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
        urls = []
        for t in soup.find_all("link", type=re.compile(r"rss|atom", re.I)):
            href = t.get("href")
            if href and "comment" not in href.lower():
                urls.append(urljoin(r.url, href))
        return urls
    except Exception:
        return []


GC_EDITORIAS = ["jornalismo", "jornalismo-policia", "jornalismo-ceara",
                "jornalismo-fortaleza", "jornalismo-politica"]
GC_PADRAO = re.compile(r"^/noticias/\d{4}/\d{2}/\d{2}/[\w\-]+$")


def gcmais_entradas():
    vistos, entradas = set(), []
    for ed in GC_EDITORIAS:
        try:
            r = requests.get(f"https://gcmais.com.br/editoria/{ed}",
                             headers=HEADERS, timeout=20)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            continue
        for a in soup.find_all("a", href=True):
            caminho = urlparse(a["href"]).path
            if GC_PADRAO.match(caminho):
                link = "https://gcmais.com.br" + caminho
                if link not in vistos:
                    vistos.add(link)
                    slug = caminho.rsplit("/", 1)[-1].replace("-", " ")
                    entradas.append({"link": link, "title": slug, "summary": "",
                                     "published": "", "detalhar": True})
    return entradas


def detalhar_materia(link):
    """Lê título, descrição e data nas meta tags da matéria."""
    try:
        r = requests.get(link, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return "", "", ""

    def meta(nome):
        t = (soup.find("meta", attrs={"property": nome})
             or soup.find("meta", attrs={"name": nome}))
        return t["content"].strip() if t and t.get("content") else ""

    data = meta("article:published_time")
    try:
        data = datetime.fromisoformat(data.replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")
    except Exception:
        data = ""
    return meta("og:title"), meta("og:description"), data


def entradas_da_fonte(f):
    tipo, valor = f["tipo"], f["valor"]
    if tipo == "rss":
        return baixar_feed(valor)
    if tipo == "google":
        return baixar_feed(google_news_url(valor))
    if tipo == "gcmais":
        return gcmais_entradas()
    if tipo == "site":
        dominio = limpar_dominio(valor)
        candidatos = ([f["_rss"]] if f.get("_rss") else []) + descobrir_rss(dominio)
        for url in candidatos:
            try:
                ent = baixar_feed(url)
                if ent:
                    f["_rss"] = url  # guarda no cache (apenas em memória)
                    return ent
            except Exception:
                continue
        return baixar_feed(google_news_url(f"site:{dominio}"))
    raise ValueError(f"Tipo de fonte desconhecido: {tipo}")


def fontes_da_regiao(estado, cidade=""):
    fontes = []
    if estado == "Ceará":
        fontes += [
            {"nome": "Diário do Nordeste", "tipo": "site",
             "valor": "diariodonordeste.verdesmares.com.br"},
            {"nome": "O Povo", "tipo": "site", "valor": "opovo.com.br"},
            {"nome": "GC+", "tipo": "gcmais", "valor": "gcmais.com.br"},
        ]
    slug = ESTADOS.get(estado)
    if slug:
        fontes.append({"nome": f"G1 {estado}", "tipo": "rss",
                       "valor": f"https://g1.globo.com/rss/g1/{slug}/"})
    if cidade.strip():
        fontes.append({"nome": f"Google News: {cidade.strip()}",
                       "tipo": "google", "valor": cidade.strip()})
    for f in fontes:
        f["ativo"] = True
    return fontes


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def enviar_telegram(token, chat_id, msg):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={
            "chat_id": chat_id,
            "text": msg[:4096],
            "parse_mode": "HTML",
            # sem a prévia (foto/cartão) do link abaixo da mensagem
            "link_preview_options": json.dumps({"is_disabled": True}),
            "disable_web_page_preview": "true",
        },
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram respondeu: {r.text[:200]}")


def descobrir_chat_id(token):
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    dados = r.json()
    if not dados.get("ok"):
        raise RuntimeError(dados.get("description", "token inválido"))
    for u in reversed(dados.get("result", [])):
        for k in ("message", "channel_post", "edited_message"):
            if k in u:
                return str(u[k]["chat"]["id"])
    return None


# ----------------------------------------------------------------------
# Motor de monitoramento (thread)
# ----------------------------------------------------------------------
class Monitor(threading.Thread):
    def __init__(self, cfg, log):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.log = log
        self.parar = threading.Event()
        self.agora = threading.Event()

    def parar_agora(self):
        self.parar.set()
        self.agora.set()

    def verificar_agora(self):
        self.agora.set()

    def run(self):
        self.log("▶ Monitoramento iniciado.")
        while not self.parar.is_set():
            try:
                self.ciclo()
            except Exception as ex:
                self.log(f"Erro inesperado: {ex}")
            self.agora.wait(self.cfg["intervalo"] * 60)
            self.agora.clear()
        self.log("⏹ Monitoramento parado.")

    def ciclo(self):
        cfg = self.cfg
        baseline = (not os.path.exists(ENVIADOS_FILE)) and cfg.get("ignorar_antigas", True)
        enviados = carregar_enviados()
        palavras = [sem_acento(p) for p in cfg["palavras"]]
        locais = [sem_acento(l) for l in cfg["locais"]]
        total = 0
        falhas = 0
        self.log("Verificando fontes...")
        if baseline:
            self.log("Primeira execução: as matérias atuais serão marcadas como "
                     "já vistas, sem envio.")
        try:
            for f in cfg["fontes"]:
                if self.parar.is_set():
                    break
                if not f.get("ativo", True):
                    continue
                try:
                    entradas = entradas_da_fonte(f)
                except Exception as ex:
                    self.log(f"[{f['nome']}] falha ao ler: {ex}")
                    falhas += 1
                    continue
                for e in entradas:
                    if self.parar.is_set():
                        break
                    link = e.get("link")
                    if not link or link in enviados:
                        continue
                    if baseline:
                        enviados[link] = 1
                        continue
                    titulo, resumo, data = e["title"], e["summary"], e["published"]
                    if e.get("detalhar"):
                        t, d, dt = detalhar_materia(link)
                        titulo, resumo, data = t or titulo, d or resumo, dt or data
                    if pertinente(f"{titulo} {resumo}", palavras, locais):
                        data = data or datetime.now().strftime("%d/%m/%Y %H:%M")
                        bairro, nomes = "", []
                        if cfg.get("extrair_detalhes", True):
                            base = f"{titulo}. {resumo}. {texto_completo(link)}"
                            bairro = extrair_bairro(base)
                            nomes = extrair_nomes(base)
                        msg = montar_mensagem(f["nome"], titulo,
                                              resumo_util(titulo, resumo), data, link,
                                              bairro, nomes)
                        enviar_telegram(cfg["token"], cfg["chat_id"], msg)
                        total += 1
                        self.log(f"✉ [{f['nome']}] {titulo}")
                        time.sleep(1)
                    enviados[link] = 1
        except Exception as ex:
            self.log(f"Interrompido: {ex}")
        finally:
            if baseline and (self.parar.is_set() or falhas):
                if falhas:
                    self.log("Primeira execução incompleta (alguma fonte falhou): "
                             "será refeita no próximo ciclo, sem enviar nada antigo.")
            else:
                salvar_enviados(enviados)
        self.log(f"Ciclo concluído: {total} alerta(s) enviado(s).")
