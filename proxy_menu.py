#!/usr/bin/env python
# encoding: utf-8
# PAINEL DE GESTÃO PARA PROXY HÍBRIDO
# Unifica WebSocket (101) e HTTP/Socks (200 OK) com autoinstalação de serviço.
# ATUALIZADO PARA PYTHON 3
import socket, threading, select, sys, time, os, re, json, shutil, signal

# --- Configurações ---
PASS = ''
LISTENING_ADDR = '0.0.0.0'
BUFLEN = 8196 * 8
TIMEOUT = 60
DEFAULT_HOST = "127.0.0.1:22"

# --- Configurações do Serviço ---
INSTALL_DIR = "/opt/proxy"
SCRIPT_NAME = "wsproxy.py"
SERVICE_NAME = "proxy.service"
STATE_FILE = os.path.join(INSTALL_DIR, "proxy_state.json")

# --- Respostas Padrão do Protocolo HTTP ---
RESPONSE_WS = b'HTTP/1.1 101 Switching Protocols\r\n\r\n'
RESPONSE_HTTP = b'HTTP/1.1 200 Connection established\r\n\r\n'
RESPONSE_ERROR = b'HTTP/1.1 502 Bad Gateway\r\n\r\n'

# --- Gerenciador de Servidores Ativos ---
active_servers = {}
shutdown_requested = False

class Server(threading.Thread):
    def __init__(self, host, port):
        threading.Thread.__init__(self)
        self.daemon = True
        self.running = False
        self.host = host
        self.port = port
        self.threads = []
        self.threadsLock = threading.Lock()
        self.logLock = threading.Lock()
        self.soc = None

    def run(self):
        try:
            self.soc = socket.socket(socket.AF_INET)
            self.soc.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.soc.settimeout(2)
            self.soc.bind((self.host, self.port))
            self.soc.listen(0)
            self.running = True
        except Exception as e:
            self.printLog("Erro ao iniciar o servidor na porta {}: {}".format(self.port, e))
            self.running = False
            return

        try:
            while self.running and not shutdown_requested:
                try:
                    c, addr = self.soc.accept()
                    c.setblocking(1)
                except socket.timeout:
                    continue
                except socket.error:
                    break

                conn = ConnectionHandler(c, self, addr)
                conn.start()
                self.addConn(conn)
        finally:
            self.close_all_connections()
            if self.soc:
                self.soc.close()

    def printLog(self, log):
        with self.logLock:
            if '--service' in sys.argv:
                print(log, flush=True)
            else:
                print("\r" + " " * 80 + "\r", end="")
                print(log)
                if main_loop_active.is_set():
                    print("\n\033[1;96m❯ \033[1;37mEscolha uma opção: \033[0m", end="", flush=True)

    def addConn(self, conn):
        with self.threadsLock:
            if self.running:
                self.threads.append(conn)

    def removeConn(self, conn):
        with self.threadsLock:
            try:
                self.threads.remove(conn)
            except ValueError:
                pass
    
    def close_all_connections(self):
        with self.threadsLock:
            threads = list(self.threads)
            for c in threads:
                c.close()

    def close(self):
        self.running = False
        self.close_all_connections()
        if self.soc:
            try:
                self.soc.shutdown(socket.SHUT_RDWR)
                self.soc.close()
            except socket.error:
                pass

class ConnectionHandler(threading.Thread):
    def __init__(self, socClient, server, addr):
        threading.Thread.__init__(self)
        self.daemon = True
        self.clientClosed = False
        self.targetClosed = True
        self.client = socClient
        self.client_buffer = b''
        self.server = server
        self.log = 'Conexão: {} na porta {}'.format(str(addr), self.server.port)

    def close(self):
        try:
            if not self.clientClosed:
                self.client.shutdown(socket.SHUT_RDWR)
                self.client.close()
        except: pass
        finally: self.clientClosed = True

        try:
            if not self.targetClosed:
                self.target.shutdown(socket.SHUT_RDWR)
                self.target.close()
        except: pass
        finally: self.targetClosed = True

    def run(self):
        try:
            peek_buffer = self.client.recv(1024, socket.MSG_PEEK)
            if not peek_buffer: return

            is_websocket = b'upgrade: websocket' in peek_buffer.lower()

            if is_websocket:
                self.client.sendall(RESPONSE_WS)
                self.client_buffer = self.client.recv(BUFLEN)
            else:
                self.client_buffer = self.client.recv(BUFLEN)
            
            if self.client_buffer:
                self.process_request()

        except Exception as e:
            self.server.printLog("Erro no handler para {}: {}".format(self.log, str(e)))
        finally:
            self.close()
            self.server.removeConn(self)
    
    def process_request(self):
        hostPort = self.findHeader(self.client_buffer, b'X-Real-Host')
        if not hostPort:
            hostPort = DEFAULT_HOST.encode('utf-8')

        if self.findHeader(self.client_buffer, b'X-Split'):
            self.client.recv(BUFLEN)
        
        passwd = self.findHeader(self.client_buffer, b'X-Pass')
        hostPort_str = hostPort.decode('utf-8', errors='ignore')

        allow = False
        if len(PASS) == 0:
            allow = True
        elif passwd.decode('utf-8', errors='ignore') == PASS:
            allow = True
        
        if allow:
            self.method_CONNECT(hostPort_str, b'upgrade: websocket' not in self.client_buffer.lower())
        else:
            self.client.send(b'HTTP/1.1 400 WrongPass!\r\n\r\n')

    def findHeader(self, head, header):
        aux = head.find(header + b': ')
        if aux == -1: return b''
        head = head[aux+len(header)+2:]
        aux = head.find(b'\r\n')
        if aux == -1: return b''
        return head[:aux]

    def connect_target(self, host):
        try:
            i = host.find(':')
            port = int(host[i+1:]) if i != -1 else 80
            host = host[:i] if i != -1 else host
            
            soc_family, _, _, _, address = socket.getaddrinfo(host, port)[0]
            self.target = socket.socket(soc_family)
            self.targetClosed = False
            self.target.connect(address)
            return True
        except Exception as e:
            self.server.printLog(f"Erro ao conectar ao destino {host}:{port} - {e}")
            return False

    def method_CONNECT(self, path, send_200_ok):
        self.server.printLog(f"{self.log} - CONNECT {path}")
        if self.connect_target(path):
            if send_200_ok:
                self.client.sendall(RESPONSE_HTTP)
            self.doCONNECT()
        else:
            self.client.sendall(RESPONSE_ERROR)

    def doCONNECT(self):
        socs = [self.client, self.target]
        count = 0
        error = False
        while not error and not shutdown_requested:
            count += 1
            (recv, _, err) = select.select(socs, [], socs, 3)
            if err: error = True
            if recv:
                for sock in recv:
                    try:
                        data = sock.recv(BUFLEN)
                        if data:
                            if sock is self.target:
                                self.client.send(data)
                            else:
                                self.target.sendall(data)
                            count = 0
                        else:
                            error = True
                            break
                    except:
                        error = True
                        break
            if count > TIMEOUT: error = True

# --- Funções de Serviço e Persistência ---

def save_state():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(list(active_servers.keys()), f)

def load_state_and_start_proxies():
    try:
        # O ficheiro de estado só é lido se o serviço estiver instalado
        service_path = f"/etc/systemd/system/{SERVICE_NAME}"
        if os.path.exists(service_path) and os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                ports = json.load(f)
            print("\033[1;32m✓ Restaurando sessão anterior do serviço...\033[0m")
            for port in ports:
                if isinstance(port, int) and 0 < port < 65536:
                     server = Server(LISTENING_ADDR, port)
                     server.start()
                     if server.running:
                         active_servers[port] = server
                         print(f"\033[1;32m  ➤ Proxy reativado na porta {port}.\033[0m")
    except Exception:
        print("\033[1;31m✗ Ficheiro de estado corrompido ou ilegível.\033[0m")
    
    if '--service' not in sys.argv: time.sleep(2)

# --- Funções do Painel ---

def display_menu():
    clear_screen()
    
    service_path = f"/etc/systemd/system/{SERVICE_NAME}"
    is_installed = os.path.exists(service_path)

    print("\033[1;36m" + "═" * 65)
    print("║" + " " * 63 + "║")
    print("║" + "\033[1;97m🚀 MULTIFLOW PROXY - PAINEL DE GESTÃO 🚀\033[1;36m".center(75) + "║")
    print("║" + " " * 63 + "║")
    print("╠" + "═" * 63 + "╣")
    
    if active_servers:
        ports = ", ".join(str(p) for p in sorted(active_servers.keys()))
        status_icon = "🟢"
        status_text = f"\033[1;32m{status_icon} ATIVO\033[1;36m"
        ports_text = f"\033[1;33mPortas: {ports}\033[1;36m"
        print(f"║  \033[1;37mStatus:\033[1;36m {status_text:<20} {ports_text:<30} ║")
    else:
        status_icon = "🔴"
        status_text = f"\033[1;31m{status_icon} INATIVO\033[1;36m"
        print(f"║  \033[1;37mStatus:\033[1;36m {status_text:<35} ║")
    
    print("║" + " " * 63 + "║")
    print("╠" + "═" * 63 + "╣")
    print("║" + " " * 63 + "║")
    
    print("║  \033[1;97m📋 OPÇÕES DISPONÍVEIS:\033[1;36m" + " " * 32 + "║")
    print("║" + " " * 63 + "║")
    
    if is_installed:
        print("║    \033[1;91m[1]\033[1;37m ⚙️  Remover Serviço do Proxy\033[1;36m" + " " * 22 + "║")
    else:
        print("║    \033[1;92m[1]\033[1;37m ⚙️  Instalar Serviço do Proxy (Recomendado)\033[1;36m" + " " * 5 + "║")

    print("║    \033[1;92m[2]\033[1;37m ▶️  Abrir Porta\033[1;36m" + " " * 42 + "║")
    print("║    \033[1;91m[3]\033[1;37m ⏹️  Fechar Porta\033[1;36m" + " " * 41 + "║")
    print("║" + " " * 63 + "║")
    print("║    \033[1;90m[0]\033[1;37m 🔽 Voltar (Minimizar Painel)\033[1;36m" + " " * 22 + "║")
    print("║" + " " * 63 + "║")
    print("╚" + "═" * 63 + "╝\033[0m")
    print()

def start_proxy_port():
    try:
        print("\033[1;96m┌─────────────────────────────────────┐")
        print("│       \033[1;97m🚀 INICIAR PROXY\033[1;96m         │")
        print("└─────────────────────────────────────┘\033[0m")
        print()
        user_input = input("\033[1;97m➤ \033[1;37mDigite a porta para abrir \033[1;90m(ou 'voltar')\033[1;37m: \033[1;33m").lower()
        if user_input.startswith('v'): return

        port = int(user_input)
        if port in active_servers:
            print(f"\n\033[1;31m❌ Erro: A porta {port} já está em uso.\033[0m")
        elif not 0 < port < 65536:
            print("\n\033[1;31m❌ Erro: Porta inválida (1-65535).\033[0m")
        else:
            print(f"\n\033[1;93m⏳ Iniciando proxy na porta {port}...\033[0m")
            server = Server(LISTENING_ADDR, port)
            server.start()
            if server.running:
                active_servers[port] = server
                save_state()
                print(f"\n\033[1;32m✅ Proxy iniciado com sucesso na porta {port}! 🎉\033[0m")
    except ValueError:
        print("\n\033[1;31m❌ Erro: Entrada inválida. Digite apenas números.\033[0m")
    
    if not user_input.startswith('v'):
        input("\n\033[1;96m📱 Pressione Enter para voltar ao menu...\033[0m")

def stop_proxy_port():
    try:
        print("\033[1;91m┌─────────────────────────────────────┐")
        print("│        \033[1;97m⏹️  PARAR PROXY\033[1;91m         │")
        print("└─────────────────────────────────────┘\033[0m")
        print()
        user_input = input("\033[1;97m➤ \033[1;37mDigite a porta para fechar \033[1;90m(ou 'voltar')\033[1;37m: \033[1;33m").lower()
        if user_input.startswith('v'): return

        port = int(user_input)
        if port in active_servers:
            print(f"\n\033[1;93m⏳ Encerrando proxy na porta {port}...\033[0m")
            active_servers.pop(port).close()
            save_state()
            print(f"\n\033[1;32m✅ Proxy na porta {port} encerrado com sucesso! 🛑\033[0m")
        else:
            print(f"\n\033[1;31m❌ Erro: Não há proxy ativo na porta {port}.\033[0m")
    except ValueError:
        print("\n\033[1;31m❌ Erro: Entrada inválida. Digite apenas números.\033[0m")

    if not user_input.startswith('v'):
        input("\n\033[1;96m📱 Pressione Enter para voltar ao menu...\033[0m")

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

# --- Lógica de Gestão do Serviço ---

def manage_service():
    service_path = f"/etc/systemd/system/{SERVICE_NAME}"
    is_installed = os.path.exists(service_path)
    
    clear_screen()
    print("\033[1;96m" + "═" * 60)
    print("║" + "\033[1;97m🔧 GESTÃO DO SERVIÇO DO PROXY\033[1;96m".center(70) + "║")
    print("╠" + "═" * 58 + "╣")

    if is_installed:
        print("║ \033[1;32m   O serviço do proxy já está instalado.\033[1;96m" + " " * 15 + "║")
        print("║ \033[1;37m   Isto garante que os proxies iniciam com o sistema.\033[1;96m" + " " * 5 + "║")
        print("╚" + "═" * 58 + "╝\033[0m")
        choice = input("\n\033[1;91mDeseja desinstalar o serviço? (s/N): \033[0m").lower().strip()
        if choice == 's':
            uninstall_service()
        else:
            print("\n\033[1;37mNenhuma alteração foi feita.\033[0m")
    else:
        print("║ \033[1;93m   O serviço do proxy não está instalado.\033[1;96m" + " " * 16 + "║")
        print("║ \033[1;37m   Instalar o serviço torna o proxy permanente.\033[1;96m" + " " * 7 + "║")
        print("╚" + "═" * 58 + "╝\033[0m")
        choice = input("\n\033[1;92mDeseja instalar o serviço agora? (S/n): \033[0m").lower().strip()
        if choice == '' or choice == 's':
            install_service()
        else:
            print("\n\033[1;37mNenhuma alteração foi feita.\033[0m")
            
    input("\n\033[1;90mPressione Enter para voltar ao menu principal...\033[0m")


def install_service():
    if os.geteuid() != 0:
        print("\n\033[1;31m❌ Erro: A instalação do serviço requer privilégios de root.\033[0m")
        print(f"\033[1;37mPor favor, execute novamente com 'sudo': \033[1;33msudo python3 {os.path.basename(__file__)}\033[0m")
        sys.exit(1)
    
    print("\033[1;96m🔧 Iniciando a instalação do serviço...\033[0m")
    
    script_path = os.path.abspath(__file__)
    install_path = os.path.join(INSTALL_DIR, SCRIPT_NAME)
    service_path = f"/etc/systemd/system/{SERVICE_NAME}"
    
    service_content = f"""[Unit]
Description=Serviço de Proxy Híbrido (Python)
After=network.target
[Service]
Type=simple
User=root
WorkingDirectory={INSTALL_DIR}
ExecStart=/usr/bin/python3 {install_path} --service
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
"""
    try:
        print(f"\033[1;93m➤ Criando diretório: {INSTALL_DIR}\033[0m")
        os.makedirs(INSTALL_DIR, exist_ok=True)
        print(f"\033[1;93m➤ Copiando script: {install_path}\033[0m")
        shutil.copy(script_path, install_path)
        print(f"\033[1;93m➤ Criando serviço: {service_path}\033[0m")
        with open(service_path, "w") as f: f.write(service_content)
        print("\033[1;93m➤ Recarregando systemd...\033[0m")
        os.system("systemctl daemon-reload")
        print("\033[1;93m➤ Habilitando para boot...\033[0m")
        os.system(f"systemctl enable {SERVICE_NAME}")
        print("\033[1;93m➤ Iniciando serviço...\033[0m")
        os.system(f"systemctl start {SERVICE_NAME}")
        print(f"\n\033[1;32m✅ Serviço instalado e iniciado com sucesso! 🎉\033[0m")
        print(f"\033[1;37mUse 'sudo systemctl status {SERVICE_NAME}' para verificar.\033[0m")
    except Exception as e:
        print(f"\n\033[1;31m❌ Erro durante a instalação: {e}\033[0m")
        uninstall_service(feedback=False)
        sys.exit(1)

def uninstall_service(feedback=True):
    if os.geteuid() != 0:
        print("\n\033[1;31m❌ Erro: A desinstalação requer privilégios de root. Use 'sudo'.\033[0m")
        print(f"\033[1;37mPor favor, execute novamente com 'sudo': \033[1;33msudo python3 {os.path.basename(__file__)}\033[0m")
        sys.exit(1)

    if feedback: print("\033[1;91m🗑️  Iniciando a desinstalação do serviço...\033[0m")
    
    service_path = f"/etc/systemd/system/{SERVICE_NAME}"
    
    try:
        print("\033[1;93m➤ Parando serviço...\033[0m")
        os.system(f"systemctl stop {SERVICE_NAME}")
        print("\033[1;93m➤ Desabilitando serviço...\033[0m")
        os.system(f"systemctl disable {SERVICE_NAME}")
        if os.path.exists(service_path):
            print(f"\033[1;93m➤ Removendo: {service_path}\033[0m")
            os.remove(service_path)
        print("\033[1;93m➤ Recarregando systemd...\033[0m")
        os.system("systemctl daemon-reload")
        if os.path.isdir(INSTALL_DIR):
            print(f"\033[1;93m➤ Removendo: {INSTALL_DIR}\033[0m")
            shutil.rmtree(INSTALL_DIR)
        if feedback: print("\n\033[1;32m✅ Serviço desinstalado com sucesso! 🗑️\033[0m")
    except Exception as e:
        if feedback: print(f"\n\033[1;31m❌ Erro durante a desinstalação: {e}\033[0m")
        sys.exit(1)

def signal_handler(signum, frame):
    global shutdown_requested
    if not shutdown_requested:
        shutdown_requested = True
        print("\n\033[1;31m⚠️  Sinal de encerramento recebido...\033[0m")
        cleanup_and_exit()
        
def cleanup_and_exit():
    global shutdown_requested
    shutdown_requested = True
    print("\n\033[1;93m🔄 Fechando todas as conexões ativas...\033[0m")
    for port in list(active_servers.keys()):
        active_servers.pop(port).close()
    print("\033[1;32m✅ Todos os proxies foram encerrados com sucesso! 👋\033[0m")
    sys.exit(0)

main_loop_active = threading.Event()

def main_panel():
    main_loop_active.set()
    load_state_and_start_proxies()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not shutdown_requested:
        display_menu()
        choice = input("\033[1;96m❯ \033[1;37mEscolha uma opção: \033[1;33m").lower().strip()
        
        if choice == '1':   manage_service()
        elif choice == '2': start_proxy_port()
        elif choice == '3': stop_proxy_port()
        elif choice == '0':
            main_loop_active.clear()
            break
        else:
            print("\n\033[1;31m❌ Opção inválida. Tente novamente.\033[0m")
            time.sleep(1)

    clear_screen()
    if active_servers:
        ports = ", ".join(str(p) for p in sorted(active_servers.keys()))
        print("\033[1;96m" + "═" * 60)
        print("║" + "\033[1;97m📱 PAINEL MINIMIZADO - PROXIES ATIVOS\033[1;96m".center(70) + "║")
        print("╠" + "═" * 58 + "╣")
        print(f"║  \033[1;32m🟢 Proxies em execução: \033[1;33m{ports}\033[1;96m" + " " * (32 - len(ports)) + "║")
        print("║" + " " * 58 + "║")
        print("║  \033[1;37m💡 Os proxies continuarão funcionando em segundo plano\033[1;96m ║")
        print("║  \033[1;37m🔄 Execute novamente para voltar ao painel de controle\033[1;96m ║")
        print("╚" + "═" * 58 + "╝\033[0m")
        
        try:
            print("\n\033[1;90m(Pressione Ctrl+C para encerrar todos os proxies)\033[0m")
            while not shutdown_requested:
                time.sleep(1)
        except KeyboardInterrupt:
            signal_handler(signal.SIGINT, None)
    else:
        print("\n\033[1;32m👋 Saindo do painel. Nenhum proxy ativo.\033[0m")

def main_service():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("🚀 Iniciando proxy em modo de serviço...")
    load_state_and_start_proxies()
    if not active_servers:
        print("❌ Nenhuma porta configurada. A sair.")
        return
    print(f"✅ Proxy ativo em: {', '.join(str(p) for p in sorted(active_servers.keys()))}")
    
    try:
        while not shutdown_requested:
            time.sleep(60)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

if __name__ == '__main__':
    # Trata argumentos de linha de comando que não iniciam o painel
    if '--install-service' in sys.argv:
        install_service()
    elif '--uninstall-service' in sys.argv:
        uninstall_service()
    elif '--help' in sys.argv:
        display_help()
    elif '--service' in sys.argv:
        try:
            main_service()
        except KeyboardInterrupt:
            signal_handler(signal.SIGINT, None)
    else:
        # Bloco para o painel interativo
        try:
            main_panel()
        except SystemExit:
            pass # Permite a saída limpa
        except KeyboardInterrupt:
            signal_handler(signal.SIGINT, None)
        except Exception as e:
            print(f"\n\033[1;31m❌ Erro inesperado no fluxo principal: {e}\033[0m")
            cleanup_and_exit()

