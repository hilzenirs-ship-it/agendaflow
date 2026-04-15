import os
import re
import unicodedata
import calendar
import sqlite3
import smtplib
from email.message import EmailMessage
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
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RENDER_DISK_PATH = (os.environ.get("RENDER_DISK_PATH") or "").strip()
DB_DIR = RENDER_DISK_PATH if RENDER_DISK_PATH else BASE_DIR

os.makedirs(DB_DIR, exist_ok=True)

DB = os.path.join(DB_DIR, "banco.db")

IS_DEBUG = (
    os.environ.get("FLASK_DEBUG", "").strip().lower() in ("1", "true", "yes")
    or os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")
)

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if IS_DEBUG or os.environ.get("FLASK_ENV", "").strip().lower() == "development" or __name__ == "__main__":
        SECRET_KEY = "agendaflow_dev_" + os.urandom(16).hex()
    else:
        raise RuntimeError("SECRET_KEY environment variable must be set in production for AgendaFlow.")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = SECRET_KEY

app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not IS_DEBUG

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

EMAIL_HOST = (os.environ.get("EMAIL_HOST") or "").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = (os.environ.get("EMAIL_USE_TLS", "true").strip().lower() in ("1", "true", "sim", "yes"))
EMAIL_HOST_USER = (os.environ.get("EMAIL_HOST_USER") or "").strip()
EMAIL_HOST_PASSWORD = (os.environ.get("EMAIL_HOST_PASSWORD") or "").strip()
EMAIL_FROM = (os.environ.get("EMAIL_FROM") or EMAIL_HOST_USER or "").strip()
RECUPERACAO_SENHA_HORAS = int(os.environ.get("RECUPERACAO_SENHA_HORAS", "2"))

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


def gerar_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = os.urandom(16).hex()
        session["_csrf_token"] = token
    return token


def validar_csrf_token(token):
    csrf_value = session.get("_csrf_token", "")
    if not token or not csrf_value:
        return False
    return hmac.compare_digest(csrf_value, token)


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": gerar_csrf_token}


@app.before_request
def proteger_csrf():
    if request.method == "POST":
        endpoint = request.endpoint or ""
        if endpoint.startswith("static"):
            return None
        if endpoint in {"mercadopago_webhook"}:
            return None

        token = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
        if not validar_csrf_token(token):
            return "CSRF token inválido", 400


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


def normalizar_email(email):
    return (email or "").strip().lower()


def normalizar_telefone(telefone):
    return re.sub(r"\D", "", telefone or "")


def email_valido(email):
    email = normalizar_email(email)
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def sanitizar_texto(texto, max_len=255):
    """Remove scripts e limita tamanho"""
    if not texto:
        return ""
    # Remove tags HTML/script
    texto = re.sub(r'<[^>]+>', '', texto)
    # Remove caracteres de controle exceto quebras de linha
    texto = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', texto)
    return texto.strip()[:max_len]


def validar_nome(nome):
    """Valida nome: não vazio, só letras/espacos, tamanho adequado"""
    nome = sanitizar_texto(nome, 100)
    if not nome or len(nome) < 2:
        return False, "Nome deve ter pelo menos 2 caracteres"
    if not re.match(r"^[a-zA-ZÀ-ÿ\s\-']+$", nome):
        return False, "Nome contém caracteres inválidos"
    return True, nome


def validar_email(email):
    """Valida email com regex mais robusta"""
    email = normalizar_email(email)
    if not email or len(email) > 254:
        return False, "Email inválido ou muito longo"
    # Regex mais precisa para email
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, email):
        return False, "Formato de email inválido"
    return True, email


def validar_senha(senha):
    """Valida senha: comprimento mínimo, complexidade básica"""
    if not senha or len(senha) < 8:
        return False, "Senha deve ter pelo menos 8 caracteres"
    if len(senha) > 128:
        return False, "Senha muito longa"
    # Pelo menos uma letra e um número
    if not re.search(r'[a-zA-Z]', senha) or not re.search(r'\d', senha):
        return False, "Senha deve conter letras e números"
    return True, senha


def validar_telefone(telefone):
    """Valida telefone brasileiro"""
    telefone = normalizar_telefone(telefone)
    if not telefone:
        return True, ""  # Telefone opcional
    # Aceita 10 ou 11 dígitos (com DDD)
    if not re.match(r'^\d{10,11}$', telefone):
        return False, "Telefone deve ter 10 ou 11 dígitos"
    return True, telefone


def validar_preco(preco_str):
    """Valida preço: positivo, até 2 casas decimais"""
    try:
        preco = float(preco_str.replace(',', '.'))
        if preco < 0 or preco > 999999.99:
            return False, "Preço deve ser entre 0 e 999.999,99"
        # Verifica se tem no máximo 2 casas decimais
        if '.' in preco_str:
            decimal_part = preco_str.split('.')[-1]
            if len(decimal_part) > 2:
                return False, "Preço deve ter no máximo 2 casas decimais"
        return True, preco
    except ValueError:
        return False, "Preço inválido"


def validar_duracao(duracao):
    """Valida duração: formato como '1h', '30min', etc."""
    duracao = sanitizar_texto(duracao, 20)
    if not duracao:
        return True, ""  # Opcional
    # Aceita formatos como 1h, 30min, 1h30min, etc.
    if not re.match(r'^(\d+h)?(\d+min)?$', duracao.replace(' ', '')):
        return False, "Duração deve ser no formato '1h', '30min' ou '1h30min'"
    return True, duracao


def validar_data(data_str):
    """Valida data no formato YYYY-MM-DD"""
    try:
        datetime.strptime(data_str, '%Y-%m-%d')
        return True, data_str
    except ValueError:
        return False, "Data inválida"


def validar_hora(hora_str):
    """Valida hora no formato HH:MM"""
    try:
        datetime.strptime(hora_str, '%H:%M')
        return True, hora_str
    except ValueError:
        return False, "Hora inválida"


def obter_serializer_recuperacao():
    return URLSafeTimedSerializer(app.secret_key)


def gerar_token_recuperacao(email):
    serializer = obter_serializer_recuperacao()
    return serializer.dumps(normalizar_email(email), salt="recuperacao-senha")


def validar_token_recuperacao(token, max_age_segundos=None):
    serializer = obter_serializer_recuperacao()

    if max_age_segundos is None:
        max_age_segundos = RECUPERACAO_SENHA_HORAS * 3600

    email = serializer.loads(
        token,
        salt="recuperacao-senha",
        max_age=max_age_segundos
    )
    return normalizar_email(email)


def smtp_configurado():
    return bool(EMAIL_HOST and EMAIL_PORT and EMAIL_HOST_USER and EMAIL_HOST_PASSWORD and EMAIL_FROM)


def montar_url_base():
    if APP_BASE_URL:
        return APP_BASE_URL
    return request.url_root.rstrip("/")


def enviar_email(destinatario, assunto, corpo_texto, corpo_html=None):
    if not smtp_configurado():
        raise RuntimeError("SMTP não configurado.")

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = EMAIL_FROM
    msg["To"] = destinatario
    msg.set_content(corpo_texto)

    if corpo_html:
        msg.add_alternative(corpo_html, subtype="html")

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=20) as servidor:
        if EMAIL_USE_TLS:
            servidor.starttls()
        servidor.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)
        servidor.send_message(msg)


def enviar_email_recuperacao(nome, email, link_recuperacao):
    nome_exibicao = nome or "AgendaFlow"

    assunto = "Recuperação de senha - AgendaFlow"

    corpo_texto = (
        f"Olá, {nome_exibicao}!\n\n"
        "Recebemos uma solicitação para redefinir sua senha no AgendaFlow.\n\n"
        f"Acesse este link para criar uma nova senha:\n{link_recuperacao}\n\n"
        f"Esse link expira em {RECUPERACAO_SENHA_HORAS} hora(s).\n\n"
        "Se você não pediu essa alteração, ignore este e-mail.\n"
    )

    corpo_html = f"""
    <html>
    <body style="font-family:Arial,Helvetica,sans-serif;background:#f8edf2;padding:24px;color:#4f4362;">
        <div style="max-width:520px;margin:0 auto;background:#ffffff;border-radius:24px;padding:28px;border:1px solid rgba(210,196,235,0.45);box-shadow:0 18px 50px rgba(123,90,224,0.12);">
            <div style="width:76px;height:76px;margin:0 auto 14px;border-radius:24px;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#e78fb3,#7b5ae0);color:#fff;font-size:32px;font-weight:bold;">A</div>
            <h1 style="text-align:center;color:#57486c;margin:0 0 8px;">Recuperação de senha</h1>
            <p style="text-align:center;color:#8a7f9c;margin:0 0 20px;">AgendaFlow</p>
            <p>Olá, <strong>{nome_exibicao}</strong>!</p>
            <p>Recebemos uma solicitação para redefinir sua senha.</p>
            <p style="margin:24px 0;text-align:center;">
                <a href="{link_recuperacao}" style="display:inline-block;padding:14px 22px;border-radius:16px;background:linear-gradient(135deg,#e78fb3,#7b5ae0);color:#ffffff;text-decoration:none;font-weight:bold;">Redefinir senha</a>
            </p>
            <p>Ou copie e cole este link no navegador:</p>
            <p style="word-break:break-all;color:#7b5ae0;">{link_recuperacao}</p>
            <p>Esse link expira em <strong>{RECUPERACAO_SENHA_HORAS} hora(s)</strong>.</p>
            <p>Se você não pediu essa alteração, ignore este e-mail.</p>
        </div>
    </body>
    </html>
    """

    enviar_email(email, assunto, corpo_texto, corpo_html)


def buscar_ou_criar_cliente(cur, usuario_id, nome, telefone, observacoes=""):
    nome = (nome or "").strip()
    telefone = normalizar_telefone(telefone)
    observacoes = (observacoes or "").strip()

    cliente_existente = None

    if telefone:
        cliente_existente = cur.execute("""
            SELECT id, nome, telefone, observacoes
            FROM clientes
            WHERE usuario_id = ? AND telefone = ?
            ORDER BY id DESC
            LIMIT 1
        """, (usuario_id, telefone)).fetchone()

    if not cliente_existente and nome:
        cliente_existente = cur.execute("""
            SELECT id, nome, telefone, observacoes
            FROM clientes
            WHERE usuario_id = ? AND LOWER(TRIM(nome)) = LOWER(TRIM(?))
            ORDER BY id DESC
            LIMIT 1
        """, (usuario_id, nome)).fetchone()

    if cliente_existente:
        nome_atual = (cliente_existente["nome"] or "").strip()
        telefone_atual = normalizar_telefone(cliente_existente["telefone"] or "")
        observacoes_atuais = (cliente_existente["observacoes"] or "").strip()

        novo_nome = nome_atual or nome
        novo_telefone = telefone_atual or telefone
        novas_observacoes = observacoes_atuais or observacoes

        cur.execute("""
            UPDATE clientes
            SET nome = ?, telefone = ?, observacoes = ?
            WHERE id = ?
        """, (novo_nome, novo_telefone, novas_observacoes, cliente_existente["id"]))

        return cliente_existente["id"]

    cur.execute("""
        INSERT INTO clientes (usuario_id, nome, telefone, observacoes)
        VALUES (?, ?, ?, ?)
    """, (usuario_id, nome, telefone, observacoes))

    return cur.lastrowid


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

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_email_unico
        ON usuarios(email)
        WHERE email IS NOT NULL AND TRIM(email) <> ''
    """)

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
            cliente_id INTEGER,
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

    if not coluna_existe(cur, "agendamentos", "cliente_id"):
        cur.execute("ALTER TABLE agendamentos ADD COLUMN cliente_id INTEGER")

    if not coluna_existe(cur, "agendamentos", "observacoes"):
        cur.execute("ALTER TABLE agendamentos ADD COLUMN observacoes TEXT DEFAULT ''")

    cur.execute("""
        UPDATE agendamentos
        SET usuario_id = ?
        WHERE usuario_id IS NULL
    """, (admin_id,))

    agendamentos_sem_cliente_id = cur.execute("""
        SELECT id, usuario_id, cliente, telefone, observacoes
        FROM agendamentos
        WHERE cliente_id IS NULL
    """).fetchall()

    for ag in agendamentos_sem_cliente_id:
        usuario_ag = ag["usuario_id"]
        nome_cliente = (ag["cliente"] or "").strip()
        telefone_cliente = normalizar_telefone(ag["telefone"] or "")
        observacoes_cliente = (ag["observacoes"] or "").strip()

        if not usuario_ag:
            continue

        if not nome_cliente and not telefone_cliente:
            continue

        cliente_id = buscar_ou_criar_cliente(
            cur,
            usuario_ag,
            nome_cliente,
            telefone_cliente,
            observacoes_cliente
        )

        cur.execute("""
            UPDATE agendamentos
            SET cliente_id = ?
            WHERE id = ?
        """, (cliente_id, ag["id"]))

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS notificacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id INTEGER NOT NULL,
            tipo TEXT DEFAULT 'agendamento',
            titulo TEXT DEFAULT '',
            mensagem TEXT DEFAULT '',
            link TEXT DEFAULT '/agenda',
            lida INTEGER DEFAULT 0,
            criado_em TEXT,
            payload_json TEXT DEFAULT ''
        )
    """)

    if not coluna_existe(cur, "notificacoes", "tipo"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN tipo TEXT DEFAULT 'agendamento'")

    if not coluna_existe(cur, "notificacoes", "titulo"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN titulo TEXT DEFAULT ''")

    if not coluna_existe(cur, "notificacoes", "mensagem"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN mensagem TEXT DEFAULT ''")

    if not coluna_existe(cur, "notificacoes", "link"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN link TEXT DEFAULT '/agenda'")

    if not coluna_existe(cur, "notificacoes", "lida"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN lida INTEGER DEFAULT 0")

    if not coluna_existe(cur, "notificacoes", "criado_em"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN criado_em TEXT")

    if not coluna_existe(cur, "notificacoes", "payload_json"):
        cur.execute("ALTER TABLE notificacoes ADD COLUMN payload_json TEXT DEFAULT ''")

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


def formatar_data_br_curta(data_str):
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


def gerar_link_whatsapp_admin(usuario_id, cliente, servico, data_agendamento, hora_agendamento):
    config = obter_configuracoes(usuario_id)
    whatsapp = re.sub(r"\D", "", config.get("whatsapp", ""))

    if not whatsapp:
        return ""

    mensagem = (
        "🔔 Novo agendamento recebido no AgendaFlow\n\n"
        f"Cliente: {cliente or '-'}\n"
        f"Serviço: {servico or '-'}\n"
        f"Data: {formatar_data_br_curta(data_agendamento)}\n"
        f"Horário: {hora_agendamento or '-'}"
    )

    return f"https://wa.me/{whatsapp}?text={urllib.parse.quote(mensagem)}"


def criar_notificacao_agendamento(cur, usuario_id, cliente, servico, data_agendamento, hora_agendamento, agendamento_id):
    titulo = "Novo agendamento"
    mensagem = f"{cliente or 'Cliente'} agendou {servico or 'um serviço'} para {formatar_data_br_curta(data_agendamento)} às {hora_agendamento}"

    link_destino = f"/agenda?data={data_agendamento}"

    payload = {
        "cliente": cliente or "",
        "servico": servico or "",
        "data": formatar_data_br_curta(data_agendamento),
        "data_iso": data_agendamento or "",
        "hora": hora_agendamento or "",
        "agendamento_id": agendamento_id,
        "link": link_destino,
        "whatsapp_link": gerar_link_whatsapp_admin(
            usuario_id, cliente, servico, data_agendamento, hora_agendamento
        )
    }

    cur.execute("""
        INSERT INTO notificacoes (
            usuario_id, tipo, titulo, mensagem, link, lida, criado_em, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        usuario_id,
        "agendamento",
        titulo,
        mensagem,
        link_destino,
        0,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        json.dumps(payload, ensure_ascii=False)
    ))


def listar_notificacoes_usuario(usuario_id, limite=15):
    con = conectar()
    cur = con.cursor()

    rows = cur.execute("""
        SELECT *
        FROM notificacoes
        WHERE usuario_id = ?
        ORDER BY datetime(criado_em) DESC, id DESC
        LIMIT ?
    """, (usuario_id, limite)).fetchall()

    total_nao_lidas = cur.execute("""
        SELECT COUNT(*) AS total
        FROM notificacoes
        WHERE usuario_id = ? AND lida = 0
    """, (usuario_id,)).fetchone()["total"]

    con.close()

    notificacoes = []

    for row in rows:
        payload = {}
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}

        link_final = (
            payload.get("link")
            or row["link"]
            or (
                f"/agenda?data={payload.get('data_iso')}"
                if payload.get("data_iso")
                else "/agenda"
            )
        )

        notificacoes.append({
            "id": row["id"],
            "tipo": row["tipo"] or "",
            "titulo": row["titulo"] or "",
            "mensagem": row["mensagem"] or "",
            "link": link_final,
            "lida": int(row["lida"] or 0),
            "criado_em": row["criado_em"] or "",
            "cliente": payload.get("cliente", ""),
            "servico": payload.get("servico", ""),
            "data": payload.get("data", ""),
            "data_iso": payload.get("data_iso", ""),
            "hora": payload.get("hora", ""),
            "agendamento_id": payload.get("agendamento_id"),
            "whatsapp_link": payload.get("whatsapp_link", "")
        })

    return notificacoes, total_nao_lidas


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
        "esqueci_senha",
        "redefinir_senha",
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
        "verificar_novos",
        "notificacoes",
        "marcar_notificacao_como_lida"
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
        email = normalizar_email(request.form.get("email"))
        senha = (request.form.get("senha") or "").strip()
        confirmar_senha = (request.form.get("confirmar_senha") or "").strip()

        # Validações robustas
        valido, nome = validar_nome(nome)
        if not valido:
            erro = nome  # nome contém a mensagem de erro
            return render_template("cadastro.html", erro=erro, sucesso=sucesso)

        valido, email = validar_email(email)
        if not valido:
            erro = email
            return render_template("cadastro.html", erro=erro, sucesso=sucesso)

        valido, senha = validar_senha(senha)
        if not valido:
            erro = senha
            return render_template("cadastro.html", erro=erro, sucesso=sucesso)

        if senha != confirmar_senha:
            erro = "As senhas não coincidem."
            return render_template("cadastro.html", erro=erro, sucesso=sucesso)

        con = conectar()
        cur = con.cursor()

        email_existente = cur.execute("""
            SELECT id
            FROM usuarios
            WHERE LOWER(email) = LOWER(?)
              AND TRIM(COALESCE(email, '')) <> ''
        """, (email,)).fetchone()

        if email_existente:
            con.close()
            erro = "Esse e-mail já está cadastrado."
            return render_template("cadastro.html", erro=erro, sucesso=sucesso)

        base_usuario = email.split("@")[0].strip() or "usuario"
        slug = gerar_slug_unico(cur, nome or base_usuario)

        usuario_final = base_usuario
        contador = 2

        while cur.execute("""
            SELECT id
            FROM usuarios
            WHERE LOWER(usuario) = LOWER(?)
            LIMIT 1
        """, (usuario_final,)).fetchone():
            usuario_final = f"{base_usuario}{contador}"
            contador += 1

        data_expiracao = (datetime.now() + timedelta(days=TESTE_GRATIS_DIAS)).strftime("%Y-%m-%d")

        cur.execute("""
            INSERT INTO usuarios (nome, usuario, senha, slug, email, plano, data_expiracao)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            nome,
            usuario_final,
            generate_password_hash(senha),
            slug,
            email,
            "teste",
            data_expiracao
        ))

        novo_usuario_id = cur.lastrowid
        garantir_configuracao_usuario(cur, novo_usuario_id, nome)
        criar_servicos_exemplo(cur, novo_usuario_id)
        criar_horarios_exemplo(cur, novo_usuario_id)

        con.commit()
        con.close()

        sucesso = "Cadastro realizado com sucesso. Agora faça login com seu e-mail."
        return render_template("cadastro.html", erro="", sucesso=sucesso)

    return render_template("cadastro.html", erro=erro, sucesso=sucesso)


@app.route("/login", methods=["GET", "POST"])
def login():
    erro = ""
    sucesso = session.pop("sucesso_login", "")

    if request.method == "POST":
        email = normalizar_email(request.form.get("email"))
        senha = (request.form.get("senha") or "").strip()

        # Validações
        valido, email = validar_email(email)
        if not valido:
            erro = email
            return render_template("login.html", erro=erro, sucesso=sucesso)

        if not senha:
            erro = "Preencha sua senha."
            return render_template("login.html", erro=erro, sucesso=sucesso)

        con = conectar()
        cur = con.cursor()

        dados = cur.execute("""
            SELECT *
            FROM usuarios
            WHERE LOWER(email) = LOWER(?)
              AND TRIM(COALESCE(email, '')) <> ''
            LIMIT 1
        """, (email,)).fetchone()

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
            erro = "E-mail ou senha inválidos."

    return render_template("login.html", erro=erro, sucesso=sucesso)


@app.route("/esqueci-senha", methods=["GET", "POST"])
def esqueci_senha():
    erro = ""
    sucesso = ""
    info = ""
    email_digitado = ""

    if request.method == "POST":
        email_digitado = normalizar_email(request.form.get("email"))

        if not email_digitado:
            erro = "Informe seu e-mail."
            return render_template(
                "esqueci_senha.html",
                erro=erro,
                sucesso=sucesso,
                info=info,
                email=email_digitado
            )

        if not email_valido(email_digitado):
            erro = "Informe um e-mail válido."
            return render_template(
                "esqueci_senha.html",
                erro=erro,
                sucesso=sucesso,
                info=info,
                email=email_digitado
            )

        con = conectar()
        cur = con.cursor()

        usuario = cur.execute("""
            SELECT id, nome, email
            FROM usuarios
            WHERE LOWER(email) = LOWER(?)
              AND TRIM(COALESCE(email, '')) <> ''
            LIMIT 1
        """, (email_digitado,)).fetchone()

        con.close()

        sucesso = "Se esse e-mail estiver cadastrado, você receberá as instruções para redefinir sua senha."

        if usuario:
            token = gerar_token_recuperacao(usuario["email"])
            link_recuperacao = f"{montar_url_base()}{url_for('redefinir_senha', token=token)}"

            try:
                if smtp_configurado():
                    enviar_email_recuperacao(usuario["nome"], usuario["email"], link_recuperacao)
                else:
                    if app.debug:
                        info = f"SMTP não configurado. Em desenvolvimento, use este link: {link_recuperacao}"
                    else:
                        info = "O servidor ainda não está configurado para envio de e-mails. Configure o SMTP para ativar a recuperação por e-mail."
            except Exception as exc:
                erro = f"Não foi possível enviar o e-mail de recuperação: {exc}"
                sucesso = ""

        return render_template(
            "esqueci_senha.html",
            erro=erro,
            sucesso=sucesso,
            info=info,
            email=email_digitado
        )

    return render_template(
        "esqueci_senha.html",
        erro=erro,
        sucesso=sucesso,
        info=info,
        email=email_digitado
    )


@app.route("/redefinir-senha/<token>", methods=["GET", "POST"])
def redefinir_senha(token):
    erro = ""
    sucesso = ""
    email_token = ""

    try:
        email_token = validar_token_recuperacao(token)
    except SignatureExpired:
        erro = "Esse link expirou. Solicite uma nova recuperação de senha."
        return render_template(
            "redefinir_senha.html",
            erro=erro,
            sucesso=sucesso,
            token_valido=False,
            email=""
        )
    except BadSignature:
        erro = "Link inválido de recuperação de senha."
        return render_template(
            "redefinir_senha.html",
            erro=erro,
            sucesso=sucesso,
            token_valido=False,
            email=""
        )

    con = conectar()
    cur = con.cursor()

    usuario = cur.execute("""
        SELECT id, nome, email
        FROM usuarios
        WHERE LOWER(email) = LOWER(?)
          AND TRIM(COALESCE(email, '')) <> ''
        LIMIT 1
    """, (email_token,)).fetchone()

    if not usuario:
        con.close()
        erro = "Conta não encontrada para esse link."
        return render_template(
            "redefinir_senha.html",
            erro=erro,
            sucesso=sucesso,
            token_valido=False,
            email=""
        )

    if request.method == "POST":
        senha = (request.form.get("senha") or "").strip()
        confirmar_senha = (request.form.get("confirmar_senha") or "").strip()

        if not senha or not confirmar_senha:
            con.close()
            erro = "Preencha os dois campos de senha."
            return render_template(
                "redefinir_senha.html",
                erro=erro,
                sucesso=sucesso,
                token_valido=True,
                email=usuario["email"]
            )

        if senha != confirmar_senha:
            con.close()
            erro = "As senhas não coincidem."
            return render_template(
                "redefinir_senha.html",
                erro=erro,
                sucesso=sucesso,
                token_valido=True,
                email=usuario["email"]
            )

        if len(senha) < 4:
            con.close()
            erro = "Sua nova senha precisa ter pelo menos 4 caracteres."
            return render_template(
                "redefinir_senha.html",
                erro=erro,
                sucesso=sucesso,
                token_valido=True,
                email=usuario["email"]
            )

        cur.execute("""
            UPDATE usuarios
            SET senha = ?
            WHERE id = ?
        """, (generate_password_hash(senha), usuario["id"]))

        con.commit()
        con.close()

        session["sucesso_login"] = "Senha redefinida com sucesso. Agora entre com sua nova senha."
        return redirect(url_for("login"))

    con.close()
    return render_template(
        "redefinir_senha.html",
        erro=erro,
        sucesso=sucesso,
        token_valido=True,
        email=usuario["email"]
    )


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
        SELECT
            a.*,
            COALESCE(c.nome, a.cliente) AS nome_cliente,
            COALESCE(c.telefone, a.telefone) AS telefone_cliente
        FROM agendamentos a
        LEFT JOIN clientes c
            ON c.id = a.cliente_id
           AND c.usuario_id = a.usuario_id
        WHERE a.usuario_id = ?
        ORDER BY a.data ASC, a.hora ASC
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
        SELECT
            a.id,
            COALESCE(c.nome, a.cliente) AS cliente,
            COALESCE(c.telefone, a.telefone) AS telefone,
            a.servico,
            a.data,
            a.hora,
            a.observacoes
        FROM agendamentos a
        LEFT JOIN clientes c
            ON c.id = a.cliente_id
           AND c.usuario_id = a.usuario_id
        WHERE a.usuario_id = ?
          AND a.data >= ?
          AND a.data < ?
        ORDER BY a.data ASC, a.hora ASC
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
                SELECT
                    a.id,
                    COALESCE(c.nome, a.cliente) AS cliente,
                    COALESCE(c.telefone, a.telefone) AS telefone,
                    a.servico,
                    a.data,
                    a.hora,
                    a.observacoes AS observacao
                FROM agendamentos a
                LEFT JOIN clientes c
                    ON c.id = a.cliente_id
                   AND c.usuario_id = a.usuario_id
                WHERE a.usuario_id = ? AND a.data = ?
                ORDER BY a.hora ASC
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
        SELECT
            a.id,
            COALESCE(c.nome, a.cliente) AS cliente,
            COALESCE(c.telefone, a.telefone) AS telefone,
            a.servico,
            a.data,
            a.hora,
            a.observacoes AS observacao
        FROM agendamentos a
        LEFT JOIN clientes c
            ON c.id = a.cliente_id
           AND c.usuario_id = a.usuario_id
        WHERE a.usuario_id = ? AND a.data = ?
        ORDER BY a.hora ASC
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

        # Validações
        valido, nome = validar_nome(nome)
        if not valido:
            # Para serviços, permitir nomes mais flexíveis
            nome = sanitizar_texto(nome, 100)
            if not nome:
                con.close()
                return redirect(url_for("servicos"))

        valido, preco = validar_preco(preco)
        if not valido:
            con.close()
            return redirect(url_for("servicos"))

        valido, duracao = validar_duracao(duracao)
        if not valido:
            duracao = ""  # Torna opcional se inválido

        if nome:
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

        # Validações
        valido, nome = validar_nome(nome)
        if not valido:
            nome = sanitizar_texto(nome, 100)
            if not nome:
                con.close()
                return redirect(url_for("servicos"))

        valido, preco = validar_preco(preco)
        if not valido:
            con.close()
            return redirect(url_for("servicos"))

        valido, duracao = validar_duracao(duracao)
        if not valido:
            duracao = ""

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


@app.route("/excluir_horario/<int:id>", methods=["POST"])
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
        telefone = normalizar_telefone(request.form.get("telefone"))
        observacoes = (request.form.get("observacoes") or "").strip()

        # Validações
        valido, nome = validar_nome(nome)
        if not valido:
            con.close()
            return redirect(url_for("clientes"))

        valido, telefone = validar_telefone(telefone)
        if not valido:
            con.close()
            return redirect(url_for("clientes"))

        observacoes = sanitizar_texto(observacoes, 500)  # Limita observações

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


@app.route("/excluir_cliente/<int:id>", methods=["POST"])
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
            data = (request.args.get("data") or "").strip()
            if data:
                return redirect(url_for("agendar_publico_slug", slug=slug, data=data))
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
    telefone = normalizar_telefone(request.form.get("telefone"))
    servico = (request.form.get("servico") or "").strip()
    hora = (request.form.get("hora") or "").strip()
    observacoes = (request.form.get("observacoes") or "").strip()

    # Validações robustas
    valido, cliente = validar_nome(cliente)
    if not valido:
        erro = cliente
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

    valido, telefone = validar_telefone(telefone)
    if not valido:
        erro = telefone
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

    # Valida serviço
    servico_valido = any(s["nome"] == servico for s in servicos_lista)
    if not servico_valido:
        erro = "Serviço inválido."
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

    # Valida hora
    valido, hora = validar_hora(hora)
    if not valido:
        erro = "Horário inválido."
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

    observacoes = sanitizar_texto(observacoes, 500)

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

    cliente_id = buscar_ou_criar_cliente(
        cur,
        usuario_id,
        cliente,
        telefone,
        observacoes
    )

    cur.execute("""
        INSERT INTO agendamentos
        (usuario_id, cliente_id, cliente, telefone, servico, data, hora, criado_em, observacoes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        usuario_id,
        cliente_id,
        cliente,
        telefone,
        servico,
        data_selecionada,
        hora,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        observacoes
    ))

    agendamento_id = cur.lastrowid

    criar_notificacao_agendamento(
        cur,
        usuario_id,
        cliente,
        servico,
        data_selecionada,
        hora,
        agendamento_id
    )

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


@app.route("/excluir_agendamento/<int:id>", methods=["POST"])
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
        return jsonify({
            "ultimo_id": 0,
            "nao_lidas": 0,
            "notificacoes": []
        })

    usuario_id = usuario_id_logado()
    notificacoes, total_nao_lidas = listar_notificacoes_usuario(usuario_id, limite=8)

    ultimo_id = notificacoes[0]["id"] if notificacoes else 0

    return jsonify({
        "ultimo_id": ultimo_id,
        "nao_lidas": total_nao_lidas,
        "notificacoes": notificacoes
    })


@app.route("/notificacoes")
def notificacoes():
    if not usuario_logado():
        return jsonify([])

    usuario_id = usuario_id_logado()
    notificacoes_lista, _ = listar_notificacoes_usuario(usuario_id, limite=20)

    return jsonify(notificacoes_lista)


@app.route("/notificacoes/<int:notificacao_id>/ler", methods=["POST"])
def marcar_notificacao_como_lida(notificacao_id):
    if not usuario_logado():
        return jsonify({"ok": False, "erro": "Não autenticado"}), 401

    usuario_id = usuario_id_logado()

    con = conectar()
    cur = con.cursor()

    cur.execute("""
        UPDATE notificacoes
        SET lida = 1
        WHERE id = ? AND usuario_id = ?
    """, (notificacao_id, usuario_id))

    con.commit()
    alteradas = cur.rowcount
    con.close()

    return jsonify({
        "ok": alteradas > 0
    })


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
        SELECT
            a.*,
            COALESCE(c.nome, a.cliente) AS nome_cliente
        FROM agendamentos a
        LEFT JOIN clientes c
            ON c.id = a.cliente_id
           AND c.usuario_id = a.usuario_id
        WHERE a.usuario_id = ?
        ORDER BY a.data DESC, a.hora DESC
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
            "cliente": a["nome_cliente"] or a["cliente"] or "",
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

    email = normalizar_email(request.form.get("email"))
    if not email or not email_valido(email):
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
    app.run(host="0.0.0.0", port=port, debug=IS_DEBUG)