import sys
import time
import requests
import threading
from threading import Lock
from bottle import run, route, request, json_dumps, abort, post, put

SYNC_EVERY_SECONDS = 3
lock_t = Lock()
lock_db = Lock()
lock_pk = Lock()
tempo = -1


def set_tempo(novo_tempo=None):
    """
    Incremento e atualização de tempo no relógio lógico
    :param novo_tempo: int: se passado, apenas atualiza o tempo para o valor informado
    :return: int: tempo
    """
    global tempo
    with lock_t:
        # estratégia quando o novo tempo informado não é inteiro ele apenas retorna o próprio valor
        # isso é usado no start do server quando eles tem que sincronizar apenas os peers
        if novo_tempo:
            if type(novo_tempo) == int:
                tempo = novo_tempo
            else:
                return novo_tempo
        else:
            tempo += 1
        return tempo


class DB(object):
    """
    Classe que implementa um banco de dados
    """
    def __init__(self):
        self.pk = 0
        self.produtos = dict()
        self.peers = dict()
        self.eventos = dict()

    def get_produto_pk(self, pk=None):
        with lock_pk:
            if pk:
                if pk > self.pk:
                    self.pk = pk
                return pk
            self.pk += 1
            return self.pk

    def insert_produto(self, seller, nome, qtde, pk=None):
        with lock_db:
            produto = dict(seller=seller, nome=nome, qtde=qtde)
            self.produtos.update({self.get_produto_pk(pk): produto})
            return produto

    def update_produto(self, pk, nome=None, qtde=None):
        with lock_db:
            if nome:
                self.produtos[pk].nome = nome
            if qtde:
                self.produtos[pk].qtde = qtde
            return self.produtos[pk]

    def select_produto(self):
        return [prod.update({'pk': k}) for k, prod in self.produtos.items()]

    def comprar(self, pk, qtde):
        with lock_db:
            produto = self.produtos[pk]
            if produto.get('qtde') >= qtde:
                nova_qtde = produto.get('qtde') - qtde
                self.evento("produto", "update", pk=pk, qtde=nova_qtde)
                return True
            return False

    def insert_peer(self, ip, porta):
        with lock_db:
            peer = dict(ip=ip, porta=porta)
            self.peers['{}:{}'.format(ip, porta)] = peer
            return peer

    def delete_peer(self, ip, porta):
        with lock_db:
            peer = dict(ip=ip, porta=porta)
            self.peers.pop('{}:{}'.format(ip, porta), None)
            return peer

    def select_peer(self):
        with lock_db:
            return list(self.peers.keys())

    def evento(self, tipo, acao, tempo=None, **kwargs):
        """
        Registra um evento, ou seja, uma inserção, edição ou exclusão de um objeto.
        Altera o objeto e depois registra o evento.
        :param tipo: str: tipo do objeto
        :param acao: str: ação executada
        :param tempo: caso for uma sincronização de dados o tempo virá setado
        :param args: dados do objeto
        """
        obj = getattr(self, '{}_{}'.format(acao, tipo))(**kwargs)
        with lock_db:
            tempo = set_tempo(tempo)
            self.eventos[tempo] = dict(tipo=tipo, acao=acao, dados=obj)

    def select_evento(self):
        with lock_db:
            return self.eventos


db = DB()  # BANCO DE DADOS

try:
    initial_peer = sys.argv[2]
except:
    initial_peer = None

# quando conecta pela primeira vez informa o servidor que já está rodando, ele vai nos adicionar à lista de peers
if initial_peer and len(initial_peer.split(':')) != 2:
    print('quando informar um peer, informe ip:porta')
elif initial_peer:
    requests.post('http://' + initial_peer + '/peer', json={'porta': int(sys.argv[1])})
    db.evento("peer", "insert", **dict(ip=initial_peer.split(':')[0], porta=int(initial_peer.split(':')[1])), tempo=initial_peer)


@route('/')
def index():
    return "Hello, World!"


@post('/peer')
def inserir_peer():
    global db

    if not request.json:
        abort(400)
    elif 'porta' in request.json and type(request.json['porta']) != int:
        abort(400)

    db.evento("peer", "insert", **dict(ip=request.remote_addr, porta=request.json['porta'],
                                       tempo='{}:{}'.format(request.remote_addr, request.json['porta'])))

    return json_dumps({'sucesso': 'cadastrado com sucesso'})


@route('/peers')
def listar_peers():
    global db
    return json_dumps({'peers': db.select_peer()})


@route('/produtos')
def listar_produtos():
    global db
    return json_dumps({'produtos': db.select_produto()})


@route('/eventos')
def listar_eventos():
    global db
    return json_dumps({'eventos': db.select_evento()})


@post('/produtos')
def inserir_produto():
    global db

    if not request.json:
        abort(400)
    elif 'nome' in request.json and type(request.json['nome']) != str:
        abort(400)
    elif 'qtde' in request.json and type(request.json['qtde']) != int:
        abort(400)

    db.evento("insert", "produto", **dict(seller=request.remote_addr, nome=request.json['nome'], qtde=request.json['qtde']))

    return json_dumps({'sucesso': 'cadastrado com sucesso'})


@put('/produtos/<id_produto>')
def atualiza_produto(id_produto):
    global db

    if not request.json:
        abort(400)
    elif id_produto not in db.select_produto().keys():
        return json_dumps({'erro', 'produto não existe'}), 404
    elif 'nome' in request.json and type(request.json['nome']) != str:
        abort(400)
    elif 'qtde' in request.json and type(request.json['qtde']) != int:
        abort(400)

    db.evento("update", "produto", **dict(nome=request.json.get('nome'), qtde=request.json.get('qtde')))

    return json_dumps({'sucesso': 'atualizado com sucesso'})


@put('/comprar')
def comprar_produto():
    global db

    if not request.json:
        abort(400)
    elif 'id' in request.json and type(request.json['id']) != int:
        abort(400)
    elif 'qtde' in request.json and type(request.json['qtde']) != int:
        abort(400)
    elif request.json['id'] not in db.select_produto().keys():
        return json_dumps({'erro', 'produto não encontrado'}), 404
    elif db.select_produto().get(request.json['id']).qtde < request.json['qtde']:
        return json_dumps({'erro': 'quantidade insuficiente'})

    resultado = db.comprar(request.json['id'], request.json['qtde'])

    if not resultado:
        return json_dumps({'erro': 'quantidade insuficiente'})

    return json_dumps({'sucesso': 'comprado com sucesso'})


def replicador():
    """
    Passa em todos os peers conhecidos e atualiza os eventos, inclusive os outros peers que foram conectados
    """
    time.sleep(SYNC_EVERY_SECONDS)
    while True:
        time.sleep(SYNC_EVERY_SECONDS)
        peers = list(db.select_peer())
        for peer in peers:
            try:
                r = requests.get('http://' + peer + '/eventos')
            except:
                time.sleep(1)
                continue
            else:
                if r.status_code == 200:
                    eventos = r.json().get('eventos')
                    # só atualizamos os eventos que não temos pra economizar tempo
                    chaves = set(eventos.keys()) - set(db.eventos.keys())

                    for chave in chaves:
                        # a chave é o tempo do evento
                        evento = eventos[chave]
                        db.evento(evento.get('tipo'), evento.get('acao'), chave, **evento.get('dados', {}))

                    time.sleep(1)

        if db.select_peer():
            print(', '.join(db.select_peer()))
        if tempo != -1:
            print(tempo)


t = threading.Thread(target=replicador)
t.start()
run(host='localhost', port=int(sys.argv[1]))
