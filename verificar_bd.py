import sqlite3

conn = sqlite3.connect("banc_temps.db")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute("SELECT * FROM usuaris")
usuaris = cursor.fetchall()

print("--- Usuaris a la Base de Dades ---")
if not usuaris:
    print("La base de dades està BUIDA!")
else:
    for u in usuaris:
        admin = "Sí" if u["es_admin"] else "No"
        print(
            f"ID: {u['id']} | Nom: {u['nom']} | Correu: {u['correu']} | "
            f"Ciutat: {u['ciutat']} | Saldo: {u['saldo']}h | Admin: {admin}"
        )
        # Per seguretat, no s'imprimeix la contrasenya (ni tan sols en format xifrat).

conn.close()
