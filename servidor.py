# -*- coding: utf-8 -*-
"""
Monitor de Notícias -> Telegram (SEM interface, para servidor 24h)

Uso:
    python servidor.py             # fica rodando e verifica a cada N minutos
    python servidor.py --uma-vez   # faz UMA verificação e encerra (GitHub Actions/cron)

Lê o config.json (gerado pela versão com janela, na aba "Salvar") que
estiver na mesma pasta e roda até receber Ctrl+C ou SIGTERM.
Dependências: pip install requests feedparser beautifulsoup4
"""
import argparse
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler

from nucleo import APP_DIR, INTERVALO_MINIMO, Monitor, carregar_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uma-vez", action="store_true",
                    help="faz uma única verificação e encerra")
    args = ap.parse_args()

    cfg = carregar_config()

    problemas = []
    if not cfg["token"] or not cfg["chat_id"]:
        problemas.append("token/chat_id do Telegram não configurados")
    if not cfg["palavras"]:
        problemas.append("nenhuma palavra-chave configurada")
    if not any(f.get("ativo", True) for f in cfg["fontes"]):
        problemas.append("nenhuma fonte ativa")
    if problemas:
        print("Configuração incompleta (edite o config.json ou use a versão com janela):")
        for p in problemas:
            print(" -", p)
        sys.exit(1)

    cfg["intervalo"] = max(INTERVALO_MINIMO, int(cfg.get("intervalo", 20)))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%d/%m/%Y %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            RotatingFileHandler(os.path.join(APP_DIR, "monitor.log"),
                                maxBytes=1_000_000, backupCount=3,
                                encoding="utf-8"),
        ],
    )

    monitor = Monitor(cfg, logging.info)

    if args.uma_vez:
        monitor.ciclo()
        return

    def encerrar(*_):
        logging.info("Sinal de encerramento recebido.")
        monitor.parar_agora()

    signal.signal(signal.SIGINT, encerrar)
    signal.signal(signal.SIGTERM, encerrar)

    monitor.start()
    while monitor.is_alive():
        monitor.join(timeout=1)


if __name__ == "__main__":
    main()
