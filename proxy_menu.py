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

# --- Gerenciador de Servidores Ativos (Usado apenas pelo serviço) ---
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

def is_service_installed():
    return os.path.exists(f"/etc/systemd/system/{SERVICE_NAME}")

def get_ports_from_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        return []
    return []

def save_ports_to_state(ports):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(ports, f)

# --- Funções do Painel ---

def display_menu():
    clear_screen()
    
    is_installed = is_service_installed()
    display_ports = get_ports_from_state() if is_installed else []

    print("\033[1;36m" + "═" * 65)
    print("║" + " " * 63 + "║")
    print("║" + "\033[1;97m🚀 MULTIFLOW PROXY - PAINEL DE GESTÃO 🚀\033[1;36m".center(75) + "║")
    print("║" + " " * 63 + "║")
    print("╠" + "═" * 63 + "╣")
    
    if display_ports:
        ports_str = ", ".join(str(p) for p in sorted(display_ports))
        status_icon = "🟢"
        status_text = f"\033[1;32m{status_icon} ATIVO\033[1;36m"
        ports_text = f"\033[1;33mPortas: {ports_str}\033[1;36m"
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
        print("║    \033[1;91m[1]\033[1;37m ⚙️  Desinstalar Proxy\033[1;36m" + " " * 32 + "║")
    else:
        print("║    \033[1;92m[1]\033[1;37m ⚙️  Instalar Proxy (Obrigatório)\033[1;36m" + " " * 15 + "║")

    print("║    \033[1;92m[2]\033[1;37m ▶️  Abrir Porta\033[1;36m" + " " * 42 + "║")
    print("║    \033[1;91m[3]\033[1;37m ⏹️  Fechar Porta\033[1;36m" + " " * 41 + "║")
    print("║" + " " * 63 + "║")
    print("║    \033[1;90m[0]\033[1;37m 🔽 Voltar (Sair do Painel)\033[1;36m" + " " * 25 + "║")
    print("║" + " " * 63 + "║")
    print("╚" + "═" * 63 + "╝\033[0m")
    print()

def start_proxy_port():
    try:
        print("\033[1;96m┌─────────────────────────────────────┐")
        print("│       \033[1;97m🚀 ABRIR PORTA NO SERVIÇO\033[1;96m     │")
        print("└─────────────────────────────────────┘\033[0m")
        print()
        user_input = input("\033[1;97m➤ \033[1;37mDigite a porta para abrir \033[1;90m(ou 'voltar')\033[1;37m: \033[1;33m").lower()
        if user_input.startswith('v'): return

        port = int(user_input)
        
        if os.geteuid() != 0:
            print("\n\033[1;31m❌ Erro: Para gerir o serviço, precisa de privilégios de root.\033[0m")
            print(f"\033[1;37m   Execute novamente com 'sudo': \033[1;33msudo python3 {os.path.basename(__file__)}\033[0m")
        else:
            ports = get_ports_from_state()
            if port in ports:
                print(f"\n\033[1;31m❌ Erro: A porta {port} já está configurada no serviço.\033[0m")
            else:
                ports.append(port)
                save_ports_to_state(ports)
                print(f"\n\033[1;93m⏳ Reiniciando o serviço para aplicar a nova porta {port}...\033[0m")
                os.system(f"systemctl restart {SERVICE_NAME}")
                print(f"\n\033[1;32m✅ Serviço reiniciado com sucesso! A porta {port} está agora ativa.\033[0m")

    except ValueError:
        print("\n\033[1;31m❌ Erro: Entrada inválida. Digite apenas números.\033[0m")
    
    if not user_input.startswith('v'):
        input("\n\033[1;96m📱 Pressione Enter para voltar ao menu...\033[0m")

def stop_proxy_port():
    try:
        print("\033[1;91m┌─────────────────────────────────────┐")
        print("│      \033[1;97m⏹️  FECHAR PORTA NO SERVIÇO\033[1;91m     │")
        print("└─────────────────────────────────────┘\033[0m")
        print()
        user_input = input("\033[1;97m➤ \033[1;37mDigite a porta para fechar \033[1;90m(ou 'voltar')\033[1;37m: \033[1;33m").lower()
        if user_input.startswith('v'): return

        port = int(user_input)

        if os.geteuid() != 0:
            print("\n\033[1;31m❌ Erro: Para gerir o serviço, precisa de privilégios de root.\033[0m")
            print(f"\033[1;37m   Execute novamente com 'sudo': \033[1;33msudo python3 {os.path.basename(__file__)}\033[0m")
        else:
            ports = get_ports_from_state()
            if port not in ports:
                print(f"\n\033[1;31m❌ Erro: A porta {port} não está configurada no serviço.\033[0m")
            else:
                ports.remove(port)
                save_ports_to_state(ports)
                print(f"\n\033[1;93m⏳ Reiniciando o serviço para remover a porta {port}...\033[0m")
                os.system(f"systemctl restart {SERVICE_NAME}")
                print(f"\n\033[1;32m✅ Serviço reiniciado com sucesso! A porta {port} foi desativada.\033[0m")

    except ValueError:
        print("\n\033[1;31m❌ Erro: Entrada inválida. Digite apenas números.\033[0m")

    if not user_input.startswith('v'):
        input("\n\033[1;96m📱 Pressione Enter para voltar ao menu...\033[0m")

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

# --- Lógica de Gestão do Serviço ---

def manage_service():
    is_installed = is_service_installed()
    
    clear_screen()
    print("\033[1;96m" + "═" * 60)
    print("║" + "\033[1;97m🔧 GESTÃO DO SERVIÇO DO PROXY\033[1;96m".center(70) + "║")
    print("╠" + "═" * 58 + "╣")

    if is_installed:
        print("║ \033[1;32m   O serviço do proxy já está instalado.\033[1;96m" + " " * 15 + "║")
        print("╚" + "═" * 58 + "╝\033[0m")
        choice = input("\n\033[1;91mDeseja desinstalar o serviço? (s/N): \033[0m").lower().strip()
        if choice == 's':
            uninstall_service()
    else:
        print("║ \033[1;93m   O serviço do proxy não está instalado.\033[1;96m" + " " * 16 + "║")
        print("║ \033[1;37m   Este passo é obrigatório para gerir as portas.\033[1;96m" + " " * 7 + "║")
        print("╚" + "═" * 58 + "╝\033[0m")
        choice = input("\n\033[1;92mDeseja instalar o serviço agora? (S/n): \033[0m").lower().strip()
        if choice == '' or choice == 's':
            install_service()
    
    input("\n\033[1;90mPressione Enter para voltar ao menu principal...\033[0m")

def install_service():
    if os.geteuid() != 0:
        print("\n\033[1;31m❌ Erro: A instalação requer privilégios de root.\033[0m")
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
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while not shutdown_requested:
        display_menu()
        choice = input("\033[1;96m❯ \033[1;37mEscolha uma opção: \033[1;33m").lower().strip()
        
        is_installed = is_service_installed()

        if choice == '1':
            manage_service()
        elif choice in ['2', '3']:
            if not is_installed:
                print("\n\033[1;31mInstale o Proxy primeiro!!!!\033[0m")
                input("\n\033[1;90mPressione Enter para voltar ao menu...\033[0m")
            elif choice == '2':
                start_proxy_port()
            elif choice == '3':
                stop_proxy_port()
        elif choice == '0':
            main_loop_active.clear()
            break
        else:
            print("\n\033[1;31m❌ Opção inválida. Tente novamente.\033[0m")
            time.sleep(1)

    clear_screen()
    print("\n\033[1;32m👋 Painel encerrado.\033[0m")
    if is_service_installed():
        print("\033[1;37mO serviço permanente continua a funcionar em segundo plano.\033[0m")

def main_service():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("🚀 Iniciando proxy em modo de serviço...")
    
    ports = get_ports_from_state()
    if not ports:
        print("🟡 Nenhuma porta configurada no ficheiro de estado. O serviço está em espera.")
    else:
        for port in ports:
            if isinstance(port, int) and 0 < port < 65536:
                server = Server(LISTENING_ADDR, port)
                server.start()
                if server.running:
                    active_servers[port] = server
        if active_servers:
            print(f"✅ Serviço do proxy ativo com as portas: {', '.join(str(p) for p in sorted(active_servers.keys()))}")
    
    try:
        while not shutdown_requested:
            time.sleep(60)
    except KeyboardInterrupt:
        signal_handler(signal.SIGINT, None)

if __name__ == '__main__':
    if '--install-service' in sys.argv: install_service()
    elif '--uninstall-service' in sys.argv: uninstall_service()
    elif '--help' in sys.argv: display_help()
    elif '--service' in sys.argv:
        try: main_service()
        except KeyboardInterrupt: signal_handler(signal.SIGINT, None)
    else:
        try: main_panel()
        except SystemExit: pass 
        except KeyboardInterrupt: signal_handler(signal.SIGINT, None)
        except Exception as e:
            print(f"\n\033[1;31m❌ Erro inesperado no fluxo principal: {e}\033[0m")
            cleanup_and_exit()