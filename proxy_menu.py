#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy com menu e múltiplas portas:
 - Fluxo por conexão (compatível com main.rs):
   1) "HTTP/1.1 101 <status>\r\n\r\n"
   2) consome 1024 bytes do cliente (sem timeout)
   3) "HTTP/1.1 200 <status>\r\n\r\n"
   4) peek (1s); sem dados ou contém b"SSH" => SSH, senão => OVPN
   5) túnel bidirecional
 - Menu:
   1) Abrir porta
   2) Fechar porta
   3) Desativar todas as portas e remover COMPLETAMENTE este arquivo
   0) Sair
 - Status no topo:
   "status: ativo p.: 80, 8080"   ou   "status: Inativo"

Observação: portas <1024 exigem privilégios em Unix (sudo).
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

# =========================
# Configuração padrão
# =========================
@dataclass
class ProxyConfig:
    listen_port: int = 80
    status_banner: str = "@RustyManager"
    ssh_host: str = "127.0.0.1"
    ssh_port: int = 22
    ovpn_host: str = "127.0.0.1"
    ovpn_port: int = 1194
    backlog: int = 128
    dual_stack: bool = True          # tenta IPv6 com V6ONLY=0 (aceita IPv4 também)
    buf_size: int = 8192
    sniff_timeout: float = 1.0       # timeout do peek (s)
    preconsume: int = 1024           # bytes descartados logo após o 101

# =========================
# Servidor assíncrono (1 porta)
# =========================
class AsyncProxyServer:
    def __init__(self, cfg: ProxyConfig) -> None:
        self.cfg = cfg
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping = asyncio.Event()
        self._socks: List[socket.socket] = []
        self._accept_tasks: List[asyncio.Task] = []
        self._client_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._socks = []
        errors = []

        # Tenta IPv6 dual-stack
        if self.cfg.dual_stack:
            try:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with contextlib.suppress(OSError, AttributeError):
                    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                s6.bind(("::", self.cfg.listen_port))
                s6.listen(self.cfg.backlog)
                s6.setblocking(False)
                self._socks.append(s6)
            except OSError as e:
                errors.append(("IPv6 dual-stack", e))

        # Fallback: sockets separados
        if not self._socks:
            try:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with contextlib.suppress(OSError, AttributeError):
                    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                s6.bind(("::", self.cfg.listen_port))
                s6.listen(self.cfg.backlog)
                s6.setblocking(False)
                self._socks.append(s6)
            except OSError as e:
                errors.append(("IPv6 only", e))

            try:
                s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s4.bind(("0.0.0.0", self.cfg.listen_port))
                s4.listen(self.cfg.backlog)
                s4.setblocking(False)
                self._socks.append(s4)
            except OSError as e:
                errors.append(("IPv4", e))

        if not self._socks:
            for where, err in errors:
                print(f"[ERRO] {where}: {err}")
            raise RuntimeError("Não foi possível iniciar o listener.")

        self._accept_tasks = [asyncio.create_task(self._accept_loop(s)) for s in self._socks]

    async def shutdown(self) -> None:
        self._stopping.set()
        for s in self._socks:
            with contextlib.suppress(Exception):
                s.close()
        self._socks.clear()

        for t in self._accept_tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._accept_tasks, return_exceptions=True)
        self._accept_tasks.clear()

        if self._client_tasks:
            for t in list(self._client_tasks):
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()

    async def _accept_loop(self, listen_sock: socket.socket) -> None:
        assert self.loop is not None
        while not self._stopping.is_set():
            try:
                client_sock, addr = await self.loop.sock_accept(listen_sock)
            except asyncio.CancelledError:
                break
            except OSError:
                if self._stopping.is_set():
                    break
                continue

            client_sock.setblocking(False)
            task = asyncio.create_task(self._handle_client(client_sock, addr))
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(self, client_sock: socket.socket, addr) -> None:
        assert self.loop is not None
        cfg = self.cfg
        try:
            # 1) 101
            await self.loop.sock_sendall(
                client_sock, f"HTTP/1.1 101 {cfg.status_banner}\r\n\r\n".encode()
            )
            # 2) consome 1024 bytes (sem timeout)
            with contextlib.suppress(ConnectionError, OSError):
                _ = await self.loop.sock_recv(client_sock, cfg.preconsume)
            # 3) 200
            await self.loop.sock_sendall(
                client_sock, f"HTTP/1.1 200 {cfg.status_banner}\r\n\r\n".encode()
            )
            # 4) peek (timeout)
            ok, data = await self._peek_stream(client_sock, cfg.sniff_timeout, cfg.buf_size)

            # 5) roteamento (igual ao Rust)
            if ok and (not data or (b"SSH" in data)):
                upstream_host, upstream_port = cfg.ssh_host, cfg.ssh_port
            elif ok:
                upstream_host, upstream_port = cfg.ovpn_host, cfg.ovpn_port
            else:
                upstream_host, upstream_port = cfg.ssh_host, cfg.ssh_port

            upstream = await self._connect_upstream(upstream_host, upstream_port)
            await self._bidirectional_proxy(client_sock, upstream)

        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                client_sock.close()

    async def _peek_stream(self, sock: socket.socket, timeout: float, buf_size: int) -> Tuple[bool, bytes]:
        """
        Faz recv(MSG_PEEK) com timeout em thread. Restaura o modo original do socket
        (fundamental para manter non-blocking no asyncio).
        """
        def _peek_blocking() -> Tuple[bool, bytes]:
            orig_to = sock.gettimeout()  # 0.0 => non-blocking; None => blocking; >0 => blocking com timeout
            try:
                sock.settimeout(timeout)   # bloqueante com timeout
                data = sock.recv(buf_size, socket.MSG_PEEK)
                return True, data
            except socket.timeout:
                return True, b""
            except OSError:
                return False, b""
            finally:
                # Restaura exatamente o estado original
                if orig_to == 0.0:
                    sock.setblocking(False)
                else:
                    sock.settimeout(orig_to)
        return await asyncio.to_thread(_peek_blocking)

    async def _connect_upstream(self, host: str, port: int) -> socket.socket:
        assert self.loop is not None
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setblocking(False)
        try:
            # Opcional: latência/keepalive
            with contextlib.suppress(OSError):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with contextlib.suppress(OSError):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            await self.loop.sock_connect(s, (host, port))
            return s
        except Exception:
            with contextlib.suppress(Exception):
                s.close()
            raise

    async def _transfer(self, src: socket.socket, dst: socket.socket) -> None:
        assert self.loop is not None
        while True:
            try:
                data = await self.loop.sock_recv(src, self.cfg.buf_size)
                if not data:
                    break
                await self.loop.sock_sendall(dst, data)
            except (ConnectionError, OSError):
                break
        with contextlib.suppress(Exception):
            dst.shutdown(socket.SHUT_WR)

    async def _bidirectional_proxy(self, a: socket.socket, b: socket.socket) -> None:
        t1 = asyncio.create_task(self._transfer(a, b))
        t2 = asyncio.create_task(self._transfer(b, a))
        try:
            await asyncio.gather(t1, t2)
        finally:
            with contextlib.suppress(Exception):
                a.close()
            with contextlib.suppress(Exception):
                b.close()

# =========================
# Controlador (1 porta) em thread
# =========================
class ServerController:
    def __init__(self, cfg: ProxyConfig) -> None:
        self.cfg = cfg
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[AsyncProxyServer] = None
        self._started = threading.Event()
        self._running_ok = False

    def start(self) -> bool:
        if self.is_running():
            return True

        def runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = AsyncProxyServer(self.cfg)

            async def _boot():
                ok = False
                try:
                    await self._server.start()
                    ok = True
                except Exception as e:
                    print(f"[ERRO] Porta {self.cfg.listen_port}: {e}")
                finally:
                    self._running_ok = ok
                    self._started.set()

            self._loop.create_task(_boot())
            try:
                self._loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop=self._loop)
                for t in pending:
                    t.cancel()
                with contextlib.suppress(Exception):
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()

        self._thread = threading.Thread(target=runner, name=f"proxy-{self.cfg.listen_port}", daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)
        return self._running_ok

    def stop(self) -> None:
        if not (self._thread and self._thread.is_alive()):
            self._running_ok = False
            return
        assert self._loop is not None and self._server is not None

        def _stopper():
            async def _inner():
                await self._server.shutdown()
                self._loop.stop()
            asyncio.create_task(_inner())

        self._loop.call_soon_threadsafe(_stopper)
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
        self._server = None
        self._running_ok = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._running_ok

# =========================
# Gerenciador de múltiplas portas
# =========================
class MultiServerManager:
    def __init__(self) -> None:
        self._by_port: Dict[int, ServerController] = {}

    def active_ports(self) -> List[int]:
        # Retorna somente as portas realmente ativas
        return sorted(p for p, c in self._by_port.items() if c.is_running())

    def open_port(self, port: int) -> bool:
        if port in self._by_port and self._by_port[port].is_running():
            return True
        cfg = ProxyConfig(listen_port=port)
        ctrl = ServerController(cfg)
        ok = ctrl.start()
        if ok:
            self._by_port[port] = ctrl
        else:
            # se já existia mas falhou agora, garante remoção
            self._by_port.pop(port, None)
        return ok

    def close_port(self, port: int) -> bool:
        ctrl = self._by_port.get(port)
        if not ctrl:
            return False
        ctrl.stop()
        self._by_port.pop(port, None)
        return True

    def close_all(self) -> None:
        for p, ctrl in list(self._by_port.items()):
            with contextlib.suppress(Exception):
                ctrl.stop()
        self._by_port.clear()

    def any_running(self) -> bool:
        return any(c.is_running() for c in self._by_port.values())

# =========================
# Util: remoção do próprio arquivo
# =========================
def remove_self_and_exit() -> None:
    """
    Tenta remover este arquivo imediatamente. Se falhar (ex.: Windows),
    agenda a remoção ao encerrar o processo.
    """
    path = os.path.realpath(__file__)
    try:
        os.remove(path)
        print(f"[OK] Arquivo removido: {path}")
        sys.exit(0)
    except Exception as e:
        if os.name == "nt":
            # Agenda remoção via .bat
            try:
                bat = os.path.join(tempfile.gettempdir(), "rm_proxy_menu.bat")
                with open(bat, "w", encoding="utf-8") as f:
                    f.write(f"""@echo off
ping 127.0.0.1 -n 2 >NUL
del /F /Q "{path}"
if exist "{path}" (
  timeout /T 2 >NUL
  del /F /Q "{path}"
)
del /F /Q "%~f0"
""")
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.Popen(["cmd", "/c", bat], creationflags=creationflags)
                print("[OK] Remoção agendada; o arquivo será apagado ao encerrar.")
                sys.exit(0)
            except Exception as e2:
                print(f"[ERRO] Falha ao agendar remoção: {e2}")
                sys.exit(1)
        else:
            # Unix-like: agenda remoção após 1s
            try:
                subprocess.Popen(["sh", "-c", f'sleep 1; rm -f "{path}"'])
                print("[OK] Remoção agendada; o arquivo será apagado ao encerrar.")
                sys.exit(0)
            except Exception as e2:
                print(f"[ERRO] Falha ao agendar remoção: {e2}")
                sys.exit(1)

# =========================
# Menu
# =========================
def ask_port(default_port: int) -> int:
    raw = input(f"Digite a porta para abrir [{default_port}]: ").strip()
    if not raw:
        return default_port
    try:
        val = int(raw)
        if not (1 <= val <= 65535):
            raise ValueError
        return val
    except ValueError:
        print("[ERRO] Porta inválida. Mantendo padrão.")
        return default_port

def run_menu() -> None:
    mgr = MultiServerManager()
    default_port = 80

    while True:
        ports = mgr.active_ports()
        if ports:
            print(f"\nstatus: ativo p.: {', '.join(str(p) for p in ports)}")
        else:
            print("\nstatus: Inativo")

        print("==============================")
        print("1) Abrir porta")
        print("2) Fechar porta")
        print("3) Desativar TODAS as portas e remover COMPLETAMENTE este arquivo")
        print("0) Sair")
        choice = input("Escolha: ").strip()

        if choice == "1":
            port = ask_port(default_port)
            ok = mgr.open_port(port)
            if ok:
                print(f"[OK] Porta {port} aberta.")
                default_port = port  # atualiza sugestão
                if port < 1024 and os.name != "nt":
                    print("    (Atenção: portas <1024 costumam exigir sudo em Unix)")
            else:
                print(f"[ERRO] Falhou ao abrir a porta {port}.")
                print("      Dicas: porta em uso, permissão insuficiente, firewall/antivírus.")
        elif choice == "2":
            if not ports:
                print("[INFO] Nenhuma porta ativa.")
                continue
            try:
                p = int(input(f"Porta para fechar [{ports[0]}]: ").strip() or ports[0])
            except ValueError:
                print("[ERRO] Porta inválida.")
                continue
            if mgr.close_port(p):
                print(f"[OK] Porta {p} fechada.")
            else:
                print(f"[INFO] Porta {p} não estava ativa.")
        elif choice == "3":
            resp = input("Confirmar: encerrar todas as portas e REMOVER este arquivo? (s/N): ").strip().lower()
            if resp == "s":
                mgr.close_all()
                print("[OK] Todas as portas foram desativadas.")
                # remove a si mesmo e encerra
                remove_self_and_exit()
            else:
                print("[INFO] Operação cancelada.")
        elif choice == "0":
            mgr.close_all()
            print("Saindo...")
            break
        else:
            print("[ERRO] Opção inválida.")

if __name__ == "__main__":
    try:
        run_menu()
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")
