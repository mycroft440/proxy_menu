#!/usr/bin/env python3
"""
Menu interativo para um proxy que replica o mais fielmente possível o
comportamento do main.rs (Rust).

Alterações para Conformidade Total:
  - O servidor agora escuta em todas as interfaces, tanto IPv4 como IPv6
    (dual-stack), tal como o código em Rust.
  - A lógica de decisão para o roteamento (SSH vs. Outro) foi reescrita
    para espelhar exatamente a estrutura do código em Rust.

Sequência Idêntica ao Rust:
  1) Envia "HTTP/1.1 101 <STATUS>\r\n\r\n"
  2) Lê e descarta até 1024 bytes do cliente (preconsume)
  3) Envia "HTTP/1.1 200 <STATUS>\r\n\r\n"
  4) Simula um 'peek' (leitura não destrutiva) com timeout de 1s
  5) Se os dados lidos estiverem vazios OU contiverem "SSH", conecta-se
     a 0.0.0.0:22. Caso contrário, conecta-se a 0.0.0.0:1194.
  6) Inicia a cópia bidirecional de dados.
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

# Habilite para depuração detalhada
ENABLE_DEBUG_LOGS = False

def log_debug(msg):
    if ENABLE_DEBUG_LOGS:
        print(f"[DEBUG] {msg}", file=sys.stderr)

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
        # *** ALTERAÇÃO CRÍTICA PARA CONFORMIDADE ***
        # Usar `host=None` permite que o socket escute em todas as interfaces,
        # tanto IPv4 como IPv6, replicando o `[::]` do Rust.
        self.server = await asyncio.start_server(
            self.handle_client,
            host=None,
            port=self.port
        )
        self.running = True
        addrs = ', '.join(str(sock.getsockname()) for sock in self.server.sockets)
        print(f"Iniciando serviço em: {addrs}")

    async def stop(self):
        """Para o servidor"""
        if self.server and not self.server.is_serving():
            return
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.running = False
        log_debug("Servidor parado.")

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

            # 2) Lê e descarta até 1024 bytes (payload do cliente)
            await reader.read(PRECONSUME)

            # 3) Envia HTTP/1.1 200
            response_200 = f"HTTP/1.1 200 {self.status}\r\n\r\n"
            writer.write(response_200.encode())
            await writer.drain()

            # 4) Simula peek para determinar o destino
            peek_buffer = b""
            try:
                peek_buffer = await asyncio.wait_for(reader.read(BUF_SIZE), timeout=PEEK_TIMEOUT)
                log_debug(f"Dados do 'peek': {peek_buffer[:60]}...")
            except asyncio.TimeoutError:
                log_debug("Timeout no 'peek', o buffer está vazio.")
                pass
            
            # *** ALTERAÇÃO CRÍTICA PARA CONFORMIDADE ***
            # A lógica agora é uma tradução direta da do Rust:
            # `if data.contains("SSH") || data.is_empty()`
            if not peek_buffer or b"SSH" in peek_buffer:
                addr_proxy = UPSTREAM_SSH
            else:
                addr_proxy = UPSTREAM_OTHER
            
            log_debug(f"Conectando ao destino: {addr_proxy}")
            # 5) Conecta no servidor upstream
            upstream_reader, upstream_writer = await asyncio.open_connection(
                addr_proxy[0], addr_proxy[1]
            )
            log_debug("Conexão com o destino estabelecida.")

            # 6) Inicia a transferência bidirecional
            task1 = asyncio.create_task(self.transfer_data(reader, upstream_writer, peek_buffer, f"{client_addr} -> upstream"))
            task2 = asyncio.create_task(self.transfer_data(upstream_reader, writer, None, f"upstream -> {client_addr}"))

            await asyncio.gather(task1, task2)

        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError) as e:
            log_debug(f"Conexão com {client_addr} fechada de forma limpa ou abrupta: {e}")
        except Exception as e:
            log_debug(f"Erro inesperado ao lidar com {client_addr}: {e}")
        finally:
            log_debug(f"Encerrando e limpando conexão com {client_addr}.")
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()
            if upstream_writer and not upstream_writer.is_closing():
                upstream_writer.close()
                await upstream_writer.wait_closed()

    async def transfer_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial_data: Optional[bytes], direction: str):
        """Transfere dados de um leitor para um escritor."""
        try:
            if initial_data:
                log_debug(f"[{direction}] Enviando dados iniciais ({len(initial_data)} bytes)")
                writer.write(initial_data)
                await writer.drain()

            while not reader.at_eof():
                data = await reader.read(BUF_SIZE)
                if not data:
                    break
                log_debug(f"[{direction}] Transferindo {len(data)} bytes")
                writer.write(data)
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            log_debug(f"[{direction}] Transferência interrompida (conexão fechada).")
        except Exception as e:
            log_debug(f"[{direction}] Erro inesperado em transfer_data: {e}")
        finally:
            log_debug(f"[{direction}] Encerrando o stream de escrita.")
            if writer.can_write_eof():
                writer.write_eof()
            if not writer.is_closing():
                writer.close()
                await writer.wait_closed()


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
        self.port, self.error_message = port, None
        self._started_event.clear()
        result = {"success": False}

        def run_proxy():
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._proxy = AsyncProxy(port, self.status)
                
                async def runner():
                    try:
                        await self._proxy.start()
                        result["success"] = True
                    except Exception as e:
                        self.error_message = str(e)
                    finally:
                        self._started_event.set()

                    if result["success"] and self._proxy.server:
                        await self._proxy.server.serve_forever()
                
                self._loop.run_until_complete(runner())
            except Exception as e:
                self.error_message = str(e)
                if not self._started_event.is_set(): self._started_event.set()

        self._thread = threading.Thread(target=run_proxy, daemon=True)
        self._thread.start()
        if not self._started_event.wait(timeout=5.0):
            self.error_message = "Falha ao iniciar o servidor (timeout)"
        return result.get("success", False)

    def stop(self) -> None:
        if not self.is_running() or not self._loop or not self._proxy: return
        
        async def do_stop():
            if self._proxy: await self._proxy.stop()
            if self._loop and self._loop.is_running(): self._loop.stop()

        future = asyncio.run_coroutine_threadsafe(do_stop(), self._loop)
        try: future.result(timeout=5.0)
        except (TimeoutError, asyncio.TimeoutError): log_debug("Timeout ao parar o proxy.")

        if self._thread: self._thread.join(timeout=3.0)
        self._thread = self._loop = self._proxy = self.port = self.error_message = None

    def is_running(self) -> bool:
        return self._thread and self._thread.is_alive() and self._proxy and self._proxy.running


def ask_port(default: int) -> Optional[int]:
    C_CYAN, C_YELLOW, C_RESET = "\033[96m", "\033[93m", "\033[0m"
    val = input(f"  {C_CYAN}› Digite a porta para abrir [{default}]: {C_RESET}").strip()
    if not val: return default
    try:
        p = int(val)
        if not (1 <= p <= 65535): raise ValueError
        return p
    except (ValueError, TypeError):
        print(f"  {C_YELLOW}⚠️ Porta inválida. Tente um número entre 1 e 65535.{C_RESET}")
        time.sleep(2)
        return None


def run_menu(status: str) -> None:
    ctrl = ProxyController(status=status)
    default_port = DEFAULT_PORT
    
    C_RESET, C_BOLD, C_GREEN, C_RED, C_YELLOW, C_BLUE, C_CYAN, C_WHITE = (
        "\033[0m", "\033[1m", "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[96m", "\033[97m"
    )

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
                print(f"\n  {C_YELLOW}💡 O proxy já está ativo.{C_RESET}")
                time.sleep(2)
                continue
            port = ask_port(default_port)
            if port is None: continue
            
            print(f"\n  {C_WHITE}Iniciando o proxy na porta {port}...{C_RESET}")
            if ctrl.start(port):
                print(f"  {C_GREEN}✔ Proxy iniciado com sucesso.{C_RESET}")
                default_port = port
                if port < 1024 and sys.platform != 'win32':
                    print(f"  {C_YELLOW}  (Atenção: Portas < 1024 podem exigir privilégios de root){C_RESET}")
                time.sleep(2.5)
            else:
                print(f"  {C_RED}✘ Falha ao iniciar o proxy.{C_RESET}")
                if ctrl.error_message: print(f"    {C_RED}Motivo: {ctrl.error_message}{C_RESET}")
                time.sleep(4)

        elif choice == "2":
            if not ctrl.is_running():
                print(f"\n  {C_YELLOW}💡 O proxy já está inativo.{C_RESET}")
            else:
                print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}")
                ctrl.stop()
                print(f"  {C_GREEN}✔ Proxy interrompido.{C_RESET}")
            time.sleep(2)

        elif choice == "3":
            print(f"\n  {C_RED}{C_BOLD}ATENÇÃO: Esta ação removerá o próprio script permanentemente.{C_RESET}")
            if input(f"  {C_CYAN}› Deseja realmente continuar? (s/N): {C_RESET}").strip().lower() == 's':
                if ctrl.is_running():
                    print(f"\n  {C_WHITE}Parando o proxy antes de remover...{C_RESET}")
                    ctrl.stop()
                    time.sleep(1)
                try:
                    script_path = os.path.abspath(__file__)
                    print(f"  {C_WHITE}Removendo: {script_path}{C_RESET}")
                    os.remove(script_path)
                    print(f"  {C_GREEN}✔ Script removido. Saindo...{C_RESET}")
                    time.sleep(2)
                    break
                except Exception as e:
                    print(f"\n  {C_RED}✘ Erro ao remover o script: {e}{C_RESET}")
                    time.sleep(4)
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
            print(f"\n  {C_RED}✘ Opção inválida.{C_RESET}")
            time.sleep(1.5)

if __name__ == "__main__":
    args = parse_args()
    try:
        run_menu(status=args.status)
    except KeyboardInterrupt:
        print("\n\nEncerrado pelo utilizador.")
