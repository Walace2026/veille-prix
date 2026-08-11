#!/usr/bin/env python3
"""Veille des prix anormaux sur Amazon.ca — usage prive.

Cherche les erreurs de prix : un article a 250 $ alors qu il en vaut 4 500.
Pas les rabais ordinaires, pas les pourcentages spectaculaires sur des objets
a trois dollars — les accidents.

POURQUOI CE MONTAGE

Le premier reflexe serait de demander a Keepa l historique de chaque produit
et de comparer. C est ruineux : /product coute UN jeton PAR ASIN, et le compte
plafonne a 1200 jetons (20 par minute, et les jetons expirent apres 60 min, ce
qui fixe le plafond a une heure de recharge).

/deal, lui, coute CINQ jetons par tranche de 150 offres. Huit pages, soit 1200
offres, reviennent donc a 40 jetons. On peut balayer tout le catalogue toutes
les heures pour 960 jetons par jour, moins de 4 % de la recharge quotidienne.

D ou la strategie en deux temps :
  1. un filet large et bon marche  — /deal, 40 jetons, tout le catalogue ;
  2. une verification ciblee       — /product, un jeton, seulement sur les
                                     rares candidats qui franchissent le seuil.

LE CRITERE EST EN DOLLARS, PAS EN POURCENTAGE

Un rabais de 94 % sur un porte-cles a 3 $ ne vaut rien. Le meme pourcentage
sur un velo electrique a 4 500 $, c est une prise. On trie donc sur l ECART EN
DOLLARS, et le pourcentage ne sert qu a preselectionner cote serveur.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

KEEPA_DEAL = "https://api.keepa.com/deal"
KEEPA_PRODUIT = "https://api.keepa.com/product"
KEEPA_TOKEN = "https://api.keepa.com/token"
DOMAINE = 6                       # Amazon.ca

# Indices des series de prix Keepa, en base zero cote Python.
AMAZON, NEUF, LISTPRICE = 0, 1, 4
NOTE, AVIS = 16, 17
# Intervalles des tableaux avg / deltaPercent : jour, semaine, mois, 90 jours.
JOUR, SEMAINE, MOIS, TRIMESTRE = 0, 1, 2, 3

PAGES = 8                         # 8 x 150 = 1200 offres, 40 jetons
RABAIS_MIN_PCT = 60               # preselection cote serveur
ECART_MIN = 150.00                # l ecart en dollars qui declenche l alerte
PRIX_MIN = 25.00                  # sous ce prix, meme un gros ecart est du bruit
CONFIRMER_MAX = 25                # appels /product par passage, au maximum
MEMOIRE_HEURES = 48               # on ne resignale pas deux fois la meme chose
GARDER_HISTORIQUE = 200

TAG = "dtlinformat0f-20"
FICHIER_VUS = "vus.json"
FICHIER_HISTORIQUE = "historique.json"
FICHIER_PAGE = "index.html"
EST = timezone(timedelta(hours=-4))


def maintenant():
    return datetime.now(EST)


# ---------------------------------------------------------------------------
# Lecture des donnees Keepa
# ---------------------------------------------------------------------------

def case(tableau, i):
    """Une valeur de prix Keepa, ou None. -1 signifie « inconnu » chez Keepa."""
    try:
        v = tableau[i]
    except (IndexError, TypeError):
        return None
    return v if isinstance(v, (int, float)) and v > 0 else None


def case2(tableau, i, j):
    try:
        return case(tableau[i], j)
    except (IndexError, TypeError):
        return None


def prix_courant(offre):
    """Le prix qu on paierait aujourd hui : neuf d abord, sinon Amazon."""
    for i in (NEUF, AMAZON):
        v = case(offre.get("current") or [], i)
        if v:
            return v / 100.0, i
    return None, None


def prix_normal(offre, i):
    """Ce que l article vaut d habitude.

    On prend la moyenne la plus longue disponible — 90 jours de preference.
    Volontairement PAS la moyenne du jour : si l erreur de prix est en cours,
    la moyenne du jour est deja contaminee par l erreur elle-meme et l ecart
    parait plus petit qu il ne l est.
    """
    avg = offre.get("avg") or []
    for intervalle in (TRIMESTRE, MOIS, SEMAINE):
        v = case2(avg, intervalle, i)
        if v:
            return v / 100.0
    return None


def candidat(offre):
    """Renvoie une prise, ou None. Tout le jugement est ici."""
    prix, i = prix_courant(offre)
    if prix is None or prix < PRIX_MIN:
        return None
    normal = prix_normal(offre, i)
    if normal is None or normal <= prix:
        return None

    ecart = normal - prix
    if ecart < ECART_MIN:
        return None

    courant = offre.get("current") or []
    note = case(courant, NOTE)
    return {
        "asin": offre.get("asin"),
        "titre": (offre.get("title") or "").strip(),
        "prix": round(prix, 2),
        "normal": round(normal, 2),
        "ecart": round(ecart, 2),
        "pct": round(100 * ecart / normal),
        "note": round(note / 10, 1) if note else None,
        "avis": case(courant, AVIS),
        "lien": f"https://www.amazon.ca/dp/{offre.get('asin')}?tag={TAG}",
        "vu": maintenant().strftime("%Y-%m-%d %H:%M"),
        "plancher": None,
        "confirme": None,
    }


def balayer(cle):
    """Le filet large : huit pages de /deal, 40 jetons."""
    trouves, vus_asin = [], set()
    for page in range(PAGES):
        selection = {
            "page": page, "domainId": DOMAINE, "priceTypes": [NEUF],
            "dateRange": JOUR, "deltaPercentRange": [RABAIS_MIN_PCT, 100],
            "isRangeEnabled": True, "isFilterEnabled": True, "sortType": 4,
        }
        try:
            r = requests.get(KEEPA_DEAL, timeout=90,
                             params={"key": cle, "selection": json.dumps(selection)})
            r.raise_for_status()
            paquet = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  page {page} ignoree ({type(e).__name__})")
            continue

        offres = ((paquet.get("deals") or {}).get("dr")) or []
        if not offres:
            break
        for o in offres:
            asin = o.get("asin")
            if not asin or asin in vus_asin:
                continue
            vus_asin.add(asin)
            c = candidat(o)
            if c:
                trouves.append(c)
        print(f"  page {page} : {len(offres)} offres, {len(trouves)} candidat(s) "
              f"cumule(s), {paquet.get('tokensLeft', '?')} jetons")
    return trouves


def confirmer(cle, prises):
    """Le second temps : verifier contre le plancher des 365 derniers jours.

    Un jeton par ASIN, et seulement sur la poignee de candidats retenus. Un
    prix sous son propre plancher d un an n a jamais ete aussi bas : c est la
    signature d une anomalie, pas d une promotion saisonniere.
    """
    if not prises:
        return
    lot = [p["asin"] for p in prises[:CONFIRMER_MAX]]
    try:
        r = requests.get(KEEPA_PRODUIT, timeout=120,
                         params={"key": cle, "domain": DOMAINE,
                                 "asin": ",".join(lot), "stats": 365})
        r.raise_for_status()
        produits = r.json().get("products") or []
    except (requests.RequestException, ValueError) as e:
        print(f"  confirmation impossible ({type(e).__name__}) — on garde les candidats")
        return

    par_asin = {p.get("asin"): p for p in produits}
    for prise in prises:
        p = par_asin.get(prise["asin"])
        if not p:
            continue
        stats = p.get("stats") or {}
        mini = None
        for i in (NEUF, AMAZON):
            try:
                paire = (stats.get("min") or [])[i]
                if isinstance(paire, list) and len(paire) > 1 and paire[1] > 0:
                    mini = paire[1] / 100.0
                    break
            except (IndexError, TypeError):
                continue
        if mini:
            prise["plancher"] = round(mini, 2)
            prise["confirme"] = prise["prix"] <= mini


# ---------------------------------------------------------------------------
# Memoire : ne pas resignaler la meme chose toutes les heures
# ---------------------------------------------------------------------------

def charger(chemin, defaut):
    if not os.path.exists(chemin):
        return defaut
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return defaut


def enregistrer(chemin, donnees):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=1)


def filtrer_deja_vus(prises):
    """Une prise est « nouvelle » si on ne l a pas signalee dans les 48 h.

    La cle inclut le prix arrondi : si le prix rebaisse encore, c est une
    nouvelle information et ca merite de reapparaitre.
    """
    vus = charger(FICHIER_VUS, {})
    limite = maintenant() - timedelta(hours=MEMOIRE_HEURES)
    vus = {k: v for k, v in vus.items()
           if datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=EST) > limite}

    neuves = []
    for p in prises:
        cle = f"{p['asin']}@{int(p['prix'])}"
        if cle not in vus:
            neuves.append(p)
        vus[cle] = p["vu"]
    enregistrer(FICHIER_VUS, vus)
    return neuves


# ---------------------------------------------------------------------------
# La page
# ---------------------------------------------------------------------------

def e(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def carte(p, neuf=False):
    badge = ('<span class="b conf">sous son plancher de 365 jours</span>'
             if p.get("confirme") else
             ('<span class="b">plancher inconnu</span>' if p.get("plancher") is None
              else f'<span class="b">plancher 365 j : {p["plancher"]:.2f} $</span>'))
    marque = '<span class="neuf">NOUVEAU</span>' if neuf else ""
    note = (f'<span class="n">{p["note"]}★ · {p["avis"]} avis</span>'
            if p.get("note") else "")
    return f"""<article class="p{' fort' if p.get('confirme') else ''}">
  <div class="ec">−{p['ecart']:.0f} $</div>
  <div class="co">
    <h2><a href="{e(p['lien'])}" target="_blank" rel="noopener">{e(p['titre'][:120])}</a>{marque}</h2>
    <p class="pr"><strong>{p['prix']:.2f} $</strong> <s>{p['normal']:.2f} $</s>
       <span class="pc">−{p['pct']} %</span> {note}</p>
    <p class="me">{badge} · repéré le {e(p['vu'])} · <code>{e(p['asin'])}</code></p>
  </div>
</article>"""


def ecrire_page(neuves, historique, jetons, duree):
    d = maintenant()
    corps = []

    if neuves:
        corps.append(f"<h1>{len(neuves)} anomalie(s) à regarder maintenant</h1>")
        corps += [carte(p, neuf=True) for p in neuves]
    else:
        corps.append("<h1>Rien de neuf</h1>")
        corps.append('<p class="vide">Aucun écart de plus de '
                     f'{ECART_MIN:.0f} $ depuis le dernier passage. '
                     'C’est le cas le plus fréquent — une vraie erreur de prix '
                     'est rare, et c’est exactement ce qui la rend intéressante.</p>')

    anciennes = [h for h in historique if h not in neuves][:40]
    if anciennes:
        corps.append("<h1 class='h2'>Repérées dans les derniers jours</h1>")
        corps += [carte(p) for p in anciennes]

    return f"""<!doctype html>
<html lang="fr-CA">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Veille des prix anormaux — {d.strftime('%d %b %H:%M')}</title>
<style>
:root{{--nuit:#12232e;--nuit2:#1a303e;--creme:#f6f7f9;--gris:#8fa3b5;
       --or:#f5a623;--vert:#4caf80}}
*{{box-sizing:border-box}}
body{{margin:0;padding:24px 16px 64px;background:var(--nuit);color:var(--creme);
     font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
.w{{max-width:900px;margin:0 auto}}
header{{border-bottom:1px solid #294256;padding-bottom:16px;margin-bottom:28px}}
header b{{color:var(--or)}}
.st{{color:var(--gris);font-size:14px;margin:6px 0 0}}
h1{{font-size:20px;margin:32px 0 16px}}
h1.h2{{color:var(--gris);font-size:16px;font-weight:600;
       border-top:1px solid #294256;padding-top:28px}}
.p{{display:flex;gap:16px;background:var(--nuit2);border-radius:12px;
    padding:14px 16px;margin-bottom:10px;border-left:4px solid #2f4a60}}
.p.fort{{border-left-color:var(--vert)}}
.ec{{flex:0 0 96px;font-size:22px;font-weight:700;color:var(--or);
     display:flex;align-items:center;justify-content:center}}
.p.fort .ec{{color:var(--vert)}}
.co{{flex:1;min-width:0}}
h2{{font-size:15px;margin:0 0 6px;font-weight:600;line-height:1.35}}
h2 a{{color:var(--creme);text-decoration:none}}
h2 a:hover{{text-decoration:underline}}
.neuf{{background:var(--or);color:var(--nuit);font-size:10px;font-weight:700;
       border-radius:4px;padding:2px 6px;margin-left:8px;vertical-align:middle}}
.pr{{margin:0 0 4px;font-size:15px}}
.pr s{{color:var(--gris);margin-left:6px}}
.pc{{color:var(--or);margin-left:6px}}
.n{{color:var(--gris);font-size:13px;margin-left:8px}}
.me{{margin:0;color:var(--gris);font-size:12.5px}}
.b{{color:var(--gris)}} .b.conf{{color:var(--vert);font-weight:600}}
code{{font-size:12px;color:#6f8699}}
.vide{{color:var(--gris)}}
footer{{margin-top:48px;border-top:1px solid #294256;padding-top:16px;
        color:var(--gris);font-size:13px}}
</style>
<div class="w">
<header>
  <b>VEILLE DES PRIX ANORMAUX</b>
  <p class="st">Dernier passage : {d.strftime('%Y-%m-%d %H:%M')} (heure de l’Est) ·
     {jetons} jetons Keepa restants · {duree} s ·
     seuil : écart de {ECART_MIN:.0f} $ ou plus</p>
</header>
{''.join(corps)}
<footer>
  Page privée, régénérée chaque heure. Le seuil est un <strong>écart en
  dollars</strong>, pas un pourcentage : un rabais de 90 % sur un article à
  3 $ ne vaut rien, le même sur un vélo à 4 500 $ en vaut la peine.
  La mention verte signifie que le prix est passé sous son plus bas des
  365 derniers jours — la vraie signature d’une anomalie.
  <br><br>
  Amazon annule fréquemment les commandes passées sur un prix erroné.
  Rien ici n’est vérifié à la main.
</footer>
</div>
"""


# ---------------------------------------------------------------------------

def jetons_restants(cle):
    try:
        r = requests.get(KEEPA_TOKEN, params={"key": cle}, timeout=30)
        r.raise_for_status()
        return r.json().get("tokensLeft", "?")
    except (requests.RequestException, ValueError):
        return "?"


def main():
    debut = time.time()
    cle = os.environ.get("KEEPA_API_KEY", "").strip()
    if not cle:
        raise SystemExit("ERREUR : KEEPA_API_KEY absente des secrets du depot.")

    print(f"Balayage de {PAGES} pages ({PAGES * 5} jetons)")
    prises = balayer(cle)
    prises.sort(key=lambda p: p["ecart"], reverse=True)
    print(f"{len(prises)} candidat(s) au-dessus de {ECART_MIN} $ d ecart")

    neuves = filtrer_deja_vus(prises)
    print(f"{len(neuves)} nouvelle(s) depuis le dernier passage")

    confirmer(cle, neuves)

    historique = charger(FICHIER_HISTORIQUE, [])
    historique = (neuves + historique)[:GARDER_HISTORIQUE]
    enregistrer(FICHIER_HISTORIQUE, historique)

    duree = int(time.time() - debut)
    with open(FICHIER_PAGE, "w", encoding="utf-8") as f:
        f.write(ecrire_page(neuves, historique, jetons_restants(cle), duree))

    confirmees = sum(1 for p in neuves if p.get("confirme"))
    print(f"OK : {len(neuves)} nouvelle(s), dont {confirmees} sous leur plancher "
          f"365 j — {duree} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
