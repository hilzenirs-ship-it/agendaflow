import os
import re
import unicodedata
import calendar
import sqlite3
from datetime import datetime, timedelta
import urllib.parse
import urllib.request
import urllib.error
import json
import hmac
import hashlib

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from jinja2 import TemplateNotFound
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE_DIR, "banco.db")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "agenda_app_chave_123")

app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"

DIAS_MAX_AGENDAMENTO = 30
INTERVALO_MINUTOS = 60

TESTE_GRATIS_DIAS = int(os.environ.get("TESTE_GRATIS_DIAS", "7"))
PLANO_VALOR = float(os.environ.get("PLANO_VALOR", "14.99"))
PLANO_MOEDA = os.environ.get("PLANO_MOEDA", "BRL").strip() or "BRL"
MP_ACCESS_TOKEN = (os.environ.get("MP_ACCESS_TOKEN") or "").strip()
MP_WEBHOOK_SECRET = (os.environ.get("MP_WEBHOOK_SECRET") or "").strip()
APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
MP_API_BASE = "https://api.mercadopago.com"
MP_TIMEOUT = int(os.environ.get("MP_TIMEOUT", "20"))

MESES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
]

DIAS_SEMANA = [
    "Domingo", "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"
]

DIAS_SEMANA_NOME = [
    "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"
]


# --------------------------------------------------
# LOGIN / SESSÃO
# --------------------------------------------------
def usuario_logado():
    return "usuario" in session and "usuario_id" in session


def usuario_id_logado():
    return session.get("usuario_id")


# --------------------------------------------------
# BANCO
# --------------------------------------------------
def conectar():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def coluna_existe(cur, tabela, coluna):
    info = cur.execute(f"PRAGMA table_info({tabela})").fetchall()
    nomes = [c[1] for c in info]
    return coluna in nomes


def slugify(texto):
    texto = (texto or "").strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("utf-8")
    texto = re.sub(r"[^a-z0-9]+", "-", texto)
    texto = re.sub(r"-+", "-", texto).strip("-")
    return texto or "studio"


def gerar_slug_unico(cur, base_texto):
    base_slug = slugify(base_texto)
    slug_final = base_slug
    contador = 2

    while cur.execute("""
        SELECT id
        FROM usuarios
        WHERE slug = ?
        LIMIT 1
    """, (slug_final,)).fetchone():
        slug_final = f"{base_slug}-{contador}"
        contador += 1

    return slug_final


def criar_usuario_padrao_se_nao_existir(cur):
    usuario_admin = cur.execute("""
        SELECT id
        FROM usuarios
        WHERE usuario = ?
    """, ("admin",)).fetchone()

    if not usuario_admin:
        cur.execute("""
            INSERT INTO usuarios (nome, usuario, senha, slug, email, plano, data_expiracao)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "Administrador",
            "admin",
            generate_password_hash("1234"),
            "admin",
            "",
            "teste",
            (datetime.now() + timedelta(days=TESTE_GRATIS_DIAS)).strftime("%Y-%m-%d")
        ))

    usuario_admin = cur.execute("""
        SELECT id
        FROM usuarios
        WHERE usuario = ?
    """, ("admin",)).fetchone()

    return usuario_admin["id"]


def garantir_configuracao_usuario(cur, usuario_id, nome_profissional="AgendaFlow"):
    existente = cur.execute("""
        SELECT id
        FROM configuracoes_usuario
        WHERE usuario_id = ?
    """, (usuario_id,)).fetchone()

    if not existente:
        cur.execute("""
            INSERT INTO configuracoes_usuario (usuario_id, nome_profissional, whatsapp)
            VALUES (?, ?, ?)
        """, (usuario_id, nome_profissional, ""))


def criar_servicos_exemplo(cur, usuario_id):
    exemplos = [
        ("Tufinho", 35.0, "40min"),
        ("Volume brasileiro", 80.0, "2h"),
        ("Volume egípcio", 80.0, "2h"),
        ("Fox eyes", 100.0, "2h"),
        ("Escova", 45.0, "50min"),
        ("Corte feminino", 60.0, "1h"),
        ("Luzes", 180.0, "3h"),
        ("Sobrancelha", 25.0, "30min"),
    ]

    for nome, preco, duracao in exemplos:
        cur.execute("""
            INSERT INTO servicos (usuario_id, nome, preco, duracao)
            VALUES (?, ?, ?, ?)
        """, (usuario_id, nome, preco, duracao))


def criar_horarios_exemplo(cur, usuario_id):
    exemplos = [
        ("Segunda", "07:00", "18:00"),
        ("Terça", "07:00", "18:00"),
        ("Quarta", "07:00", "18:00"),
        ("Quinta", "07:00", "18:00"),
        ("Sexta", "07:00", "18:00"),
    ]

    for dia_semana, hora_inicio, hora_fim in exemplos:
        cur.execute("""
            INSERT INTO horarios (usuario_id, dia_semana, hora_inicio, hora_fim)
            VALUES (?, ?, ?, ?)
        """, (usuario_id, dia_semana, hora_inicio, hora_fim))


def garantir_dados_iniciais_usuario(usuario_id, nome_profissional="AgendaFlow"):
    con = conectar()
    cur = con.cursor()

    garantir_configuracao_usuario(cur, usuario_id, nome_profissional)

    total_servicos = cur.execute("""
        SELECT COUNT(*) AS total
        FROM servicos
        WHERE usuario_id = ?
    """, (usuario_id,)).fetchone()["total"]

    total_horarios = cur.execute("""
        SELECT COUNT(*) AS total
        FROM horarios
        WHERE usuario_id = ?
    """, (usuario_id,)).fetchone()["total"]

    if total_servicos == 0:
        criar_servicos_exemplo(cur, usuario_id)

    if total_horarios == 0:
        criar_horarios_exemplo(cur, usuario_id)

    con.commit()
    con.close()


def migrar_slugs_antigos(cur):
    usuarios_sem_slug = cur.execute("""
        SELECT id, nome, usuario
        FROM usuarios
        WHERE slug IS NULL OR TRIM(slug) = ''
    """).fetchall()

    for u in usuarios_sem_slug:
        base = u["usuario"] or u["nome"] or f"studio-{u['id']}"
        slug = slugify(base)

        slug_final = slug
        contador = 2

        while cur.execute("""
            SELECT id
            FROM usuarios
            WHERE slug = ? AND id != ?
            LIMIT 1
        """, (slug_final, u["id"])).fetchone():
            slug_final = f"{slug}-{contador}"
            contador += 1

        cur.execute("""
            UPDATE usuarios
            SET slug = ?
            WHERE id = ?
        """, (slug_final, u["id"]))


def criar_tabelas():
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            usuario TEXT NOT NULL UNIQUE,
            senha TEXT NOT NULL,
            slug TEXT UNIQUE,
            email TEXT DEFAULT '',
            plano TEXT DEFAULT 'teste',
            data_expiracao TEXT,
            mp_preapproval_id TEXT DEFAULT '',
            mp_status TEXT DEFAULT '',
            mp_payer_email TEXT DEFAULT '',
            mp_next_billing_date TEXT DEFAULT ''
        )
    """)

    if not coluna_existe(cur, "usuarios", "slug"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN slug TEXT")

    if not coluna_existe(cur, "usuarios", "email"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN email TEXT DEFAULT ''")

    if not coluna_existe(cur, "usuarios", "plano"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN plano TEXT DEFAULT 'teste'")

    if not coluna_existe(cur, "usuarios", "data_expiracao"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN data_expiracao TEXT")

    if not coluna_existe(cur, "usuarios", "mp_preapproval_id"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN mp_preapproval_id TEXT DEFAULT ''")

    if not coluna_existe(cur, "usuarios", "mp_status"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN mp_status TEXT DEFAULT ''")

    if not coluna_existe(cur, "usuarios", "mp_payer_email"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN mp_payer_email TEXT DEFAULT ''")

    if not coluna_existe(cur, "usuarios", "mp_next_billing_date"):
        cur.execute("ALTER TABLE usuarios ADD COLUMN mp_next_billing_date TEXT DEFAULT ''")

    admin_id = criar_usuario_padrao_se_nao_existir(cur)
    migrar_slugs_antigos(cur)

    cur.execute("""
        UPDATE usuarios
        SET plano = COALESCE(NULLIF(plano, ''), 'teste')
    """)

    cur.execute("""
        UPDATE usuarios
        SET data_expiracao = ?
        WHERE usuario = 'admin'
          AND (data_expiracao IS NULL OR TRIM(data_expiracao) = '')
    """, ((datetime.now() + timedelta(days=TESTE_GRATIS_DIAS)).strftime("%Y-%m-%d"),))

    cur.execute("""
        UPDATE usuarios
        SET data_expiracao = ?
        WHERE plano = 'teste'
          AND (data_expiracao IS NULL OR TRIM(data_expiracao) = '')
    """, ((datetime.now() + timedelta(days=TESTE_GRATIS_DIAS)).strftime("%Y-%m-%d"),))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS servicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            nome TEXT NOT NULL,
            preco REAL DEFAULT 0,
            duracao TEXT DEFAULT ''
        )
    """)

    if not coluna_existe(cur, "servicos", "usuario_id"):
        cur.execute("ALTER TABLE servicos ADD COLUMN usuario_id INTEGER")

    if not coluna_existe(cur, "servicos", "duracao"):
        cur.execute("ALTER TABLE servicos ADD COLUMN duracao TEXT DEFAULT ''")

    cur.execute("""
        UPDATE servicos
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS horarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            dia_semana TEXT,
            hora_inicio TEXT,
            hora_fim TEXT
        )
    """)

    if not coluna_existe(cur, "horarios", "usuario_id"):
        cur.execute("ALTER TABLE horarios ADD COLUMN usuario_id INTEGER")

    cur.execute("""
        UPDATE horarios
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            nome TEXT,
            telefone TEXT,
            observacoes TEXT DEFAULT ''
        )
    """)

    if not coluna_existe(cur, "clientes", "usuario_id"):
        cur.execute("ALTER TABLE clientes ADD COLUMN usuario_id INTEGER")

    if not coluna_existe(cur, "clientes", "observacoes"):
        cur.execute("ALTER TABLE clientes ADD COLUMN observacoes TEXT DEFAULT ''")

    cur.execute("""
        UPDATE clientes
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            cliente TEXT,
            telefone TEXT,
            servico TEXT,
            data TEXT,
            hora TEXT,
            criado_em TEXT,
            observacoes TEXT DEFAULT ''
        )
    """)

    if not coluna_existe(cur, "agendamentos", "usuario_id"):
        cur.execute("ALTER TABLE agendamentos ADD COLUMN usuario_id INTEGER")

    if not coluna_existe(cur, "agendamentos", "observacoes"):
        cur.execute("ALTER TABLE agendamentos ADD COLUMN observacoes TEXT DEFAULT ''")

    cur.execute("""
        UPDATE agendamentos
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS disponibilidade_dia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            data TEXT NOT NULL,
            hora TEXT NOT NULL,
            status TEXT DEFAULT 'livre'
        )
    """)

    if not coluna_existe(cur, "disponibilidade_dia", "usuario_id"):
        cur.execute("ALTER TABLE disponibilidade_dia ADD COLUMN usuario_id INTEGER")

    cur.execute("""
        UPDATE disponibilidade_dia
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configuracao_dia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER,
            data TEXT NOT NULL,
            tipo TEXT DEFAULT 'padrao',
            hora_inicio TEXT DEFAULT '',
            hora_fim TEXT DEFAULT ''
        )
    """)

    if not coluna_existe(cur, "configuracao_dia", "usuario_id"):
        cur.execute("ALTER TABLE configuracao_dia ADD COLUMN usuario_id INTEGER")

    cur.execute("""
        UPDATE configuracao_dia
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configuracoes_usuario (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL UNIQUE,
            nome_profissional TEXT DEFAULT 'AgendaFlow',
            whatsapp TEXT DEFAULT ''
        )
    """)

    garantir_configuracao_usuario(cur, admin_id, "AgendaFlow")

    con.commit()
    con.close()


criar_tabelas()


# --------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------
# --------------------------------------------------
# PLANO / ASSINATURA
# --------------------------------------------------
def data_str_hoje():
    return datetime.now().strftime("%Y-%m-%d")


def adicionar_dias(data_base, dias):
    return (data_base + timedelta(days=dias)).strftime("%Y-%m-%d")


def formatar_data_br(data_str):
    if not data_str:
        return ""
    try:
        return datetime.strptime(data_str[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return data_str


def obter_usuario(cur, usuario_id):
    return cur.execute("""
        SELECT *
        FROM usuarios
        WHERE id = ?
        LIMIT 1
    """, (usuario_id,)).fetchone()


def obter_status_plano_usuario(usuario_id):
    con = conectar()
    cur = con.cursor()

    usuario = obter_usuario(cur, usuario_id)
    con.close()

    if not usuario:
        return {
            "plano_ativo": False,
            "tipo_plano": "nenhum",
            "data_expiracao": "",
            "status_mp": "",
            "tem_assinatura_ativa": False
        }

    plano = (usuario["plano"] or "").strip().lower()
    data_expiracao = (usuario["data_expiracao"] or "").strip()
    mp_status = (usuario["mp_status"] or "").strip().lower()

    plano_ativo = False
    tipo_plano = "nenhum"

    if plano == "ativo" and mp_status == "authorized":
        plano_ativo = True
        tipo_plano = "pago"
    elif plano == "teste" and data_expiracao:
        try:
            plano_ativo = datetime.now().date() <= datetime.strptime(data_expiracao, "%Y-%m-%d").date()
            tipo_plano = "teste"
        except ValueError:
            plano_ativo = False

    return {
        "plano_ativo": plano_ativo,
        "tipo_plano": tipo_plano,
        "data_expiracao": formatar_data_br(data_expiracao),
        "status_mp": mp_status,
        "tem_assinatura_ativa": mp_status == "authorized"
    }


def usuario_tem_acesso(usuario_id):
    status = obter_status_plano_usuario(usuario_id)
    return status["plano_ativo"]


def atualizar_plano_local(cur, usuario_id, plano, data_expiracao=None, mp_preapproval_id=None, mp_status=None,
                          mp_payer_email=None, mp_next_billing_date=None, email=None):
    partes = ["plano = ?"]
    valores = [plano]

    if data_expiracao is not None:
        partes.append("data_expiracao = ?")
        valores.append(data_expiracao)

    if mp_preapproval_id is not None:
        partes.append("mp_preapproval_id = ?")
        valores.append(mp_preapproval_id)

    if mp_status is not None:
        partes.append("mp_status = ?")
        valores.append(mp_status)

    if mp_payer_email is not None:
        partes.append("mp_payer_email = ?")
        valores.append(mp_payer_email)

    if mp_next_billing_date is not None:
        partes.append("mp_next_billing_date = ?")
        valores.append(mp_next_billing_date)

    if email is not None:
        partes.append("email = ?")
        valores.append(email)

    valores.append(usuario_id)

    cur.execute(f"""
        UPDATE usuarios
        SET {", ".join(partes)}
        WHERE id = ?
    """, tuple(valores))


def mp_headers(extra=None):
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    if extra:
        headers.update(extra)
    return headers


def mp_request(method, path, payload=None, query=None):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN não configurado.")

    url = f"{MP_API_BASE}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"

    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers=mp_headers(),
        method=method.upper()
    )

    try:
        with urllib.request.urlopen(req, timeout=MP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Mercado Pago HTTP {exc.code}: {detalhe}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Erro de conexão com Mercado Pago: {exc}")


def criar_assinatura_mercadopago(usuario_id, payer_email):
    if not APP_BASE_URL:
        raise RuntimeError("APP_BASE_URL não configurada.")

    payload = {
        "reason": f"AgendaFlow - Plano mensal R$ {PLANO_VALOR:.2f}",
        "external_reference": str(usuario_id),
        "payer_email": payer_email,
        "back_url": f"{APP_BASE_URL}/assinatura/retorno",
        "notification_url": f"{APP_BASE_URL}/webhook/mercadopago",
        "status": "pending",
        "auto_recurring": {
            "frequency": 1,
            "frequency_type": "months",
            "transaction_amount": PLANO_VALOR,
            "currency_id": PLANO_MOEDA
        }
    }

    return mp_request("POST", "/preapproval", payload=payload)


def obter_assinatura_mercadopago(preapproval_id):
    return mp_request("GET", f"/preapproval/{preapproval_id}")


def validar_assinatura_webhook(request_obj):
    if not MP_WEBHOOK_SECRET:
        return True

    assinatura = (request_obj.headers.get("x-signature") or "").strip()
    request_id = (request_obj.headers.get("x-request-id") or "").strip()

    if not assinatura or not request_id:
        return False

    partes = {}
    for item in assinatura.split(","):
        if "=" in item:
            chave, valor = item.split("=", 1)
            partes[chave.strip()] = valor.strip()

    ts = partes.get("ts", "")
    v1 = partes.get("v1", "")

    data_id = ""
    body_json = request_obj.get_json(silent=True) or {}
    if isinstance(body_json, dict):
        data_id = (
            body_json.get("data", {}).get("id")
            or body_json.get("id")
            or request_obj.args.get("data.id")
            or request_obj.args.get("id")
            or ""
        )

    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    assinatura_calculada = hmac.new(
        MP_WEBHOOK_SECRET.encode("utf-8"),
        msg=manifest.encode("utf-8"),
        digestmod=hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(assinatura_calculada, v1)


def sincronizar_assinatura_por_preapproval(preapproval_id):
    if not preapproval_id:
        return False, "ID da assinatura não informado."

    dados = obter_assinatura_mercadopago(preapproval_id)
    if not isinstance(dados, dict):
        return False, "Resposta inválida do Mercado Pago."

    external_reference = str(dados.get("external_reference") or "").strip()
    if not external_reference.isdigit():
        return False, "Assinatura sem external_reference numérico."

    usuario_id = int(external_reference)
    status = (dados.get("status") or "").strip().lower()
    payer_email = (dados.get("payer_email") or "").strip()
    next_billing_date = (dados.get("next_payment_date") or "").strip()
    data_expiracao = ""

    if next_billing_date:
        data_expiracao = next_billing_date[:10]

    if status == "authorized":
        plano = "ativo"
        if not data_expiracao:
            data_expiracao = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
    elif status == "pending":
        plano = "teste"
        con = conectar()
        cur = con.cursor()
        user = obter_usuario(cur, usuario_id)
        con.close()
        data_expiracao = (user["data_expiracao"] or "") if user else ""
    else:
        plano = "vencido"
        data_expiracao = datetime.now().strftime("%Y-%m-%d")

    con = conectar()
    cur = con.cursor()
    atualizar_plano_local(
        cur,
        usuario_id,
        plano=plano,
        data_expiracao=data_expiracao,
        mp_preapproval_id=preapproval_id,
        mp_status=status,
        mp_payer_email=payer_email,
        mp_next_billing_date=next_billing_date,
        email=payer_email or None
    )
    con.commit()
    con.close()

    return True, status


def usuario_tem_config_mp():
    return bool(MP_ACCESS_TOKEN and APP_BASE_URL)


def pagina_assinatura_html(config, email_atual="", erro="", info=""):
    nome_studio = config.get("nome_profissional", "AgendaFlow")
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Assinar - AgendaFlow</title>
<style>
*{{box-sizing:border-box;font-family:Arial,Helvetica,sans-serif;}}
body{{margin:0;min-height:100vh;background:linear-gradient(135deg,#f8edf2 0%,#f3eefb 100%);display:flex;align-items:center;justify-content:center;padding:24px;color:#43374a;}}
.card{{width:100%;max-width:460px;background:#fff;border-radius:28px;padding:28px;box-shadow:0 18px 50px rgba(123,90,224,0.12);border:1px solid rgba(210,196,235,0.45);}}
.logo{{width:76px;height:76px;margin:0 auto 14px;border-radius:24px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#e78fb3,#7b5ae0);color:#fff;font-size:32px;font-weight:bold;}}
h1{{margin:0 0 8px;text-align:center;color:#57486c;}}
.sub{{text-align:center;color:#8a7f9c;line-height:1.5;margin-bottom:16px;}}
.plano{{background:linear-gradient(135deg,rgba(231,143,179,0.12),rgba(123,90,224,0.08));border:1px solid #eadff4;border-radius:18px;padding:16px;margin-bottom:16px;}}
.plano strong{{color:#4f4362;}}
.campo{{margin-bottom:14px;}}
label{{display:block;font-size:14px;font-weight:bold;margin-bottom:7px;color:#6c5d81;}}
input{{width:100%;padding:14px 15px;border-radius:14px;border:1px solid #ddd5ec;background:#fff;font-size:15px;color:#4f4362;}}
input:focus{{outline:none;border-color:#b487ea;box-shadow:0 0 0 4px rgba(180,135,234,0.12);}}
.btn{{width:100%;border:none;border-radius:16px;padding:15px;font-size:17px;font-weight:bold;color:white;cursor:pointer;background:linear-gradient(135deg,#e78fb3,#7b5ae0);box-shadow:0 10px 24px rgba(123,90,224,0.18);}}
.msg{{padding:12px 14px;border-radius:14px;margin-bottom:14px;font-size:14px;line-height:1.5;}}
.erro{{background:#fff0f3;color:#b24065;border:1px solid #f4bfd0;}}
.info{{background:#faf6ff;color:#5f536d;border:1px solid #eadff4;}}
.links{{margin-top:14px;text-align:center;font-size:14px;}}
a{{color:#7b5ae0;text-decoration:none;font-weight:bold;}}
</style>
</head>
<body>
<div class="card">
<div class="logo">A</div>
<h1>Assinar AgendaFlow</h1>
<div class="sub">{nome_studio}</div>
<div class="plano">
<strong>Plano mensal:</strong> R$ {PLANO_VALOR:.2f}/mês<br>
<strong>Teste grátis:</strong> {TESTE_GRATIS_DIAS} dias<br>
Pagamento recorrente pelo Mercado Pago.
</div>
{f'<div class="msg erro">{erro}</div>' if erro else ''}
{f'<div class="msg info">{info}</div>' if info else ''}
<form method="POST" action="/assinar">
<div class="campo">
<label for="email">E-mail para a assinatura</label>
<input type="email" id="email" name="email" placeholder="seuemail@exemplo.com" value="{email_atual}" required>
</div>
<button type="submit" class="btn">Continuar para o pagamento</button>
</form>
<div class="links">
<a href="/dashboard">Voltar ao painel</a>
</div>
</div>
</body>
</html>"""


def render_primeiro_template(opcoes, **contexto):
    for nome in opcoes:
        try:
            return render_template(nome, **contexto)
        except TemplateNotFound:
            continue
    return "<h1>Template não encontrado</h1>"


def gerar_horarios_intervalo(hora_inicio, hora_fim, intervalo_minutos=60):
    horarios = []

    try:
        inicio = datetime.strptime(hora_inicio, "%H:%M")
        fim = datetime.strptime(hora_fim, "%H:%M")
    except ValueError:
        return horarios

    atual = inicio
    while atual < fim:
        horarios.append(atual.strftime("%H:%M"))
        atual += timedelta(minutes=intervalo_minutos)

    return horarios


def dia_semana_por_data(data_str):
    try:
        data_obj = datetime.strptime(data_str, "%Y-%m-%d")
        return DIAS_SEMANA_NOME[data_obj.weekday()]
    except ValueError:
        return ""


def data_dentro_limite(data_str):
    try:
        data_obj = datetime.strptime(data_str, "%Y-%m-%d").date()
    except ValueError:
        return False

    hoje = datetime.now().date()
    limite_final = hoje + timedelta(days=DIAS_MAX_AGENDAMENTO)
    return hoje <= data_obj <= limite_final


def buscar_horario_semanal(cur, usuario_id, data_str):
    nome_dia = dia_semana_por_data(data_str)
    if not nome_dia:
        return None

    return cur.execute("""
        SELECT *
        FROM horarios
        WHERE usuario_id = ? AND dia_semana = ?
        ORDER BY id DESC
        LIMIT 1
    """, (usuario_id, nome_dia)).fetchone()


def buscar_configuracao_dia(cur, usuario_id, data_str):
    return cur.execute("""
        SELECT *
        FROM configuracao_dia
        WHERE usuario_id = ? AND data = ?
        ORDER BY id DESC
        LIMIT 1
    """, (usuario_id, data_str)).fetchone()


def buscar_horarios_base_por_data(cur, usuario_id, data_str):
    config_dia = buscar_configuracao_dia(cur, usuario_id, data_str)

    if config_dia:
        tipo = (config_dia["tipo"] or "padrao").strip().lower()

        if tipo == "fechado":
            return []

        if tipo == "personalizado":
            hora_inicio = (config_dia["hora_inicio"] or "").strip()
            hora_fim = (config_dia["hora_fim"] or "").strip()

            if hora_inicio and hora_fim:
                return gerar_horarios_intervalo(
                    hora_inicio,
                    hora_fim,
                    INTERVALO_MINUTOS
                )

    horario_semana = buscar_horario_semanal(cur, usuario_id, data_str)

    if not horario_semana:
        return []

    return gerar_horarios_intervalo(
        horario_semana["hora_inicio"],
        horario_semana["hora_fim"],
        INTERVALO_MINUTOS
    )


def buscar_horarios_disponiveis(cur, usuario_id, data_selecionada):
    if not data_selecionada or not data_dentro_limite(data_selecionada):
        return [], []

    horarios_base = buscar_horarios_base_por_data(cur, usuario_id, data_selecionada)

    horas_ocupadas = [
        row["hora"] for row in cur.execute("""
            SELECT hora
            FROM agendamentos
            WHERE usuario_id = ? AND data = ?
            ORDER BY hora
        """, (usuario_id, data_selecionada)).fetchall()
    ]

    disponibilidade = cur.execute("""
        SELECT hora, status
        FROM disponibilidade_dia
        WHERE usuario_id = ? AND data = ?
        ORDER BY hora
    """, (usuario_id, data_selecionada)).fetchall()

    horas_ocupadas_set = set(horas_ocupadas)

    if disponibilidade:
        status_por_hora = {row["hora"]: row["status"] for row in disponibilidade}

        horas_livres = []
        for hora in horarios_base:
            if hora in horas_ocupadas_set:
                continue

            status = status_por_hora.get(hora)

            if status in ("fechado", "bloqueado"):
                continue

            horas_livres.append(hora)

        horas_extras = [
            row["hora"] for row in disponibilidade
            if row["status"] == "livre"
            and row["hora"] not in horas_ocupadas_set
            and row["hora"] not in horas_livres
        ]

        horas_livres.extend(horas_extras)
        horas_livres = sorted(horas_livres)

        return horas_livres, horas_ocupadas

    horas_livres_padrao = [
        hora for hora in horarios_base
        if hora not in horas_ocupadas_set
    ]

    return horas_livres_padrao, horas_ocupadas


def obter_configuracoes(usuario_id):
    con = conectar()
    cur = con.cursor()

    config = cur.execute("""
        SELECT nome_profissional, whatsapp
        FROM configuracoes_usuario
        WHERE usuario_id = ?
        LIMIT 1
    """, (usuario_id,)).fetchone()

    con.close()

    if config:
        return {
            "nome_profissional": config["nome_profissional"] or "AgendaFlow",
            "whatsapp": config["whatsapp"] or ""
        }

    return {
        "nome_profissional": "AgendaFlow",
        "whatsapp": ""
    }


def salvar_configuracoes(usuario_id, nome_profissional, whatsapp):
    con = conectar()
    cur = con.cursor()

    garantir_configuracao_usuario(cur, usuario_id, nome_profissional or "AgendaFlow")

    cur.execute("""
        UPDATE configuracoes_usuario
        SET nome_profissional = ?, whatsapp = ?
        WHERE usuario_id = ?
    """, (nome_profissional, whatsapp, usuario_id))

    con.commit()
    con.close()


def montar_status_dias_agendamento(cur, usuario_id, hoje, data_maxima):
    dias_livres = []
    dias_ocupados = []

    data_atual_loop = hoje
    while data_atual_loop <= data_maxima:
        data_str = data_atual_loop.strftime("%Y-%m-%d")
        horarios_livres_dia, _ = buscar_horarios_disponiveis(cur, usuario_id, data_str)

        if horarios_livres_dia:
            dias_livres.append(data_str)
        else:
            dias_ocupados.append(data_str)

        data_atual_loop += timedelta(days=1)

    return dias_livres, dias_ocupados


def obter_coluna_nome_cliente_agendamentos(cur):
    colunas = [col[1] for col in cur.execute("PRAGMA table_info(agendamentos)").fetchall()]
    if "cliente_nome" in colunas:
        return "cliente_nome"
    return "cliente"


def buscar_slug_usuario(usuario_id):
    con = conectar()
    cur = con.cursor()

    row = cur.execute("""
        SELECT slug
        FROM usuarios
        WHERE id = ?
        LIMIT 1
    """, (usuario_id,)).fetchone()

    con.close()

    if row and row["slug"]:
        return row["slug"]
    return ""


# --------------------------------------------------
# CADASTRO / LOGIN
# --------------------------------------------------
@app.before_request
def proteger_rotas_com_plano():
    endpoint = request.endpoint or ""

    publicos = {
        "login",
        "cadastro",
        "logout",
        "book_sem_usuario",
        "redirecionar_book_id",
        "agendar_publico_slug",
        "sucesso",
        "mercadopago_webhook",
        "assinatura_retorno"
    }

    liberados_sem_plano = {
        "dashboard",
        "configuracoes",
        "assinar",
        "assinatura_bloqueada",
        "logout",
        "verificar_novos"
    }

    if endpoint.startswith("static"):
        return None

    if endpoint in publicos:
        return None

    if not usuario_logado():
        return None

    if endpoint in liberados_sem_plano:
        return None

    if not usuario_tem_acesso(usuario_id_logado()):
        return redirect(url_for("assinatura_bloqueada"))


@app.route("/cadastro", methods=["GET", "POST"])
def cadastro():
    erro = ""
    sucesso = ""

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        usuario = (request.form.get("usuario") or "").strip()
        senha = (request.form.get("senha") or "").strip()
        confirmar_senha = (request.form.get("confirmar_senha") or "").strip()

        if not nome or not usuario or not senha or not confirmar_senha:
            erro = "Preencha todos os campos."
            return render_primeiro_template(
                ["cadastro.html", "login.html"],
                erro=erro,
                sucesso=sucesso
            )

        if senha != confirmar_senha:
            erro = "As senhas não coincidem."
            return render_primeiro_template(
                ["cadastro.html", "login.html"],
                erro=erro,
                sucesso=sucesso
            )

        con = conectar()
        cur = con.cursor()

        usuario_existente = cur.execute("""
            SELECT id
            FROM usuarios
            WHERE usuario = ?
        """, (usuario,)).fetchone()

        if usuario_existente:
            con.close()
            erro = "Esse usuário já existe."
            return render_primeiro_template(
                ["cadastro.html", "login.html"],
                erro=erro,
                sucesso=sucesso
            )

        slug = gerar_slug_unico(cur, nome or usuario)

        data_expiracao = (datetime.now() + timedelta(days=TESTE_GRATIS_DIAS)).strftime("%Y-%m-%d")

        cur.execute("""
            INSERT INTO usuarios (nome, usuario, senha, slug, email, plano, data_expiracao)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            nome,
            usuario,
            generate_password_hash(senha),
            slug,
            "",
            "teste",
            data_expiracao
        ))

        novo_usuario_id = cur.lastrowid
        garantir_configuracao_usuario(cur, novo_usuario_id, nome)
        criar_servicos_exemplo(cur, novo_usuario_id)
        criar_horarios_exemplo(cur, novo_usuario_id)

        con.commit()
        con.close()

        sucesso = "Cadastro realizado com sucesso. Agora faça login."
        return render_primeiro_template(
            ["cadastro.html", "login.html"],
            erro="",
            sucesso=sucesso
        )

    return render_primeiro_template(
        ["cadastro.html", "login.html"],
        erro=erro,
        sucesso=sucesso
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = ""

    if request.method == "POST":
        usuario = (request.form.get("usuario") or "").strip()
        senha = (request.form.get("senha") or "").strip()

        con = conectar()
        cur = con.cursor()

        dados = cur.execute("""
            SELECT *
            FROM usuarios
            WHERE usuario = ?
            LIMIT 1
        """, (usuario,)).fetchone()

        con.close()

        if dados and check_password_hash(dados["senha"], senha):
            session.clear()
            session["usuario"] = dados["usuario"]
            session["nome"] = dados["nome"]
            session["usuario_id"] = dados["id"]
            session["slug"] = dados["slug"]

            garantir_dados_iniciais_usuario(dados["id"], dados["nome"] or "AgendaFlow")
            return redirect(url_for("dashboard"))
        else:
            erro = "Usuário ou senha inválidos."

    return render_template("login.html", erro=erro)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------
# TELA INICIAL / DASHBOARD
# --------------------------------------------------
@app.route("/")
@app.route("/dashboard")
def dashboard():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    garantir_dados_iniciais_usuario(usuario_id, session.get("nome") or "AgendaFlow")

    con = conectar()
    cur = con.cursor()

    agendamentos = cur.execute("""
        SELECT *
        FROM agendamentos
        WHERE usuario_id = ?
        ORDER BY data ASC, hora ASC
    """, (usuario_id,)).fetchall()

    total_agendamentos = cur.execute("""
        SELECT COUNT(*) AS total
        FROM agendamentos
        WHERE usuario_id = ?
    """, (usuario_id,)).fetchone()["total"]

    total_clientes = cur.execute("""
        SELECT COUNT(*) AS total
        FROM clientes
        WHERE usuario_id = ?
    """, (usuario_id,)).fetchone()["total"]

    total_servicos = cur.execute("""
        SELECT COUNT(*) AS total
        FROM servicos
        WHERE usuario_id = ?
    """, (usuario_id,)).fetchone()["total"]

    faturamento_row = cur.execute("""
        SELECT COALESCE(SUM(s.preco), 0) AS total
        FROM agendamentos a
        LEFT JOIN servicos s
            ON a.servico = s.nome
           AND s.usuario_id = a.usuario_id
        WHERE a.usuario_id = ?
    """, (usuario_id,)).fetchone()

    usuario_row = cur.execute("""
        SELECT slug
        FROM usuarios
        WHERE id = ?
        LIMIT 1
    """, (usuario_id,)).fetchone()

    faturamento = faturamento_row["total"] if faturamento_row else 0
    slug_usuario = usuario_row["slug"] if usuario_row and usuario_row["slug"] else ""

    con.close()

    config = obter_configuracoes(usuario_id)
    status_plano = obter_status_plano_usuario(usuario_id)

    return render_primeiro_template(
        ["dashboard.html", "agenda.html"],
        agendamentos=agendamentos,
        total_agendamentos=total_agendamentos,
        total_clientes=total_clientes,
        total_servicos=total_servicos,
        faturamento=faturamento,
        config=config,
        usuario_id=usuario_id,
        slug_usuario=slug_usuario,
        plano_ativo=status_plano["plano_ativo"],
        data_expiracao=status_plano["data_expiracao"],
        tipo_plano=status_plano["tipo_plano"],
        status_mp=status_plano["status_mp"]
    )


# --------------------------------------------------
# AGENDA
# --------------------------------------------------
@app.route("/agenda")
def agenda():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    hoje = datetime.now()
    mes = request.args.get("mes", type=int) or hoje.month
    ano = request.args.get("ano", type=int) or hoje.year
    data_selecionada = (request.args.get("data") or "").strip()

    if mes < 1:
        mes = 12
        ano -= 1
    elif mes > 12:
        mes = 1
        ano += 1

    cal = calendar.Calendar(firstweekday=0)
    calendario_mes = cal.monthdayscalendar(ano, mes)
    mes_nome = MESES_PT[mes - 1]

    con = conectar()
    cur = con.cursor()

    inicio_mes = f"{ano:04d}-{mes:02d}-01"

    if mes == 12:
        proximo_mes_data = f"{ano + 1:04d}-01-01"
    else:
        proximo_mes_data = f"{ano:04d}-{mes + 1:02d}-01"

    agendamentos_bd = cur.execute("""
        SELECT id, cliente, telefone, servico, data, hora, observacoes
        FROM agendamentos
        WHERE usuario_id = ?
          AND data >= ?
          AND data < ?
        ORDER BY data ASC, hora ASC
    """, (usuario_id, inicio_mes, proximo_mes_data)).fetchall()

    agendamentos_por_dia = {}

    for ag in agendamentos_bd:
        data_ag = ag["data"]

        if data_ag not in agendamentos_por_dia:
            agendamentos_por_dia[data_ag] = []

        agendamentos_por_dia[data_ag].append({
            "id": ag["id"],
            "cliente": ag["cliente"],
            "telefone": ag["telefone"],
            "servico": ag["servico"],
            "data": ag["data"],
            "hora": ag["hora"],
            "observacao": ag["observacoes"]
        })

    agendamentos_dia = []
    data_formatada = ""
    nome_dia_semana = ""
    eh_hoje_detalhe = False

    if data_selecionada:
        try:
            data_obj = datetime.strptime(data_selecionada, "%Y-%m-%d")
            agendamentos_dia = cur.execute("""
                SELECT id, cliente, telefone, servico, data, hora, observacoes AS observacao
                FROM agendamentos
                WHERE usuario_id = ? AND data = ?
                ORDER BY hora ASC
            """, (usuario_id, data_selecionada)).fetchall()

            data_formatada = data_obj.strftime("%d/%m/%Y")
            nome_dia_semana = DIAS_SEMANA_NOME[data_obj.weekday()]
            eh_hoje_detalhe = data_obj.date() == hoje.date()
        except ValueError:
            data_selecionada = ""
            agendamentos_dia = []

    con.close()

    if mes == 1:
        mes_anterior = 12
        ano_anterior = ano - 1
    else:
        mes_anterior = mes - 1
        ano_anterior = ano

    if mes == 12:
        proximo_mes = 1
        proximo_ano = ano + 1
    else:
        proximo_mes = mes + 1
        proximo_ano = ano

    hoje_str = hoje.strftime("%Y-%m-%d")
    config = obter_configuracoes(usuario_id)

    return render_template(
        "agenda.html",
        mes=mes,
        ano=ano,
        mes_nome=mes_nome,
        calendario=calendario_mes,
        agendamentos_por_dia=agendamentos_por_dia,
        mes_anterior=mes_anterior,
        ano_anterior=ano_anterior,
        proximo_mes=proximo_mes,
        proximo_ano=proximo_ano,
        hoje=hoje_str,
        mes_atual=(mes == hoje.month),
        ano_atual=(ano == hoje.year),
        data_selecionada=data_selecionada,
        data_formatada=data_formatada,
        nome_dia_semana=nome_dia_semana,
        agendamentos_dia=agendamentos_dia,
        eh_hoje_detalhe=eh_hoje_detalhe,
        config=config
    )


@app.route("/agenda/dia/<data>")
def agenda_dia(data):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()

    try:
        data_obj = datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("agenda"))

    con = conectar()
    cur = con.cursor()

    agendamentos_dia = cur.execute("""
        SELECT id, cliente, telefone, servico, data, hora, observacoes AS observacao
        FROM agendamentos
        WHERE usuario_id = ? AND data = ?
        ORDER BY hora ASC
    """, (usuario_id, data)).fetchall()

    con.close()

    data_formatada = data_obj.strftime("%d/%m/%Y")
    nome_dia_semana = DIAS_SEMANA_NOME[data_obj.weekday()]
    eh_hoje = data_obj.date() == datetime.now().date()

    return render_template(
        "dia_agenda.html",
        data=data,
        data_obj=data_obj,
        data_formatada=data_formatada,
        nome_dia_semana=nome_dia_semana,
        agendamentos=agendamentos_dia,
        eh_hoje=eh_hoje
    )


# --------------------------------------------------
# EDITAR DISPONIBILIDADE POR DIA
# --------------------------------------------------
@app.route("/agenda/dia/<data>/disponibilidade", methods=["GET", "POST"])
def editar_disponibilidade(data):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()

    try:
        data_obj = datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("agenda"))

    con = conectar()
    cur = con.cursor()

    config_dia = buscar_configuracao_dia(cur, usuario_id, data)

    acao_dia = ""
    hora_inicio_personalizada = ""
    hora_fim_personalizada = ""

    if config_dia:
        tipo_config = (config_dia["tipo"] or "padrao").strip().lower()

        if tipo_config == "fechado":
            acao_dia = "fechado"
        elif tipo_config == "personalizado":
            acao_dia = "personalizado"
            hora_inicio_personalizada = (config_dia["hora_inicio"] or "").strip()
            hora_fim_personalizada = (config_dia["hora_fim"] or "").strip()
        else:
            acao_dia = "aberto"
    else:
        acao_dia = "aberto"

    if request.method == "POST":
        acao_dia = (request.form.get("acao_dia") or "aberto").strip().lower()
        horarios_marcados = request.form.getlist("horarios")
        hora_inicio_personalizada = (request.form.get("hora_inicio_personalizada") or "").strip()
        hora_fim_personalizada = (request.form.get("hora_fim_personalizada") or "").strip()

        cur.execute("""
            DELETE FROM disponibilidade_dia
            WHERE usuario_id = ? AND data = ?
        """, (usuario_id, data))

        cur.execute("""
            DELETE FROM configuracao_dia
            WHERE usuario_id = ? AND data = ?
        """, (usuario_id, data))

        if acao_dia == "fechado":
            cur.execute("""
                INSERT INTO configuracao_dia (usuario_id, data, tipo, hora_inicio, hora_fim)
                VALUES (?, ?, ?, ?, ?)
            """, (usuario_id, data, "fechado", "", ""))

            con.commit()
            con.close()
            return redirect(url_for("agenda", data=data))

        elif acao_dia == "personalizado":
            if not hora_inicio_personalizada or not hora_fim_personalizada:
                agendamentos_dia = cur.execute("""
                    SELECT hora
                    FROM agendamentos
                    WHERE usuario_id = ? AND data = ?
                """, (usuario_id, data)).fetchall()

                horas_agendadas = [row["hora"] for row in agendamentos_dia]
                horarios_tela = []

                for hora in sorted(horas_agendadas):
                    horarios_tela.append({
                        "hora": hora,
                        "status": "livre",
                        "agendado": True
                    })

                con.close()
                return render_template(
                    "editar_disponibilidade.html",
                    data=data,
                    data_obj=data_obj,
                    data_formatada=data_obj.strftime("%d/%m/%Y"),
                    nome_dia_semana=DIAS_SEMANA_NOME[data_obj.weekday()],
                    horarios_tela=horarios_tela,
                    dia_fechado=False,
                    acao_dia="personalizado",
                    hora_inicio_personalizada=hora_inicio_personalizada,
                    hora_fim_personalizada=hora_fim_personalizada
                )

            try:
                dt_inicio = datetime.strptime(hora_inicio_personalizada, "%H:%M")
                dt_fim = datetime.strptime(hora_fim_personalizada, "%H:%M")
            except ValueError:
                agendamentos_dia = cur.execute("""
                    SELECT hora
                    FROM agendamentos
                    WHERE usuario_id = ? AND data = ?
                """, (usuario_id, data)).fetchall()

                horas_agendadas = [row["hora"] for row in agendamentos_dia]
                horarios_tela = []

                for hora in sorted(horas_agendadas):
                    horarios_tela.append({
                        "hora": hora,
                        "status": "livre",
                        "agendado": True
                    })

                con.close()
                return render_template(
                    "editar_disponibilidade.html",
                    data=data,
                    data_obj=data_obj,
                    data_formatada=data_obj.strftime("%d/%m/%Y"),
                    nome_dia_semana=DIAS_SEMANA_NOME[data_obj.weekday()],
                    horarios_tela=horarios_tela,
                    dia_fechado=False,
                    acao_dia="personalizado",
                    hora_inicio_personalizada=hora_inicio_personalizada,
                    hora_fim_personalizada=hora_fim_personalizada
                )

            if dt_inicio >= dt_fim:
                agendamentos_dia = cur.execute("""
                    SELECT hora
                    FROM agendamentos
                    WHERE usuario_id = ? AND data = ?
                """, (usuario_id, data)).fetchall()

                horas_agendadas = [row["hora"] for row in agendamentos_dia]
                horarios_tela = []

                for hora in sorted(horas_agendadas):
                    horarios_tela.append({
                        "hora": hora,
                        "status": "livre",
                        "agendado": True
                    })

                con.close()
                return render_template(
                    "editar_disponibilidade.html",
                    data=data,
                    data_obj=data_obj,
                    data_formatada=data_obj.strftime("%d/%m/%Y"),
                    nome_dia_semana=DIAS_SEMANA_NOME[data_obj.weekday()],
                    horarios_tela=horarios_tela,
                    dia_fechado=False,
                    acao_dia="personalizado",
                    hora_inicio_personalizada=hora_inicio_personalizada,
                    hora_fim_personalizada=hora_fim_personalizada
                )

            cur.execute("""
                INSERT INTO configuracao_dia (usuario_id, data, tipo, hora_inicio, hora_fim)
                VALUES (?, ?, ?, ?, ?)
            """, (
                usuario_id,
                data,
                "personalizado",
                hora_inicio_personalizada,
                hora_fim_personalizada
            ))

            horarios_base = gerar_horarios_intervalo(
                hora_inicio_personalizada,
                hora_fim_personalizada,
                INTERVALO_MINUTOS
            )

            horas_agendadas = [
                row["hora"] for row in cur.execute("""
                    SELECT hora
                    FROM agendamentos
                    WHERE usuario_id = ? AND data = ?
                """, (usuario_id, data)).fetchall()
            ]

            horas_agendadas_set = set(horas_agendadas)

            for hora in horarios_base:
                status = "livre" if (hora in horarios_marcados or hora in horas_agendadas_set) else "bloqueado"
                cur.execute("""
                    INSERT INTO disponibilidade_dia (usuario_id, data, hora, status)
                    VALUES (?, ?, ?, ?)
                """, (usuario_id, data, hora, status))

            for hora in horas_agendadas:
                if hora not in horarios_base:
                    cur.execute("""
                        INSERT INTO disponibilidade_dia (usuario_id, data, hora, status)
                        VALUES (?, ?, ?, ?)
                    """, (usuario_id, data, hora, "livre"))

            con.commit()
            con.close()
            return redirect(url_for("agenda", data=data))

        else:
            con.commit()
            con.close()
            return redirect(url_for("agenda", data=data))

    disponibilidade_salva = cur.execute("""
        SELECT hora, status
        FROM disponibilidade_dia
        WHERE usuario_id = ? AND data = ?
    """, (usuario_id, data)).fetchall()

    agendamentos_dia = cur.execute("""
        SELECT hora
        FROM agendamentos
        WHERE usuario_id = ? AND data = ?
    """, (usuario_id, data)).fetchall()

    config_dia = buscar_configuracao_dia(cur, usuario_id, data)
    horarios_base = buscar_horarios_base_por_data(cur, usuario_id, data)

    con.close()

    disponibilidade_dict = {row["hora"]: row["status"] for row in disponibilidade_salva}
    horas_agendadas = [row["hora"] for row in agendamentos_dia]

    dia_fechado = False
    if config_dia and (config_dia["tipo"] or "").lower() == "fechado":
        dia_fechado = True

    horas_para_exibir = list(horarios_base)

    for hora in horas_agendadas:
        if hora not in horas_para_exibir:
            horas_para_exibir.append(hora)

    horas_para_exibir = sorted(horas_para_exibir)

    horarios_tela = []
    for hora in horas_para_exibir:
        status = disponibilidade_dict.get(hora, "livre")
        horarios_tela.append({
            "hora": hora,
            "status": status,
            "agendado": hora in horas_agendadas
        })

    data_formatada = data_obj.strftime("%d/%m/%Y")
    nome_dia_semana = DIAS_SEMANA_NOME[data_obj.weekday()]

    return render_template(
        "editar_disponibilidade.html",
        data=data,
        data_obj=data_obj,
        data_formatada=data_formatada,
        nome_dia_semana=nome_dia_semana,
        horarios_tela=horarios_tela,
        dia_fechado=dia_fechado,
        acao_dia=acao_dia,
        hora_inicio_personalizada=hora_inicio_personalizada,
        hora_fim_personalizada=hora_fim_personalizada
    )


# --------------------------------------------------
# SERVIÇOS
# --------------------------------------------------
@app.route("/servicos", methods=["GET", "POST"])
def servicos():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        preco = (request.form.get("preco") or "0").strip()
        duracao = (request.form.get("duracao") or "").strip()

        if nome:
            try:
                preco = float(preco.replace(",", "."))
            except Exception:
                preco = 0

            cur.execute("""
                INSERT INTO servicos (usuario_id, nome, preco, duracao)
                VALUES (?, ?, ?, ?)
            """, (usuario_id, nome, preco, duracao))
            con.commit()

        con.close()
        return redirect(url_for("servicos"))

    lista = cur.execute("""
        SELECT *
        FROM servicos
        WHERE usuario_id = ?
        ORDER BY nome ASC
    """, (usuario_id,)).fetchall()

    con.close()
    return render_template("servicos.html", servicos=lista)


@app.route("/editar_servico/<int:id>", methods=["GET", "POST"])
def editar_servico(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    servico = cur.execute("""
        SELECT *
        FROM servicos
        WHERE id = ? AND usuario_id = ?
    """, (id, usuario_id)).fetchone()

    if not servico:
        con.close()
        return redirect(url_for("servicos"))

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        preco = (request.form.get("preco") or "0").strip()
        duracao = (request.form.get("duracao") or "").strip()

        try:
            preco = float(preco.replace(",", "."))
        except Exception:
            preco = 0

        cur.execute("""
            UPDATE servicos
            SET nome = ?, preco = ?, duracao = ?
            WHERE id = ? AND usuario_id = ?
        """, (nome, preco, duracao, id, usuario_id))

        con.commit()
        con.close()
        return redirect(url_for("servicos"))

    con.close()
    return render_primeiro_template(
        ["editar_servico.html", "servicos.html"],
        servico=servico
    )


@app.route("/excluir_servico/<int:id>", methods=["POST"])
def excluir_servico(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        DELETE FROM servicos
        WHERE id = ? AND usuario_id = ?
    """, (id, usuario_id))

    con.commit()
    con.close()

    return redirect(url_for("servicos"))


# --------------------------------------------------
# HORÁRIOS SEMANAIS
# --------------------------------------------------
@app.route("/horarios", methods=["GET", "POST"])
def horarios():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    if request.method == "POST":
        dia = (request.form.get("dia_semana") or "").strip()
        inicio = (request.form.get("hora_inicio") or "").strip()
        fim = (request.form.get("hora_fim") or "").strip()

        if dia and inicio and fim:
            try:
                dt_inicio = datetime.strptime(inicio, "%H:%M")
                dt_fim = datetime.strptime(fim, "%H:%M")
            except ValueError:
                con.close()
                return redirect(url_for("horarios"))

            if dt_inicio >= dt_fim:
                con.close()
                return redirect(url_for("horarios"))

            existente = cur.execute("""
                SELECT id
                FROM horarios
                WHERE usuario_id = ? AND dia_semana = ?
            """, (usuario_id, dia)).fetchone()

            if existente:
                cur.execute("""
                    UPDATE horarios
                    SET hora_inicio = ?, hora_fim = ?
                    WHERE usuario_id = ? AND dia_semana = ?
                """, (inicio, fim, usuario_id, dia))
            else:
                cur.execute("""
                    INSERT INTO horarios (usuario_id, dia_semana, hora_inicio, hora_fim)
                    VALUES (?, ?, ?, ?)
                """, (usuario_id, dia, inicio, fim))

            con.commit()

        con.close()
        return redirect(url_for("horarios"))

    lista = cur.execute("""
        SELECT *
        FROM horarios
        WHERE usuario_id = ?
        ORDER BY
            CASE dia_semana
                WHEN 'Segunda' THEN 1
                WHEN 'Terça' THEN 2
                WHEN 'Quarta' THEN 3
                WHEN 'Quinta' THEN 4
                WHEN 'Sexta' THEN 5
                WHEN 'Sábado' THEN 6
                WHEN 'Domingo' THEN 7
                ELSE 8
            END
    """, (usuario_id,)).fetchall()

    con.close()
    return render_template("horarios.html", horarios=lista)


@app.route("/excluir_horario/<int:id>")
def excluir_horario(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        DELETE FROM horarios
        WHERE id = ? AND usuario_id = ?
    """, (id, usuario_id))

    con.commit()
    con.close()

    return redirect(url_for("horarios"))


# --------------------------------------------------
# CLIENTES
# --------------------------------------------------
@app.route("/clientes", methods=["GET", "POST"])
def clientes():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        telefone = (request.form.get("telefone") or "").strip()
        observacoes = (request.form.get("observacoes") or "").strip()

        if nome:
            cur.execute("""
                INSERT INTO clientes (usuario_id, nome, telefone, observacoes)
                VALUES (?, ?, ?, ?)
            """, (usuario_id, nome, telefone, observacoes))
            con.commit()

        con.close()
        return redirect(url_for("clientes"))

    lista_clientes = cur.execute("""
        SELECT *
        FROM clientes
        WHERE usuario_id = ?
        ORDER BY nome ASC
    """, (usuario_id,)).fetchall()

    con.close()
    return render_template("clientes.html", clientes=lista_clientes)


@app.route("/excluir_cliente/<int:id>")
def excluir_cliente(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        DELETE FROM clientes
        WHERE id = ? AND usuario_id = ?
    """, (id, usuario_id))

    con.commit()
    con.close()

    return redirect(url_for("clientes"))


# --------------------------------------------------
# LINK PÚBLICO
# --------------------------------------------------
@app.route("/book")
@app.route("/agendar")
def book_sem_usuario():
    if usuario_logado():
        slug = buscar_slug_usuario(usuario_id_logado())
        if slug:
            return redirect(url_for("agendar_publico_slug", slug=slug))
    return redirect(url_for("login"))


@app.route("/book/<int:usuario_id>")
@app.route("/agendar/<int:usuario_id>")
def redirecionar_book_id(usuario_id):
    slug = buscar_slug_usuario(usuario_id)
    if slug:
        return redirect(url_for("agendar_publico_slug", slug=slug))
    return "<h1>Studio não encontrado.</h1>"


# --------------------------------------------------
# AGENDAMENTO DA CLIENTE / LINK PÚBLICO BONITO
# --------------------------------------------------
@app.route("/studio/<slug>", methods=["GET", "POST"])
def agendar_publico_slug(slug):
    con = conectar()
    cur = con.cursor()

    usuario = cur.execute("""
        SELECT id, nome, usuario, slug
        FROM usuarios
        WHERE slug = ?
        LIMIT 1
    """, (slug,)).fetchone()

    if not usuario:
        con.close()
        return "<h1>Studio não encontrado.</h1>"

    usuario_id = usuario["id"]
    garantir_dados_iniciais_usuario(usuario_id, usuario["nome"] or "AgendaFlow")
    config = obter_configuracoes(usuario_id)

    servicos_lista = cur.execute("""
        SELECT *
        FROM servicos
        WHERE usuario_id = ?
        ORDER BY nome ASC
    """, (usuario_id,)).fetchall()

    erro = ""
    data_selecionada = (request.values.get("data") or "").strip()
    horas_ocupadas = []
    horarios_disponiveis = []

    hoje = datetime.now().date()
    data_maxima = hoje + timedelta(days=DIAS_MAX_AGENDAMENTO)

    dias_livres, dias_ocupados = montar_status_dias_agendamento(cur, usuario_id, hoje, data_maxima)

    if data_selecionada:
        if data_dentro_limite(data_selecionada):
            horarios_disponiveis, horas_ocupadas = buscar_horarios_disponiveis(
                cur,
                usuario_id,
                data_selecionada
            )
        else:
            erro = f"Você pode agendar somente entre hoje e os próximos {DIAS_MAX_AGENDAMENTO} dias."

    if request.method == "GET":
        con.close()
        return render_template(
            "agendar.html",
            servicos=servicos_lista,
            horarios_fixos=horarios_disponiveis,
            horas_ocupadas=horas_ocupadas,
            data_selecionada=data_selecionada,
            erro=erro,
            data_min=hoje.strftime("%Y-%m-%d"),
            data_max=data_maxima.strftime("%Y-%m-%d"),
            dias_livres=dias_livres,
            dias_ocupados=dias_ocupados,
            config=config
        )

    cliente = (request.form.get("cliente") or "").strip()
    telefone = (request.form.get("telefone") or "").strip()
    servico = (request.form.get("servico") or "").strip()
    hora = (request.form.get("hora") or "").strip()

    if not cliente or not servico or not data_selecionada or not hora:
        erro = "Preencha nome, serviço, data e horário."
        con.close()
        return render_template(
            "agendar.html",
            servicos=servicos_lista,
            horarios_fixos=horarios_disponiveis,
            horas_ocupadas=horas_ocupadas,
            data_selecionada=data_selecionada,
            erro=erro,
            data_min=hoje.strftime("%Y-%m-%d"),
            data_max=data_maxima.strftime("%Y-%m-%d"),
            dias_livres=dias_livres,
            dias_ocupados=dias_ocupados,
            config=config
        )

    if not data_dentro_limite(data_selecionada):
        erro = f"Você pode agendar somente entre hoje e os próximos {DIAS_MAX_AGENDAMENTO} dias."
        con.close()
        return render_template(
            "agendar.html",
            servicos=servicos_lista,
            horarios_fixos=horarios_disponiveis,
            horas_ocupadas=horas_ocupadas,
            data_selecionada=data_selecionada,
            erro=erro,
            data_min=hoje.strftime("%Y-%m-%d"),
            data_max=data_maxima.strftime("%Y-%m-%d"),
            dias_livres=dias_livres,
            dias_ocupados=dias_ocupados,
            config=config
        )

    horarios_disponiveis, horas_ocupadas = buscar_horarios_disponiveis(cur, usuario_id, data_selecionada)

    if hora not in horarios_disponiveis:
        erro = "Esse horário não está disponível para agendamento."
        con.close()
        return render_template(
            "agendar.html",
            servicos=servicos_lista,
            horarios_fixos=horarios_disponiveis,
            horas_ocupadas=horas_ocupadas,
            data_selecionada=data_selecionada,
            erro=erro,
            data_min=hoje.strftime("%Y-%m-%d"),
            data_max=data_maxima.strftime("%Y-%m-%d"),
            dias_livres=dias_livres,
            dias_ocupados=dias_ocupados,
            config=config
        )

    ja_existe = cur.execute("""
        SELECT id
        FROM agendamentos
        WHERE usuario_id = ? AND data = ? AND hora = ?
    """, (usuario_id, data_selecionada, hora)).fetchone()

    if ja_existe:
        erro = "Esse horário já foi agendado. Escolha outro."
        con.close()
        return render_template(
            "agendar.html",
            servicos=servicos_lista,
            horarios_fixos=horarios_disponiveis,
            horas_ocupadas=horas_ocupadas,
            data_selecionada=data_selecionada,
            erro=erro,
            data_min=hoje.strftime("%Y-%m-%d"),
            data_max=data_maxima.strftime("%Y-%m-%d"),
            dias_livres=dias_livres,
            dias_ocupados=dias_ocupados,
            config=config
        )

    coluna_nome_cliente = obter_coluna_nome_cliente_agendamentos(cur)

    if coluna_nome_cliente == "cliente_nome":
        cur.execute(f"""
            INSERT INTO agendamentos
            (usuario_id, {coluna_nome_cliente}, telefone, servico, data, hora, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            usuario_id,
            cliente,
            telefone,
            servico,
            data_selecionada,
            hora,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
    else:
        cur.execute("""
            INSERT INTO agendamentos
            (usuario_id, cliente, telefone, servico, data, hora, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            usuario_id,
            cliente,
            telefone,
            servico,
            data_selecionada,
            hora,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

    con.commit()
    con.close()

    session["sucesso_cliente"] = cliente
    session["sucesso_servico"] = servico
    session["sucesso_data"] = data_selecionada
    session["sucesso_hora"] = hora

    return redirect(url_for("sucesso", slug=slug))


@app.route("/sucesso/<slug>")
def sucesso(slug):
    con = conectar()
    cur = con.cursor()

    usuario = cur.execute("""
        SELECT id
        FROM usuarios
        WHERE slug = ?
        LIMIT 1
    """, (slug,)).fetchone()

    if not usuario:
        con.close()
        return "<h1>Studio não encontrado.</h1>"

    usuario_id = usuario["id"]
    config = obter_configuracoes(usuario_id)
    con.close()

    cliente = session.get("sucesso_cliente", "")
    servico = session.get("sucesso_servico", "")
    data_agendamento = session.get("sucesso_data", "")
    hora_agendamento = session.get("sucesso_hora", "")

    data_formatada = data_agendamento
    if data_agendamento:
        try:
            data_obj = datetime.strptime(data_agendamento, "%Y-%m-%d")
            data_formatada = data_obj.strftime("%d/%m/%Y")
        except ValueError:
            pass

    mensagem = (
        "Olá! Acabei de agendar um horário.\n"
        f"Nome: {cliente}\n"
        f"Serviço: {servico}\n"
        f"Data: {data_formatada}\n"
        f"Horário: {hora_agendamento}"
    )

    mensagem_whatsapp = urllib.parse.quote(mensagem)

    return render_template(
        "sucesso.html",
        slug=slug,
        config=config,
        mensagem_whatsapp=mensagem_whatsapp,
        cliente=cliente,
        servico=servico,
        data_agendamento=data_formatada,
        hora_agendamento=hora_agendamento
    )


@app.route("/excluir_agendamento/<int:id>")
def excluir_agendamento(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        DELETE FROM agendamentos
        WHERE id = ? AND usuario_id = ?
    """, (id, usuario_id))

    con.commit()
    con.close()

    return redirect(url_for("agenda"))


@app.route("/verificar_novos")
def verificar_novos():
    if not usuario_logado():
        return {"ultimo": ""}

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    ultimo = cur.execute("""
        SELECT criado_em
        FROM agendamentos
        WHERE usuario_id = ?
        ORDER BY criado_em DESC
        LIMIT 1
    """, (usuario_id,)).fetchone()

    con.close()

    if ultimo:
        return {"ultimo": ultimo["criado_em"]}
    return {"ultimo": ""}


# --------------------------------------------------
# FINANCEIRO
# --------------------------------------------------
@app.route("/financeiro")
def financeiro():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    con = conectar()
    cur = con.cursor()

    agendamentos = cur.execute("""
        SELECT *
        FROM agendamentos
        WHERE usuario_id = ?
        ORDER BY data DESC, hora DESC
    """, (usuario_id,)).fetchall()

    dados = []
    total = 0

    for a in agendamentos:
        preco_row = cur.execute("""
            SELECT preco
            FROM servicos
            WHERE usuario_id = ? AND nome = ?
            LIMIT 1
        """, (usuario_id, a["servico"])).fetchone()

        preco = preco_row["preco"] if preco_row else 0
        total += preco

        dados.append({
            "cliente": a["cliente"] if "cliente" in a.keys() else a["cliente_nome"],
            "servico": a["servico"],
            "data": a["data"],
            "hora": a["hora"],
            "preco": preco
        })

    con.close()

    return render_template(
        "financeiro.html",
        dados=dados,
        total=total
    )


# --------------------------------------------------
# CONFIGURAÇÕES
# --------------------------------------------------
@app.route("/configuracoes", methods=["GET", "POST"])
def configuracoes():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()

    if request.method == "POST":
        nome_profissional = (request.form.get("nome_profissional") or "").strip()
        whatsapp = (request.form.get("whatsapp") or "").strip()
        whatsapp = re.sub(r"\D", "", whatsapp)

        if not nome_profissional:
            nome_profissional = "AgendaFlow"

        salvar_configuracoes(usuario_id, nome_profissional, whatsapp)
        return redirect(url_for("dashboard"))

    config = obter_configuracoes(usuario_id)
    status_plano = obter_status_plano_usuario(usuario_id)
    return render_template(
        "configuracoes.html",
        config=config,
        plano_ativo=status_plano["plano_ativo"],
        data_expiracao=status_plano["data_expiracao"],
        tipo_plano=status_plano["tipo_plano"],
        status_mp=status_plano["status_mp"]
    )


@app.route("/assinatura-bloqueada")
def assinatura_bloqueada():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    config = obter_configuracoes(usuario_id)
    status_plano = obter_status_plano_usuario(usuario_id)

    return pagina_assinatura_html(
        config=config,
        email_atual="",
        erro="Seu acesso está bloqueado porque o teste grátis terminou ou sua assinatura não está ativa.",
        info=f"Plano mensal: R$ {PLANO_VALOR:.2f}. Status atual: {status_plano['status_mp'] or 'sem assinatura ativa'}."
    )


@app.route("/assinar", methods=["GET", "POST"])
def assinar():
    if not usuario_logado():
        return redirect(url_for("login"))

    usuario_id = usuario_id_logado()
    config = obter_configuracoes(usuario_id)

    if not usuario_tem_config_mp():
        return pagina_assinatura_html(
            config=config,
            erro="Falta configurar MP_ACCESS_TOKEN e APP_BASE_URL no servidor antes de ativar a assinatura automática."
        )

    con = conectar()
    cur = con.cursor()
    usuario = obter_usuario(cur, usuario_id)
    con.close()

    status_plano = obter_status_plano_usuario(usuario_id)
    email_atual = (usuario["email"] or usuario["mp_payer_email"] or "").strip()

    if status_plano["tipo_plano"] == "pago" and status_plano["status_mp"] == "authorized":
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return pagina_assinatura_html(
            config=config,
            email_atual=email_atual,
            info=f"Seu plano mensal será cobrado automaticamente em R$ {PLANO_VALOR:.2f}/mês pelo Mercado Pago."
        )

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return pagina_assinatura_html(
            config=config,
            email_atual=email_atual,
            erro="Informe um e-mail válido para a assinatura."
        )

    try:
        resposta = criar_assinatura_mercadopago(usuario_id, email)
    except Exception as exc:
        return pagina_assinatura_html(
            config=config,
            email_atual=email,
            erro=f"Não foi possível iniciar a assinatura: {exc}"
        )

    init_point = (resposta.get("init_point") or "").strip()
    preapproval_id = (resposta.get("id") or "").strip()
    status = (resposta.get("status") or "pending").strip().lower()

    con = conectar()
    cur = con.cursor()
    user = obter_usuario(cur, usuario_id)
    data_exp = (user["data_expiracao"] or "") if user else ""

    atualizar_plano_local(
        cur,
        usuario_id,
        plano="teste" if data_exp else "vencido",
        data_expiracao=data_exp,
        mp_preapproval_id=preapproval_id,
        mp_status=status,
        mp_payer_email=email,
        email=email
    )
    con.commit()
    con.close()

    if init_point:
        return redirect(init_point)

    return pagina_assinatura_html(
        config=config,
        email_atual=email,
        erro=f"O Mercado Pago não retornou o link de pagamento. Resposta: {resposta}"
    )


@app.route("/assinatura/retorno")
def assinatura_retorno():
    if not usuario_logado():
        return redirect(url_for("login"))

    preapproval_id = (request.args.get("preapproval_id") or request.args.get("id") or "").strip()

    if preapproval_id:
        try:
            sincronizar_assinatura_por_preapproval(preapproval_id)
        except Exception:
            pass
    else:
        usuario_id = usuario_id_logado()
        con = conectar()
        cur = con.cursor()
        usuario = obter_usuario(cur, usuario_id)
        con.close()
        if usuario and usuario["mp_preapproval_id"]:
            try:
                sincronizar_assinatura_por_preapproval(usuario["mp_preapproval_id"])
            except Exception:
                pass

    return redirect(url_for("dashboard"))


@app.route("/webhook/mercadopago", methods=["POST"])
def mercadopago_webhook():
    if not validar_assinatura_webhook(request):
        return jsonify({"ok": False, "erro": "assinatura inválida"}), 401

    body = request.get_json(silent=True) or {}

    data_id = ""
    if isinstance(body, dict):
        data_id = (
            body.get("data", {}).get("id")
            or body.get("id")
            or request.args.get("data.id")
            or request.args.get("id")
            or ""
        )

    tipo = (
        request.args.get("type")
        or request.args.get("topic")
        or body.get("type")
        or body.get("topic")
        or body.get("action")
        or ""
    ).lower()

    if "preapproval" not in tipo and "subscription" not in tipo and not data_id:
        return jsonify({"ok": True, "ignorado": True}), 200

    try:
        sucesso, detalhe = sincronizar_assinatura_por_preapproval(data_id)
        return jsonify({"ok": sucesso, "detalhe": detalhe}), 200
    except Exception as exc:
        return jsonify({"ok": False, "erro": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
