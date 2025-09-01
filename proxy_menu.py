#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy TCP com menu interativo (bonito) em terminal.

Recursos:
- Abrir porta: inicia o proxy escutando em uma porta (dual‑stack IPv6/IPv4).
- Fechar porta: encerra o proxy e libera a porta.
- Remover Proxy: desativa o serviço (fecha todas as portas ativas, se houver),
  remove o próprio arquivo do disco e encerra o programa.
- Sair: sai do menu mantendo o proxy ativo se ainda houver portas ativas.

Notas de implementação:
- Usa asyncio em um thread dedicado para o servidor, enquanto o menu roda na thread principal. As operações de abrir/fechar são despachadas com run_coroutine_threadsafe para o event loop do servidor.
- O handshake imita o comportamento descrito: envia "HTTP/1.1 101 <status>", espia dados por até ~1s (MSG_PEEK) para decidir destino, depois envia "HTTP/1.1 200 <status>" e inicia o túnel bidirecional.
- Decisão de destino: se "SSH" está nos bytes iniciais (ou nenhum dado) -> 0.0.0.0:22, senão -> 0.0.0.0:1194.
"""

import asyncio
import socket
import threading
import time
from typing import Optional, Tuple, List
from concurrent.futures import Future

# ============================ Núcleo do Proxy ============================
BUF_SIZE = 8192


async def transfer_data(loop: asyncio.AbstractEventLoop, src: socket.socket, dst: socket.socket) -> None:
    """Copia bytes de src -> dst até EOF."""
    src.setblocking(False)
    dst.setblocking(False)
    try:
        while True:
            data = await loop.sock_recv(src, BUF_SIZE)
            if not data:
                break
            await loop.sock_sendall(dst, data)
    except (ConnectionError, OSError):
        pass


async def bidirectional_proxy(loop: asyncio.AbstractEventLoop, a: socket.socket, b: socket.socket) -> None:
    """Encaminha dados nos dois sentidos até uma das metades fechar."""
    await asyncio.gather(
        transfer_data(loop, a, b),
        transfer_data(loop, b, a),
        return_exceptions=True
    )
    try:
        a.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        b.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    a.close()
    b.close()


async def peek_stream(sock: socket.socket, timeout: float = 1.0) -> Tuple[bool, bytes]:
    """
    Espia bytes disponíveis (sem consumir) por até 'timeout' segundos.
    Retorna (ok, dados).
    """
    sock.setblocking(False)
    start = time.time()
    last_err = None
    while (time.time() - start) < timeout:
        try:
            data = sock.recv(1024, socket.MSG_PEEK)
            return True, data
        except (BlockingIOError, InterruptedError) as e:
            last_err = e
            await asyncio.sleep(0.05)
        except OSError as e:
            last_err = e
            break
    return False, b''


async def handle_client(loop: asyncio.AbstractEventLoop, client_sock: socket.socket, status_banner: str) -> None:
    """Atende um cliente: handshake 101/200, detecção de destino e túnel."""
    client_sock.setblocking(False)
    # 101
    try:
        await loop.sock_sendall(client_sock, f"HTTP/1.1 101 {status_banner}\r\n\r\n".encode())
    except (ConnectionError, OSError):
        client_sock.close()
        return

    ok, data = await peek_stream(client_sock, timeout=1.0)

    if ok and (not data or b"SSH" in data.upper()):
        upstream_host, upstream_port = "0.0.0.0", 22
    elif ok:
        upstream_host, upstream_port = "0.0.0.0", 1194
    else:
        upstream_host, upstream_port = "0.0.0.0", 22  # fallback

    # Conecta no servidor de destino
    upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    upstream.setblocking(False)
    try:
        await loop.sock_connect(upstream, (upstream_host, upstream_port))
    except (ConnectionError, OSError):
        upstream.close()
        client_sock.close()
        return

    # 200
    try:
        await loop.sock_sendall(client_sock, f"HTTP/1.1 200 {status_banner}\r\n\r\n".encode())
    except (ConnectionError, OSError):
        upstream.close()
        client_sock.close()
        return

    # Começa o túnel
    await bidirectional_proxy(loop, client_sock, upstream)


class ProxyServer:
    """
    Controla socket de escuta dual-stack e laço de accept.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, status_banner: str = "@RustyManager"):
        self.loop = loop
        self.status_banner = status_banner
        self.listen_sock: Optional[socket.socket] = None
        self.listen_port: Optional[int] = None
        self.accept_task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

    async def open(self, port: int) -> None:
        if self.accept_task and not self.accept_task.done():
            # já ativo em outra porta -> fecha antes
            await self.close()

        # Socket IPv6 dual-stack (quando suportado)
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            # Sistemas que não suportam desabilitar V6ONLY
            pass
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::", port))
        s.listen(200)
        s.setblocking(False)

        self.listen_sock = s
        self.listen_port = port
        self._stop_evt = asyncio.Event()

        async def accept_loop():
            while not self._stop_evt.is_set():
                try:
                    client, _addr = await self.loop.sock_accept(self.listen_sock)
                except (OSError, ConnectionError):
                    if self._stop_evt.is_set():
                        break
                    await asyncio.sleep(0.05)
                    continue
                # cria tarefa para o cliente
                self.loop.create_task(handle_client(self.loop, client, self.status_banner))
            # fechar o socket quando sair
            if self.listen_sock:
                try:
                    self.listen_sock.close()
                except OSError:
                    pass

        self.accept_task = self.loop.create_task(accept_loop())

    async def close(self) -> None:
        # encerra accept loop e fecha socket
        self._stop_evt.set()
        if self.listen_sock:
            try:
                self.listen_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.listen_sock.close()
            except OSError:
                pass
            self.listen_sock = None
        if self.accept_task:
            try:
                await asyncio.wait_for(self.accept_task, timeout=1.5)
            except asyncio.TimeoutError:
                self.accept_task.cancel()
        self.listen_port = None

    def is_active(self) -> bool:
        return (
            self.listen_sock is not None
            and self.listen_port is not None
            and self.accept_task
            and not self.accept_task.done()
        )


# =========================== Controller em Thread ===========================
class ProxyController:
    """
    Executa o event loop do servidor em background e expõe métodos thread‑safe.

    Nesta versão, a thread de servidor não é marcada como daemon para que,
    se o menu for encerrado com o proxy ativo, a aplicação continue
    executando e mantendo o socket de escuta aberto. O menu em si termina,
    mas a thread do servidor permanece em execução.
    """

    def __init__(self, status_banner: str = "@RustyManager"):
        # Cria a thread do event loop. Não usamos daemon=True para que o servidor
        # continue rodando mesmo após a finalização do menu interativo.
        self.thread = threading.Thread(target=self._thread_main, daemon=False)
        self.loop_ready = threading.Event()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server: Optional[ProxyServer] = None
        self.status_banner = status_banner
        self.thread.start()
        self.loop_ready.wait()

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.server = ProxyServer(loop, status_banner=self.status_banner)
        self.loop_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.stop()
            loop.close()

    def _submit(self, coro) -> Future:
        if not self.loop:
            raise RuntimeError("Event loop não inicializado")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def abrir_porta(self, port: int) -> None:
        fut = self._submit(self.server.open(port))
        fut.result()

    def fechar_porta(self) -> None:
        fut = self._submit(self.server.close())
        fut.result()

    def ativo(self) -> bool:
        return self.server.is_active() if self.server else False

    def porta(self) -> Optional[int]:
        return self.server.listen_port if self.server else None


# ================================ CLI Bonita ================================
# Códigos ANSI para formatação de cor e estilo
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CYAN = "\033[36m"
YELL = "\033[33m"

# Função para limpar a tela e reposicionar o cursor no canto superior esquerdo.
# Esta função é usada para garantir que o cabeçalho (status) esteja sempre visível
# no topo da tela a cada iteração do menu.
def clear():
    # Sequência ANSI: \033[2J limpa a tela, \033[H move o cursor para (1,1)
    print("\033[2J\033[H", end="")


def banner_status(ctrl: ProxyController) -> str:
    if ctrl.ativo():
        return f"{GREEN}ATIVO{RESET} na porta {BOLD}{ctrl.porta()}{RESET}"
    return f"{RED}INATIVO{RESET}"


def print_header(ctrl: ProxyController):
    """
    Exibe o cabeçalho do menu, incluindo o status atual do proxy. Sempre
    limpa a tela antes de imprimir para manter o status visível e
    atualizado conforme mudanças de porta e estado.
    """
    clear()
    # Linha de título e descrição
    print(BOLD + "Rusty Proxy (Python)" + RESET + "  •  " + DIM + "Dual-stack IPv6/IPv4" + RESET)
    # Linha de status
    print("Status: " + banner_status(ctrl))
    # Linha divisória
    print("-" * 60)


def input_int(prompt: str, minimo: int = 1, maximo: int = 65535) -> Optional[int]:
    try:
        v = int(input(prompt).strip())
        if v < minimo or v > maximo:
            print(f"{RED}Porta inválida (1-65535).{RESET}")
            return None
        return v
    except ValueError:
        print(f"{RED}Digite um número válido.{RESET}")
        return None


def remover_proxy(ctrl: ProxyController) -> None:
    """
    Desativa o proxy em todas as portas (se estiver ativo), remove o arquivo
    atual do disco e encerra o programa. Este método é chamado pela opção
    "Remover Proxy" do menu principal.
    """
    import os
    import sys
    from pathlib import Path
    # Imprime cabeçalho atualizado
    print_header(ctrl)
    # Tenta fechar a porta ativa, se houver
    try:
        if ctrl.ativo():
            ctrl.fechar_porta()
            print_header(ctrl)
            print(f"{GREEN}Proxy desativado com sucesso.{RESET}")
    except Exception as e:
        print_header(ctrl)
        print(f"{YELL}Aviso ao desativar proxy: {e}{RESET}")

    # Tenta parar o loop de eventos para encerrar a thread do servidor
    try:
        # Envia um sinal para parar o loop se estiver rodando
        if getattr(ctrl, 'loop', None):
            ctrl.loop.call_soon_threadsafe(ctrl.loop.stop)
    except Exception:
        pass
    # Remove o próprio arquivo
    try:
        me = Path(__file__).resolve()
        # Garante que o arquivo exista antes de remover
        if me.exists():
            me.unlink()
            print(f"{GREEN}Arquivo removido: {me}{RESET}")
    except Exception as e:
        print(f"{RED}Falha ao remover arquivo: {e}{RESET}")
    # Encerra o processo
    sys.exit(0)


def menu_principal(ctrl: ProxyController):
    """
    Loop principal de interação. O status do proxy é exibido de forma
    permanente no cabeçalho. Permite abrir/fechar porta, atualizar
    manualmente o status e sair a qualquer momento sem encerrar o servidor.
    """
    while True:
        # imprime cabeçalho com status dinâmico
        print_header(ctrl)
        # opções do menu
        print(f"{BOLD}1){RESET} Abrir porta")
        print(f"{BOLD}2){RESET} Fechar porta")
        print(f"{BOLD}3){RESET} Remover Proxy")
        print(f"{BOLD}0){RESET} Sair (o proxy continua ativo se estiver ATIVO)")
        op = input(f"\n{CYAN}Selecione uma opção:{RESET} ").strip()
        if op == "1":
            # Abrir porta
            porta = None
            while porta is None:
                print_header(ctrl)
                porta = input_int("Informe a porta para escutar: ")
            try:
                ctrl.abrir_porta(porta)
                print_header(ctrl)
                print(f"{GREEN}Proxy iniciado na porta {porta}.{RESET}")
            except Exception as e:
                print_header(ctrl)
                print(f"{RED}Falha ao abrir porta: {e}{RESET}")
            input(DIM + "\nPressione Enter para continuar..." + RESET)
        elif op == "2":
            # Fechar porta
            print_header(ctrl)
            if not ctrl.ativo():
                print(f"{YELL}Proxy já está inativo.{RESET}")
            else:
                try:
                    ctrl.fechar_porta()
                    print_header(ctrl)
                    print(f"{GREEN}Proxy encerrado com sucesso.{RESET}")
                except Exception as e:
                    print_header(ctrl)
                    print(f"{RED}Falha ao fechar: {e}{RESET}")
            input(DIM + "\nPressione Enter para continuar..." + RESET)
        elif op == "3":
            # Desativa o proxy, remove o arquivo e encerra
            remover_proxy(ctrl)
        elif op == "0":
            # Sair sem encerrar o servidor se estiver ativo
            print_header(ctrl)
            if ctrl.ativo():
                print(f"{YELL}Saindo do menu, mas o proxy permanecerá {banner_status(ctrl)}.{RESET}")
            else:
                print(f"{DIM}Encerrando menu.{RESET}")
            break
        else:
            print_header(ctrl)
            print(f"{RED}Opção inválida.{RESET}")
            time.sleep(0.8)


def submenu_porta(ctrl: ProxyController):
    while True:
        print_header(ctrl)
        print(f"{BOLD}1){RESET} Abrir porta")
        print(f"{BOLD}2){RESET} Fechar porta")
        print(f"{BOLD}9){RESET} Voltar")
        op = input(f"\n{CYAN}Selecione uma opção:{RESET} ").strip()
        if op == "1":
            porta = None
            while porta is None:
                porta = input_int("Informe a porta para escutar: ")
            try:
                ctrl.abrir_porta(porta)
                print(f"{GREEN}Proxy iniciado na porta {porta}.{RESET}")
            except Exception as e:
                print(f"{RED}Falha ao abrir porta: {e}{RESET}")
            time.sleep(0.8)
        elif op == "2":
            if not ctrl.ativo():
                print(f"{YELL}Proxy já está inativo.{RESET}")
            else:
                try:
                    ctrl.fechar_porta()
                    print(f"{GREEN}Proxy encerrado com sucesso.{RESET}")
                except Exception as e:
                    print(f"{RED}Falha ao fechar: {e}{RESET}")
            time.sleep(0.8)
        elif op == "9":
            return
        else:
            print(f"{RED}Opção inválida.{RESET}")


def main():
    """
    Ponto de entrada do script. Inicializa o controlador e inicia o menu.
    Após sair do menu, não encerra o servidor automaticamente; se o proxy
    estiver ativo, ele continuará rodando em background. Para encerrar
    completamente, execute o programa novamente e escolha a opção de
    "Fechar porta" ou encerre o processo manualmente.
    """
    ctrl = ProxyController(status_banner="@RustyManager")
    menu_principal(ctrl)
    # Não finalizamos o proxy automaticamente ao sair. O usuário pode
    # encerrar a porta posteriormente, se desejar.
    if ctrl.ativo():
        print(f"{YELL}Proxy permanece ativo na porta {ctrl.porta()}.{RESET}")
    else:
        print(f"{DIM}Nenhuma porta ativa. Programa encerrado.{RESET}")


if __name__ == "__main__":
    main()