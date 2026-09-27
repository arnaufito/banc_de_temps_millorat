import os
import re
import sqlite3
from functools import wraps

from flask import Flask, session, redirect, url_for, request, render_template, flash
from werkzeug.security import generate_password_hash, check_password_hash

# ==========================================
# CONFIGURACIÓ
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Es pot apuntar a un altre lloc (p. ex. un disc persistent a Render) amb la
# variable d'entorn DB_PATH; si no es defineix, es fa servir la mateixa carpeta
# de sempre.
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "banc_temps.db"))

app = Flask(__name__)
# La clau es pot sobreescriure amb la variable d'entorn CLAU_SECRETA en producció.
app.secret_key = os.environ.get("CLAU_SECRETA", "clau_super_secreta_del_tdr")

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CIUTATS_PREDEFINIDES = [
    "Barcelona", "Girona", "Lleida", "Tarragona",
    "L'Hospitalet de Llobregat", "Badalona", "Terrassa", "Sabadell",
]


# ==========================================
# BASE DE DADES
# ==========================================
def get_db():
    """Obre una connexió nova amb accés per nom de columna I per índex,
    perquè les plantilles existents (oferta[1], usuari[2]...) segueixin funcionant."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def inicialitzar_bd():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS usuaris (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nom TEXT NOT NULL,
        correu TEXT UNIQUE NOT NULL,
        contrasenya TEXT NOT NULL,
        ciutat TEXT NOT NULL,
        saldo REAL DEFAULT 5.0
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS ofertes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_usuari INTEGER NOT NULL,
        titol TEXT NOT NULL,
        descripcio TEXT,
        hores REAL NOT NULL,
        categoria TEXT DEFAULT 'Altres',
        FOREIGN KEY (id_usuari) REFERENCES usuaris (id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS transaccions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_pagador INTEGER NOT NULL,
        id_cobrador INTEGER NOT NULL,
        hores REAL NOT NULL,
        data TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (id_pagador) REFERENCES usuaris (id),
        FOREIGN KEY (id_cobrador) REFERENCES usuaris (id)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS missatges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_oferta INTEGER NOT NULL,
        id_remitent INTEGER NOT NULL,
        missatge TEXT NOT NULL
    )
    ''')

    # Taula de converses: cada fila és una conversa privada entre UN comprador
    # concret i el venedor, sobre UNA oferta concreta. Això és el que abans
    # faltava: sense això, tothom veia els missatges de tothom per a una
    # mateixa oferta.
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS converses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_oferta INTEGER NOT NULL,
        id_comprador INTEGER NOT NULL,
        id_venedor INTEGER NOT NULL,
        creat_a TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(id_oferta, id_comprador),
        FOREIGN KEY (id_oferta) REFERENCES ofertes (id),
        FOREIGN KEY (id_comprador) REFERENCES usuaris (id),
        FOREIGN KEY (id_venedor) REFERENCES usuaris (id)
    )
    ''')

    # --- Migracions afegint columnes noves sense perdre dades existents ---
    columnes_noves = [
        ("usuaris", "es_admin", "INTEGER DEFAULT 0"),
        ("usuaris", "descripcio", "TEXT DEFAULT ''"),
        ("transaccions", "concepte", "TEXT DEFAULT ''"),
        ("missatges", "id_conversa", "INTEGER"),
    ]
    for taula, columna, tipus in columnes_noves:
        try:
            cursor.execute(f"ALTER TABLE {taula} ADD COLUMN {columna} {tipus}")
        except sqlite3.OperationalError:
            pass  # La columna ja existeix

    # Índexs per accelerar les consultes més habituals
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ofertes_usuari ON ofertes(id_usuari)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_missatges_conversa ON missatges(id_conversa)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transaccions_pagador ON transaccions(id_pagador)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transaccions_cobrador ON transaccions(id_cobrador)")

    # --- Migració d'aparadors antics: si hi ha missatges d'abans de tenir
    # converses (id_conversa buit), els agrupem en una conversa per oferta
    # de la millor manera possible. És idempotent: un cop migrats, ja no
    # queden files amb id_conversa NULL i no torna a fer res. ---
    cursor.execute("SELECT DISTINCT id_oferta FROM missatges WHERE id_conversa IS NULL")
    ofertes_antigues = cursor.fetchall()
    for fila in ofertes_antigues:
        id_oferta = fila["id_oferta"]
        cursor.execute("SELECT id_usuari FROM ofertes WHERE id = ?", (id_oferta,))
        propietari = cursor.fetchone()
        if not propietari:
            continue  # L'oferta ja no existeix; deixem aquests missatges sense conversa (orfes, ocults)
        id_venedor = propietari["id_usuari"]

        cursor.execute(
            "SELECT DISTINCT id_remitent FROM missatges WHERE id_oferta = ? AND id_remitent != ?",
            (id_oferta, id_venedor),
        )
        candidats = [r["id_remitent"] for r in cursor.fetchall()]
        id_comprador = candidats[0] if candidats else id_venedor

        cursor.execute(
            "INSERT OR IGNORE INTO converses (id_oferta, id_comprador, id_venedor) VALUES (?, ?, ?)",
            (id_oferta, id_comprador, id_venedor),
        )
        cursor.execute(
            "SELECT id FROM converses WHERE id_oferta = ? AND id_comprador = ?",
            (id_oferta, id_comprador),
        )
        id_conversa = cursor.fetchone()["id"]
        cursor.execute(
            "UPDATE missatges SET id_conversa = ? WHERE id_oferta = ? AND id_conversa IS NULL",
            (id_conversa, id_oferta),
        )

    # --- Usuari administrador per defecte ---
    cursor.execute("SELECT * FROM usuaris WHERE correu = 'admin@gmail.com'")
    admin_existeix = cursor.fetchone()
    if not admin_existeix:
        contrasenya_admin = os.environ.get("CONTRASENYA_ADMIN", "admin1234")
        contrasenya_xifrada = generate_password_hash(contrasenya_admin)
        cursor.execute("""
            INSERT INTO usuaris (nom, correu, contrasenya, ciutat, saldo, es_admin)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("Administrador Mestre", "admin@gmail.com", contrasenya_xifrada, "Central", 9999, 1))
        if "CONTRASENYA_ADMIN" not in os.environ:
            print("⚠️  S'ha creat l'admin admin@gmail.com amb la contrasenya per defecte 'admin1234'. "
                  "Canvia-la (defineix CONTRASENYA_ADMIN) abans de publicar la web.")

    conn.commit()
    conn.close()


inicialitzar_bd()


# ==========================================
# UTILITATS
# ==========================================
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'id_usuari' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'id_usuari' not in session:
            return redirect(url_for('login'))
        if session.get('es_admin') != 1:
            return render_template(
                "error.html",
                titol="Accés denegat",
                missatge="Aquesta àrea és només per a administradors.",
                enllac=url_for('mercat'),
                text_enllac="Tornar al mercat",
            ), 403
        return f(*args, **kwargs)
    return wrapper


def parse_hores(text):
    """Converteix un text a hores vàlides (float > 0) o retorna None si no és vàlid."""
    try:
        valor = round(float(text), 2)
    except (TypeError, ValueError):
        return None
    return valor if valor > 0 else None


@app.context_processor
def injectar_saldo():
    if 'id_usuari' not in session:
        return dict(saldo=0.0)

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT saldo FROM usuaris WHERE id = ?", (session['id_usuari'],))
        resultat = cursor.fetchone()
        if resultat is None:
            saldo_real = 0.0
        elif resultat["saldo"] is None:
            # Només reparem un saldo TRENCAT (NULL). Un saldo de 0 és vàlid
            # i no s'ha de regenerar, o qualsevol usuari podria "resetejar-se"
            # a 5h gratis gastant tot el que té.
            cursor.execute("UPDATE usuaris SET saldo = 5.0 WHERE id = ?", (session['id_usuari'],))
            conn.commit()
            saldo_real = 5.0
        else:
            saldo_real = resultat["saldo"]
    except sqlite3.Error:
        saldo_real = 0.0
    finally:
        conn.close()
    return dict(saldo=saldo_real)


@app.before_request
def verificar_sessio_fantasma():
    if request.endpoint == 'static':
        return
    if 'id_usuari' in session:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM usuaris WHERE id = ?", (session['id_usuari'],))
        usuari_real = cursor.fetchone()
        conn.close()
        if not usuari_real:
            session.clear()
            return redirect(url_for('login'))


# ==========================================
# 1. ZONA PÚBLICA I ACCÉS
# ==========================================
@app.route("/")
def inici():
    if 'nom' in session:
        return redirect(url_for('mercat'))
    return render_template("index.html")


@app.route("/registre", methods=["GET", "POST"])
def registre():
    if request.method == "POST":
        nom = (request.form.get("nom") or "").strip()
        correu = (request.form.get("correu") or "").strip().lower()
        ciutat = (request.form.get("ciutat") or "").strip()
        ciutat_altra = (request.form.get("ciutat_altra") or "").strip()
        contrasenya = request.form.get("contrasenya") or ""
        contrasenya2 = request.form.get("contrasenya2") or ""

        if ciutat == "Altres" and ciutat_altra:
            ciutat = ciutat_altra
        if not ciutat:
            ciutat = "No especificada"

        if not nom or not correu or not contrasenya:
            flash("Omple tots els camps obligatoris.", "danger")
            return render_template("registre.html")
        if not EMAIL_REGEX.match(correu):
            flash("Introdueix un correu electrònic vàlid.", "danger")
            return render_template("registre.html")
        if len(contrasenya) < 6:
            flash("La contrasenya ha de tenir com a mínim 6 caràcters.", "danger")
            return render_template("registre.html")
        if contrasenya != contrasenya2:
            flash("Les contrasenyes no coincideixen.", "danger")
            return render_template("registre.html")

        contrasenya_encriptada = generate_password_hash(contrasenya)
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO usuaris (nom, correu, contrasenya, ciutat) VALUES (?, ?, ?, ?)",
                (nom, correu, contrasenya_encriptada, ciutat),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            flash("Aquest correu ja està registrat.", "danger")
            return render_template("registre.html")
        conn.close()

        flash("Compte creat correctament! Ja pots iniciar sessió.", "success")
        return redirect(url_for('login'))

    return render_template("registre.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('inici'))


@app.route("/normes")
def normes():
    return render_template("normes.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        correu = (request.form.get("correu") or "").strip().lower()
        contrasenya = request.form.get("contrasenya") or ""

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT id, nom, contrasenya, es_admin FROM usuaris WHERE correu = ?", (correu,))
        usuari = cursor.fetchone()
        conn.close()

        if usuari and check_password_hash(usuari["contrasenya"], contrasenya):
            session['id_usuari'] = usuari["id"]
            session['nom'] = usuari["nom"]
            session['es_admin'] = usuari["es_admin"]
            return redirect(url_for('mercat'))

        flash("Correu o contrasenya incorrectes.", "danger")
        return render_template("login.html")

    return render_template("login.html")


# ==========================================
# 2. ADMINISTRACIÓ
# ==========================================
@app.route("/eliminar_usuari/<int:id_usuari_a_esborrar>")
@admin_required
def eliminar_usuari(id_usuari_a_esborrar):
    if id_usuari_a_esborrar == session['id_usuari']:
        flash("No et pots auto-eliminar.", "danger")
        return redirect(url_for('admin'))

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM missatges WHERE id_conversa IN "
        "(SELECT id FROM converses WHERE id_comprador = ? OR id_venedor = ?)",
        (id_usuari_a_esborrar, id_usuari_a_esborrar),
    )
    cursor.execute(
        "DELETE FROM converses WHERE id_comprador = ? OR id_venedor = ?",
        (id_usuari_a_esborrar, id_usuari_a_esborrar),
    )
    cursor.execute("DELETE FROM ofertes WHERE id_usuari = ?", (id_usuari_a_esborrar,))
    cursor.execute("DELETE FROM usuaris WHERE id = ?", (id_usuari_a_esborrar,))
    conn.commit()
    conn.close()

    flash("Usuari eliminat correctament.", "success")
    return redirect(url_for('admin'))


@app.route("/eliminar_oferta/<int:id_oferta>")
@login_required
def eliminar_oferta(id_oferta):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id_usuari FROM ofertes WHERE id = ?", (id_oferta,))
    oferta = cursor.fetchone()

    if not oferta:
        conn.close()
        flash("Aquesta oferta ja no existeix.", "danger")
        return redirect(url_for('mercat'))

    creador_id = oferta["id_usuari"]

    if session['id_usuari'] != creador_id and session.get('es_admin') != 1:
        conn.close()
        flash("Només pots esborrar les teves pròpies ofertes.", "danger")
        return redirect(url_for('mercat'))

    cursor.execute(
        "DELETE FROM missatges WHERE id_conversa IN (SELECT id FROM converses WHERE id_oferta = ?)",
        (id_oferta,),
    )
    cursor.execute("DELETE FROM converses WHERE id_oferta = ?", (id_oferta,))
    cursor.execute("DELETE FROM ofertes WHERE id = ?", (id_oferta,))
    conn.commit()
    conn.close()

    flash("Oferta eliminada.", "success")
    if session.get('es_admin') == 1:
        return redirect(url_for('admin'))
    return redirect(url_for('mercat'))


@app.route("/admin")
@admin_required
def admin():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, nom, correu, ciutat, saldo FROM usuaris ORDER BY id")
    tots_usuaris = cursor.fetchall()
    cursor.execute("SELECT id, titol, id_usuari FROM ofertes ORDER BY id DESC")
    totes_ofertes = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) AS n, COALESCE(SUM(saldo), 0) AS total_hores FROM usuaris")
    stats = cursor.fetchone()
    conn.close()

    return render_template(
        "admin.html",
        usuaris=tots_usuaris,
        ofertes=totes_ofertes,
        total_usuaris=stats["n"],
        total_hores=round(stats["total_hores"], 2),
        total_ofertes=len(totes_ofertes),
    )


# ==========================================
# 3. MERCAT I OFERTES
# ==========================================
@app.route("/mercat")
@login_required
def mercat():
    cerca = (request.args.get("q") or "").strip()
    categoria_activa = (request.args.get("categoria") or "").strip()

    conn = get_db()
    cursor = conn.cursor()

    consulta = "SELECT id, titol, descripcio, hores, categoria FROM ofertes WHERE 1=1"
    params = []
    if cerca:
        consulta += " AND (titol LIKE ? OR descripcio LIKE ?)"
        params += [f"%{cerca}%", f"%{cerca}%"]
    if categoria_activa:
        consulta += " AND categoria = ?"
        params.append(categoria_activa)
    consulta += " ORDER BY id DESC"

    cursor.execute(consulta, params)
    ofertes = cursor.fetchall()

    cursor.execute("SELECT DISTINCT categoria FROM ofertes WHERE categoria IS NOT NULL ORDER BY categoria")
    categories = [r["categoria"] for r in cursor.fetchall()]
    conn.close()

    return render_template(
        "mercat.html",
        ofertes=ofertes,
        categories=categories,
        cerca=cerca,
        categoria_activa=categoria_activa,
    )


@app.route("/crear_oferta", methods=["GET", "POST"])
@login_required
def crear_oferta():
    if request.method == "POST":
        titol = (request.form.get("titol") or "").strip()
        descripcio = (request.form.get("descripcio") or "").strip()
        categoria = request.form.get("categoria") or "Altres"
        hores = parse_hores(request.form.get("hores"))

        if not titol or not descripcio or hores is None:
            flash("Revisa el formulari: falta algun camp o les hores no són vàlides.", "danger")
            return render_template("crear_oferta.html")

        autor_id = session["id_usuari"]
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ofertes (titol, descripcio, categoria, hores, id_usuari) VALUES (?, ?, ?, ?, ?)",
            (titol, descripcio, categoria, hores, autor_id),
        )
        conn.commit()
        conn.close()

        flash("Oferta publicada correctament!", "success")
        return redirect(url_for('mercat'))

    return render_template("crear_oferta.html")


@app.route("/oferta/<int:id_oferta>")
@login_required
def detall_oferta(id_oferta):
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT o.id, o.titol, o.descripcio, o.id_usuari, u.nom, u.ciutat, o.hores
        FROM ofertes o
        LEFT JOIN usuaris u ON o.id_usuari = u.id
        WHERE o.id = ?
    ''', (id_oferta,))
    oferta = cursor.fetchone()

    if not oferta:
        conn.close()
        return render_template(
            "error.html",
            titol="Oferta no trobada",
            missatge="Aquesta oferta no existeix o ha estat eliminada.",
            enllac=url_for('mercat'),
            text_enllac="Tornar al mercat",
        ), 404

    # Si l'autor de l'oferta va ser eliminat, mostrem un nom per defecte.
    if oferta["nom"] is None:
        oferta = list(oferta)
        oferta[4] = "Usuari eliminat"
        oferta[5] = "Desconeguda"

    cursor.execute("SELECT saldo FROM usuaris WHERE id = ?", (user_id,))
    resultat = cursor.fetchone()
    saldo_usuari = resultat["saldo"] if resultat and resultat["saldo"] is not None else 5.0
    conn.close()

    return render_template("detall_oferta.html", oferta=oferta, saldo=saldo_usuari)


# ==========================================
# 4. ZONA PERSONAL
# ==========================================
@app.route("/perfil", methods=["GET", "POST"])
@login_required
def perfil():
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()

    if request.method == "POST":
        nou_nom = (request.form.get("nom") or "").strip()
        nova_ciutat = (request.form.get("ciutat") or "").strip()
        ciutat_altra = (request.form.get("ciutat_altra") or "").strip()
        nova_descripcio = (request.form.get("descripcio") or "").strip()

        if nova_ciutat == "Altres" and ciutat_altra:
            nova_ciutat = ciutat_altra
        if not nou_nom:
            nou_nom = session.get('nom', 'Usuari')

        cursor.execute(
            "UPDATE usuaris SET nom = ?, ciutat = ?, descripcio = ? WHERE id = ?",
            (nou_nom, nova_ciutat, nova_descripcio, user_id),
        )
        conn.commit()
        session['nom'] = nou_nom
        flash("Perfil actualitzat correctament.", "success")

    cursor.execute("SELECT nom, correu, ciutat, saldo, descripcio FROM usuaris WHERE id = ?", (user_id,))
    usuari_bd = cursor.fetchone()
    conn.close()

    saldo_usuari = usuari_bd["saldo"] if usuari_bd else 0.0
    usuari_complet = [
        usuari_bd["nom"] if usuari_bd else "Usuari",
        usuari_bd["correu"] if usuari_bd else "",
        usuari_bd["ciutat"] if usuari_bd else "No especificada",
        usuari_bd["descripcio"] if usuari_bd and usuari_bd["descripcio"] else "",
    ]

    return render_template("perfil.html", usuari=usuari_complet, saldo=saldo_usuari)


# ==========================================
# 5. XATS (conversa privada = oferta + comprador)
# ==========================================
def _llista_converses(cursor, user_id):
    cursor.execute("""
        SELECT c.id AS id_conversa, o.titol AS titol,
               CASE WHEN c.id_comprador = ? THEN venedor.nom ELSE comprador.nom END AS altre_nom
        FROM converses c
        JOIN ofertes o ON c.id_oferta = o.id
        JOIN usuaris comprador ON c.id_comprador = comprador.id
        JOIN usuaris venedor ON c.id_venedor = venedor.id
        WHERE c.id_comprador = ? OR c.id_venedor = ?
        ORDER BY c.id DESC
    """, (user_id, user_id, user_id))
    return cursor.fetchall()


@app.route("/contactar/<int:id_oferta>")
@login_required
def contactar_venedor(id_oferta):
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id_usuari FROM ofertes WHERE id = ?", (id_oferta,))
    oferta = cursor.fetchone()
    if not oferta:
        conn.close()
        flash("Aquesta oferta ja no existeix.", "danger")
        return redirect(url_for('mercat'))

    id_venedor = oferta["id_usuari"]
    if user_id == id_venedor:
        conn.close()
        return redirect(url_for('xat'))

    cursor.execute("SELECT id FROM converses WHERE id_oferta = ? AND id_comprador = ?", (id_oferta, user_id))
    fila = cursor.fetchone()
    if fila:
        id_conversa = fila["id"]
    else:
        cursor.execute(
            "INSERT INTO converses (id_oferta, id_comprador, id_venedor) VALUES (?, ?, ?)",
            (id_oferta, user_id, id_venedor),
        )
        conn.commit()
        id_conversa = cursor.lastrowid
    conn.close()

    return redirect(url_for('xat_concret', id_conversa=id_conversa))


@app.route("/xat")
@login_required
def xat():
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()
    chats = _llista_converses(cursor, user_id)
    conn.close()

    return render_template(
        "xat.html", chats=chats, missatges=None, id_conversa=None,
        titol=None, altre_nom=None, el_meu_id=user_id,
    )


@app.route("/xat/<int:id_conversa>", methods=["GET", "POST"])
@login_required
def xat_concret(id_conversa):
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT c.id, c.id_oferta, c.id_comprador, c.id_venedor, o.titol,
               comprador.nom AS nom_comprador, venedor.nom AS nom_venedor
        FROM converses c
        JOIN ofertes o ON c.id_oferta = o.id
        JOIN usuaris comprador ON c.id_comprador = comprador.id
        JOIN usuaris venedor ON c.id_venedor = venedor.id
        WHERE c.id = ?
    ''', (id_conversa,))
    conversa = cursor.fetchone()

    if not conversa or user_id not in (conversa["id_comprador"], conversa["id_venedor"]):
        conn.close()
        flash("Aquesta conversa no existeix o no hi tens accés.", "danger")
        return redirect(url_for('xat'))

    if request.method == "POST":
        text_missatge = (request.form.get("missatge") or "").strip()
        if text_missatge:
            cursor.execute(
                "INSERT INTO missatges (id_oferta, id_remitent, id_conversa, missatge) VALUES (?, ?, ?, ?)",
                (conversa["id_oferta"], user_id, id_conversa, text_missatge),
            )
            conn.commit()
        conn.close()
        return redirect(url_for('xat_concret', id_conversa=id_conversa))

    chats = _llista_converses(cursor, user_id)

    cursor.execute('''
        SELECT u.nom, m.missatge, m.id_remitent
        FROM missatges m
        JOIN usuaris u ON m.id_remitent = u.id
        WHERE m.id_conversa = ?
        ORDER BY m.id ASC
    ''', (id_conversa,))
    missatges = cursor.fetchall()
    conn.close()

    altre_nom = conversa["nom_venedor"] if user_id == conversa["id_comprador"] else conversa["nom_comprador"]

    return render_template(
        "xat.html", chats=chats, missatges=missatges, id_conversa=id_conversa,
        titol=conversa["titol"], altre_nom=altre_nom, el_meu_id=user_id,
    )


@app.route("/eliminar_xat/<int:id_conversa>")
@login_required
def eliminar_xat(id_conversa):
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id_comprador, id_venedor FROM converses WHERE id = ?", (id_conversa,))
    conversa = cursor.fetchone()
    if not conversa or user_id not in (conversa["id_comprador"], conversa["id_venedor"]):
        conn.close()
        flash("No tens accés a aquesta conversa.", "danger")
        return redirect(url_for('xat'))

    cursor.execute("DELETE FROM missatges WHERE id_conversa = ?", (id_conversa,))
    cursor.execute("DELETE FROM converses WHERE id = ?", (id_conversa,))
    conn.commit()
    conn.close()

    flash("Conversa eliminada.", "success")
    return redirect(url_for('xat'))


# ==========================================
# 6. TRANSFERÈNCIES I HISTORIAL
# ==========================================
@app.route("/transferencia", methods=["GET", "POST"])
@login_required
def transferencia():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE usuaris SET saldo = 5.0 WHERE saldo IS NULL")
    conn.commit()

    id_pagador = session['id_usuari']
    cursor.execute("SELECT saldo FROM usuaris WHERE id = ?", (id_pagador,))
    resultat = cursor.fetchone()
    saldo_actual = resultat["saldo"] if (resultat and resultat["saldo"] is not None) else 5.0

    if request.method == "POST":
        correu_destinatari = (request.form.get("correu_destinatari") or "").strip().lower()
        concepte = (request.form.get("concepte") or "").strip()
        hores = parse_hores(request.form.get("hores"))

        if hores is None:
            conn.close()
            flash("Les hores han de ser un número vàlid més gran que 0.", "danger")
            return render_template("transferencia.html", saldo=saldo_actual)

        cursor.execute("SELECT id FROM usuaris WHERE correu = ?", (correu_destinatari,))
        destinatari = cursor.fetchone()

        if not destinatari:
            conn.close()
            flash(f"No existeix cap usuari amb el correu «{correu_destinatari}».", "danger")
            return render_template("transferencia.html", saldo=saldo_actual)

        id_cobrador = destinatari["id"]

        if id_pagador == id_cobrador:
            conn.close()
            flash("No pots pagar-te a tu mateix.", "danger")
            return render_template("transferencia.html", saldo=saldo_actual)

        if saldo_actual < hores:
            conn.close()
            flash(f"No tens prou saldo (tens {saldo_actual}h).", "danger")
            return render_template("transferencia.html", saldo=saldo_actual)

        cursor.execute("UPDATE usuaris SET saldo = saldo - ? WHERE id = ?", (hores, id_pagador))
        cursor.execute("UPDATE usuaris SET saldo = saldo + ? WHERE id = ?", (hores, id_cobrador))
        cursor.execute(
            "INSERT INTO transaccions (id_pagador, id_cobrador, hores, concepte) VALUES (?, ?, ?, ?)",
            (id_pagador, id_cobrador, hores, concepte),
        )
        conn.commit()
        conn.close()

        flash(f"Pagament de {hores}h realitzat amb èxit! ✅", "success")
        return redirect(url_for('historial'))

    conn.close()
    return render_template("transferencia.html", saldo=saldo_actual)


@app.route("/historial")
@login_required
def historial():
    user_id = session['id_usuari']
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT id, titol, descripcio FROM ofertes WHERE id_usuari = ? ORDER BY id DESC", (user_id,))
    les_meves_ofertes = cursor.fetchall()

    cursor.execute('''
        SELECT t.hores, t.data, t.concepte,
               pagador.nom AS nom_pagador,
               cobrador.nom AS nom_cobrador,
               t.id_pagador
        FROM transaccions t
        JOIN usuaris pagador ON t.id_pagador = pagador.id
        JOIN usuaris cobrador ON t.id_cobrador = cobrador.id
        WHERE t.id_pagador = ? OR t.id_cobrador = ?
        ORDER BY t.data DESC
    ''', (user_id, user_id))
    historial_transaccions = cursor.fetchall()
    conn.close()

    return render_template(
        "historial.html",
        ofertes=les_meves_ofertes,
        transaccions=historial_transaccions,
        el_meu_id=user_id,
    )


# ==========================================
# GESTIÓ D'ERRORS
# ==========================================
@app.errorhandler(404)
def pagina_no_trobada(e):
    return render_template(
        "error.html", titol="Pàgina no trobada",
        missatge="La pàgina que busques no existeix.",
    ), 404


@app.errorhandler(500)
def error_intern(e):
    return render_template(
        "error.html", titol="Error intern",
        missatge="Alguna cosa ha fallat al servidor. Torna-ho a provar.",
    ), 500


# ==========================================
# EXECUCIÓ DEL SERVIDOR
# ==========================================
if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG", "1") == "1")
