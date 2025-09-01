#!/usr/bin/env python3
"""
Menu interativo para um proxy que replica o comportamento do main.rs (Rust).

Sequência (idêntica ao Rust, conforme esperado):
  1) Envia "HTTP/1.1 101 <STATUS>\r\n\r\n"
  2) Lê e descarta 1024 bytes do cliente (preconsume)
  3) Envia "HTTP/1.1 200 <STATUS>\r\n\r\n"
  4) Faz peek de até 8192 bytes (timeout 1s)
  5) Se vazio ou contém "SSH" -> conecta em 0.0.0.0:22, senão 0.0.0.0:1194
  6) Faz a cópia bidirecional de dados até EOF/erro

Observação importante:
- Os destinos "0.0.0.0:22/1194" existem aqui apenas para reproduzir fielmente
  o Rust. Para um proxy realmente funcional, altere para "127.0.0.1" ou um
  IP/host real.

Uso (linha de comando):
    python3 proxy_menu.py [--status "<Texto do Status>"]
"""

import argparse
import asyncio
import contextlib
import os
import socket
import sys
import threading
import time
from typing import Optional, Tuple

# ==== Parâmetros (mantidos iguais ao Rust por padrão) ====
DEFAULT_STATUS = "@RustyManager"
DEFAULT_PORT = 80
BUF_SIZE = 8192
PRECONSUME = 1024
PEEK_TIMEOUT = 1.0  # segundos
UPSTREAM_SSH = ("0.0.0.0", 22)
UPSTREAM_OTHER = ("0.0.0.0", 1194)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--status", default=DEFAULT_STATUS, help="Texto do status a enviar nas respostas 101/200")
    return p.parse_args()


async def peek_stream_async(reader: asyncio.StreamReader, timeout: float = PEEK_TIMEOUT, bufsize: int = BUF_SIZE) -> Tuple[bool, bytes]:
    """Executa peek usando asyncio streams com timeout."""
    try:
        # Usa asyncio.wait_for para timeout
        peek_task = asyncio.create_task(reader.read(bufsize))
        data = await asyncio.wait_for(peek_task, timeout=timeout)
        
        # Se conseguiu ler dados, precisamos "devolvê-los" ao buffer
        # Como não há peek real em asyncio, vamos usar uma abordagem diferente
        return True, data if data else b""
    except asyncio.TimeoutError:
        return True, b""
    except Exception:
        return False, b""


async def transfer_data_streams(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Transfere dados entre streams asyncio até EOF/erro."""
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


class AsyncProxyIdentico:
    """Servidor proxy assíncrono que replica a lógica do main.rs."""
    def __init__(self, port: int, status: str) -> None:
        self.port = port
        self.status = status
        self.server: Optional[asyncio.Server] = None

    async def start(self) -> None:
        """Inicia o servidor."""
        self.server = await asyncio.start_server(
            self._handle_client,
            '::', 
            self.port
        )
        
    async def stop(self) -> None:
        """Para o servidor."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Manipula cada cliente - SEQUÊNCIA CORRIGIDA CONFORME RUST."""
        try:
            # 1) Envia HTTP/1.1 101
            response_101 = f"HTTP/1.1 101 {self.status}\r\n\r\n".encode()
            writer.write(response_101)
            await writer.drain()

            # 2) LÊ E DESCARTA 1024 bytes (preconsume) - CORRIGIDO: ANTES do 200
            try:
                await asyncio.wait_for(reader.read(PRECONSUME), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass

            # 3) Envia HTTP/1.1 200 - CORRIGIDO: DEPOIS do preconsume
            response_200 = f"HTTP/1.1 200 {self.status}\r\n\r\n".encode()
            writer.write(response_200)
            await writer.drain()

            # 4) Faz peek com timeout - IMPLEMENTAÇÃO SIMPLIFICADA
            upstream_addr = UPSTREAM_SSH  # padrão
            
            try:
                # Tenta ler alguns bytes com timeout para decisão
                peek_data = await asyncio.wait_for(reader.read(BUF_SIZE), timeout=PEEK_TIMEOUT)
                if peek_data:
                    # Se contém SSH ou está vazio -> SSH (porta 22)
                    if b"SSH" in peek_data:
                        upstream_addr = UPSTREAM_SSH
                    else:
                        upstream_addr = UPSTREAM_OTHER
                    
                    # IMPORTANTE: Como não podemos fazer peek real, 
                    # criamos um novo reader que inclui os dados já lidos
                    class PrependedReader:
                        def __init__(self, prepend_data: bytes, original_reader: asyncio.StreamReader):
                            self.prepend_data = prepend_data
                            self.original_reader = original_reader
                            self.prepend_consumed = False
                        
                        async def read(self, n: int = -1) -> bytes:
                            if not self.prepend_consumed:
                                self.prepend_consumed = True
                                if n == -1 or n >= len(self.prepend_data):
                                    remaining = await self.original_reader.read(n - len(self.prepend_data) if n != -1 else -1)
                                    return self.prepend_data + remaining
                                else:
                                    # Caso complexo, simplificado para o contexto
                                    return self.prepend_data[:n]
                            return await self.original_reader.read(n)
                    
                    reader = PrependedReader(peek_data, reader)
                else:
                    # Dados vazios -> SSH
                    upstream_addr = UPSTREAM_SSH
                    
            except asyncio.TimeoutError:
                # Timeout -> SSH (padrão)
                upstream_addr = UPSTREAM_SSH
            except Exception:
                # Erro -> SSH (padrão)
                upstream_addr = UPSTREAM_SSH

            # 5) Conecta no upstream
            try:
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    upstream_addr[0], upstream_addr[1]
                )
            except Exception:
                return

            # 6) Faz a ponte bidirecional
            task1 = asyncio.create_task(transfer_data_streams(reader, upstream_writer))
            task2 = asyncio.create_task(transfer_data_streams(upstream_reader, writer))
            
            try:
                await asyncio.gather(task1, task2, return_exceptions=True)
            finally:
                # Cleanup
                for task in [task1, task2]:
                    if not task.done():
                        task.cancel()
                
                try:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
                except Exception:
                    pass

        except Exception:
            # Silencioso como o Rust
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


class ProxyController:
    """Controla o proxy em thread separada e expõe status/erros."""
    def __init__(self, status: str):
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[AsyncProxyIdentico] = None
        self.port: Optional[int] = None
        self.status: str = status
        self.error_message: Optional[str] = None
        self._server_obj: Optional[asyncio.Server] = None

    def start(self, port: int) -> bool:
        """Inicia o proxy em background. Retorna True se iniciou."""
        if self.is_running():
            return True
        self.port = port
        self.error_message = None

        started = threading.Event()
        result_ok = False

        def run():
            nonlocal result_ok
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._server = AsyncProxyIdentico(port, self.status)

                async def boot():
                    await self._server.start()
                    self._server_obj = self._server.server
                    return True

                self._loop.run_until_complete(boot())
                result_ok = True
                started.set()
                self._loop.run_forever()
                
            except Exception as e:
                self.error_message = str(e)
                result_ok = False
                started.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

        started.wait(timeout=5.0)
        if not result_ok and not self.error_message:
            self.error_message = "Falha ao iniciar o servidor (tempo excedido)."

        return result_ok

    def stop(self) -> None:
        """Encerra o proxy se estiver rodando."""
        if not self.is_running() or not self._loop or not self._server:
            return

        def shutdown_sequence():
            async def stop_all():
                if self._server:
                    await self._server.stop()
                if self._loop and self._loop.is_running():
                    self._loop.stop()

            asyncio.create_task(stop_all())

        self._loop.call_soon_threadsafe(shutdown_sequence)

        if self._thread:
            self._thread.join(timeout=5.0)

        self._thread = None
        self._loop = None
        self._server = None
        self._server_obj = None
        self.port = None
        self.error_message = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


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
    C_BOLD = "\033[1m"
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
        print("\n\nEncerrado pelo usuário.")
