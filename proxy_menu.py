#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import contextlib
import socket
import threading
from dataclasses import dataclass
from typing import Optional, Tuple, List

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
# Servidor assíncrono
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
        def _peek_blocking() -> Tuple[bool, bytes]:
            try:
                sock.settimeout(timeout)
                try:
                    data = sock.recv(buf_size, socket.MSG_PEEK)
                finally:
                    sock.settimeout(None)
                return True, data
            except socket.timeout:
                return True, b""
            except OSError:
                return False, b""
        return await asyncio.to_thread(_peek_blocking)

    async def _connect_upstream(self, host: str, port: int) -> socket.socket:
        assert self.loop is not None
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setblocking(False)
        try:
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
# Controlador em thread
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
                nonlocal_running_ok = False
                try:
                    await self._server.start()
                    nonlocal_running_ok = True
                except Exception as e:
                    print(f"[ERRO] Falha ao iniciar servidor: {e}")
                finally:
                    self._running_ok = nonlocal_running_ok
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

        self._thread = threading.Thread(target=runner, name="proxy-server", daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)
        return self._running_ok

    def stop(self) -> None:
        if not self.is_running():
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
        self._started.clear()
        self._running_ok = False

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._running_ok

# =========================
# Menu minimalista
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
    cfg = ProxyConfig()
    ctrl = ServerController(cfg)

    while True:
        status = f"ATIVO (porta {cfg.listen_port})" if ctrl.is_running() else "INATIVO"
        print("\n==============================")
        print(f"  STATUS: {status}")
        print("==============================")
        print("1) Abrir porta")
        print("2) Fechar porta")
        print("0) Sair")
        choice = input("Escolha: ").strip()

        if choice == "1":
            if ctrl.is_running():
                print("[INFO] Já está ativo. Feche antes se quiser trocar de porta.")
                continue
            cfg.listen_port = ask_port(cfg.listen_port)
            ok = ctrl.start()
            if ok:
                print(f"[OK] Porta aberta em {cfg.listen_port}.")
                print("    (Dica: portas <1024 exigem sudo em Unix)")
            else:
                print("[ERRO] Não foi possível abrir a porta. Verifique permissões/conflitos.")
        elif choice == "2":
            if not ctrl.is_running():
                print("[INFO] Já está inativo.")
                continue
            ctrl.stop()
            print("[OK] Porta fechada.")
        elif choice == "0":
            if ctrl.is_running():
                ctrl.stop()
            print("Saindo...")
            break
        else:
            print("[ERRO] Opção inválida.")

if __name__ == "__main__":
    try:
        run_menu()
    except KeyboardInterrupt:
        print("\nEncerrado pelo usuário.")
