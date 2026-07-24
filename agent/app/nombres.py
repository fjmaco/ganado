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

# Roman-numeral suffixes for the (very unlikely) case of outgrowing the list.
_SUFIJOS = ("II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X")


def asignar_nombre(usados: set[str]) -> str:
    """Pick the first unused name, in committed order.

    `usados` is every name currently in the Vacas tab. Matching is done on the
    normalised form so a hand-typed "lucia" still counts as Lucía being taken.
    """
    tomados = {normalizar(n) for n in usados if n}

    for nombre in NOMBRES:
        if normalizar(nombre) not in tomados:
            return nombre

    # More than ~160 cows: keep going rather than fail, still without duplicates.
    for sufijo in _SUFIJOS:
        for nombre in NOMBRES:
            candidato = f"{nombre} {sufijo}"
            if normalizar(candidato) not in tomados:
                return candidato

    raise RuntimeError("Se agotaron los nombres disponibles para el hato.")


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
