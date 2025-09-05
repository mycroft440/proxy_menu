#!/usr/bin/env python
# encoding: utf-8
# PROXY HÍBRIDO com Painel de Controle por @Crazy_vpn & Scott
# Unifica a funcionalidade de WebSocket (101) e HTTP/Socks (200 OK).
# ATUALIZADO PARA PYTHON 3
import socket, threading, select, sys, time, os

# --- Configurações ---
MSG = '@TMYCOMNECTVPN'
COR = '<font color="null">'
FTAG = '</font>'
PASS = ''
LISTENING_ADDR = '0.0.0.0'
BUFLEN = 8196 * 8
TIMEOUT = 60
DEFAULT_HOST = "127.0.0.1:22"

# --- Respostas personalizadas para cada tipo de conexão ---
RESPONSE_WS = ('HTTP/1.1 101 '+str(COR)+str(MSG)+str(FTAG)+' \r\n\r\n').encode('utf-8')
RESPONSE_HTTP = ("HTTP/1.1 200 " + str(COR) + str(MSG) + str(FTAG) + "\r\n\r\n").encode('utf-8')

# --- Gerenciador de Servidores Ativos ---
active_servers = {}

class Server(threading.Thread):
    def __init__(self, host, port):
        threading.Thread.__init__(self)
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
            while self.running:
                try:
                    c, addr = self.soc.accept()
                    c.setblocking(1)
                except socket.timeout:
                    continue
                except socket.error:
                    break # Socket foi fechado

                conn = ConnectionHandler(c, self, addr)
                conn.start()
                self.addConn(conn)
        finally:
            self.close_all_connections()
            if self.soc:
                self.soc.close()

    def printLog(self, log):
        with self.logLock:
            print(log)

    def addConn(self, conn):
        with self.threadsLock:
            if self.running:
                self.threads.append(conn)

    def removeConn(self, conn):
        with self.threadsLock:
            try:
                self.threads.remove(conn)
            except ValueError:
                pass # Ignora se a conexão já foi removida
    
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
                pass # Ignora se o socket já foi fechado

class ConnectionHandler(threading.Thread):
    def __init__(self, socClient, server, addr):
        threading.Thread.__init__(self)
        self.clientClosed = False
        self.targetClosed = True
        self.client = socClient
        self.client_buffer = b''
        self.server = server
        self.log = 'Connection: ' + str(addr)

    def close(self):
        try:
            if not self.clientClosed:
                self.client.shutdown(socket.SHUT_RDWR)
                self.client.close()
        except:
            pass
        finally:
            self.clientClosed = True

        try:
            if not self.targetClosed:
                self.target.shutdown(socket.SHUT_RDWR)
                self.target.close()
        except:
            pass
        finally:
            self.targetClosed = True

    def run(self):
        try:
            peek_buffer = self.client.recv(1024, socket.MSG_PEEK)
            is_websocket = b'upgrade: websocket' in peek_buffer.lower()

            if is_websocket:
                self.client.sendall(RESPONSE_WS)
                self.client_buffer = self.client.recv(BUFLEN)
                self.process_request(is_websocket=True)
            else:
                self.client_buffer = self.client.recv(BUFLEN)
                if not self.client_buffer: return
                self.process_request(is_websocket=False)

        except Exception as e:
            pass
        finally:
            self.close()
            self.server.removeConn(self)
    
    def process_request(self, is_websocket):
        hostPort = self.findHeader(self.client_buffer, b'X-Real-Host')

        if hostPort == b'':
            hostPort = DEFAULT_HOST.encode('utf-8')

        split = self.findHeader(self.client_buffer, b'X-Split')
        if split != b'':
            self.client.recv(BUFLEN)

        if hostPort != b'':
            passwd = self.findHeader(self.client_buffer, b'X-Pass')
            
            # Decodifica hostPort para a verificação startswith
            hostPort_str = hostPort.decode('utf-8', errors='ignore')

            if len(PASS) != 0 and passwd.decode('utf-8', errors='ignore') == PASS:
                self.method_CONNECT(hostPort_str, not is_websocket)
            elif len(PASS) != 0 and passwd.decode('utf-8', errors='ignore') != PASS:
                self.client.send(b'HTTP/1.1 400 WrongPass!\r\n\r\n')
            elif hostPort_str.startswith('127.0.0.1') or hostPort_str.startswith('localhost'):
                self.method_CONNECT(hostPort_str, not is_websocket)
            else:
                self.client.send(b'HTTP/1.1 403 Forbidden!\r\n\r\n')
        else:
            self.server.printLog('- No X-Real-Host!')
            self.client.send(b'HTTP/1.1 400 NoXRealHost!\r\n\r\n')

    def findHeader(self, head, header):
        aux = head.find(header + b': ')
        if aux == -1: return b''
        head = head[aux+len(header)+2:]
        aux = head.find(b'\r\n')
        if aux == -1: return b''
        return head[:aux]

    def connect_target(self, host):
        i = host.find(':')
        if i != -1:
            port = int(host[i+1:])
            host = host[:i]
        else:
            port = 80
        
        (soc_family, soc_type, proto, _, address) = socket.getaddrinfo(host, port)[0]
        self.target = socket.socket(soc_family, soc_type, proto)
        self.targetClosed = False
        self.target.connect(address)

    def method_CONNECT(self, path, send_200_ok):
        self.log += ' - CONNECT ' + path
        self.connect_target(path)
        if send_200_ok:
            self.client.sendall(RESPONSE_HTTP)
        self.client_buffer = b''
        self.server.printLog(self.log)
        self.doCONNECT()

    def doCONNECT(self):
        socs = [self.client, self.target]
        count = 0
        error = False
        while not error:
            count += 1
            (recv, _, err) = select.select(socs, [], socs, 3)
            if err: error = True
            if recv:
                for in_ in recv:
                    try:
                        data = in_.recv(BUFLEN)
                        if data:
                            if in_ is self.target:
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

# --- Funções do Menu Interativo ---

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def display_menu():
    clear_screen()
    print("\033[1;34m")
    print("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    print("┃      \033[1;32mPAINEL DE CONTROLE PROXY HÍBRIDO\033[1;34m      ┃")
    print("┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫")
    print("┃                                      ┃")
    print("┃   \033[1;36m[1]\033[0m \033[1;37mAbrir Porta\033[1;34m                       ┃")
    print("┃   \033[1;36m[2]\033[0m \033[1;37mFechar Porta\033[1;34m                      ┃")
    print("┃   \033[1;36m[3]\033[0m \033[1;37mStatus das Portas\033[1;34m                 ┃")
    print("┃                                      ┃")
    print("┃   \033[1;31m[0]\033[0m \033[1;37mSair do Painel\033[1;34m                    ┃")
    print("┃                                      ┃")
    print("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    print("\033[0m")

def start_proxy():
    try:
        port = int(input("\n\033[1;33mDigite a porta que deseja abrir: \033[0m"))
        if port in active_servers:
            print("\n\033[1;31mErro: A porta {} já está em uso.\033[0m".format(port))
        elif not 0 < port < 65536:
            print("\n\033[1;31mErro: O número da porta deve estar entre 1 e 65535.\033[0m")
        else:
            server = Server(LISTENING_ADDR, port)
            server.start()
            # Pequena espera para verificar se a thread iniciou com sucesso
            time.sleep(0.1)
            if server.running:
                active_servers[port] = server
                print("\n\033[1;32mProxy iniciado com sucesso na porta {}.\033[0m".format(port))
            else:
                print("\n\033[1;31mFalha ao iniciar o proxy na porta {}. Verifique as permissões ou se a porta já está ocupada por outro processo.\033[0m".format(port))
    except ValueError:
        print("\n\033[1;31mErro: Por favor, digite um número de porta válido.\033[0m")
    input("\n\033[1;37mPressione Enter para voltar ao menu...\033[0m")

def stop_proxy():
    try:
        port = int(input("\n\033[1;33mDigite a porta que deseja fechar: \033[0m"))
        if port in active_servers:
            server = active_servers.pop(port)
            server.close()
            server.join()
            print("\n\033[1;32mProxy na porta {} fechado com sucesso.\033[0m".format(port))
        else:
            print("\n\033[1;31mErro: Não há nenhum proxy ativo na porta {}.\033[0m".format(port))
    except ValueError:
        print("\n\033[1;31mErro: Por favor, digite um número de porta válido.\033[0m")
    input("\n\033[1;37mPressione Enter para voltar ao menu...\033[0m")

def show_status():
    clear_screen()
    print("\033[1;34m")
    print("┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    print("┃   \033[1;32mSTATUS DAS PORTAS ATIVAS\033[1;34m   ┃")
    print("┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫")
    if not active_servers:
        print("┃    \033[1;31mNenhuma porta ativa no\033[1;34m    ┃")
        print("┃         \033[1;31mmomento.\033[1;34m           ┃")
    else:
        for port in sorted(active_servers.keys()):
            status_line = "┃ Porta: \033[1;33m{:<5}\033[0m -> \033[1;32mATIVA\033[1;34m".format(port)
            padding = 30 - len(status_line) + 13
            print(status_line + ' ' * padding + '┃')
    print("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")
    print("\033[0m")
    input("\n\033[1;37mPressione Enter para voltar ao menu...\033[0m")


def main():
    while True:
        display_menu()
        choice = input("\n\033[1;33mEscolha uma opção: \033[0m")
        if choice == '1':
            start_proxy()
        elif choice == '2':
            stop_proxy()
        elif choice == '3':
            show_status()
        elif choice == '0':
            if active_servers:
                print("\n\033[1;31mErro: É necessário fechar todas as portas ativas antes de sair.\033[0m")
                input("\n\033[1;37mPressione Enter para continuar...\033[0m")
            else:
                print("\n\033[1;32mSaindo do painel de controle. Até logo!\033[0m")
                break
        else:
            print("\n\033[1;31mOpção inválida. Tente novamente.\033[0m")
            time.sleep(1)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\033[1;31mSaindo abruptamente. Fechando todas as conexões...\033[0m")
        for port in list(active_servers.keys()):
            server = active_servers.pop(port)
            server.close()
        print("\033[0m")

