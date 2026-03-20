import sqlite3

DB = "banco.db"

con = sqlite3.connect(DB)
cur = con.cursor()

print("Verificando banco...")

# verificar colunas da tabela agendamentos
cols = [c[1] for c in cur.execute("PRAGMA table_info(agendamentos)").fetchall()]

# adicionar coluna cliente se não existir
if "cliente" not in cols:
    print("Criando coluna cliente...")
    cur.execute("ALTER TABLE agendamentos ADD COLUMN cliente TEXT")

# copiar cliente_nome para cliente
if "cliente_nome" in cols:
    print("Migrando cliente_nome -> cliente")
    cur.execute("""
        UPDATE agendamentos
        SET cliente = cliente_nome
        WHERE cliente IS NULL OR cliente = ''
    """)

con.commit()
con.close()

print("Banco atualizado com sucesso.")