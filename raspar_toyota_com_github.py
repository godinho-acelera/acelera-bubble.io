# -*- coding: utf-8 -*-
"""Scraper Toyota: coleta versões/cores, recorta sprites e publica imagens/CSV no GitHub.

Requisitos:
    pip install requests beautifulsoup4 Pillow
    Git instalado e autenticado no Windows (Git Credential Manager recomendado).

O repositório precisa ser público para que as URLs raw das imagens sejam acessíveis
sem autenticação pelo Bubble.io.
"""

import csv
import hashlib
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURAÇÕES
# ============================================================

BASE_URL = "https://www.toyota.com.br"
GITHUB_REPO_URL = "https://github.com/godinho-acelera/acelera-bubble.io.git"
GITHUB_OWNER = "godinho-acelera"
GITHUB_REPO = "acelera-bubble.io"
GITHUB_BRANCH = "main"

# O repositório será clonado em uma pasta ao lado deste script.
PASTA_BASE = Path(__file__).resolve().parent
PASTA_REPOSITORIO = PASTA_BASE / GITHUB_REPO
PASTA_IMAGENS_REPO = PASTA_REPOSITORIO / "imagens" / "toyota"
ARQUIVO_CSV_LOCAL = PASTA_BASE / "toyota_modelos.csv"
ARQUIVO_LOG = PASTA_BASE / "raspar_toyota.log"
ARQUIVO_CSV_REPO = PASTA_REPOSITORIO / "toyota_modelos.csv"

# O sprite que você verificou tem 18.166 x 316 px e 31 quadros de 586 x 316 px.
SPRITE_QUADROS_HORIZONTAL = 31

TIMEOUT = 35
PAUSA_ENTRE_PAGINAS = 1.0
PAUSA_ENTRE_IMAGENS = 0.15

COLUNAS = [
    "modelo",
    "versao",
    "codigo_versao",
    "ano_modelo",
    "cor",
    "codigo_cor",
    "rgb_cor",
    "preco",
    "url_imagem",
]

PAGINAS_MODELOS = [
    "/modelos/corolla",
    "/modelos/yaris-cross",
    "/modelos/corolla-cross",
    "/modelos/sw4",
    "/modelos/rav4",
    "/modelos/bz4x",
    "/modelos/corolla-hybrid",
    "/modelos/corolla-cross-hybrid",
    "/modelos/yaris-cross-hybrid",
    "/modelos/hilux-cabine-dupla",
    "/modelos/hilux-cabine-simples",
    "/modelos/hiace",
    "/modelos/hiace-furgao",
    "/modelos/gr-corolla",
    "/modelos/corolla-gr-sport",
    "/modelos/corolla-cross-gr-sport",
    "/modelos/gr-yaris",
]

# URLs públicas das imagens depois de publicadas no branch main.
GITHUB_RAW_BASE = (
    f"https://raw.githubusercontent.com/{GITHUB_OWNER}/"
    f"{GITHUB_REPO}/{GITHUB_BRANCH}/imagens/toyota"
)


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(ARQUIVO_LOG, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)


# ============================================================
# HTTP
# ============================================================

def criar_sessao():
    sessao = requests.Session()
    tentativas = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adaptador = HTTPAdapter(max_retries=tentativas)
    sessao.mount("https://", adaptador)
    sessao.mount("http://", adaptador)
    sessao.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    return sessao


def texto(valor):
    return "" if valor is None else str(valor).strip()


def extrair_json_next_data(html):
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        raise ValueError("O HTML não contém o script __NEXT_DATA__.")
    conteudo = script.string or script.get_text()
    if not conteudo.strip():
        raise ValueError("O script __NEXT_DATA__ está vazio.")
    return json.loads(conteudo)


def encontrar_listas_de_versoes(dados):
    listas = []
    ids_listas = set()

    def percorrer(objeto):
        if isinstance(objeto, dict):
            for chave, valor in objeto.items():
                if chave == "carVersionsData" and isinstance(valor, list):
                    identificador = id(valor)
                    if identificador not in ids_listas:
                        ids_listas.add(identificador)
                        listas.append(valor)
                else:
                    percorrer(valor)
        elif isinstance(objeto, list):
            for item in objeto:
                percorrer(item)

    percorrer(dados)
    return listas


# ============================================================
# FONTE DA IMAGEM POR COR
# ============================================================

def url_valida_toyota(url):
    return url.startswith((
        "https://media.toyota.com.br/",
        "http://media.toyota.com.br/",
    ))


def extrair_fonte_imagem(cor):
    """Retorna (URL, tipo). Sprites são priorizados e serão recortados."""
    for campo in ("spriteDesktop", "spriteMobile"):
        url = texto(cor.get(campo))
        if url_valida_toyota(url):
            return url, "sprite"

    # As miniaturas só são usadas se não houver sprite para essa cor.
    for campo in ("thumbHoverImages", "thumbHoverMobImages"):
        imagens = cor.get(campo)
        if not isinstance(imagens, list):
            continue
        for imagem in imagens:
            if isinstance(imagem, str) and url_valida_toyota(imagem):
                return imagem, "estatica"
            if isinstance(imagem, dict):
                url = texto(imagem.get("url"))
                if url_valida_toyota(url):
                    return url, "estatica"
    return "", ""


# ============================================================
# SCRAPING
# ============================================================

def extrair_registros(dados, pagina_url):
    registros = []
    listas_versoes = encontrar_listas_de_versoes(dados)
    if not listas_versoes:
        logging.warning("Nenhuma carVersionsData encontrada em %s", pagina_url)
        return registros

    for lista_versoes in listas_versoes:
        for versao in lista_versoes:
            if not isinstance(versao, dict):
                continue
            modelo = texto(versao.get("model"))
            nome_versao = texto(versao.get("name"))
            codigo_versao = texto(versao.get("code"))
            ano_modelo = versao.get("year")
            cores = versao.get("display", [])
            if not isinstance(cores, list):
                continue

            for cor in cores:
                if not isinstance(cor, dict):
                    continue
                fonte_url, tipo_fonte = extrair_fonte_imagem(cor)
                registros.append({
                    "modelo": modelo,
                    "versao": nome_versao,
                    "codigo_versao": codigo_versao,
                    "ano_modelo": ano_modelo,
                    "cor": texto(cor.get("name")),
                    "codigo_cor": texto(cor.get("code")),
                    "rgb_cor": texto(cor.get("rgb")),
                    "preco": cor.get("price"),
                    # Campos internos removidos antes de exportar o CSV.
                    "_fonte_imagem_url": fonte_url,
                    "_tipo_fonte_imagem": tipo_fonte,
                    "url_imagem": "",
                })
    return registros


def coletar_pagina(sessao, caminho):
    url = BASE_URL + caminho
    logging.info("Consultando: %s", url)
    try:
        resposta = sessao.get(url, timeout=TIMEOUT)
        resposta.raise_for_status()
        dados = extrair_json_next_data(resposta.text)
        registros = extrair_registros(dados, url)
        logging.info("Página concluída: %s | registros: %s", caminho, len(registros))
        return registros
    except requests.RequestException as erro:
        logging.error("Erro HTTP ao consultar %s: %s", url, erro)
    except (ValueError, json.JSONDecodeError) as erro:
        logging.error("Erro ao interpretar %s: %s", url, erro)
    except Exception:
        logging.exception("Erro inesperado ao processar %s", url)
    return []


def remover_duplicados(registros):
    resultado = []
    vistos = set()
    for registro in registros:
        chave = (
            registro["modelo"], registro["versao"], registro["codigo_versao"],
            texto(registro["ano_modelo"]), registro["cor"], registro["codigo_cor"],
        )
        if chave in vistos:
            continue
        vistos.add(chave)
        resultado.append(registro)
    return resultado


# ============================================================
# GIT/GITHUB
# ============================================================

def executar_git(argumentos, cwd=None, check=True):
    comando = ["git", *argumentos]
    logging.info("Executando: %s", " ".join(comando))
    resultado = subprocess.run(
        comando,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if resultado.stdout.strip():
        logging.info(resultado.stdout.strip())
    if resultado.stderr.strip():
        logging.info(resultado.stderr.strip())
    if check and resultado.returncode != 0:
        raise RuntimeError(
            f"O comando Git falhou ({resultado.returncode}): {' '.join(comando)}\n"
            f"{resultado.stderr.strip() or resultado.stdout.strip()}"
        )
    return resultado


def preparar_repositorio():
    if shutil.which("git") is None:
        raise RuntimeError(
            "Git não foi encontrado. Instale o Git for Windows e tente novamente."
        )

    if not PASTA_REPOSITORIO.exists():
        PASTA_REPOSITORIO.parent.mkdir(parents=True, exist_ok=True)
        executar_git(["clone", GITHUB_REPO_URL, str(PASTA_REPOSITORIO)])
    elif not (PASTA_REPOSITORIO / ".git").exists():
        if any(PASTA_REPOSITORIO.iterdir()):
            raise RuntimeError(
                f"A pasta {PASTA_REPOSITORIO} existe, mas não é um repositório Git. "
                "Renomeie essa pasta ou configure outra pasta de trabalho."
            )
        executar_git(["clone", GITHUB_REPO_URL, str(PASTA_REPOSITORIO)])

    remoto = executar_git(["remote", "get-url", "origin"], cwd=PASTA_REPOSITORIO)
    url_remota = remoto.stdout.strip().lower().removesuffix(".git")
    url_esperada = GITHUB_REPO_URL.lower().removesuffix(".git")
    if url_remota != url_esperada:
        raise RuntimeError(
            "A pasta local já está ligada a outro repositório Git. "
            f"Encontrado: {remoto.stdout.strip()}"
        )

    # Se o branch main já existe no remoto, atualiza antes de escrever os arquivos.
    refs = executar_git(["ls-remote", "--heads", "origin", GITHUB_BRANCH], cwd=PASTA_REPOSITORIO)
    if refs.stdout.strip():
        branch_atual = executar_git(["branch", "--show-current"], cwd=PASTA_REPOSITORIO).stdout.strip()
        if branch_atual != GITHUB_BRANCH:
            executar_git(["checkout", GITHUB_BRANCH], cwd=PASTA_REPOSITORIO, check=False)
            branch_atual = executar_git(["branch", "--show-current"], cwd=PASTA_REPOSITORIO).stdout.strip()
            if branch_atual != GITHUB_BRANCH:
                executar_git(["checkout", "-b", GITHUB_BRANCH], cwd=PASTA_REPOSITORIO)
        executar_git(["pull", "--rebase", "origin", GITHUB_BRANCH], cwd=PASTA_REPOSITORIO)
    else:
        # Repositório recém-criado e ainda sem commits.
        executar_git(["checkout", "-B", GITHUB_BRANCH], cwd=PASTA_REPOSITORIO)

    PASTA_IMAGENS_REPO.mkdir(parents=True, exist_ok=True)


def nome_arquivo_imagem(url):
    # Hash da URL cria nome estável e evita colisões entre cores/modelos.
    hash_url = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    return f"toyota_{hash_url}.png"


def baixar_bytes(sessao, url):
    resposta = sessao.get(url, timeout=TIMEOUT)
    resposta.raise_for_status()
    if not resposta.content:
        raise ValueError("O servidor retornou um arquivo vazio.")
    return resposta.content


def processar_imagem(sessao, url, tipo, destino):
    """Baixa a fonte; recorta o primeiro quadro de sprites ou conserva a miniatura."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and destino.stat().st_size > 0:
        return True

    conteudo = baixar_bytes(sessao, url)
    temporario = destino.with_suffix(".download")
    temporario.write_bytes(conteudo)
    try:
        with Image.open(temporario) as original:
            imagem = original.convert("RGBA")
            largura, altura = imagem.size
            if tipo == "sprite":
                quantidade = SPRITE_QUADROS_HORIZONTAL
                if largura < quantidade or altura < 1:
                    raise ValueError(f"Dimensões inválidas para sprite: {largura}x{altura}")
                largura_quadro = largura // quantidade
                if largura % quantidade != 0:
                    logging.warning(
                        "A largura do sprite (%s px) não é divisível por %s; "
                        "o primeiro quadro será recortado com %s px. URL: %s",
                        largura, quantidade, largura_quadro, url,
                    )
                # Recorta apenas o primeiro quadro, da extrema esquerda.
                imagem = imagem.crop((0, 0, largura_quadro, altura))
            imagem.save(destino, format="PNG", optimize=True)
    except (UnidentifiedImageError, OSError, ValueError) as erro:
        logging.error("Não foi possível processar a imagem %s: %s", url, erro)
        destino.unlink(missing_ok=True)
        return False
    finally:
        temporario.unlink(missing_ok=True)
    return True


def publicar_imagens(sessao, registros):
    """Gera PNGs locais no repositório e preenche url_imagem com URLs raw do GitHub."""
    fontes_processadas = {}
    fontes_unicas = {}
    for registro in registros:
        url = registro.get("_fonte_imagem_url", "")
        tipo = registro.get("_tipo_fonte_imagem", "")
        if url and tipo:
            fontes_unicas[url] = tipo

    logging.info("Fontes de imagem únicas para processar: %s", len(fontes_unicas))
    for indice, (url, tipo) in enumerate(fontes_unicas.items(), start=1):
        nome = nome_arquivo_imagem(url)
        destino = PASTA_IMAGENS_REPO / nome
        logging.info("Imagem %s/%s: %s", indice, len(fontes_unicas), url)
        try:
            sucesso = processar_imagem(sessao, url, tipo, destino)
            if sucesso:
                fontes_processadas[url] = f"{GITHUB_RAW_BASE}/{nome}"
            else:
                fontes_processadas[url] = ""
        except requests.RequestException as erro:
            logging.error("Falha ao baixar imagem %s: %s", url, erro)
            fontes_processadas[url] = ""
        except Exception:
            logging.exception("Falha ao processar fonte de imagem %s", url)
            fontes_processadas[url] = ""
        time.sleep(PAUSA_ENTRE_IMAGENS)

    for registro in registros:
        fonte = registro.get("_fonte_imagem_url", "")
        registro["url_imagem"] = fontes_processadas.get(fonte, "") if fonte else ""


def exportar_csv(registros, caminho):
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", newline="", encoding="utf-8-sig") as arquivo:
        escritor = csv.DictWriter(
            arquivo,
            fieldnames=COLUNAS,
            delimiter=";",
            extrasaction="ignore",
        )
        escritor.writeheader()
        for registro in registros:
            escritor.writerow({coluna: registro.get(coluna, "") for coluna in COLUNAS})
    logging.info("CSV salvo em: %s", caminho.resolve())


def publicar_repositorio():
    # Apenas imagens Toyota e o CSV são adicionados ao commit.
    executar_git(["add", "toyota_modelos.csv", "imagens/toyota"], cwd=PASTA_REPOSITORIO)
    status = executar_git(["status", "--porcelain"], cwd=PASTA_REPOSITORIO).stdout.strip()
    if status:
        mensagem = f"Atualiza catálogo Toyota e imagens ({time.strftime('%Y-%m-%d %H:%M')})"
        executar_git(["commit", "-m", mensagem], cwd=PASTA_REPOSITORIO)
    else:
        logging.info("Nenhuma alteração nova; verificando se há commits pendentes de envio.")

    # Executa push mesmo sem alterações: permite recuperar automaticamente uma
    # execução anterior em que o commit ocorreu, mas o envio ao GitHub falhou.
    executar_git(["push", "-u", "origin", GITHUB_BRANCH], cwd=PASTA_REPOSITORIO)


def main():
    logging.info("=" * 68)
    logging.info("INÍCIO DO SCRAPER TOYOTA COM PUBLICAÇÃO NO GITHUB")
    logging.info("Repositório: %s", GITHUB_REPO_URL)
    logging.info("=" * 68)

    preparar_repositorio()
    sessao = criar_sessao()
    todos_registros = []

    for indice, caminho in enumerate(PAGINAS_MODELOS, start=1):
        logging.info("Página %s/%s", indice, len(PAGINAS_MODELOS))
        todos_registros.extend(coletar_pagina(sessao, caminho))
        if indice < len(PAGINAS_MODELOS):
            time.sleep(PAUSA_ENTRE_PAGINAS)

    registros = remover_duplicados(todos_registros)
    if not registros:
        raise RuntimeError(
            "Nenhum registro foi extraído. O CSV não será publicado para evitar "
            "substituir o catálogo por um arquivo vazio."
        )

    publicar_imagens(sessao, registros)

    # O CSV é salvo dentro do repositório para o Bubble/importação e também ao lado do script.
    exportar_csv(registros, ARQUIVO_CSV_REPO)
    publicar_repositorio()
    shutil.copy2(ARQUIVO_CSV_REPO, ARQUIVO_CSV_LOCAL)

    total = len(registros)
    com_imagem = sum(bool(r.get("url_imagem")) for r in registros)
    sem_imagem = total - com_imagem
    csv_url = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/"
        f"{GITHUB_BRANCH}/toyota_modelos.csv"
    )

    logging.info("=" * 68)
    logging.info("PROCESSO CONCLUÍDO")
    logging.info("Registros: %s", total)
    logging.info("Com imagem pública: %s", com_imagem)
    logging.info("Sem imagem: %s", sem_imagem)
    logging.info("CSV local: %s", ARQUIVO_CSV_LOCAL)
    logging.info("CSV no GitHub: %s", csv_url)
    logging.info("Imagens no GitHub: %s", GITHUB_RAW_BASE)
    logging.info("Repositório local: %s", PASTA_REPOSITORIO)
    logging.info("=" * 68)
    print("\nPROCESSO CONCLUÍDO")
    print(f"CSV local: {ARQUIVO_CSV_LOCAL}")
    print(f"CSV público: {csv_url}")
    print(f"Pasta pública de imagens: {GITHUB_RAW_BASE}/")
    print(f"Registros: {total} | com imagem: {com_imagem} | sem imagem: {sem_imagem}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nProcesso interrompido pelo usuário.")
    except Exception as erro:
        logging.exception("O processo não foi concluído: %s", erro)
        print("\nERRO: o processo não foi concluído.")
        print(erro)
        print(
            "Verifique se o Git está instalado, se você está autenticado no GitHub "
            "e se o repositório é público. Consulte raspar_toyota.log para detalhes."
        )
        raise
