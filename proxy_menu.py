#!/usr/bin/env python3
"""
Menu interativo para um proxy com encaminhamento direto para SSH.

Esta versão foi simplificada para remover a inspeção de dados ('peek')
e encaminhar TODO o tráfego diretamente para a porta 22, tornando
a conexão mais robusta e direta para túneis SSH.
"""

import argparse
import asyncio
import os
import sys
import threading
import time
import socket
from typing import Optional

# ==== Parâmetros ====
DEFAULT_STATUS = "@RustyManager"
DEFAULT_PORT = 80
BUF_SIZE = 8192
PRECONSUME = 1024
# O destino agora é fixo
UPSTREAM_SSH = ("0.0.0.0", 22)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--status", default=DEFAULT_STATUS, help="Texto do status a enviar nas respostas 101/200")
    return p.parse_args()


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
        """Manipula o cliente, encaminhando diretamente para SSH."""
        client_sock = writer.get_extra_info('socket')
        client_sock.setblocking(False)
        loop = asyncio.get_running_loop()
        
        upstream_writer = None
        try:
            # 1) Responde ao handshake HTTP do cliente
            response_101 = f"HTTP/1.1 101 {self.status}\r\n\r\n"
            writer.write(response_101.encode())
            await writer.drain()
            
            # 2) Lê e descarta o payload inicial
            await reader.read(PRECONSUME)

            response_200 = f"HTTP/1.1 200 {self.status}\r\n\r\n"
            writer.write(response_200.encode())
            await writer.drain()

            # 3) Conecta-se diretamente ao destino SSH
            upstream_reader, upstream_writer = await asyncio.open_connection(
                UPSTREAM_SSH[0], UPSTREAM_SSH[1]
            )
            upstream_sock = upstream_writer.get_extra_info('socket')
            upstream_sock.setblocking(False)

            # 4) Inicia a transferência bidirecional de dados
            # A transferência agora é direta, sem necessidade de 'peek'
            task1 = asyncio.create_task(self.transfer_low_level(loop, client_sock, upstream_sock))
            task2 = asyncio.create_task(self.transfer_low_level(loop, upstream_sock, client_sock))

            await asyncio.gather(task1, task2)

        except Exception:
            pass
        finally:
            writer.close()
            # CORREÇÃO: Devemos fechar o StreamWriter, não o socket de baixo nível.
            if upstream_writer:
                upstream_writer.close()

    async def transfer_low_level(self, loop: asyncio.AbstractEventLoop, r_sock: socket.socket, w_sock: socket.socket):
        """Transfere dados entre sockets de forma eficiente."""
        try:
            while True:
                data = await loop.sock_recv(r_sock, BUF_SIZE)
                if not data:
                    break
                await loop.sock_sendall(w_sock, data)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass # Erros esperados quando uma conexão é fechada
        finally:
            pass

    async def stop(self):
        """Para o servidor"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        self.running = False


# O resto do código (ProxyController, run_menu, etc.) permanece o mesmo.
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
                if not self._started_event.is_set(): self._started_event.set()
        self._thread = threading.Thread(target=run_proxy, daemon=True)
        self._thread.start()
        started = self._started_event.wait(timeout=5.0)
        if not started and not self.error_message:
            self.error_message = "Falha ao iniciar o servidor (timeout)"
        return result["success"]

    def stop(self) -> None:
        if not self.is_running() or not self._loop: return
        async def do_stop():
            if self._proxy: await self._proxy.stop()
            if self._loop.is_running(): self._loop.stop()
        future = asyncio.run_coroutine_threadsafe(do_stop(), self._loop)
        try:
            future.result(timeout=3.0)
        except (TimeoutError, asyncio.TimeoutError): pass
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
        print(f"  {C_RED}[3]{C_RESET} - Remover Proxy")
        print(f"  {C_YELLOW}[0]{C_RESET} - Sair\n")
        choice = input(f"  {C_CYAN}› Selecione uma opção: {C_RESET}").strip()
        if choice == "1":
            if ctrl.is_running():
                print(f"\n  {C_YELLOW}💡 O proxy já está ativo.{C_RESET}"); time.sleep(2); continue
            port = ask_port(default_port)
            if port is None: continue
            print(f"\n  {C_WHITE}Iniciando o proxy na porta {port}...{C_RESET}")
            ok = ctrl.start(port)
            if ok:
                print(f"  {C_GREEN}✔ Proxy iniciado com sucesso.{C_RESET}"); default_port = port; time.sleep(2.5)
            else:
                print(f"  {C_RED}✘ Falha ao iniciar o proxy.{C_RESET}")
                if ctrl.error_message: print(f"    {C_RED}Motivo: {ctrl.error_message}{C_RESET}")
                time.sleep(4)
        elif choice == "2":
            if not ctrl.is_running(): print(f"\n  {C_YELLOW}💡 O proxy já está inativo.{C_RESET}")
            else: print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}"); ctrl.stop(); print(f"  {C_GREEN}✔ Proxy interrompido.{C_RESET}")
            time.sleep(2)
        elif choice == "3":
            print(f"\n  {C_RED}{C_BOLD}ATENÇÃO: Ação irreversível.{C_RESET}")
            if input(f"  {C_CYAN}› Deseja realmente remover o script? (s/N): {C_RESET}").strip().lower() == 's':
                if ctrl.is_running(): print(f"\n  {C_WHITE}Parando o proxy...{C_RESET}"); ctrl.stop(); time.sleep(1)
                try:
                    script_path = os.path.abspath(__file__)
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
    args = parse_args()
    try:
        run_menu(status=args.status)
    except KeyboardInterrupt:
        print("\n\nEncerrado pelo utilizador.")

