#!/usr/bin/env python3
"""Veille des prix anormaux sur Amazon.ca — usage prive.

Cherche les erreurs de prix : un article a 250 $ alors qu il en vaut 4 500.
Pas les rabais ordinaires, pas les pourcentages spectaculaires sur des objets
a trois dollars — les accidents.

POURQUOI CE MONTAGE

/product coute UN jeton PAR ASIN ; le compte plafonne a 1200 jetons (20 par
minute, et les jetons expirent apres 60 min, ce qui fixe le plafond a une
heure de recharge). Interroger tout le catalogue de cette facon est ruineux.

/deal, lui, coute CINQ jetons par tranche de 150 offres. Huit pages, soit 1200
offres, reviennent a 40 jetons. D ou la strategie en deux temps :

  1. un filet large et bon marche  — /deal, 40 jetons, tout le catalogue ;
  2. un jugement serieux            — /product, un jeton par candidat, sur les
                                      quelques centaines qui franchissent le
                                      pre-tri.

Compte : 40 + environ 250 jetons par passage, soit ~7000 par jour, un quart de
la recharge quotidienne (28 800). Et jamais plus de 340 d un coup, tres loin
du plafond de 1200.

POURQUOI ON NE PEUT PAS SE FIER AUX MOYENNES DE KEEPA

Premier essai reel : 212 « anomalies », toutes fausses. Un piston de moto a
195 $ annonce « avant 126 717 $ », une rotule de suspension a 98 $ « avant
3 905 $ », un livre de poche a 236 $ « avant 2 043 $ ».

L explication est la meme dans les trois cas : l article n etait pas
disponible. Un seul vendeur le listait, a un prix delirant, faute de stock.
La moyenne Keepa — sur un jour comme sur 90 — enregistre ce prix fantome.
Quand un vrai vendeur reapparait a 195 $, Keepa annonce -100 %. Ce n est pas
une erreur de prix, c est un retour en stock.

Prendre la mediane des quatre moyennes ne corrige rien : les quatre sont
polluees en meme temps. Il faut donc juger autrement.

LE JUGEMENT, VERSION 3

On telecharge l historique complet (il vient avec /product) et on calcule
nous-memes trois choses que Keepa ne donne pas :

  - la MEDIANE PONDEREE PAR LE TEMPS sur 90 jours. Une annonce a 130 000 $
    affichee trois heures fait exploser une moyenne ; elle ne deplace pas une
    mediane ponderee par la duree.

  - le PLANCHER D AVANT : le prix le plus bas des 365 derniers jours en
    excluant les 48 dernieres heures. L exclusion est essentielle : si la
    baisse d aujourd hui est justement l anomalie, elle devient le minimum de
    l annee et rend le test circulaire.

  - la COHERENCE de la serie : le rapport entre la mediane 90 jours et le
    plancher d avant. Un vrai produit oscille dans un rapport de 1 a 3. Le
    piston affiche 1160. Au-dela de 8, la serie ne veut rien dire.

CATEGORIES EXCLUES

Les livres, la musique et les films sortent du balayage. Sur ces fiches, tous
les vendeurs et toutes les editions partagent un seul ASIN : un titre epuise
reste liste un an a 2 060 $ par un revendeur pendant que les avis viennent de
l edition de poche a 12 $. La serie est coherente avec elle-meme et resiste a
tous les tests ci-dessus. Au passage v3, 22 des 24 « anomalies » etaient des
livres. Ce ne sont de toute facon pas les rabais recherches.

S y ajoutent deux garde-fous tires de /product :

  - le produit doit VENDRE (salesRankDrops90 > 0). Les fantomes ne vendent
    jamais ; c est meme ce qui les rend fantomes.
  - il doit avoir ete EN STOCK au moins la moitie des 90 derniers jours.

LE CRITERE EST EN DOLLARS, PAS EN POURCENTAGE

Un rabais de 94 % sur un porte-cles a 3 $ ne vaut rien. Le meme pourcentage
sur un velo electrique a 4 500 $, c est une prise. On trie sur l ECART EN
DOLLARS ; le pourcentage ne sert qu a preselectionner cote serveur.
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
ECART_MIN = 200.00                # l ecart en dollars qui declenche l alerte
PRIX_MIN = 25.00                  # sous ce prix, meme un gros ecart est du bruit
VERIFIER_MAX = 300                # appels /product par passage, au maximum
LOT_PRODUIT = 100                 # ASIN par requete /product (maximum Keepa)
MEMOIRE_HEURES = 48               # on ne resignale pas deux fois la meme chose
GARDER_HISTORIQUE = 200

# Les seuils du jugement, version 3. Voir l en-tete pour le raisonnement.
SOUS_LE_PLANCHER = 0.90           # il faut etre 10 % sous le plancher d avant
COHERENCE_MAX = 8.0               # mediane90 / plancher : au-dela, serie folle
FENETRE_MEDIANE = 90              # jours
IGNORER_RECENT_H = 48             # heures exclues du calcul du plancher
STOCK_MIN_PCT = 50                # en stock au moins la moitie du temps
VENTES_MIN = 3                    # ventes estimees en 90 jours, ou bien...
AVIS_MIN = 10                     # ...des avis, pour les articles chers qui
                                  # se vendent peu mais existent vraiment
COUVERTURE_MIN_J = 30             # jours de prix connus exiges avant de juger

# Les categories qu on ne balaie pas. Voir l en-tete pour le pourquoi.
GROUPES_EXCLUS = frozenset([
    "book", "books", "ebooks", "abis_book",
    "music", "digital music track", "digital music album", "abis_music",
    "dvd", "video", "abis_dvd", "movies", "theatrical",
])
RELIURES_EXCLUES = frozenset([
    "paperback", "hardcover", "mass market paperback", "board book",
    "library binding", "perfect paperback", "pocket book", "spiral-bound",
    "audio cd", "audio cassette", "vinyl", "kindle edition", "comic",
    "loose leaf", "printed access code", "school & library binding",
])

TAG = "dtlinformat0f-20"
FICHIER_VUS = "vus.json"
FICHIER_HISTORIQUE = "historique.json"
FICHIER_PAGE = "index.html"

EST = timezone(timedelta(hours=-4))
# Les horodatages Keepa comptent les minutes depuis le 1er janvier 2011 UTC.
EPOQUE_KEEPA = datetime(2011, 1, 1, tzinfo=timezone.utc)


def maintenant():
    return datetime.now(EST)


# ---------------------------------------------------------------------------
# Petits acces surs aux tableaux Keepa
# ---------------------------------------------------------------------------

def case(tableau, i):
    """Valeur i du tableau, ou None. Keepa met -1 pour « inconnu »."""
    try:
        v = tableau[i]
    except (IndexError, TypeError):
        return None
    return None if v is None or v < 0 else v


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


def reference_grossiere(offre, i):
    """Le prix « avant » annonce par Keepa — la mediane de ses moyennes.

    Cette valeur ne sert QU AU PRE-TRI : elle est souvent fausse (voir
    l en-tete). Le vrai jugement se fait plus loin, sur l historique.
    """
    avg = offre.get("avg") or []
    valeurs = sorted(v for v in
                     (case2(avg, n, i) for n in (JOUR, SEMAINE, MOIS, TRIMESTRE))
                     if v)
    if len(valeurs) < 2:
        return None
    return valeurs[(len(valeurs) - 1) // 2] / 100.0


def candidat(offre):
    """Pre-tri bon marche. Genereux : c est /product qui tranchera."""
    prix, i = prix_courant(offre)
    if prix is None or prix < PRIX_MIN:
        return None
    grossier = reference_grossiere(offre, i)
    if grossier is None or grossier - prix < ECART_MIN:
        return None

    courant = offre.get("current") or []
    note = case(courant, NOTE)
    return {
        "asin": offre.get("asin"),
        "titre": (offre.get("title") or "").strip(),
        "prix": round(prix, 2),
        "grossier": round(grossier, 2),
        "note": round(note / 10, 1) if note else None,
        "avis": case(courant, AVIS),
        "lien": f"https://www.amazon.ca/dp/{offre.get('asin')}?tag={TAG}",
        "vu": maintenant().strftime("%Y-%m-%d %H:%M"),
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


# ---------------------------------------------------------------------------
# L historique : ce que Keepa ne calcule pas pour nous
# ---------------------------------------------------------------------------

def intervalles(serie, depuis, jusqu_a):
    """Decoupe une serie csv Keepa en (prix_en_dollars, duree_en_minutes).

    Une serie csv est plate : [minute, prix, minute, prix, ...]. Chaque couple
    dit « a partir de cette minute, le prix est celui-la », jusqu au couple
    suivant. Un prix de -1 signifie « pas disponible » : on saute l intervalle
    plutot que de le compter comme un prix de zero.

    depuis et jusqu_a sont en minutes Keepa. On rogne les intervalles qui
    depassent la fenetre pour ne compter que le temps reellement dedans.
    """
    if not serie or len(serie) < 2:
        return []
    resultat = []
    for k in range(0, len(serie) - 1, 2):
        t = serie[k]
        p = serie[k + 1]
        fin = serie[k + 2] if k + 2 < len(serie) else jusqu_a
        debut = max(t, depuis)
        fin = min(fin, jusqu_a)
        if p is None or p < 0 or fin <= debut:
            continue
        resultat.append((p / 100.0, fin - debut))
    return resultat


def mediane_ponderee(morceaux):
    """La mediane du prix ponderee par le temps passe a ce prix.

    C est la statistique qui resiste aux annonces fantomes : trois heures a
    130 000 $ pesent trois heures, pas plus.
    """
    if not morceaux:
        return None
    total = sum(d for _, d in morceaux)
    if total <= 0:
        return None
    cumul = 0
    for prix, duree in sorted(morceaux):
        cumul += duree
        if cumul >= total / 2:
            return prix
    return sorted(morceaux)[-1][0]


def minutes_keepa(moment):
    return int((moment - EPOQUE_KEEPA).total_seconds() // 60)


def analyser_historique(produit, fin=None):
    """Renvoie (mediane 90 j, plancher d avant, jours de prix connus).

    Le plancher exclut les 48 dernieres heures. Sans cette exclusion, la
    baisse qu on cherche a detecter deviendrait elle-meme le minimum de
    l annee, et le test « est-il sous son plancher » serait toujours faux.

    Le troisieme retour est la COUVERTURE : combien de jours, sur les 365,
    on connait vraiment un prix. Un article mis en vente la semaine derniere
    n a pas de plancher digne de ce nom ; on ne veut pas le juger. On compte
    des jours et non des points de mesure : un prix stable toute l annee ne
    produit qu un seul point, et c est pourtant l historique le plus solide
    qui soit.
    """
    csv = produit.get("csv") or []
    serie = None
    for i in (NEUF, AMAZON):
        s = csv[i] if i < len(csv) else None
        if s and len(s) >= 4:
            serie = s
            break
    if serie is None:
        return None, None, 0

    if fin is None:
        fin = minutes_keepa(datetime.now(timezone.utc))
    m90 = mediane_ponderee(intervalles(serie, fin - FENETRE_MEDIANE * 1440, fin))

    avant = fin - IGNORER_RECENT_H * 60
    anciens = intervalles(serie, fin - 365 * 1440, avant)
    plancher = min((p for p, _ in anciens), default=None)
    couverture = sum(d for _, d in anciens) / 1440.0
    return m90, plancher, couverture


def stat_entier(stats, nom, defaut=0):
    v = stats.get(nom)
    return v if isinstance(v, (int, float)) and v >= 0 else defaut


def stock_connu(stats):
    """Le pourcentage de temps EN STOCK sur 90 jours, ou None si inconnu.

    Piege releve au passage v3 : Keepa renvoie outOfStockPercentage90 sous
    forme de TABLEAU indexe par type de prix, pas d entier. La premiere
    version lisait un entier ; la valeur etait donc toujours rejetee et le
    garde-fou ne servait a rien.
    """
    v = stats.get("outOfStockPercentage90")
    if isinstance(v, list):
        v = case(v, NEUF)
    if not isinstance(v, (int, float)) or v < 0:
        return None
    return 100 - int(v)


def categorie_exclue(produit, asin=None):
    """Vrai pour les livres, la musique et les films.

    Trois signaux, parce qu aucun ne suffit seul : le groupe de produit, la
    reliure, et la forme de l ASIN. Un ASIN qui commence par un chiffre est un
    ISBN-10 : c est un livre, sans exception. C est le signal le plus sur des
    trois, et le seul qui ne depende pas de champs que Keepa remplit de facon
    inegale.

    Sur ces fiches, tous les vendeurs et toutes les editions partagent le meme
    ASIN. Un titre epuise reste liste un an a 2 060 $ par un revendeur pendant
    que les 1 605 avis viennent de l edition de poche a 12 $. La serie de prix
    est alors parfaitement coherente avec elle-meme, le produit « vend », il a
    des avis — aucun des garde-fous precedents ne la voit passer. C est ce qui
    remplissait la page au passage v3 : 24 anomalies, 22 livres.

    Ce ne sont de toute facon pas les rabais recherches.
    """
    code = asin or produit.get("asin") or ""
    if code[:1].isdigit():
        return True
    groupe = (produit.get("productGroup") or "").strip().lower()
    reliure = (produit.get("binding") or "").strip().lower()
    return groupe in GROUPES_EXCLUS or reliure in RELIURES_EXCLUES


def juger(prise, produit, fin=None):
    """Le verdict, ecrit dans la prise. Renvoie True si on publie.

    Chaque refus est enregistre dans prise["refus"] : c est ce qui permet de
    comprendre, en lisant le journal du passage, pourquoi la page est vide.
    """
    if categorie_exclue(produit, prise.get("asin")):
        prise["refus"] = "livre, musique ou film"
        return False

    m90, plancher, couverture = analyser_historique(produit, fin)
    stats = produit.get("stats") or {}
    prix = prise["prix"]

    prise["mediane90"] = round(m90, 2) if m90 else None
    prise["plancher"] = round(plancher, 2) if plancher else None
    prise["ventes90"] = stat_entier(stats, "salesRankDrops90")
    prise["stock90"] = stock_connu(stats)

    if m90 is None or plancher is None or couverture < COUVERTURE_MIN_J:
        prise["refus"] = "historique trop court pour juger"
        return False
    if prix > plancher * SOUS_LE_PLANCHER:
        prise["refus"] = "pas sous son plancher de 12 mois"
        return False
    if m90 - prix < ECART_MIN:
        prise["refus"] = "ecart reel inferieur au seuil"
        return False
    if plancher > 0 and m90 / plancher > COHERENCE_MAX:
        prise["refus"] = "serie de prix incoherente (annonce fantome)"
        return False
    if prise["ventes90"] < VENTES_MIN and (prise.get("avis") or 0) < AVIS_MIN:
        prise["refus"] = "aucune traction : ni ventes ni avis"
        return False
    if prise["stock90"] is not None and prise["stock90"] < STOCK_MIN_PCT:
        prise["refus"] = "en rupture plus de la moitie du temps"
        return False

    prise["normal"] = round(m90, 2)
    prise["ecart"] = round(m90 - prix, 2)
    prise["pct"] = round(100 * (m90 - prix) / m90)
    prise["refus"] = None
    return True


def verifier(cle, prises):
    """Le second temps : un jeton par candidat, par lots de cent.

    Renvoie la liste des prises qui passent le jugement. On imprime aussi les
    motifs de refus : c est le tableau de bord qui permet de regler les seuils
    sans deviner.
    """
    gardees = []
    lot_total = prises[:VERIFIER_MAX]
    if len(prises) > VERIFIER_MAX:
        print(f"  ATTENTION : {len(prises) - VERIFIER_MAX} candidat(s) non "
              f"verifie(s), plafond de {VERIFIER_MAX} atteint")

    par_asin = {}
    for debut in range(0, len(lot_total), LOT_PRODUIT):
        tranche = lot_total[debut:debut + LOT_PRODUIT]
        try:
            r = requests.get(KEEPA_PRODUIT, timeout=180,
                             params={"key": cle, "domain": DOMAINE,
                                     "asin": ",".join(p["asin"] for p in tranche),
                                     "stats": 365})
            r.raise_for_status()
            paquet = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"  lot {debut // LOT_PRODUIT} : echec ({type(e).__name__})")
            continue
        for p in paquet.get("products") or []:
            par_asin[p.get("asin")] = p
        print(f"  lot {debut // LOT_PRODUIT} : {len(tranche)} ASIN, "
              f"{paquet.get('tokensLeft', '?')} jetons restants")

    motifs = {}
    for prise in lot_total:
        produit = par_asin.get(prise["asin"])
        if produit is None:
            prise["refus"] = "produit introuvable"
        elif juger(prise, produit):
            gardees.append(prise)
            continue
        motifs[prise["refus"]] = motifs.get(prise["refus"], 0) + 1

    for motif, n in sorted(motifs.items(), key=lambda x: -x[1]):
        print(f"    ecarte {n:4d} x  {motif}")

    # On imprime le groupe et la reliure des rescapes : c est comme ca qu on
    # decouvre les valeurs exactes que Keepa emploie, et donc ce qu il reste
    # a ajouter aux listes d exclusion quand un CD passe encore.
    for prise in gardees:
        p = par_asin.get(prise["asin"]) or {}
        print(f"    garde {prise['asin']} · groupe={p.get('productGroup')!r} "
              f"· reliure={p.get('binding')!r} · {prise['titre'][:60]}")
    return gardees


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
    marque = '<span class="neuf">NOUVEAU</span>' if neuf else ""
    note = (f'<span class="n">{p["note"]}★ · {p["avis"]} avis</span>'
            if p.get("note") else "")
    detail = (f'plancher 12 mois : {p["plancher"]:.2f} $'
              if p.get("plancher") else "plancher inconnu")
    if p.get("ventes90"):
        detail += f' · {p["ventes90"]} vente(s) estimée(s) en 90 j'
    return f"""<article class="p">
  <div class="ec">−{p['ecart']:.0f} $</div>
  <div class="co">
    <h2><a href="{e(p['lien'])}" target="_blank" rel="noopener">{e(p['titre'][:120])}</a>{marque}</h2>
    <p class="pr"><strong>{p['prix']:.2f} $</strong> <s>{p['normal']:.2f} $</s>
       <span class="pc">−{p['pct']} %</span> {note}</p>
    <p class="me"><span class="b conf">{detail}</span> · repéré le {e(p['vu'])}
       · <code>{e(p['asin'])}</code></p>
  </div>
</article>"""


def ecrire_page(neuves, historique, jetons, duree, examines=0):
    d = maintenant()
    corps = []

    if neuves:
        corps.append(f"<h1>{len(neuves)} anomalie(s) à regarder maintenant</h1>")
        corps += [carte(p, neuf=True) for p in neuves]
    else:
        corps.append("<h1>Rien de neuf</h1>")
        corps.append('<p class="vide">Aucun article ne passe les six tests '
                     f'ce passage-ci ({examines} candidat(s) examiné(s) en '
                     'détail). C’est le cas le plus fréquent — une vraie '
                     'erreur de prix est rare, et c’est exactement ce qui la '
                     'rend intéressante.</p>')

    vus_asin = {p["asin"] for p in neuves}
    anciennes = [h for h in historique if h.get("asin") not in vus_asin][:40]
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
    padding:14px 16px;margin-bottom:10px;border-left:4px solid var(--vert)}}
.ec{{flex:0 0 96px;font-size:22px;font-weight:700;color:var(--vert);
     display:flex;align-items:center;justify-content:center}}
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
footer li{{margin-bottom:4px}}
</style>
<div class="w">
<header>
  <b>VEILLE DES PRIX ANORMAUX</b>
  <p class="st">Dernier passage : {d.strftime('%Y-%m-%d %H:%M')} (heure de l’Est) ·
     {jetons} jetons Keepa restants · {duree} s ·
     {examines} candidat(s) passés au crible</p>
</header>
{''.join(corps)}
<footer>
  Les livres, la musique et les films sont exclus : une fiche de livre est
  partagée par tous les vendeurs et toutes les éditions, si bien qu’un titre
  épuisé listé un an à 2 000 $ ressemble à une aubaine dès qu’un vrai
  exemplaire réapparaît.
  <br><br>
  Pour le reste, un article doit franchir six tests : coûter au moins
  {PRIX_MIN:.0f} $ ; être au moins 10 % <strong>sous son plus bas prix des
  douze derniers mois</strong>, les 48 dernières heures exclues du calcul ;
  afficher un écart d’au moins {ECART_MIN:.0f} $ avec sa <strong>médiane
  pondérée par le temps sur 90 jours</strong> — pas la moyenne de Keepa, qui
  est faussée par les annonces fantômes ; avoir une série de prix cohérente
  (médiane au plus {COHERENCE_MAX:.0f} × son plancher) ; montrer une trace
  d’existence réelle — au moins {VENTES_MIN} ventes estimées en 90 jours, ou
  {AVIS_MIN} avis ; et avoir été en stock au moins {STOCK_MIN_PCT} % du temps.
  <br><br>
  Ces règles viennent d’un premier essai qui avait sorti 212 fausses
  anomalies : des articles indisponibles qu’un seul vendeur affichait à un
  prix délirant, et dont le retour en stock ressemblait à une chute de 100 %.
  <br><br>
  Page privée, régénérée chaque heure. Amazon annule fréquemment les commandes
  passées sur un prix erroné. Rien ici n’est vérifié à la main.
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
    prises.sort(key=lambda p: p["grossier"] - p["prix"], reverse=True)
    print(f"{len(prises)} candidat(s) a verifier")

    gardees = verifier(cle, prises)
    gardees.sort(key=lambda p: p["ecart"], reverse=True)
    print(f"{len(gardees)} anomalie(s) apres jugement")

    neuves = filtrer_deja_vus(gardees)
    print(f"{len(neuves)} nouvelle(s) depuis le dernier passage")

    historique = charger(FICHIER_HISTORIQUE, [])
    historique = (neuves + historique)[:GARDER_HISTORIQUE]
    enregistrer(FICHIER_HISTORIQUE, historique)

    duree = int(time.time() - debut)
    examines = min(len(prises), VERIFIER_MAX)
    with open(FICHIER_PAGE, "w", encoding="utf-8") as f:
        f.write(ecrire_page(neuves, historique, jetons_restants(cle),
                            duree, examines))

    print(f"OK : {len(neuves)} nouvelle(s) sur {examines} examine(s) — {duree} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
