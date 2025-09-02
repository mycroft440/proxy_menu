#!/usr/bin/env python3
"""
Menu interativo para um proxy que replica o comportamento do main.rs (Rust).

Versão final com gestão de tarefas de transferência de dados mais robusta
para evitar problemas de negociação de protocolos como o SSH.

Sequência:
  1) Envia "HTTP/1.1 101 <STATUS>\r\n\r\n"
  2) Lê e descarta 1024 bytes do cliente (preconsume)
  3) Envia "HTTP/1.1 200 <STATUS>\r\n\r\n"
  4) Faz uma leitura inicial (simulando peek) de até 8192 bytes (timeout 1s)
  5) Se vazio ou contém "SSH" -> conecta em 0.0.0.0:22, senão 0.0.0.0:1194
  6) Inicia a cópia bidirecional de dados de forma robusta.
"""

import argparse
import asyncio
import os
import sys
import threading
import time
from typing import Optional

# ==== Parâmetros (mantidos iguais ao Rust por padrão) ====
DEFAULT_STATUS = "@RustyManager"
DEFAULT_PORT = 80
BUF_SIZE = 8192
PRECONSUME = 1024
PEEK_TIMEOUT = 1.0
UPSTREAM_SSH = ("0.0.0.0", 22)
UPSTREAM_OTHER = ("0.0.0.0", 1194)

# Habilite para depuração detalhada do fluxo de dados
ENABLE_DEBUG_LOGS = False

def log_debug(msg):
    if ENABLE_DEBUG_LOGS:
        print(f"[DEBUG] {msg}", file=sys.stderr)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--status", default=DEFAULT_STATUS, help="Texto do status a enviar nas respostas 101/200")
    # ALTERAÇÃO: Usar parse_known_args() para ignorar argumentos desconhecidos
    # que possam ser passados pelo ambiente de execução.
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
        print(f"Iniciando serviço na porta: {self.port}")

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Manipula cada cliente seguindo exatamente a sequência do Rust"""
        client_addr = writer.get_extra_info('peername')
        log_debug(f"Nova conexão de {client_addr}")
        
        upstream_writer: Optional[asyncio.StreamWriter] = None
        try:
            # 1) Envia HTTP/1.1 101
            response_101 = f"HTTP/1.1 101 {self.status}\r\n\r\n"
            writer.write(response_101.encode())
            await writer.drain()

            # 2) Lê e descarta 1024 bytes (payload do cliente)
            await reader.read(PRECONSUME)

            # 3) Envia HTTP/1.1 200
            response_200 = f"HTTP/1.1 200 {self.status}\r\n\r\n"
            writer.write(response_200.encode())
            await writer.drain()

            # 4) Simula peek para determinar o destino
            peek_buffer = b""
            addr_proxy = UPSTREAM_SSH # Padrão
            try:
                peek_buffer = await asyncio.wait_for(reader.read(BUF_SIZE), timeout=PEEK_TIMEOUT)
                log_debug(f"Dados do 'peek': {peek_buffer[:50]}...")
                if peek_buffer and b"SSH" not in peek_buffer:
                    addr_proxy = UPSTREAM_OTHER
            except asyncio.TimeoutError:
                log_debug("Timeout no 'peek', usando destino padrão (SSH).")
                pass
            
            log_debug(f"Conectando ao destino: {addr_proxy}")
            # 5) Conecta no servidor upstream
            upstream_reader, upstream_writer = await asyncio.open_connection(
                addr_proxy[0], addr_proxy[1]
            )
            log_debug("Conexão com o destino estabelecida.")

            # 6) Inicia a transferência bidirecional
            task1 = asyncio.create_task(self.transfer_data(reader, upstream_writer, peek_buffer, f"{client_addr} -> upstream"))
            task2 = asyncio.create_task(self.transfer_data(upstream_reader, writer, None, f"upstream -> {client_addr}"))

            # *** ALTERAÇÃO CRÍTICA ***
            # Usamos asyncio.gather para aguardar que AMBAS as tarefas terminem.
            # Isto é mais robusto do que cancelar uma quando a outra termina.
            await asyncio.gather(task1, task2)

        except (ConnectionResetError, BrokenPipeError) as e:
            log_debug(f"Conexão fechada por um dos lados: {e}")
        except Exception as e:
            log_debug(f"Erro inesperado em handle_client: {e}")
        finally:
            log_debug(f"Encerrando conexão com {client_addr}.")
            if not writer.is_closing():
                writer.close()
            if upstream_writer and not upstream_writer.is_closing():
                upstream_writer.close()
            # Espera um instante para garantir que os fechamentos sejam processados
            await asyncio.sleep(0)

    async def transfer_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial_data: Optional[bytes], direction: str):
        """
        Transfere dados de um leitor para um escritor.
        Se 'initial_data' for fornecido, envia-o primeiro.
        """
        try:
            if initial_data:
                log_debug(f"[{direction}] Enviando dados iniciais ({len(initial_data)} bytes)")
                writer.write(initial_data)
                await writer.drain()

            while True:
                data = await reader.read(BUF_SIZE)
                if not data:
                    log_debug(f"[{direction}] Fim do stream (EOF), encerrando.")
                    break
                
                log_debug(f"[{direction}] Transferindo {len(data)} bytes")
                writer.write(data)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            log_debug(f"[{direction}] Transferência interrompida (conexão fechada).")
        except Exception as e:
            log_debug(f"[{direction}] Erro inesperado em transfer_data: {e}")
        finally:
            if not writer.is_closing():
                writer.close()


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
        """Inicia o proxy em segundo plano"""
        if self.is_running():
            return True
            
        self.port = port
        self.error_message = None
        self._started_event.clear()
        
        result = {"success": False}

        def run_proxy():
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
                    finally:
                        self._started_event.set()

                    if result["success"] and self._proxy.server:
                        await self._proxy.server.serve_forever()

                self._loop.run_until_complete(run_server())

            except Exception as e:
                self.error_message = str(e)
                result["success"] = False
                if not self._started_event.is_set():
                    self._started_event.set()

        self._thread = threading.Thread(target=run_proxy, daemon=True)
        self._thread.start()

        started = self._started_event.wait(timeout=5.0)

        if not started and not self.error_message:
            self.error_message = "Falha ao iniciar o servidor (timeout)"

        return result["success"]

    def stop(self) -> None:
        """Para o proxy"""
        if not self.is_running() or not self._loop or not self._proxy:
            return

        async def do_stop():
            if self._proxy:
                await self._proxy.stop()
            if self._loop and self._loop.is_running():
                self._loop.stop()

        future = asyncio.run_coroutine_threadsafe(do_stop(), self._loop)
        try:
            future.result(timeout=3.0)
        except (TimeoutError, asyncio.TimeoutError):
            log_debug("Timeout ao parar o proxy.")

        if self._thread:
            self._thread.join(timeout=3.0)

        self._thread = None
        self._loop = None
        self._proxy = None
        self.port = None
        self.error_message = None

    def is_running(self) -> bool:
        return (self._thread is not None and 
                self._thread.is_alive() and 
                self._proxy is not None and 
                self._proxy.running)


def ask_port(default: int) -> Optional[int]:
    C_CYAN = "\033[96m"
    C_YELLOW = "\033[93m"
    C_RESET = "\033[0m"

    val = input(f"  {C_CYAN}› Digite a porta para abrir [{default}]: {C_RESET}").strip()
    if not val:
        return default
    try:
        p = int(val)
        if not (1 <= p <= 65535):
            raise ValueError
        return p
    except ValueError:
        print(f"  {C_YELLOW}⚠️ Porta inválida. Tente um número entre 1 e 65535.{C_RESET}")
        time.sleep(2)
        return None


def run_menu(status: str) -> None:
    ctrl = ProxyController(status=status)
    default_port = DEFAULT_PORT

    C_RESET = "\033[0m"
    C_BOLD = "\031m"
    C_GREEN = "\033[92m"
    C_RED = "\033[91m"
    C_YELLOW = "\033[93m"
    C_BLUE = "\033[94m"
    C_CYAN = "\033[96m"
    C_WHITE = "\033[97m"

    def clear_screen():
        os.system('cls' if sys.platform == 'win32' else 'clear')

    while True:
        clear_screen()
        print(f"{C_BLUE}{C_BOLD}╔═══════════════════════════════════════════════╗")
        print(f"║        Gerenciador de Proxy ({status})        ║")
        print(f"╚═══════════════════════════════════════════════╝{C_RESET}\n")

        if ctrl.is_running() and ctrl.port is not None:
            print(f"  {C_BOLD}Status:{C_RESET} {C_GREEN}ATIVO ✅{C_RESET}")
            print(f"  {C_BOLD}Porta:{C_RESET} {C_WHITE}{ctrl.port}{C_RESET}")
        else:
            print(f"  {C_BOLD}Status:{C_RESET} {C_RED}INATIVO ❌{C_RESET}")

        if ctrl.error_message:
            print(f"  {C_BOLD}Erro:{C_RESET} {C_RED}{ctrl.error_message}{C_RESET}")

        print("\n" + "="*47 + "\n")
        print(f"  {C_YELLOW}[1]{C_RESET} - Iniciar Proxy")
        print(f"  {C_YELLOW}[2]{C_RESET} - Parar Proxy")
        print(f"  {C_RED}[3]{C_RESET} - Remover Proxy")
        print(f"  {C_YELLOW}[0]{C_RESET} - Sair\n")

        choice = input(f"  {C_CYAN}› Selecione uma opção: {C_RESET}").strip()

        if choice == "1":
            if ctrl.is_running():
                print(f"\n  {C_YELLOW}💡 O proxy já está ativo. Pare-o antes de iniciar novamente.{C_RESET}")
                time.sleep(2)
                continue
            port = ask_port(default_port)
            if port is None:
                continue
            print(f"\n  {C_WHITE}Iniciando o proxy na porta {port}...{C_RESET}")
            ok = ctrl.start(port)
            if ok:
                print(f"  {C_GREEN}✔ Proxy iniciado com sucesso na porta {port}.{C_RESET}")
                default_port = port
                if port < 1024 and sys.platform != 'win32':
                    print(f"  {C_YELLOW}  (Atenção: Portas < 1024 podem exigir privilégios de administrador){C_RESET}")
                time.sleep(2.5)
            else:
                print(f"  {C_RED}✘ Falha ao iniciar o proxy na porta {port}.{C_RESET}")
                if ctrl.error_message:
                    print(f"    {C_RED}Motivo: {ctrl.error_message}{C_RESET}")
                time.sleep(4)

        elif choice == "2":
            if not ctrl.is_running():
                print(f"\n  {C_YELLOW}💡 O proxy já está inativo.{C_RESET}")
            else:
                print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}")
                ctrl.stop()
                print(f"  {C_GREEN}✔ Proxy interrompido com sucesso.{C_RESET}")
            time.sleep(2)

        elif choice == "3":
            print(f"\n  {C_RED}{C_BOLD}ATENÇÃO: Esta ação removerá o próprio arquivo do proxy permanentemente.{C_RESET}")
            confirm = input(f"  {C_CYAN}› Deseja realmente continuar? (s/N): {C_RESET}").strip().lower()
            if confirm == 's':
                if ctrl.is_running():
                    print(f"\n  {C_WHITE}Parando o proxy antes de remover...{C_RESET}")
                    ctrl.stop()
                    time.sleep(1)
                try:
                    script_path = os.path.abspath(__file__)
                    print(f"  {C_WHITE}Removendo o arquivo: {script_path}{C_RESET}")
                    os.remove(script_path)
                    print(f"  {C_GREEN}✔ Proxy removido com sucesso.{C_RESET}")
                    print(f"  {C_BLUE}👋 Saindo...{C_RESET}")
                    time.sleep(2)
                    break
                except Exception as e:
                    print(f"\n  {C_RED}✘ Erro ao remover o arquivo: {e}{C_RESET}")
                    print(f"  {C_YELLOW}  Por favor, remova o arquivo manualmente.{C_RESET}")
                    time.sleep(4)
                    break
            else:
                print(f"\n  {C_YELLOW}💡 Remoção cancelada.{C_RESET}")
                time.sleep(2)

        elif choice == "0":
            if ctrl.is_running():
                print(f"\n  {C_WHITE}Parando o proxy antes de sair...{C_RESET}")
                ctrl.stop()
            print(f"\n  {C_BLUE}👋 Até logo!{C_RESET}")
            break

        else:
            print(f"\n  {C_RED}✘ Opção inválida. Tente novamente.{C_RESET}")
            time.sleep(1.5)


if __name__ == "__main__":
    args = parse_args()
    try:
        run_menu(status=args.status)
    except KeyboardInterrupt:
        print("\n\nEncerrado pelo utilizador.")

