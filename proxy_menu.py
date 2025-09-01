#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Proxy com menu integrado (replica o fluxo do main.rs):

Fluxo por conexão (mesmo do Rust):
  1) Envia "HTTP/1.1 101 <status>\r\n\r\n"
  2) Lê/descarta 1024 bytes do cliente (sem timeout)
  3) Envia "HTTP/1.1 200 <status>\r\n\r\n"
  4) Faz peek (até 1s). Se não houver dados OU contiver b"SSH", roteia para SSH; senão, OVPN.
  5) Conecta ao destino e faz piping bidirecional.

Requisitos: Python 3.9+ (usa asyncio.to_thread).
Obs: escutar em portas <1024 requer privilégios elevados em Unix (sudo).
"""

import asyncio
import contextlib
import socket
import threading
from dataclasses import dataclass
from typing import List, Tuple, Optional

# ------------------------
# Configuração
# ------------------------

@dataclass
class ProxyConfig:
    listen_port: int = 80
    status_banner: str = "@RustyManager"
    ssh_host: str = "127.0.0.1"
    ssh_port: int = 22
    ovpn_host: str = "127.0.0.1"
    ovpn_port: int = 1194
    backlog: int = 128
    dual_stack: bool = True           # Tentar IPv6 com V6ONLY=0 (aceita IPv4 também)
    buf_size: int = 8192
    sniff_timeout: float = 1.0        # timeout do peek em segundos
    preconsume: int = 1024            # bytes lidos/descartados logo após o 101

# ------------------------
# Servidor assíncrono
# ------------------------

class AsyncProxyServer:
    def __init__(self, cfg: ProxyConfig) -> None:
        self.cfg = cfg
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping = asyncio.Event()
        self._socks: List[socket.socket] = []
        self._accept_tasks: List[asyncio.Task] = []
        self._client_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Inicializa sockets de escuta e começa os loops de accept."""
        self.loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._socks = []

        # Tenta IPv6 com dual-stack, e faz fallback para IPv4 se necessário
        errors = []

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
                print(f"[LISTEN] IPv6 dual-stack em [::]:{self.cfg.listen_port}")
            except OSError as e:
                errors.append(("IPv6 dual-stack", e))

        # Se não conseguimos dual-stack, tenta sockets separados
        if not self._socks:
            # IPv6 only
            try:
                s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with contextlib.suppress(OSError, AttributeError):
                    s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                s6.bind(("::", self.cfg.listen_port))
                s6.listen(self.cfg.backlog)
                s6.setblocking(False)
                self._socks.append(s6)
                print(f"[LISTEN] IPv6 em [::]:{self.cfg.listen_port}")
            except OSError as e:
                errors.append(("IPv6 only", e))

            # IPv4
            try:
                s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s4.bind(("0.0.0.0", self.cfg.listen_port))
                s4.listen(self.cfg.backlog)
                s4.setblocking(False)
                self._socks.append(s4)
                print(f"[LISTEN] IPv4 em 0.0.0.0:{self.cfg.listen_port}")
            except OSError as e:
                errors.append(("IPv4", e))

        if not self._socks:
            print("[ERRO] Falha ao abrir portas:")
            for where, err in errors:
                print(f"  - {where}: {err}")
            raise RuntimeError("Não foi possível iniciar o listener.")

        # Cria tarefas de accept
        self._accept_tasks = [asyncio.create_task(self._accept_loop(s)) for s in self._socks]

    async def shutdown(self) -> None:
        """Para de aceitar e encerra todas as conexões ativas."""
        self._stopping.set()

        # Fecha listeners
        for s in self._socks:
            with contextlib.suppress(Exception):
                s.close()
        self._socks.clear()

        # Cancela loops de accept
        for t in self._accept_tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._accept_tasks, return_exceptions=True)
        self._accept_tasks.clear()

        # Cancela conexões de clientes
        if self._client_tasks:
            for t in list(self._client_tasks):
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()

        print("[OK] Servidor parado.")

    async def _accept_loop(self, listen_sock: socket.socket) -> None:
        """Loop de accept por socket."""
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
        """Fluxo por cliente: 101 -> (read 1024) -> 200 -> peek -> decide -> conecta -> túnel."""
        assert self.loop is not None
        cfg = self.cfg
        try:
            # 1) 101
            await self.loop.sock_sendall(
                client_sock, f"HTTP/1.1 101 {cfg.status_banner}\r\n\r\n".encode()
            )

            # 2) Lê/descarta 1024 bytes (sem timeout), imitando o main.rs
            with contextlib.suppress(ConnectionError, OSError):
                _ = await self.loop.sock_recv(client_sock, cfg.preconsume)

            # 3) 200
            await self.loop.sock_sendall(
                client_sock, f"HTTP/1.1 200 {cfg.status_banner}\r\n\r\n".encode()
            )

            # 4) Peek com timeout (até cfg.buf_size)
            ok, data = await self._peek_stream(client_sock, cfg.sniff_timeout, cfg.buf_size)

            # 5) Heurística do Rust:
            #    - Sem dados no peek OU contém b"SSH" -> SSH
            #    - Caso contrário -> OVPN
            if ok and (not data or (b"SSH" in data)):
                upstream_host, upstream_port = cfg.ssh_host, cfg.ssh_port
                dest_name = f"{upstream_host}:{upstream_port} (SSH)"
            elif ok:
                upstream_host, upstream_port = cfg.ovpn_host, cfg.ovpn_port
                dest_name = f"{upstream_host}:{upstream_port} (OVPN)"
            else:
                upstream_host, upstream_port = cfg.ssh_host, cfg.ssh_port
                dest_name = f"{upstream_host}:{upstream_port} (SSH, fallback)"

            # Conecta no upstream
            upstream = await self._connect_upstream(upstream_host, upstream_port)
            print(f"[ROUTE] {addr} -> {dest_name}")

            # Túnel bidirecional
            await self._bidirectional_proxy(client_sock, upstream)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Silencioso, para não poluir — pode logar se desejar
            # print(f"[ERRO] Conexão {addr}: {e}")
            pass
        finally:
            with contextlib.suppress(Exception):
                client_sock.close()

    async def _peek_stream(self, sock: socket.socket, timeout: float, buf_size: int) -> Tuple[bool, bytes]:
        """Executa recv(MSG_PEEK) com timeout em thread separada (portável)."""
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
        """Cria socket para host/port e conecta de forma não-bloqueante."""
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
        """Copia bytes de src para dst até EOF/erro; fecha metade de escrita ao término."""
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
        """Transfere dados nos dois sentidos e fecha ambos ao final."""
        t1 = asyncio.create_task(self._transfer(a, b))
        t2 = asyncio.create_task(self._transfer(b, a))
        try:
            await asyncio.gather(t1, t2)
        finally:
            with contextlib.suppress(Exception):
                a.close()
            with contextlib.suppress(Exception):
                b.close()


# ------------------------
# Controle em thread (para o menu ficar responsivo)
# ------------------------

class ServerController:
    """Executa o loop asyncio numa thread separada para manter o menu interativo."""
    def __init__(self, cfg: ProxyConfig) -> None:
        self.cfg = cfg
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[AsyncProxyServer] = None
        self._started = threading.Event()

    def start(self) -> None:
        if self.is_running():
            print("[AVISO] Servidor já está em execução.")
            return

        def runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._server = AsyncProxyServer(self.cfg)

            async def _boot():
                try:
                    await self._server.start()
                    print(f"[OK] Servidor iniciado na porta {self.cfg.listen_port}.")
                    self._started.set()
                except Exception as e:
                    print(f"[ERRO] Falha ao iniciar servidor: {e}")
                    self._started.set()  # permite continuar o menu mesmo em erro

            self._loop.create_task(_boot())
            try:
                self._loop.run_forever()
            finally:
                # limpeza de tarefas pendentes
                pending = asyncio.all_tasks(loop=self._loop)
                for t in pending:
                    t.cancel()
                with contextlib.suppress(Exception):
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                self._loop.close()

        self._thread = threading.Thread(target=runner, name="proxy-server", daemon=True)
        self._thread.start()
        # Espera até subir (ou falhar)
        self._started.wait(timeout=5)

    def stop(self) -> None:
        if not self.is_running():
            print("[AVISO] Servidor não está em execução.")
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

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ------------------------
# Menu de texto
# ------------------------

def print_config(cfg: ProxyConfig) -> None:
    print("\n=== Configuração Atual ===")
    print(f" Porta de escuta     : {cfg.listen_port}")
    print(f" Status banner       : {cfg.status_banner}")
    print(f" Destino SSH         : {cfg.ssh_host}:{cfg.ssh_port}")
    print(f" Destino OVPN        : {cfg.ovpn_host}:{cfg.ovpn_port}")
    print(f" Dual-stack IPv6/IPv4: {'sim' if cfg.dual_stack else 'não'}")
    print(f" Pre-consumo (bytes) : {cfg.preconsume}")
    print(f" Peek timeout (s)    : {cfg.sniff_timeout}")
    print("==========================\n")

def ask_int(prompt: str, default: int, min_v: int = 1, max_v: int = 65535) -> int:
    val = input(f"{prompt} [{default}]: ").strip()
    if not val:
        return default
    try:
        n = int(val)
        if not (min_v <= n <= max_v):
            raise ValueError
        return n
    except ValueError:
        print("[ERRO] Valor inválido. Mantendo o padrão.")
        return default

def ask_str(prompt: str, default: str) -> str:
    val = input(f"{prompt} [{default}]: ").strip()
    return val if val else default

def ask_host_port(prompt: str, default_host: str, default_port: int) -> Tuple[str, int]:
    raw = input(f"{prompt} [{default_host}:{default_port}]: ").strip()
    if not raw:
        return default_host, default_port
    if raw.count(":") == 0:
        # Sem porta
        return raw, default_port
    # IPv6 com colchetes: [::1]:22
    if raw.startswith("["):
        try:
            host, p = raw.split("]")
            host = host.strip("[]")
            port = int(p.strip(":"))
            return host, port
        except Exception:
            print("[ERRO] Formato inválido. Mantendo padrão.")
            return default_host, default_port
    # IPv4/hostname:porta
    try:
        host, p = raw.rsplit(":", 1)
        port = int(p)
        return host, port
    except Exception:
        print("[ERRO] Formato inválido. Mantendo padrão.")
        return default_host, default_port

def run_menu() -> None:
    cfg = ProxyConfig()
    ctrl = ServerController(cfg)

    while True:
        print_config(cfg)
        print("1) Iniciar servidor")
        print("2) Parar servidor")
        print("3) Alterar porta de escuta")
        print("4) Alterar status banner")
        print("5) Alterar destino SSH (host:porta)")
        print("6) Alterar destino OVPN (host:porta)")
        print("7) Alternar dual-stack IPv6/IPv4")
        print("8) Ajustar pre-consumo (bytes)")
        print("9) Ajustar timeout do peek (segundos)")
        print("0) Sair")
        choice = input("Escolha: ").strip()

        if choice == "1":
            ctrl.start()
        elif choice == "2":
            ctrl.stop()
        elif choice == "3":
            cfg.listen_port = ask_int("Nova porta", cfg.listen_port)
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
        elif choice == "4":
            cfg.status_banner = ask_str("Novo status", cfg.status_banner)
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
        elif choice == "5":
            cfg.ssh_host, cfg.ssh_port = ask_host_port("Novo destino SSH", cfg.ssh_host, cfg.ssh_port)
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
        elif choice == "6":
            cfg.ovpn_host, cfg.ovpn_port = ask_host_port("Novo destino OVPN", cfg.ovpn_host, cfg.ovpn_port)
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
        elif choice == "7":
            cfg.dual_stack = not cfg.dual_stack
            print(f"[OK] Dual-stack agora está {'ativado' if cfg.dual_stack else 'desativado'}.")
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
        elif choice == "8":
            cfg.preconsume = ask_int("Bytes a descartar após 101", cfg.preconsume, 0, 65536)
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
        elif choice == "9":
            # aceita float simples
            val = ask_str("Timeout do peek (s)", f"{cfg.sniff_timeout}")
            try:
                cfg.sniff_timeout = max(0.0, float(val))
            except ValueError:
                print("[ERRO] Valor inválido. Mantendo.")
            if ctrl.is_running():
                print("[INFO] Reinicie o servidor para aplicar.")
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
