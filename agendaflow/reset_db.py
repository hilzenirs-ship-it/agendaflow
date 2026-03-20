import sqlite3

con = sqlite3.connect("banco.db")
cur = con.cursor()

cur.execute("DROP TABLE IF EXISTS agenda")
cur.execute("DROP TABLE IF EXISTS servicos")
cur.execute("DROP TABLE IF EXISTS configuracoes")

cur.execute("""
CREATE TABLE servicos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    preco REAL NOT NULL
)
""")

cur.execute("""
CREATE TABLE agenda (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dia TEXT NOT NULL,
    horario TEXT NOT NULL,
    cliente TEXT,
    servico TEXT,
    preco REAL DEFAULT 0,
    status TEXT NOT NULL
)
""")

cur.execute("""
CREATE TABLE configuracoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome_profissional TEXT NOT NULL,
    whatsapp TEXT NOT NULL
)
""")

servicos = [
    ("Sobrancelha", 30),
    ("Henna", 45),
    ("Cílios Egípcio", 120),
    ("Volume Russo", 150),
    ("Manutenção de Cílios", 80)
]

cur.executemany(
    "INSERT INTO servicos (nome, preco) VALUES (?, ?)",
    servicos
)

cur.execute("""
INSERT INTO configuracoes (nome_profissional, whatsapp)
VALUES (?, ?)
""", ("Studio Flow Beauty", "5511999999999"))

dias = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta"]
horas = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]

for dia in dias:
    for hora in horas:
        cur.execute("""
            INSERT INTO agenda (dia, horario, cliente, servico, preco, status)
            VALUES (?, ?, '', '', 0, 'livre')
        """, (dia, hora))

con.commit()
con.close()

print("Banco recriado com sucesso!")