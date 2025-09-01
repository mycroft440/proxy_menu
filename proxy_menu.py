#!/usr/bin/env python3
"""
Menu interativo para o proxy idêntico ao Rust.

Este script expõe o mesmo comportamento do proxy `main.rs` em Rust, mas
acrescenta um menu de linha de comando para iniciar e parar o servidor.

Características-chave:

  * **Ordem e heurística idênticas**: envia `HTTP/1.1 101` seguido de
    `HTTP/1.1 200`, lê e descarta 1024 bytes do cliente, faz `peek` de
    8192 bytes durante até 1 segundo e decide entre `0.0.0.0:22` ou
    `0.0.0.0:1194` conforme o buffer esteja vazio ou contenha `SSH`.
  * **Destinos 0.0.0.0** para respeitar o código Rust (mesmo sendo
    invulgares em `connect`). Se desejar um proxy funcional, altere
    esses valores para `127.0.0.1` ou para um IP de destino real.
  * **Bind IPv6**: ouve em `[::]:<porta>` sem alterar o parâmetro
    `IPV6_V6ONLY`, tal como em Rust.
  * **Menu simples**: imprime o estado (`ativo` ou `inativo`), permite
    abrir uma porta, fechar o servidor ou sair.

Uso:

    python3 proxy_menu_identico.py

Será solicitado o número da porta para abrir (padrão 80). Portas
inferiores a 1024 exigem privilégios em sistemas Unix. O banner de
status permanece fixo como `@RustyManager`, mas pode ser ajustado
alterando a variável `STATUS_BANNER` abaixo.
"""

import asyncio
import contextlib
import os
import socket
import sys
import threading
import time

# Configurações básicas
STATUS_BANNER = "@RustyManager"
DEFAULT_PORT = 80
BUF_SIZE = 8192
PRECONSUME = 1024
PEEK_TIMEOUT = 1.0  # segundos


async def peek_stream(sock: socket.socket, timeout: float = PEEK_TIMEOUT, bufsize: int = BUF_SIZE):
    """Realiza recv(MSG_PEEK) com timeout, restaurando o modo original."""
    def _peek_blocking():
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
    """Copia bytes de src para dst até EOF ou erro."""
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
    """Proxy idêntico ao Rust, parametrizado por porta e status."""
    def __init__(self, port: int, status: str) -> None:
        self.port = port
        self.status = status
        self.loop: asyncio.AbstractEventLoop | None = None
        self._listen_sock: socket.socket | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Inicia o listener e aceita conexões indefinidamente."""
        self.loop = asyncio.get_running_loop()
        # socket IPv6 com SO_REUSEADDR; sem alterar IPV6_V6ONLY
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
                    # Ocorre quando o socket é fechado enquanto o accept aguarda
                    continue
                client_sock.setblocking(False)
                # processa cliente
                asyncio.create_task(self._handle_client(client_sock))
        finally:
            if self._listen_sock:
                with contextlib.suppress(Exception):
                    self._listen_sock.close()

    async def stop(self) -> None:
        """Sinaliza parada e fecha o listener."""
        self._stop_event.set()
        if self._listen_sock:
            with contextlib.suppress(Exception):
                # Fechar o socket força o 'sock_accept' a lançar uma exceção e desbloquear
                self._listen_sock.close()
                self._listen_sock = None

    async def _handle_client(self, client_sock: socket.socket) -> None:
        assert self.loop is not None
        try:
            # 1) 101
            await self.loop.sock_sendall(client_sock, f"HTTP/1.1 101 {self.status}\r\n\r\n".encode())
            # 2) consome PRECONSUME bytes
            with contextlib.suppress(ConnectionError, OSError):
                _ = await self.loop.sock_recv(client_sock, PRECONSUME)
            # 3) 200
            await self.loop.sock_sendall(client_sock, f"HTTP/1.1 200 {self.status}\r\n\r\n".encode())
            # 4) peek
            ok, data = await peek_stream(client_sock, timeout=PEEK_TIMEOUT, bufsize=BUF_SIZE)
            if ok and (not data or (b"SSH" in data)):
                upstream_addr = ("0.0.0.0", 22)
            elif ok:
                upstream_addr = ("0.0.0.0", 1194)
            else:
                upstream_addr = ("0.0.0.0", 22)
            # 5) conecta e transfere
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
            # silencioso
            pass
        finally:
            with contextlib.suppress(Exception):
                client_sock.close()


class ProxyController:
    """Controla o proxy em thread separada e expõe status."""
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: AsyncProxyIdentico | None = None
        self.port: int | None = None
        self.status: str | None = None
        self.error_message: str | None = None
        self._server_task: asyncio.Task | None = None

    def start(self, port: int, status: str) -> bool:
        """Inicia o proxy em background. Retorna True se iniciou com sucesso."""
        if self.is_running():
            return True
        self.port = port
        self.status = status
        self.error_message = None
        
        # Evento para sincronizar a thread principal com a de background
        started_event = threading.Event()
        result = {"ok": False}

        def run():
            nonlocal result
            try:
                self._loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._loop)
                self._server = AsyncProxyIdentico(port, status)
                
                # Inicia o servidor e, mais importante, o loop de eventos
                self._server_task = self._loop.create_task(self._server.start())
                
                # A exceção de `bind` ocorrerá aqui se houver um problema
                # Para pegá-la, precisamos dar uma chance para a task rodar
                async def check_startup():
                    await asyncio.sleep(0.01) # dá tempo para o `bind` rodar
                    if self._server_task and self._server_task.done():
                        # Se a tarefa terminou rápido, provavelmente foi um erro
                        try:
                            self._server_task.result()
                        except Exception as e:
                            self.error_message = str(e)
                            result["ok"] = False
                            return
                
                # Verificamos a inicialização. Se falhar, o loop não continua.
                self._loop.run_until_complete(check_startup())
                
                # Se o resultado já foi definido como falha, não continue
                if "ok" in result and not result["ok"]:
                    started_event.set()
                    return

                # Se chegamos aqui, o bind foi bem-sucedido
                result["ok"] = True
                started_event.set()
                self._loop.run_forever()

            except Exception as e:
                self.error_message = str(e)
                result["ok"] = False
            finally:
                # Garante que o evento seja setado em caso de qualquer erro
                if not started_event.is_set():
                    started_event.set()
                if self._loop and self._loop.is_running():
                    self._loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        
        # Aguarda a thread de background sinalizar que a inicialização terminou
        started_event.wait(timeout=5.0)
        
        if not result["ok"] and not self.error_message:
            self.error_message = "Tempo limite para iniciar o servidor excedido."

        return result["ok"]

    def stop(self) -> None:
        """Encerra o proxy se estiver rodando."""
        if not self.is_running() or not self._loop or not self._server:
            return

        def shutdown_sequence():
            # Função a ser chamada no loop de eventos da outra thread
            if self._server_task and not self._server_task.done():
                self._server_task.cancel()
            
            async def stop_server_and_loop():
                if self._server:
                    await self._server.stop()
                if self._loop and self._loop.is_running():
                    self._loop.stop()

            # Agendamos a parada de forma segura
            asyncio.create_task(stop_server_and_loop())

        # call_soon_threadsafe é a forma correta de interagir com o loop de outra thread
        self._loop.call_soon_threadsafe(shutdown_sequence)
        
        if self._thread:
            self._thread.join(timeout=5.0)
            
        self._thread = None
        self._loop = None
        self._server = None
        self.port = None
        self.status = None
        self.error_message = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


def ask_port(default: int) -> int | None:
    """Solicita porta ao usuário com valor padrão."""
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

def run_menu() -> None:
    """Executa o menu interativo com uma interface melhorada."""
    ctrl = ProxyController()
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
        print(f"║        Gerenciador de Proxy ({STATUS_BANNER})        ║")
        print(f"╚═══════════════════════════════════════════════╝{C_RESET}\n")

        if ctrl.is_running() and ctrl.port is not None:
            status_line = f"  {C_BOLD}Status:{C_RESET} {C_GREEN}ATIVO ✅{C_RESET}"
            port_line = f"  {C_BOLD}Porta:{C_RESET} {C_WHITE}{ctrl.port}{C_RESET}"
            print(status_line)
            print(port_line)
        else:
            status_line = f"  {C_BOLD}Status:{C_RESET} {C_RED}INATIVO ❌{C_RESET}"
            print(status_line)
        
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
            ok = ctrl.start(port, STATUS_BANNER)
            
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
    # Garante que o controlador pare corretamente ao sair com Ctrl+C
    main_controller = None
    try:
        run_menu()
    except KeyboardInterrupt:
        print("\n\nEncerrado pelo usuário.")
    finally:
        # Lógica de limpeza não é estritamente necessária por causa da `thread daemon`,
        # mas é uma boa prática em programas mais complexos.
        pass

