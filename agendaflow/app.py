import os
import calendar
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session
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
# USUÁRIOS LOGIN
# --------------------------------------------------
USUARIOS = {
    "admin": {
        "senha": generate_password_hash("1234"),
        "nome": "Administrador"
    }
}


def usuario_logado():
    return "usuario" in session


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


def criar_tabelas():
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS servicos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            preco REAL DEFAULT 0
        )
    """)

    if not coluna_existe(cur, "servicos", "duracao"):
        cur.execute("ALTER TABLE servicos ADD COLUMN duracao TEXT DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS horarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dia_semana TEXT,
            hora_inicio TEXT,
            hora_fim TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT,
            telefone TEXT
        )
    """)

    if not coluna_existe(cur, "clientes", "observacoes"):
        cur.execute("ALTER TABLE clientes ADD COLUMN observacoes TEXT DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS agendamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cliente TEXT,
            telefone TEXT,
            servico TEXT,
            data TEXT,
            hora TEXT,
            criado_em TEXT
        )
    """)

    if not coluna_existe(cur, "agendamentos", "observacoes"):
        cur.execute("ALTER TABLE agendamentos ADD COLUMN observacoes TEXT DEFAULT ''")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS disponibilidade_dia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            hora TEXT NOT NULL,
            status TEXT DEFAULT 'livre'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configuracao_dia (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL UNIQUE,
            tipo TEXT DEFAULT 'padrao',
            hora_inicio TEXT DEFAULT '',
            hora_fim TEXT DEFAULT ''
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS configuracoes (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            nome_profissional TEXT DEFAULT 'AgendaFlow',
            whatsapp TEXT DEFAULT ''
        )
    """)

    configuracao_existente = cur.execute(
        "SELECT id FROM configuracoes WHERE id = 1"
    ).fetchone()

    if not configuracao_existente:
        cur.execute("""
            INSERT INTO configuracoes (id, nome_profissional, whatsapp)
            VALUES (1, 'AgendaFlow', '')
        """)

    con.commit()
    con.close()


criar_tabelas()


# --------------------------------------------------
# FUNÇÕES AUXILIARES
# --------------------------------------------------
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


def buscar_horario_semanal(cur, data_str):
    nome_dia = dia_semana_por_data(data_str)
    if not nome_dia:
        return None

    return cur.execute("""
        SELECT *
        FROM horarios
        WHERE dia_semana = ?
        ORDER BY id DESC
        LIMIT 1
    """, (nome_dia,)).fetchone()


def buscar_configuracao_dia(cur, data_str):
    return cur.execute("""
        SELECT *
        FROM configuracao_dia
        WHERE data = ?
        LIMIT 1
    """, (data_str,)).fetchone()


def buscar_horarios_base_por_data(cur, data_str):
    """
    REGRA PRINCIPAL:
    1. Se existir configuração personalizada para o dia, ela manda.
    2. Se o dia estiver fechado, não há horários.
    3. Se não existir configuração personalizada válida, usa o horário semanal padrão.
    """
    config_dia = buscar_configuracao_dia(cur, data_str)

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

    horario_semana = buscar_horario_semanal(cur, data_str)

    if not horario_semana:
        return []

    return gerar_horarios_intervalo(
        horario_semana["hora_inicio"],
        horario_semana["hora_fim"],
        INTERVALO_MINUTOS
    )


def buscar_horarios_disponiveis(cur, data_selecionada):
    if not data_selecionada or not data_dentro_limite(data_selecionada):
        return [], []

    horarios_base = buscar_horarios_base_por_data(cur, data_selecionada)

    horas_ocupadas = [
        row["hora"] for row in cur.execute("""
            SELECT hora
            FROM agendamentos
            WHERE data = ?
            ORDER BY hora
        """, (data_selecionada,)).fetchall()
    ]

    disponibilidade = cur.execute("""
        SELECT hora, status
        FROM disponibilidade_dia
        WHERE data = ?
        ORDER BY hora
    """, (data_selecionada,)).fetchall()

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


def buscar_horarios_disponiveis_para_data(cur, data):
    horas_livres, _ = buscar_horarios_disponiveis(cur, data)
    return horas_livres


def obter_configuracoes():
    con = conectar()
    cur = con.cursor()

    config = cur.execute("""
        SELECT nome_profissional, whatsapp
        FROM configuracoes
        WHERE id = 1
    """).fetchone()

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


def salvar_configuracoes(nome_profissional, whatsapp):
    con = conectar()
    cur = con.cursor()

    cur.execute("""
        UPDATE configuracoes
        SET nome_profissional = ?, whatsapp = ?
        WHERE id = 1
    """, (nome_profissional, whatsapp))

    con.commit()
    con.close()


def montar_status_dias_agendamento(cur, hoje, data_maxima):
    dias_livres = []
    dias_ocupados = []

    data_atual_loop = hoje
    while data_atual_loop <= data_maxima:
        data_str = data_atual_loop.strftime("%Y-%m-%d")
        horarios_livres_dia, _ = buscar_horarios_disponiveis(cur, data_str)

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


# --------------------------------------------------
# LOGIN
# --------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    erro = ""

    if request.method == "POST":
        usuario = (request.form.get("usuario") or "").strip()
        senha = (request.form.get("senha") or "").strip()

        dados = USUARIOS.get(usuario)

        if dados and check_password_hash(dados["senha"], senha):
            session.clear()
            session["usuario"] = usuario
            session["nome"] = dados["nome"]
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

    con = conectar()
    cur = con.cursor()

    agendamentos = cur.execute("""
        SELECT *
        FROM agendamentos
        ORDER BY data ASC, hora ASC
    """).fetchall()

    total_agendamentos = cur.execute(
        "SELECT COUNT(*) AS total FROM agendamentos"
    ).fetchone()["total"]

    total_clientes = cur.execute(
        "SELECT COUNT(*) AS total FROM clientes"
    ).fetchone()["total"]

    total_servicos = cur.execute(
        "SELECT COUNT(*) AS total FROM servicos"
    ).fetchone()["total"]

    faturamento_row = cur.execute("""
        SELECT COALESCE(SUM(s.preco), 0) AS total
        FROM agendamentos a
        LEFT JOIN servicos s ON a.servico = s.nome
    """).fetchone()

    faturamento = faturamento_row["total"] if faturamento_row else 0

    con.close()

    config = obter_configuracoes()

    return render_primeiro_template(
        ["dashboard.html", "agenda.html"],
        agendamentos=agendamentos,
        total_agendamentos=total_agendamentos,
        total_clientes=total_clientes,
        total_servicos=total_servicos,
        faturamento=faturamento,
        config=config
    )


# --------------------------------------------------
# AGENDA
# --------------------------------------------------
@app.route("/agenda")
def agenda():
    if not usuario_logado():
        return redirect(url_for("login"))

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
        WHERE data >= ? AND data < ?
        ORDER BY data ASC, hora ASC
    """, (inicio_mes, proximo_mes_data)).fetchall()

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
    data_obj = None
    data_formatada = ""
    nome_dia_semana = ""
    eh_hoje_detalhe = False

    if data_selecionada:
        try:
            data_obj = datetime.strptime(data_selecionada, "%Y-%m-%d")
            agendamentos_dia = cur.execute("""
                SELECT
                    id,
                    cliente,
                    telefone,
                    servico,
                    data,
                    hora,
                    observacoes AS observacao
                FROM agendamentos
                WHERE data = ?
                ORDER BY hora ASC
            """, (data_selecionada,)).fetchall()

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
        eh_hoje_detalhe=eh_hoje_detalhe
    )


@app.route("/agenda/dia/<data>")
def agenda_dia(data):
    if not usuario_logado():
        return redirect(url_for("login"))

    try:
        data_obj = datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("agenda"))

    con = conectar()
    cur = con.cursor()

    agendamentos_dia = cur.execute("""
        SELECT
            id,
            cliente,
            telefone,
            servico,
            data,
            hora,
            observacoes AS observacao
        FROM agendamentos
        WHERE data = ?
        ORDER BY hora ASC
    """, (data,)).fetchall()

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

    try:
        data_obj = datetime.strptime(data, "%Y-%m-%d")
    except ValueError:
        return redirect(url_for("agenda"))

    con = conectar()
    cur = con.cursor()

    config_dia = buscar_configuracao_dia(cur, data)

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

        cur.execute("DELETE FROM disponibilidade_dia WHERE data = ?", (data,))
        cur.execute("DELETE FROM configuracao_dia WHERE data = ?", (data,))

        if acao_dia == "fechado":
            cur.execute("""
                INSERT INTO configuracao_dia (data, tipo, hora_inicio, hora_fim)
                VALUES (?, ?, ?, ?)
            """, (data, "fechado", "", ""))

            con.commit()
            con.close()
            return redirect(url_for("agenda", data=data))

        elif acao_dia == "personalizado":
            if not hora_inicio_personalizada or not hora_fim_personalizada:
                agendamentos_dia = cur.execute("""
                    SELECT hora
                    FROM agendamentos
                    WHERE data = ?
                """, (data,)).fetchall()

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
                    WHERE data = ?
                """, (data,)).fetchall()

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
                    WHERE data = ?
                """, (data,)).fetchall()

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
                INSERT INTO configuracao_dia (data, tipo, hora_inicio, hora_fim)
                VALUES (?, ?, ?, ?)
            """, (data, "personalizado", hora_inicio_personalizada, hora_fim_personalizada))

            horarios_base = gerar_horarios_intervalo(
                hora_inicio_personalizada,
                hora_fim_personalizada,
                INTERVALO_MINUTOS
            )

            horas_agendadas = [
                row["hora"] for row in cur.execute("""
                    SELECT hora
                    FROM agendamentos
                    WHERE data = ?
                """, (data,)).fetchall()
            ]

            horas_agendadas_set = set(horas_agendadas)

            for hora in horarios_base:
                status = "livre" if (hora in horarios_marcados or hora in horas_agendadas_set) else "bloqueado"
                cur.execute("""
                    INSERT INTO disponibilidade_dia (data, hora, status)
                    VALUES (?, ?, ?)
                """, (data, hora, status))

            for hora in horas_agendadas:
                if hora not in horarios_base:
                    cur.execute("""
                        INSERT INTO disponibilidade_dia (data, hora, status)
                        VALUES (?, ?, ?)
                    """, (data, hora, "livre"))

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
        WHERE data = ?
    """, (data,)).fetchall()

    agendamentos_dia = cur.execute("""
        SELECT hora
        FROM agendamentos
        WHERE data = ?
    """, (data,)).fetchall()

    config_dia = buscar_configuracao_dia(cur, data)
    horarios_base = buscar_horarios_base_por_data(cur, data)

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
                INSERT INTO servicos (nome, preco, duracao)
                VALUES (?, ?, ?)
            """, (nome, preco, duracao))
            con.commit()

        con.close()
        return redirect(url_for("servicos"))

    lista = cur.execute("""
        SELECT *
        FROM servicos
        ORDER BY nome ASC
    """).fetchall()

    con.close()
    return render_template("servicos.html", servicos=lista)


@app.route("/editar_servico/<int:id>", methods=["GET", "POST"])
def editar_servico(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    con = conectar()
    cur = con.cursor()

    servico = cur.execute(
        "SELECT * FROM servicos WHERE id = ?",
        (id,)
    ).fetchone()

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
            WHERE id = ?
        """, (nome, preco, duracao, id))

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

    con = conectar()
    cur = con.cursor()

    cur.execute("DELETE FROM servicos WHERE id = ?", (id,))
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
                WHERE dia_semana = ?
            """, (dia,)).fetchone()

            if existente:
                cur.execute("""
                    UPDATE horarios
                    SET hora_inicio = ?, hora_fim = ?
                    WHERE dia_semana = ?
                """, (inicio, fim, dia))
            else:
                cur.execute("""
                    INSERT INTO horarios (dia_semana, hora_inicio, hora_fim)
                    VALUES (?, ?, ?)
                """, (dia, inicio, fim))

            con.commit()

        con.close()
        return redirect(url_for("horarios"))

    lista = cur.execute("""
        SELECT *
        FROM horarios
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
    """).fetchall()

    con.close()
    return render_template("horarios.html", horarios=lista)


@app.route("/excluir_horario/<int:id>")
def excluir_horario(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    con = conectar()
    cur = con.cursor()

    cur.execute("DELETE FROM horarios WHERE id = ?", (id,))
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

    con = conectar()
    cur = con.cursor()

    if not coluna_existe(cur, "clientes", "observacoes"):
        cur.execute("ALTER TABLE clientes ADD COLUMN observacoes TEXT DEFAULT ''")
        con.commit()

    if request.method == "POST":
        nome = (request.form.get("nome") or "").strip()
        telefone = (request.form.get("telefone") or "").strip()
        observacoes = (request.form.get("observacoes") or "").strip()

        if nome:
            cur.execute("""
                INSERT INTO clientes (nome, telefone, observacoes)
                VALUES (?, ?, ?)
            """, (nome, telefone, observacoes))
            con.commit()

        con.close()
        return redirect(url_for("clientes"))

    lista_clientes = cur.execute("""
        SELECT *
        FROM clientes
        ORDER BY nome ASC
    """).fetchall()

    con.close()
    return render_template("clientes.html", clientes=lista_clientes)


@app.route("/excluir_cliente/<int:id>")
def excluir_cliente(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    con = conectar()
    cur = con.cursor()

    cur.execute("DELETE FROM clientes WHERE id = ?", (id,))
    con.commit()
    con.close()

    return redirect(url_for("clientes"))


# --------------------------------------------------
# AGENDAMENTO DA CLIENTE / LINK
# --------------------------------------------------
@app.route("/agendar", methods=["GET", "POST"])
@app.route("/book", methods=["GET", "POST"])
def agendar():
    con = conectar()
    cur = con.cursor()

    servicos_lista = cur.execute("""
        SELECT *
        FROM servicos
        ORDER BY nome ASC
    """).fetchall()

    erro = ""
    data_selecionada = (request.values.get("data") or "").strip()
    horas_ocupadas = []
    horarios_disponiveis = []

    hoje = datetime.now().date()
    data_maxima = hoje + timedelta(days=DIAS_MAX_AGENDAMENTO)

    dias_livres, dias_ocupados = montar_status_dias_agendamento(cur, hoje, data_maxima)

    if data_selecionada:
        if data_dentro_limite(data_selecionada):
            horarios_disponiveis, horas_ocupadas = buscar_horarios_disponiveis(
                cur,
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
            dias_ocupados=dias_ocupados
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
            dias_ocupados=dias_ocupados
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
            dias_ocupados=dias_ocupados
        )

    horarios_disponiveis, horas_ocupadas = buscar_horarios_disponiveis(cur, data_selecionada)

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
            dias_ocupados=dias_ocupados
        )

    ja_existe = cur.execute("""
        SELECT id
        FROM agendamentos
        WHERE data = ? AND hora = ?
    """, (data_selecionada, hora)).fetchone()

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
            dias_ocupados=dias_ocupados
        )

    coluna_nome_cliente = obter_coluna_nome_cliente_agendamentos(cur)

    if coluna_nome_cliente == "cliente_nome":
        cur.execute(f"""
            INSERT INTO agendamentos
            ({coluna_nome_cliente}, telefone, servico, data, hora, criado_em)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
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
            (cliente, telefone, servico, data, hora, criado_em)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            cliente,
            telefone,
            servico,
            data_selecionada,
            hora,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

    con.commit()
    con.close()

    return redirect(url_for("sucesso"))


@app.route("/sucesso")
def sucesso():
    return render_primeiro_template(["sucesso.html", "agenda.html"])


@app.route("/excluir_agendamento/<int:id>")
def excluir_agendamento(id):
    if not usuario_logado():
        return redirect(url_for("login"))

    con = conectar()
    cur = con.cursor()

    cur.execute("DELETE FROM agendamentos WHERE id = ?", (id,))
    con.commit()
    con.close()

    return redirect(url_for("agenda"))


@app.route("/verificar_novos")
def verificar_novos():
    if not usuario_logado():
        return {"ultimo": ""}

    con = conectar()
    cur = con.cursor()

    ultimo = cur.execute("""
        SELECT criado_em
        FROM agendamentos
        ORDER BY criado_em DESC
        LIMIT 1
    """).fetchone()

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

    con = conectar()
    cur = con.cursor()

    agendamentos = cur.execute("""
        SELECT *
        FROM agendamentos
        ORDER BY data DESC, hora DESC
    """).fetchall()

    dados = []
    total = 0

    for a in agendamentos:
        preco_row = cur.execute("""
            SELECT preco
            FROM servicos
            WHERE nome = ?
        """, (a["servico"],)).fetchone()

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

    if request.method == "POST":
        nome_profissional = (request.form.get("nome_profissional") or "").strip()
        whatsapp = (request.form.get("whatsapp") or "").strip()

        if not nome_profissional:
            nome_profissional = "AgendaFlow"

        salvar_configuracoes(nome_profissional, whatsapp)
        return redirect(url_for("dashboard"))

    config = obter_configuracoes()
    return render_template("configuracoes.html", config=config)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)