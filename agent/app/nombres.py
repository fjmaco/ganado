"""Spanish female names for the herd.

Every cow gets a real, single-word Spanish female name when she is created, so
your dad can say "¿cómo va Carmen?" instead of "¿cómo va la 477?" — and so the
herd reads like a herd instead of a spreadsheet.

Assignment walks this list in its committed order and takes the first name not
already in use. That makes it deterministic (a restart assigns the same name),
duplicate-free by construction, and stable — a name is never reused for a
different cow even after one leaves the herd, because the check is against
every name ever assigned.
"""

from __future__ import annotations

from .texto import normalizar, tokenizar

# ~160 real, single-word Spanish female given names. No compound names.
# Order is load-bearing: it is what makes assignment deterministic. Add new
# names at the END so existing cows keep the names they already have.
NOMBRES: tuple[str, ...] = (
    "Lucía", "Carmen", "Paloma", "Rosa", "Pilar", "Elena", "Marta", "Isabel",
    "Inés", "Beatriz", "Clara", "Nuria", "Teresa", "Ángela", "Manuela",
    "Consuelo", "Amparo", "Esperanza", "Mercedes", "Soledad", "Rocío",
    "Antonia", "Aurora", "Blanca", "Catalina", "Celia", "Elisa", "Emilia",
    "Eugenia", "Flora", "Gloria", "Guadalupe", "Irene", "Jimena", "Juana",
    "Leonor", "Lidia", "Lourdes", "Luisa", "Luz", "Magdalena", "Margarita",
    "Marina", "Matilde", "Micaela", "Nieves", "Ofelia", "Olivia", "Petra",
    "Ramona", "Raquel", "Regina", "Rita", "Rosalía", "Rosario", "Susana",
    "Trinidad", "Valentina", "Victoria", "Violeta", "Yolanda", "Adela",
    "Agustina", "Alba", "Alejandra", "Alicia", "Amalia", "Ana", "Andrea",
    "Aurelia", "Bárbara", "Benita", "Bernarda", "Camila", "Candelaria",
    "Casilda", "Cecilia", "Claudia", "Cristina", "Delia", "Diana", "Dolores",
    "Dominga", "Dora", "Elvira", "Emma", "Ernestina", "Estela", "Ester",
    "Eva", "Felisa", "Fernanda", "Filomena", "Florencia", "Francisca",
    "Gabriela", "Genoveva", "Graciela", "Griselda", "Herminia", "Hilda",
    "Ignacia", "Iris", "Jacinta", "Josefa", "Josefina", "Julia", "Justina",
    "Laura", "Leticia", "Liliana", "Lorena", "Lucrecia", "Marcela", "Mariana",
    "Martina", "Maura", "Milagros", "Mónica", "Natalia", "Norma", "Olga",
    "Patricia", "Paula", "Pastora", "Rebeca", "Renata", "Roberta", "Rufina",
    "Ruth", "Sabina", "Sandra", "Sara", "Silvia", "Sofía", "Tatiana",
    "Úrsula", "Valeria", "Vera", "Verónica", "Virginia", "Zoila", "Amanda",
    "Araceli", "Begoña", "Belén", "Berta", "Brígida", "Caridad", "Eulalia",
    "Fabiola", "Georgina", "Gisela", "Ivonne", "Lorenza", "Luciana", "Malena",
    "Nidia", "Noelia", "Olimpia", "Perla", "Rosana", "Selena", "Ximena",
)

# Not every animal in a herd is a cow. A bull calf called Carmen is the kind
# of detail that makes the whole thing feel like it wasn't built for him.
NOMBRES_MACHO: tuple[str, ...] = (
    "Aurelio", "Benito", "Camilo", "Damián", "Emilio", "Fabián", "Gerardo",
    "Hugo", "Ignacio", "Joaquín", "Leandro", "Marcelo", "Nicolás", "Octavio",
    "Pablo", "Quintín", "Rafael", "Salvador", "Teodoro", "Ulises", "Valentín",
    "Andrés", "Bernardo", "Cipriano", "Diego", "Esteban", "Federico", "Gaspar",
    "Horacio", "Isidro", "Julián", "Lorenzo", "Mauricio", "Norberto", "Ovidio",
    "Patricio", "Ramiro", "Sebastián", "Tomás", "Urbano", "Vicente", "Alonso",
    "Baltasar", "Cristóbal", "Domingo", "Eugenio", "Fermín", "Gonzalo",
    "Hilario", "Isaac", "Justo", "Lucio", "Melchor", "Nemesio", "Onésimo",
    "Prudencio", "Rómulo", "Simón", "Tadeo", "Venancio",
)

# Roman-numeral suffixes for the (very unlikely) case of outgrowing the list.
_SUFIJOS = ("II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")

HEMBRA, MACHO = "H", "M"


def asignar_nombre(usados: set[str], sexo: str = HEMBRA) -> str:
    """Pick the first unused name, in committed order.

    `usados` is every name in the Vacas tab — both lists are checked against
    the same set, so a herd can never end up with a Camilo and a Camila-style
    collision, and a retired animal's name is never handed to a new one.
    """
    tomados = {normalizar(n) for n in usados if n}
    lista = NOMBRES_MACHO if sexo == MACHO else NOMBRES

    for nombre in lista:
        if normalizar(nombre) not in tomados:
            return nombre

    # More than the whole list: keep going rather than fail, still no duplicates.
    for sufijo in _SUFIJOS:
        for nombre in lista:
            candidato = f"{nombre} {sufijo}"
            if normalizar(candidato) not in tomados:
                return candidato

    raise RuntimeError("Se agotaron los nombres disponibles para el hato.")


# Palabras con las que él distingue el sexo del animal sin pensarlo.
_MACHOS = ("ternero", "torete", "toro", "novillo", "macho", "becerro", "padrote")
_HEMBRAS = ("ternera", "novilla", "vaca", "hembra", "vaquilla", "becerra")


def sexo_mencionado(texto: str) -> str | None:
    """Infer the animal's sex from how he referred to it. None = didn't say."""
    n = normalizar(texto)
    # Las hembras primero: 'ternera' contiene 'ternera', pero 'ternero' no
    # debe capturarse por el prefijo de 'ternera'.
    for palabra in _HEMBRAS:
        if palabra in n:
            return HEMBRA
    for palabra in _MACHOS:
        if palabra in n:
            return MACHO
    return None


def buscar_por_nombre(texto: str, vacas: dict[str, str]) -> str | None:
    """Resolve a cow number from a name mentioned anywhere in the message.

    `vacas` maps cow number -> name. Returns the cow number, or None.
    Longer names are matched first so "Rosalía" never resolves as "Rosa".
    """
    n = tokenizar(texto)
    if not n:
        return None

    candidatos = sorted(
        ((num, nom) for num, nom in vacas.items() if nom),
        key=lambda par: len(par[1]),
        reverse=True,
    )
    for numero, nombre in candidatos:
        objetivo = tokenizar(nombre)
        if not objetivo:
            continue
        # Whole-word match so "Ana" doesn't fire inside "mañana".
        if f" {objetivo} " in f" {n} ":
            return numero
    return None
