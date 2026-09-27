import sqlite3


def netejar_base_de_dades():
    conn = sqlite3.connect("banc_temps.db")
    cursor = conn.cursor()

    print("Netejant la base de dades...")

    try:
        cursor.execute("DELETE FROM missatges")
        cursor.execute("DELETE FROM converses")
        cursor.execute("DELETE FROM ofertes")
        cursor.execute("DELETE FROM transaccions")
        cursor.execute("DELETE FROM usuaris")
        conn.commit()
        print("S'han buidat totes les taules correctament!")
    except sqlite3.OperationalError as e:
        print(f"Avís: No s'ha pogut trobar alguna taula o s'ha produït un error: {e}")

    conn.close()


if __name__ == "__main__":
    resposta = input(
        "⚠️  Estàs SEGUR que vols esborrar TOTES les dades (usuaris, ofertes, xats, transaccions)? "
        "Escriu 'si' per confirmar: "
    )
    if resposta.strip().lower() == "si":
        netejar_base_de_dades()
    else:
        print("Operació cancel·lada. No s'ha esborrat res.")
