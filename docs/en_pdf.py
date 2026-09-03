"""Rend un Markdown du dépôt en HTML paginé, prêt pour l'impression PDF.

    python docs/en_pdf.py docs/RAPPORT.md /tmp/rapport.html "Titre"
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        --headless --disable-gpu --no-pdf-header-footer \
        --print-to-pdf=docs/RAPPORT.pdf file:///tmp/rapport.html

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
from pathlib import Path

import markdown

source, sortie, titre = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
texte = source.read_text(encoding="utf-8")

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

# Chaque chapitre commence une page — sauf le tout premier titre, et sauf la
# section qui suit immédiatement une page de partie (elles feraient deux sauts
# consécutifs, donc une page blanche).
corps = re.sub(r"<h1", '<h1 class="partie"', corps)
corps = re.sub(r"<h2", '<h2 class="chapitre"', corps)
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
p { margin: 0 0 7pt; text-align: justify; }
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
td code { word-break: break-all; }
td { padding: 4.5pt 7pt; border-bottom: 0.5pt solid #e3e6ea; vertical-align: top; }
tbody tr:nth-child(even) { background: #f7f8fa; }
blockquote { margin: 0 0 9pt; padding: 7pt 11pt; background: #fff8e6;
             border-left: 2.6pt solid #d99b00; }
blockquote p:last-child { margin-bottom: 0; }
hr { border: none; border-top: 0.6pt solid #d5d9de; margin: 14pt 0; }
a { color: #0b3d6b; text-decoration: none; }
"""

sortie.write_text(
    f"<!doctype html><html lang=fr><head><meta charset=utf-8>"
    f"<title>{titre}</title><style>{STYLE}</style></head><body>{corps}</body></html>",
    encoding="utf-8",
)
print(f"{sortie} écrit ({len(corps)} caractères de HTML)")
