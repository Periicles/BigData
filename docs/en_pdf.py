"""Rend un Markdown du dépôt en HTML paginé, prêt pour l'impression PDF.

    python docs/en_pdf.py docs/RAPPORT.md /tmp/rapport.html "Titre"
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --headless --disable-gpu --no-pdf-header-footer \
        --virtual-time-budget=30000 \
        --print-to-pdf=docs/RAPPORT.pdf file:///tmp/rapport.html

`--virtual-time-budget` N'EST PAS FACULTATIF : les diagrammes Mermaid sont
dessinés par un script, après le chargement de la page. Sans ce délai, Chrome
imprime avant que le dessin existe et le PDF sort avec deux cadres vides.
L'impression a donc besoin du réseau, le temps de charger Mermaid depuis son
CDN — c'est le prix à payer pour que le diagramme n'ait qu'une seule source de
vérité, le bloc ```mermaid du Markdown, qui reste par ailleurs rendu tel quel
par GitHub.

DEUX DÉPENDANCES HORS `requirements.txt`, ET C'EST VOULU. `markdown` et
`pygments` ne servent qu'à fabriquer un livrable de lecture : les embarquer
dans l'environnement du pipeline ferait passer celui-ci de deux à quatre
dépendances pour une raison qui n'a rien à voir avec l'entrepôt. Elles
s'installent donc dans un environnement séparé, jetable :

    python -m venv /tmp/pdfvenv && /tmp/pdfvenv/bin/pip install markdown pygments

Le rendu PDF lui-même est confié à Chrome en mode headless, déjà présent sur
un poste de travail : pas de chaîne LaTeX à installer, et un moteur qui sait
paginer du CSS `break-before`.
"""
import re
import sys
from html import escape
from pathlib import Path

import markdown

# Épinglé : une version majeure de Mermaid change la syntaxe acceptée, et un
# diagramme qui ne compile plus ne laisse qu'un cadre vide dans le PDF.
MERMAID = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

source, sortie, titre = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
texte = source.read_text(encoding="utf-8")

# ── Diagrammes ──────────────────────────────────────────────────────────
# Les blocs ```mermaid sont retirés AVANT le rendu Markdown : laissés en
# place, ils sortiraient en bloc de code coloré, c'est-à-dire en code source
# imprimé au lieu du dessin. Ils sont remis après, dans la balise que Mermaid
# reconnaît, et échappés — sans quoi le navigateur interpréterait les `<br/>`
# des libellés comme du balisage et Mermaid ne les recevrait jamais.
_ATTENTE_DIAGRAMME = "\x00diagramme-{}\x00"
diagrammes: list[str] = []


def _extraire(m: re.Match) -> str:
    diagrammes.append(m.group(1))
    return _ATTENTE_DIAGRAMME.format(len(diagrammes) - 1)


texte = re.sub(r"```mermaid\n(.*?)\n```", _extraire, texte, flags=re.S)

corps = markdown.markdown(
    texte,
    extensions=["tables", "fenced_code", "codehilite", "toc", "sane_lists", "attr_list"],
    extension_configs={"codehilite": {"guess_lang": False, "noclasses": True,
                                      "pygments_style": "friendly"}},
)

# ── Sommaire ────────────────────────────────────────────────────────────
# Construit après le rendu, depuis les titres réellement produits : un
# sommaire écrit à la main se désynchronise dès qu'un titre change. Deux
# niveaux seulement — au troisième, il occuperait lui-même deux pages.
def _sommaire(html: str) -> str:
    entrees = re.findall(r'<h([123]) id="([^"]+)">(.*?)</h[123]>', html, re.S)
    if len(entrees) < 6:
        return ""
    lignes = ['<nav class="sommaire"><h2 class="sansnum">Sommaire</h2><ul>']
    for niveau, ancre, libelle in entrees:
        if niveau == "1" and "Dossier" in libelle or "Guide" in libelle:
            continue  # le titre du document n'entre pas dans son propre sommaire
        texte = re.sub(r"<[^>]+>", "", libelle)
        lignes.append(f'<li class="n{niveau}"><a href="#{ancre}">{texte}</a></li>')
    lignes.append("</ul></nav>")
    return "".join(lignes)

corps = corps.replace("<hr />", _sommaire(corps) + "<hr />", 1)

# ── Images ──────────────────────────────────────────────────────────────
# Les chemins du Markdown sont relatifs au DOCUMENT (`imgs/…`), alors que le
# HTML intermédiaire vit ailleurs. Sans réécriture, Chrome ne trouverait rien
# et imprimerait des cadres vides — un défaut invisible dans le HTML, qui ne
# se voit qu'en ouvrant le PDF.
def _absolutiser(html: str) -> str:
    def sub(m):
        chemin = m.group(1)
        if chemin.startswith(("http://", "https://", "data:", "file://", "/")):
            return m.group(0)
        return f'src="{(source.parent / chemin).resolve().as_uri()}"'
    return re.sub(r'src="([^"]+)"', sub, html)

corps = _absolutiser(corps)

for numero, diagramme in enumerate(diagrammes):
    corps = corps.replace(
        _ATTENTE_DIAGRAMME.format(numero),
        f'<pre class="mermaid">{escape(diagramme)}</pre>',
    )

# Chaque chapitre commence une page — sauf le tout premier titre, et sauf la
# section qui suit immédiatement une page de partie (elles feraient deux sauts
# consécutifs, donc une page blanche).
corps = re.sub(r"<h1", '<h1 class="partie"', corps)
# Seules les sections NUMÉROTÉES ouvrent une page. Les titres du chapitre de
# leçons sont aussi des h2, et leur donner un saut chacun produirait huit
# pages au quart remplies.
corps = re.sub(r'<h2 id="([^"]*)">(\s*\d)', r'<h2 class="chapitre" id="\1">\2', corps)
corps = corps.replace('<h1 class="partie"', '<h1 class="couverture"', 1)
corps = corps.replace('<h2 class="chapitre"', "<h2", 1)
corps = re.sub(
    r'(<h1 class="partie".*?)<h2 class="chapitre"',
    r"\1<h2",
    corps,
    flags=re.S,
)

STYLE = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
* { box-sizing: border-box; }
body {
  font-family: "Charter", "Iowan Old Style", Georgia, serif;
  font-size: 10.2pt; line-height: 1.52; color: #1a1a1a; margin: 0;
  -webkit-font-smoothing: antialiased; hyphens: auto;
}
h1, h2, h3, h4 { font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
                 line-height: 1.22; color: #101010; }
h1 { font-size: 21pt; margin: 0 0 4pt; letter-spacing: -0.3pt; }
h2 { font-size: 15.5pt; margin: 22pt 0 8pt; padding-bottom: 5pt;
     border-bottom: 1.6pt solid #1a1a1a; letter-spacing: -0.2pt; }
h3 { font-size: 11.6pt; margin: 16pt 0 5pt; color: #0b3d6b; }
h4 { font-size: 10.4pt; margin: 12pt 0 4pt; color: #333; }
h2.chapitre { break-before: page; }
/* Page de titre : le premier h1 n'ouvre pas de partie, il ouvre le document. */
h1.couverture { margin-top: 175pt; font-size: 30pt; text-align: center;
                letter-spacing: -0.6pt; }
h1.couverture + p { text-align: center; font-size: 12pt; color: #444;
                    margin-top: 10pt; }
h1.couverture ~ hr { display: none; }
/* Captures d'écran. Le paragraphe qui SUIT une image en est la légende :
   `:has()` évite d'avoir à baliser chaque couple en HTML dans le Markdown. */
/* Hauteur plafonnée : une console ClickHouse imprimée occupe sinon une page
   entière pour trois lignes de résultat. Les tableaux de bord, eux, sont
   denses de haut en bas et méritent la page. */
img { max-width: 100%; max-height: 290pt; width: auto; height: auto;
      display: block; margin: 0 auto;
      border: 0.6pt solid #cfd4da; border-radius: 3pt; }
img[src*="tdb-"] { max-height: 660pt; }
p:has(> img) { margin: 10pt 0 0; break-inside: avoid; break-after: avoid; }
p:has(> img) + p em { display: block; font-size: 8.8pt; color: #555;
                      text-align: left; padding: 0 4pt; }
p:has(> img) + p { margin: 4pt 0 12pt; break-inside: avoid; }
/* Deux captures qui se suivent forment une paire : elles restent groupées. */
p:has(> img) + p:has(> img) { margin-top: 5pt; }

nav.sommaire { break-before: page; break-after: page; }
nav.sommaire h2.sansnum { break-before: auto; font-size: 17pt; border-bottom: 1.6pt solid #1a1a1a; }
nav.sommaire ul { list-style: none; padding: 0; margin: 12pt 0 0; }
nav.sommaire li { margin: 0; padding: 2.5pt 0; }
nav.sommaire li.n1 { font-weight: 700; font-size: 11.4pt; margin-top: 12pt;
                     color: #0b3d6b; border-top: 0.6pt solid #d5d9de; padding-top: 8pt; }
nav.sommaire li.n2 { font-size: 10pt; padding-left: 10pt; }
nav.sommaire li.n3 { font-size: 9.2pt; padding-left: 26pt; color: #444; }
nav.sommaire a { color: inherit; }
/* Page de partie : un intertitre qui sépare franchement les deux livraisons. */
h1.partie { break-before: page; font-size: 27pt; margin: 44pt 0 10pt;
            padding-bottom: 10pt; border-bottom: 3pt solid #0b3d6b; color: #0b3d6b; }
h1.partie + p { font-size: 11pt; color: #444; text-align: left; margin-bottom: 4pt; }
h1.partie ~ p em { color: #444; }
h2, h3, h4 { break-after: avoid; }
p, ul, ol, table, pre, blockquote { break-inside: avoid-page; }
p { margin: 0 0 7pt; text-align: justify; orphans: 2; widows: 2; }
ul, ol { margin: 0 0 8pt; padding-left: 17pt; }
li { margin-bottom: 3pt; }
strong { font-weight: 700; }
code { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 8.7pt;
       background: #f2f3f5; padding: 0.5pt 3pt; border-radius: 2.5pt; }
pre { background: #f7f8fa; border: 0.6pt solid #dfe2e7; border-left: 2.6pt solid #0b3d6b;
      border-radius: 3pt; padding: 8pt 10pt; margin: 0 0 9pt; overflow-x: hidden; }
pre code { background: none; padding: 0; font-size: 8.2pt; line-height: 1.42;
           white-space: pre-wrap; word-break: break-word; }
table { border-collapse: collapse; width: 100%; margin: 0 0 10pt; font-size: 8.9pt; }
th { background: #0b3d6b; color: #fff; text-align: left; font-weight: 600;
     padding: 5pt 7pt; font-family: "Helvetica Neue", Helvetica, sans-serif;
     hyphens: none; white-space: nowrap; }
/* `break-all` coupait les codes au milieu d'un mot — « ClickHouse ne vo /
   it pas le lake ». `anywhere` ne coupe que ce qui ne tient pas, et préfère
   les frontières de mot. */
td code { overflow-wrap: anywhere; word-break: normal; }
td { padding: 4.5pt 7pt; border-bottom: 0.5pt solid #e3e6ea; vertical-align: top; }
tbody tr:nth-child(even) { background: #f7f8fa; }
blockquote { margin: 0 0 9pt; padding: 7pt 11pt; background: #fff8e6;
             border-left: 2.6pt solid #d99b00; }
blockquote p:last-child { margin-bottom: 0; }
hr { border: none; border-top: 0.6pt solid #d5d9de; margin: 14pt 0; }
a { color: #0b3d6b; text-decoration: none; }
/* Diagrammes. Le SVG produit par Mermaid porte une largeur intrinsèque qui
   déborde volontiers de la page : on la plafonne, et on borne la hauteur pour
   qu'un diagramme haut n'occupe pas deux pages à moitié vides. */
pre.mermaid { background: none; border: none; padding: 0; margin: 12pt 0;
              text-align: center; break-inside: avoid; }
/* 215 mm, et pas la hauteur utile de la page (259 mm) : le diagramme doit
   tenir SOUS le titre de sa section, sinon il bascule seul sur la page
   suivante et laisse derrière lui une page presque blanche. */
pre.mermaid svg { max-width: 100%; max-height: 215mm; height: auto; }
/* Les libellés de Mermaid sont du HTML posé dans le SVG : sans cette remise à
   zéro, ils héritent du corps de texte et se retrouvent justifiés et coupés
   par des césures À L'INTÉRIEUR des boîtes du diagramme. */
pre.mermaid foreignObject div, pre.mermaid span, pre.mermaid p {
  text-align: center; hyphens: none; }
/* Le titre d'un sous-graphe ne dispose que d'une ligne : Mermaid ne réserve
   pas la hauteur d'une seconde, qui passerait sous la bordure du cadre. */
pre.mermaid .cluster-label div, pre.mermaid .cluster-label span {
  white-space: nowrap; }
"""

SCRIPT = f"""
<script src="{MERMAID}"></script>
<script>
  mermaid.initialize({{
    startOnLoad: true,
    theme: "neutral",
    // `wrappingWidth` par défaut vaut 200 px : le titre du sous-graphe y est
    // coupé en deux lignes, dont la seconde passe sous la bordure du cadre —
    // Mermaid ne réserve la place que d'une ligne pour ces titres.
    flowchart: {{ htmlLabels: true, useMaxWidth: true, wrappingWidth: 400 }},
    // LE MODÈLE EN ÉTOILE SE LIT DE HAUT EN BAS, PAS DE GAUCHE À DROITE.
    // En disposition par défaut, les trois tables de faits s'alignent côte à
    // côte : le dessin devient deux fois plus large que haut, et réduit à la
    // largeur d'une page A4 il perd ses noms de colonnes. En `LR`, les rangs
    // se succèdent horizontalement et les entités d'un même rang s'empilent —
    // le dessin devient plus haut que large, donc lisible en portrait.
    er: {{ useMaxWidth: true, layoutDirection: "LR" }},
  }});
</script>
"""

sortie.write_text(
    f"<!doctype html><html lang=fr><head><meta charset=utf-8>"
    f"<title>{titre}</title><style>{STYLE}</style></head>"
    f"<body>{corps}{SCRIPT}</body></html>",
    encoding="utf-8",
)
print(f"{sortie} écrit ({len(corps)} caractères de HTML, "
      f"{len(diagrammes)} diagrammes)")
