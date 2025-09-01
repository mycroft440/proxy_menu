#!/usr/bin/env python3
"""
Menu interativo para um proxy que replica o comportamento do main.rs (Rust).

Sequência (idêntica ao Rust, conforme esperado):
  1) Envia "HTTP/1.1 101 <STATUS>\r\n\r\n"
  2) Envia "HTTP/1.1 200 <STATUS>\r\n\r\n"
  3) Lê e descarta 1024 bytes do cliente (preconsume)
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


async def peek_stream(sock: socket.socket, timeout: float = PEEK_TIMEOUT, bufsize: int = BUF_SIZE) -> Tuple[bool, bytes]:
    """Executa recv(MSG_PEEK) com timeout, devolvendo (ok, dados_peekados)."""
    def _peek_blocking() -> Tuple[bool, bytes]:
        orig_to = sock.gettimeout()
        try:
            sock.settimeout(timeout)
            data = sock.recv(bufsize, socket.MSG_PEEK)
            return True, data
        except socket.timeout:
            return True, b""
        except OSError:
            return False, b""
        finally:
            # restaurar non-blocking se era 0.0
            if orig_to == 0.0:
                sock.setblocking(False)
            else:
                sock.settimeout(orig_to)
    return await asyncio.to_thread(_peek_blocking)


async def transfer(loop: asyncio.AbstractEventLoop, src: socket.socket, dst: socket.socket) -> None:
    """Copia bytes de src -> dst até EOF/erro."""
    while True:
        try:
            data = await loop.sock_recv(src, BUF_SIZE)
            if not data:
                break
            await loop.sock_sendall(dst, data)
        except (ConnectionError, OSError):
            break
    with contextlib.suppress(Exception):
        dst.shutdown(socket.SHUT_WR)


class AsyncProxyIdentico:
    """Servidor proxy assíncrono que replica a lógica do main.rs."""
    def __init__(self, port: int, status: str) -> None:
        self.port = port
        self.status = status
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._listen_sock: Optional[socket.socket] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        # Socket IPv6, sem alterar IPV6_V6ONLY (igual ao Rust)
        listen_sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind(("::", self.port))
        listen_sock.listen(128)
        listen_sock.setblocking(False)
        self._listen_sock = listen_sock

        try:
            while not self._stop_event.is_set():
                try:
                    client_sock, _addr = await self.loop.sock_accept(listen_sock)
                except asyncio.CancelledError:
                    break
                except OSError:
                    # Pode ocorrer quando fechamos o listener para encerrar
                    continue
                client_sock.setblocking(False)
                asyncio.create_task(self._handle_client(client_sock))
        finally:
            if self._listen_sock:
                with contextlib.suppress(Exception):
                    self._listen_sock.close()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._listen_sock:
            with contextlib.suppress(Exception):
                # Fechar força sock_accept a sair
                self._listen_sock.close()
                self._listen_sock = None

    async def _handle_client(self, client_sock: socket.socket) -> None:
        assert self.loop is not None
        try:
            # 1) 101
            await self.loop.sock_sendall(
                client_sock, f"HTTP/1.1 101 {self.status}\r\n\r\n".encode()
            )
            # 2) 200  (ajuste: agora enviamos o 200 imediatamente, como no Rust)
            await self.loop.sock_sendall(
                client_sock, f"HTTP/1.1 200 {self.status}\r\n\r\n".encode()
            )
            # 3) preconsume
            with contextlib.suppress(ConnectionError, OSError):
                _ = await self.loop.sock_recv(client_sock, PRECONSUME)
            # 4) peek
            ok, data = await peek_stream(client_sock, timeout=PEEK_TIMEOUT, bufsize=BUF_SIZE)
            if ok and (not data or (b"SSH" in data)):
                upstream_addr = UPSTREAM_SSH
            elif ok:
                upstream_addr = UPSTREAM_OTHER
            else:
                upstream_addr = UPSTREAM_SSH

            # 5) conecta no upstream e faz a ponte
            upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            upstream.setblocking(False)
            try:
                await self.loop.sock_connect(upstream, upstream_addr)
            except Exception:
                upstream.close()
                return

            t1 = asyncio.create_task(transfer(self.loop, client_sock, upstream))
            t2 = asyncio.create_task(transfer(self.loop, upstream, client_sock))
            try:
                await asyncio.gather(t1, t2)
            finally:
                with contextlib.suppress(Exception):
                    upstream.close()
        except asyncio.CancelledError:
            pass
        except Exception:
            # silencioso (compatível com o estilo do Rust que ignora certos erros)
            pass
        finally:
            with contextlib.suppress(Exception):
                client_sock.close()


class ProxyController:
    """Controla o proxy em *thread* separada e expõe status/erros."""
    def __init__(self, status: str):
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[AsyncProxyIdentico] = None
        self.port: Optional[int] = None
        self.status: str = status
        self.error_message: Optional[str] = None
        self._server_task: Optional[asyncio.Task] = None

    def start(self, port: int) -> bool:
        """Inicia o proxy em background. Retorna True se iniciou."""
        if self.is_running():
            return True
        self.port = port
        self.error_message = None

        started = threading.Event()
        result_ok = False  # atualizado pela thread

        def run():
            nonlocal result_ok
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._server = AsyncProxyIdentico(port, self.status)

                async def boot():
                    # cria tarefa do servidor
                    self._server_task = self._loop.create_task(self._server.start())
                    # pequena janela para o bind acontecer
                    await asyncio.sleep(0)
                    return True

                # Se o bind falhar, o start() levanta exceção aqui
                self._loop.run_until_complete(boot())
                result_ok = True
            except Exception as e:
                self.error_message = str(e)
                result_ok = False
                started.set()
                return

            started.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    if self._server_task and not self._server_task.done():
                        self._server_task.cancel()
                finally:
                    with contextlib.suppress(Exception):
                        if self._loop.is_running():
                            self._loop.stop()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

        # aguarda sinalização (com *timeout* para não travar)
        started.wait(timeout=5.0)
        if not result_ok and not self.error_message:
            self.error_message = "Falha ao iniciar o servidor (tempo excedido)."

        return result_ok

    def stop(self) -> None:
        """Encerra o proxy se estiver rodando."""
        if not self.is_running() or not self._loop or not self._server:
            return

        def shutdown_sequence():
            if self._server_task and not self._server_task.done():
                self._server_task.cancel()

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
