#!/usr/bin/env python3
"""
Menu interativo para um proxy com encaminhamento direto para SSH.

Versão final e estável que usa a abstração de Streams de alto nível do asyncio
(StreamReader/StreamWriter) para garantir a estabilidade e evitar erros de
socket de baixo nível. Inclui um sistema de logging profundo para diagnosticar
problemas.
"""

import argparse
import asyncio
import os
import sys
import threading
import time
import socket
import logging
from typing import Optional

# ==== Configuração dos Logs ====
LOG_FILE = "/tmp/proxy.log"
# Previne que múltiplos handlers sejam adicionados
if not logging.getLogger(__name__).handlers:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(threadName)s - [%(funcName)s] - %(message)s',
        filename=LOG_FILE,
        filemode='a'
    )
logger = logging.getLogger(__name__)

# ==== Parâmetros ====
DEFAULT_STATUS = "@RustyManager"
DEFAULT_PORT = 80
BUF_SIZE = 8192
PRECONSUME = 1024
UPSTREAM_SSH = ("127.0.0.1", 22)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--status", default=DEFAULT_STATUS, help="Texto do status a enviar nas respostas 101/200")
    args, _ = p.parse_known_args()
    return args


class AsyncProxy:
    def __init__(self, port: int, status: str):
        self.port = port
        self.status = status
        self.server: Optional[asyncio.AbstractServer] = None
        self.running = False

    async def start(self):
        """Inicia o servidor proxy"""
        self.server = await asyncio.start_server(
            self.handle_client,
            '0.0.0.0',
            self.port
        )
        self.running = True
        logger.info(f"Servidor iniciado e a escutar na porta {self.port}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Manipula o cliente usando exclusivamente Streams de alto nível."""
        client_addr = writer.get_extra_info('peername')
        logger.info(f"Nova conexão de {client_addr}")

        upstream_writer = None
        try:
            # 1) Handshake HTTP
            logger.debug(f"[{client_addr}] Enviando HTTP/1.1 101")
            writer.write(f"HTTP/1.1 101 {self.status}\r\n\r\n".encode())
            await writer.drain()

            logger.debug(f"[{client_addr}] Lendo payload inicial (até {PRECONSUME} bytes)")
            await reader.read(PRECONSUME)

            logger.debug(f"[{client_addr}] Enviando HTTP/1.1 200")
            writer.write(f"HTTP/1.1 200 {self.status}\r\n\r\n".encode())
            await writer.drain()
            logger.info(f"[{client_addr}] Handshake concluído.")

            # 2) Conexão com o destino
            logger.debug(f"[{client_addr}] Conectando ao upstream em {UPSTREAM_SSH}")
            upstream_reader, upstream_writer = await asyncio.open_connection(
                UPSTREAM_SSH[0], UPSTREAM_SSH[1]
            )
            logger.info(f"[{client_addr}] Conexão com upstream {UPSTREAM_SSH} estabelecida.")

            # 3) Transferência bidirecional usando a API correta
            logger.debug(f"[{client_addr}] Iniciando transferência bidirecional.")
            task1 = asyncio.create_task(self.transfer(reader, upstream_writer, f"{client_addr} -> UPSTREAM"))
            task2 = asyncio.create_task(self.transfer(upstream_reader, writer, f"UPSTREAM -> {client_addr}"))

            await asyncio.gather(task1, task2)
            logger.debug(f"[{client_addr}] Transferência de dados concluída.")

        except ConnectionRefusedError:
            logger.error(f"[{client_addr}] Falha ao conectar: Conexão recusada. O serviço SSH está a correr em {UPSTREAM_SSH}?")
        except Exception as e:
            logger.error(f"[{client_addr}] Erro inesperado: {e}", exc_info=True)
        finally:
            logger.info(f"[{client_addr}] Encerrando conexão.")
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            if upstream_writer and not upstream_writer.is_closing():
                upstream_writer.close()
                await upstream_writer.wait_closed()

    async def transfer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str):
        """
        CORREÇÃO: Transfere dados usando reader.read() e writer.write(),
        a API de alto nível correta para Streams.
        """
        try:
            while True:
                data = await reader.read(BUF_SIZE) # <<< USA .read()
                if not data:
                    logger.debug(f"[{direction}] Fim do stream (EOF).")
                    break
                logger.debug(f"[{direction}] Transferindo {len(data)} bytes.")
                writer.write(data) # <<< USA .write()
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            logger.warning(f"[{direction}] Conexão de transferência redefinida.")
        except Exception as e:
            logger.error(f"[{direction}] Erro na transferência: {e}", exc_info=True)
        finally:
            if not writer.is_closing():
                writer.close()


    async def stop(self):
        """Para o servidor"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        logger.info("Servidor parado.")
        self.running = False


class ProxyController:
    """Controla o proxy numa thread separada para não bloquear o menu"""
    def __init__(self, status: str):
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._proxy: Optional[AsyncProxy] = None
        self.port: Optional[int] = None
        self.status: str = status
        self.error_message: Optional[str] = None
        self._started_event = threading.Event()

    def start(self, port: int) -> bool:
        if self.is_running(): return True
        self.port = port
        self.error_message = None
        self._started_event.clear()
        result = {"success": False}
        def run_proxy():
            threading.current_thread().name = "ProxyThread"
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._proxy = AsyncProxy(port, self.status)
                async def run_server():
                    try:
                        await self._proxy.start()
                        result["success"] = True
                    except Exception as e:
                        self.error_message = str(e)
                        logger.critical(f"Falha crítica ao iniciar o servidor: {e}", exc_info=True)
                    finally:
                        self._started_event.set()
                    if result["success"] and self._proxy.server:
                        await self._proxy.server.serve_forever()
                self._loop.run_until_complete(run_server())
            except Exception as e:
                self.error_message = str(e)
                logger.critical(f"Falha não tratada na thread do proxy: {e}", exc_info=True)
                result["success"] = False
                if not self._started_event.is_set(): self._started_event.set()
        self._thread = threading.Thread(target=run_proxy, daemon=True)
        self._thread.start()
        started = self._started_event.wait(timeout=5.0)
        if not started and not self.error_message:
            self.error_message = "Falha ao iniciar o servidor (timeout)"
            logger.error(self.error_message)
        return result["success"]

    def stop(self) -> None:
        if not self.is_running() or not self._loop: return
        logger.info("A parar o proxy...")
        async def do_stop():
            if self._proxy: await self._proxy.stop()
            if self._loop.is_running(): self._loop.stop()
        future = asyncio.run_coroutine_threadsafe(do_stop(), self._loop)
        try:
            future.result(timeout=3.0)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Timeout ao parar o proxy.")
        if self._thread: self._thread.join(timeout=3.0)
        self._thread, self._loop, self._proxy, self.port, self.error_message = None, None, None, None, None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._proxy is not None and self._proxy.running

def ask_port(default: int) -> Optional[int]:
    C_CYAN, C_YELLOW, C_RESET = "\033[96m", "\033[93m", "\033[0m"
    val = input(f"  {C_CYAN}› Digite a porta para abrir [{default}]: {C_RESET}").strip()
    if not val: return default
    try:
        p = int(val)
        if not (1 <= p <= 65535): raise ValueError
        return p
    except ValueError:
        print(f"  {C_YELLOW}⚠️ Porta inválida. Tente um número entre 1 e 65535.{C_RESET}")
        time.sleep(2)
        return None

def run_menu(status: str) -> None:
    ctrl = ProxyController(status=status)
    default_port = DEFAULT_PORT
    C_RESET, C_BOLD, C_GREEN, C_RED, C_YELLOW, C_BLUE, C_CYAN, C_WHITE = "\033[0m", "\033[1m", "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[96m", "\033[97m"
    def clear_screen(): os.system('cls' if sys.platform == 'win32' else 'clear')
    while True:
        clear_screen()
        print(f"{C_BLUE}{C_BOLD}╔═══════════════════════════════════════════════╗")
        print(f"║     Gerenciador de Proxy (SSH Direto) {status}     ║")
        print(f"╚═══════════════════════════════════════════════╝{C_RESET}\n")
        if ctrl.is_running() and ctrl.port is not None:
            print(f"  {C_BOLD}Status:{C_RESET} {C_GREEN}ATIVO ✅{C_RESET}")
            print(f"  {C_BOLD}Porta:{C_RESET} {C_WHITE}{ctrl.port}{C_RESET}")
        else:
            print(f"  {C_BOLD}Status:{C_RESET} {C_RED}INATIVO ❌{C_RESET}")
        if ctrl.error_message: print(f"  {C_BOLD}Erro:{C_RESET} {C_RED}{ctrl.error_message}{C_RESET}")
        print("\n" + "="*47 + "\n")
        print(f"  {C_YELLOW}[1]{C_RESET} - Iniciar Proxy")
        print(f"  {C_YELLOW}[2]{C_RESET} - Parar Proxy")
        print(f"  {C_YELLOW}[3]{C_RESET} - Ver Logs")
        print(f"  {C_YELLOW}[4]{C_RESET} - Limpar Logs")
        print(f"  {C_RED}[5]{C_RESET} - Remover Script")
        print(f"  {C_YELLOW}[0]{C_RESET} - Sair\n")
        choice = input(f"  {C_CYAN}› Selecione uma opção: {C_RESET}").strip()
        if choice == "1":
            if ctrl.is_running(): print(f"\n  {C_YELLOW}💡 O proxy já está ativo.{C_RESET}"); time.sleep(2); continue
            port = ask_port(default_port)
            if port is None: continue
            print(f"\n  {C_WHITE}Iniciando o proxy na porta {port}...{C_RESET}");
            ok = ctrl.start(port)
            if ok: print(f"  {C_GREEN}✔ Proxy iniciado com sucesso.{C_RESET}"); default_port = port; time.sleep(2.5)
            else:
                print(f"  {C_RED}✘ Falha ao iniciar o proxy.{C_RESET}")
                if ctrl.error_message: print(f"    {C_RED}Motivo: {ctrl.error_message}{C_RESET}")
                time.sleep(4)
        elif choice == "2":
            if not ctrl.is_running(): print(f"\n  {C_YELLOW}💡 O proxy já está inativo.{C_RESET}")
            else: print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}"); ctrl.stop(); print(f"  {C_GREEN}✔ Proxy interrompido.{C_RESET}")
            time.sleep(2)
        elif choice == "3":
            clear_screen()
            print(f"{C_BLUE}--- Logs de {LOG_FILE} ---{C_RESET}\n")
            try:
                with open(LOG_FILE, 'r') as f:
                    print(f.read())
            except FileNotFoundError:
                print(f"{C_YELLOW}Arquivo de log ainda não foi criado.{C_RESET}")
            print(f"\n{C_BLUE}--- Fim dos Logs ---{C_RESET}")
            input(f"\n{C_CYAN}Pressione Enter para voltar ao menu...{C_RESET}")
        elif choice == "4":
            try:
                with open(LOG_FILE, 'w') as f:
                    f.write('')
                logger.info("Arquivo de log limpo pelo utilizador.")
                print(f"\n  {C_GREEN}✔ Arquivo de log ({LOG_FILE}) limpo com sucesso.{C_RESET}")
            except Exception as e:
                print(f"\n  {C_RED}✘ Erro ao limpar o arquivo de log: {e}{C_RESET}")
            time.sleep(2)
        elif choice == "5":
            print(f"\n  {C_RED}{C_BOLD}ATENÇÃO: Ação irreversível.{C_RESET}")
            if input(f"  {C_CYAN}› Deseja realmente remover o script? (s/N): {C_RESET}").strip().lower() == 's':
                if ctrl.is_running(): print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}"); ctrl.stop(); time.sleep(1)
                try:
                    script_path = os.path.abspath(__file__);
                    print(f"  {C_WHITE}Removendo: {script_path}{C_RESET}"); os.remove(script_path)
                    print(f"  {C_GREEN}✔ Script removido. Saindo...{C_RESET}"); time.sleep(2); break
                except Exception as e:
                    print(f"\n  {C_RED}✘ Erro ao remover: {e}{C_RESET}"); time.sleep(4); break
            else:
                print(f"\n  {C_YELLOW}💡 Remoção cancelada.{C_RESET}"); time.sleep(2)
        elif choice == "0":
            if ctrl.is_running(): print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}"); ctrl.stop()
            print(f"\n  {C_BLUE}👋 Até logo!{C_RESET}"); break
        else:
            print(f"\n  {C_RED}✘ Opção inválida.{C_RESET}"); time.sleep(1.5)

if __name__ == "__main__":
    threading.current_thread().name = "MainThread"
    logger.info("="*20 + " Script do Menu Iniciado " + "="*20)
    args = parse_args()
    try:
        run_menu(status=args.status)
    except KeyboardInterrupt:
        logger.info("Script terminado pelo utilizador (Ctrl+C)")
        print("\n\nEncerrado pelo utilizador.")
    except Exception as e:
        logger.critical(f"Erro fatal na thread principal: {e}", exc_info=True)
        print(f"\n\nOcorreu um erro fatal. Verifique {LOG_FILE} para detalhes.")

