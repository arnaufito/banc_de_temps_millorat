import sqlite3
import sys


def fer_admin(correu):
    conn = sqlite3.connect("banc_temps.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE usuaris SET es_admin = 1 WHERE correu = ?", (correu,))
    afectats = cursor.rowcount
    conn.commit()
    conn.close()

    if afectats:
        print(f"✅ L'usuari amb correu {correu} ara és ADMINISTRADOR.")
    else:
        print(f"⚠️  No s'ha trobat cap usuari amb el correu {correu}. No s'ha canviat res.")


if __name__ == "__main__":
    # Es pot cridar com: python fer_admin.py nom@exemple.cat
    # O bé sense arguments i t'ho demanarà per pantalla.
    correu = sys.argv[1] if len(sys.argv) > 1 else input("Correu de l'usuari a fer administrador: ").strip()
    fer_admin(correu)
