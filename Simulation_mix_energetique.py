# -*- coding: utf-8 -*-
"""
=============================================================================
 DASHBOARD STREAMLIT — SIMULATEUR DE MIX ÉNERGÉTIQUE COMMUNAL
=============================================================================
 Dans l'esprit de Batterie1.py (siège TE13), généralisé au multi-énergies :
   - plusieurs filières de production (éolien, hydro, PV, extensible),
   - batterie avec rendements de charge/décharge,
   - échanges réseau (import / export),
   - KPI : TAP (taux d'autoproduction), TAC (taux d'autoconsommation),
     écrêtage, facture nette,
   - onglets : flux temporels, bilan mensuel, optimisation batterie,
     étude paramétrique du mix.

 Données attendues (mode "Fichiers réels") — CSV ou Excel à 2 colonnes :
   colonne 1 : date/heure  |  colonne 2 : valeur
   - Consommation : puissance en kW
   - Productions  : facteur de charge normalisé entre 0 et 1
     (la puissance réelle = facteur × capacité installée réglée en barre
      latérale ; c'est ce qui permet de faire varier le dimensionnement
      sans retoucher les fichiers)
     - possibilité de changer le type de fichier attendu (si Json/ Excel...)
=============================================================================
"""

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. PARAMÈTRES ET CONFIGURATION
# ==========================================
st.set_page_config(page_title="Mix Énergétique Communal", layout="wide")

FILIERES = {          # nom : couleur (extensible : ajoutez une ligne ici)
    "Éolien": "#1f77b4",
    "Hydro":  "#17becf",
    "PV":     "#ff7f0e",
}
COULEUR_BATTERIE = "#2ca02c"
COULEUR_IMPORT   = "#d62728"
COULEUR_EXPORT   = "#9467bd"

def rgba(hex_couleur, alpha=1.0):
    """'#1f77b4', 0.8 → 'rgba(31,119,180,0.8)' (les hex à 8 chiffres avec
    canal alpha ne sont pas acceptés par le validateur de couleurs Plotly)."""
    h = hex_couleur.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"

# ==========================================
# 2. DONNÉES : DÉMO SYNTHÉTIQUE OU FICHIERS RÉELS
# ==========================================

@st.cache_data
def generer_donnees_demo(annee=2025, conso_annuelle_GWh=20.0, graine=42):
    """Profils synthétiques réalistes (mêmes modèles que le prototype PyPSA) :
    conso thermosensible, vent autocorrélé, hydro pluvial-nival, PV 44°N."""
    rng = np.random.default_rng(graine)
    t = pd.date_range(f"{annee}-01-01", periods=8760, freq="h")
    jour, heure = t.dayofyear.values, t.hour.values

    # --- Consommation (MW)
    saison = 1.0 + 0.35 * np.cos(2 * np.pi * (jour - 15) / 365)
    prof_j = (0.75 + 0.20 * np.exp(-0.5 * ((heure - 8) / 2.0) ** 2)
                    + 0.30 * np.exp(-0.5 * ((heure - 19) / 2.5) ** 2))
    weekend = np.where(t.dayofweek.values >= 5, 0.92, 1.0)
    conso = saison * prof_j * weekend * (1 + 0.04 * rng.standard_normal(len(t)))
    conso = np.clip(conso, 0.2, None)
    conso = conso / conso.sum() * conso_annuelle_GWh * 1000  # MW

    # --- Éolien : vent AR(1) + biais hivernal → courbe de puissance
    vent = np.zeros(len(t)); vent[0] = 7.0
    for i in range(1, len(t)):
        vent[i] = 0.985 * vent[i-1] + 0.015 * 7.5 + 0.55 * rng.standard_normal()
    vent = np.clip(vent * (1 + 0.25 * np.cos(2*np.pi*(jour-20)/365)), 0, None)
    fc_eol = np.where(vent < 3, 0, np.where(vent < 12, ((vent-3)/9)**3,
                      np.where(vent < 25, 1.0, 0.0)))

    # --- Hydro fil de l'eau : régime saisonnier + crues, seuil technique
    base = 0.55 + 0.35 * np.cos(2 * np.pi * (jour - 60) / 365)
    crues = np.zeros(len(t))
    for i in range(1, len(t)):
        crues[i] = 0.992 * crues[i-1] + (1.2 if rng.random() < 0.0006 else 0.0)
    fc_hyd = base + crues + 0.02 * rng.standard_normal(len(t))
    fc_hyd = np.where(fc_hyd < 0.15, 0.0, np.clip(fc_hyd, 0, 1))

    # --- PV : géométrie solaire simplifiée (lat 44° N) × nébulosité persistante
    decl = np.radians(23.45 * np.sin(2 * np.pi * (284 + jour) / 365))
    lat = np.radians(44.0)
    sin_elev = (np.sin(lat)*np.sin(decl)
                + np.cos(lat)*np.cos(decl)*np.cos(np.radians(15*(heure+0.5-12))))
    ciel = np.clip(sin_elev, 0, None) ** 1.15
    # Climat méditerranéen : forte proportion de journées claires
    n_j = int(np.ceil(len(t)/24)); neb = np.zeros(n_j); neb[0] = 0.85
    for i in range(1, n_j):
        neb[i] = np.clip(0.5*neb[i-1] + 0.5*rng.uniform(0.45, 1.0), 0.2, 1.0)
    fc_pv = np.clip(0.95 * ciel * np.repeat(neb, 24)[:len(t)], 0, 1)

    return pd.DataFrame({"conso_MW": conso, "fc_Éolien": np.clip(fc_eol, 0, 1),
                         "fc_Hydro": fc_hyd, "fc_PV": fc_pv}, index=t)


@st.cache_data
def charger_fichier(fichier, nom_colonne):
    """Lit un CSV/Excel (date, valeur) → série indexée datetime.
    Robuste aux formats français : séparateur ';', décimales à virgule,
    colonnes surnuméraires, horodatages en double (changement d'heure)."""
    if fichier.name.lower().endswith(".csv"):
        # sep=None + engine='python' : détection automatique de , ; ou tab
        df = pd.read_csv(fichier, sep=None, engine="python")
        # Si les valeurs sont du texte avec virgule décimale, retenter
        if df.shape[1] >= 2 and df.iloc[:, 1].dtype == object:
            fichier.seek(0)
            df = pd.read_csv(fichier, sep=None, engine="python", decimal=",")
    else:
        df = pd.read_excel(fichier)

    if df.shape[1] < 2:
        st.error(f"Le fichier {fichier.name} doit contenir au moins 2 colonnes "
                 f"(date, valeur) — une seule détectée. Vérifiez le séparateur.")
        st.stop()
    # On ne garde que les 2 premières colonnes (les exports Enedis en ont plus)
    df = df.iloc[:, :2].copy()
    df.columns = ["date", nom_colonne]

    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce")
    df = df.dropna(subset=["date"])
    df["date"] = df["date"].dt.tz_localize(None)
    df.set_index("date", inplace=True)

    df[nom_colonne] = pd.to_numeric(
        df[nom_colonne].astype(str).str.replace(",", ".", regex=False),
        errors="coerce").fillna(0)

    df = df.sort_index()
    # Doublons d'horodatage (heure d'hiver) : on moyenne
    if df.index.has_duplicates:
        df = df.groupby(level=0).mean()
    return df[nom_colonne]

# ==========================================
# 3. MOTEUR DE SIMULATION MULTI-ÉNERGIES
# ==========================================

def simuler_mix(df, capacites_MW, cap_batt_MWh, p_batt_MW,
                rend_charge=0.95, rend_decharge=0.95,
                p_max_import_MW=1e9, p_max_export_MW=1e9,
                soc_initial_pct=0.0):
    """Dispatch à règles, généralisation multi-sources de Batterie1.py.

    Ordre de priorité : renouvelables (fatales) → batterie → réseau.
      - surplus  : charge batterie (× rend_charge), puis export (limité),
                   puis écrêtage ;
      - déficit  : décharge batterie (× rend_decharge), puis import.

    df : DataFrame avec 'conso_MW' et une colonne 'fc_<filière>' par filière.
    capacites_MW : dict {filière: MW installés}.
    Retourne (df_res, dt_heures).
    """
    dt = ((df.index[1] - df.index[0]).total_seconds() / 3600.0
          if len(df) > 1 else 1.0)

    conso = df["conso_MW"].values
    prods = {f: cap * df[f"fc_{f}"].values for f, cap in capacites_MW.items()}
    prod_tot = np.sum(list(prods.values()), axis=0) if prods else np.zeros(len(df))

    soc = cap_batt_MWh * soc_initial_pct / 100.0
    n = len(df)
    soc_h   = np.empty(n); batt_ch  = np.empty(n); batt_dech = np.empty(n)
    imp     = np.empty(n); exp      = np.empty(n); ecret     = np.empty(n)
    direct  = np.empty(n)

    for i in range(n):
        c, p = conso[i], prod_tot[i]
        flux_direct = min(c, p)
        surplus, deficit = p - flux_direct, c - flux_direct
        ch = dech = 0.0

        if surplus > 0 and cap_batt_MWh > 0:
            # Puissance max admissible pour ne pas dépasser la capacité,
            # en tenant compte du rendement de charge
            p_place = (cap_batt_MWh - soc) / (rend_charge * dt)
            ch = min(surplus, p_batt_MW, max(p_place, 0.0))
            soc += ch * rend_charge * dt
            surplus -= ch
        elif deficit > 0 and cap_batt_MWh > 0:
            # Le stock disponible restitue soc × rendement de décharge
            p_dispo = soc * rend_decharge / dt
            dech = min(deficit, p_batt_MW, max(p_dispo, 0.0))
            soc -= dech / rend_decharge * dt
            deficit -= dech

        e = min(surplus, p_max_export_MW)      # export limité par le poste
        ecr = surplus - e                      # le reste est écrêté
        im = min(deficit, p_max_import_MW)

        soc_h[i], batt_ch[i], batt_dech[i] = soc, ch, dech
        imp[i], exp[i], ecret[i], direct[i] = im, e, ecr, flux_direct

    df_res = df.copy()
    for f in prods:
        df_res[f"prod_{f}_MW"] = prods[f]
    df_res["prod_totale_MW"]   = prod_tot
    df_res["autoconso_directe_MW"] = direct
    df_res["batt_charge_MW"]   = batt_ch
    df_res["batt_decharge_MW"] = batt_dech
    df_res["SoC_MWh"]          = soc_h
    df_res["SoC_pct"] = soc_h / cap_batt_MWh * 100 if cap_batt_MWh > 0 else 0.0
    df_res["import_MW"]        = imp
    df_res["export_MW"]        = exp
    df_res["ecretage_MW"]      = ecret
    return df_res, dt


def calculer_kpi(df_res, dt, prix_import=80.0, prix_export=40.0):
    """KPI énergétiques et économiques. Conventions Batterie1.py :
    TAP = énergie locale consommée / consommation totale
    TAC = énergie produite valorisée sur place / production totale."""
    E = lambda col: float(df_res[col].sum() * dt)   # MWh
    conso, prod   = E("conso_MW"), E("prod_totale_MW")
    imp, exp, ecr = E("import_MW"), E("export_MW"), E("ecretage_MW")
    autoconso = E("autoconso_directe_MW") + E("batt_decharge_MW")
    return {
        "conso_MWh": conso, "prod_MWh": prod, "import_MWh": imp,
        "export_MWh": exp, "ecretage_MWh": ecr, "autoconso_MWh": autoconso,
        "TAP_pct": autoconso / conso * 100 if conso > 0 else 0,
        "TAC_pct": (prod - exp - ecr) / prod * 100 if prod > 0 else 0,
        "facture_k€": (imp * prix_import - exp * prix_export) / 1000,
        "h_sans_import": int((df_res["import_MW"] < 1e-6).sum()),
    }

# ==========================================
# 4. INTERFACE — BARRE LATÉRALE
# ==========================================

st.title("Simulateur de Mix Énergétique Communal")

st.sidebar.header("1. Source des données")
mode = st.sidebar.radio("Mode :", ["Démo (profils synthétiques)", "Fichiers réels"])

if mode == "Fichiers réels":
    f_conso = st.sidebar.file_uploader("Consommation (kW)", type=["csv", "xlsx", "xls"])
    unite_prod = st.sidebar.radio(
        "Unité de vos fichiers de production :",
        ["Puissance (kW)", "Facteur de charge (0-1)"],
        help="Puissance (kW) : la courbe brute de l'installation (comme dans "
             "Batterie1.py) ; indiquez alors sa puissance crête ci-dessous "
             "pour que l'outil la normalise. Facteur de charge : courbe déjà "
             "divisée par la capacité installée.")
    f_prods, p_refs = {}, {}
    for f in FILIERES:
        f_prods[f] = st.sidebar.file_uploader(
            f"Production {f}", type=["csv", "xlsx", "xls"])
        if unite_prod == "Puissance (kW)":
            p_refs[f] = st.sidebar.number_input(
                f"Puissance installée {f} du fichier (kW)",
                min_value=0.1, value=100.0, step=1.0, key=f"pref_{f}",
                help="Puissance crête de l'installation dont provient la "
                     "courbe : sert à convertir les kW en facteur de charge.")
    if f_conso is None or any(v is None for v in f_prods.values()):
        st.info("Importez la consommation et un profil par filière dans la barre "
                "latérale (ou basculez en mode Démo pour tester immédiatement).")
        st.stop()
    series = {"conso_MW": charger_fichier(f_conso, "v") / 1000.0}   # kW → MW
    for f, up in f_prods.items():
        s = charger_fichier(up, "v")
        if unite_prod == "Puissance (kW)":
            s = s / p_refs[f]                        # kW → facteur de charge
        if s.max() > 1.001:
            st.sidebar.warning(
                f"{f} : des valeurs > 1 détectées (max = {s.max():.2f}) alors "
                f"qu'un facteur de charge est attendu. Vérifiez l'unité ou la "
                f"puissance de référence — les valeurs seront plafonnées à 1.")
        series[f"fc_{f}"] = s.clip(0, 1)
    # Alignement de toutes les séries sur l'index de la consommation
    index_ref = series["conso_MW"].index
    df_complet = pd.DataFrame(
        {nom: s.reindex(index_ref).interpolate(limit=4).fillna(0)
         for nom, s in series.items()}, index=index_ref)
    if df_complet.empty or len(df_complet) < 2:
        st.error("Les fichiers importés ne se recouvrent pas dans le temps ou "
                 "sont vides après lecture. Vérifiez les colonnes de dates.")
        st.stop()
    st.sidebar.success("Fichiers chargés.")
else:
    conso_GWh = st.sidebar.number_input("Consommation annuelle (GWh)",
                                        1.0, 200.0, 20.0, 1.0)
    df_complet = generer_donnees_demo(conso_annuelle_GWh=conso_GWh)

st.sidebar.header("2. Capacités installées (MW)")
capacites = {f: st.sidebar.slider(f, 0.0, 20.0, v, 0.1)
             for f, v in zip(FILIERES, [1.5, 1.0, 6.0])}
# Productible attendu par filière : rend le lien capacité → énergie explicite
_recap = "  |  ".join(
    f"{f} : {capacites[f] * df_complet[f'fc_{f}'].mean() * 8.760:.1f} GWh/an"
    for f in FILIERES)
st.sidebar.caption(f"Productible estimé — {_recap}")

st.sidebar.header("3. Batterie")
cap_batt = st.sidebar.slider("Capacité (MWh)", 0.0, 50.0, 4.0, 0.5)
regle_moitie = st.sidebar.checkbox("Puissance = capacité / 2 (règle TE13)", True)
if regle_moitie:
    p_batt = cap_batt / 2.0
    st.sidebar.info(f"Puissance onduleur : {p_batt:.1f} MW")
else:
    p_batt = st.sidebar.slider("Puissance (MW)", 0.0, 25.0, 1.0, 0.1)
rendement_ar = st.sidebar.slider("Rendement aller-retour (%)", 70, 100, 90, 1)
rend_unitaire = np.sqrt(rendement_ar / 100.0)
soc_init = st.sidebar.slider("Charge initiale (%)", 0, 100, 0, 5)

st.sidebar.header("4. Réseau")
prix_imp = st.sidebar.number_input("Prix d'achat (€/MWh)", 0.0, 500.0, 80.0, 5.0)
prix_exp = st.sidebar.number_input("Prix de vente (€/MWh)", 0.0, 500.0, 40.0, 5.0)
p_max_imp = st.sidebar.number_input("Import max (MW)", 0.0, 100.0, 10.0, 0.5)
p_max_exp = st.sidebar.number_input("Export max (MW)", 0.0, 100.0, 10.0, 0.5)

# ==========================================
# 5. PÉRIODE D'ANALYSE
# ==========================================
st.header("Période d'analyse")
d_min, d_max = df_complet.index.min().date(), df_complet.index.max().date()
c1, c2 = st.columns(2)
d_deb = c1.date_input("Début", value=d_min, min_value=d_min, max_value=d_max,
                      format="DD/MM/YYYY")
d_fin = c2.date_input("Fin (incluse)", value=d_max, min_value=d_min,
                      max_value=d_max, format="DD/MM/YYYY")
df = df_complet.loc[(df_complet.index.date >= d_deb)
                    & (df_complet.index.date <= d_fin)]
if df.empty:
    st.error("Aucune donnée sur cette période.")
    st.stop()

# --- Simulation principale (recalculée à chaque réglage) ---
df_res, dt = simuler_mix(df, capacites, cap_batt, p_batt,
                         rend_unitaire, rend_unitaire,
                         p_max_imp, p_max_exp, soc_init)
kpi = calculer_kpi(df_res, dt, prix_imp, prix_exp)

# ==========================================
# 6. ONGLETS
# ==========================================
tab1, tab2, tab3, tab4 = st.tabs([
    "Flux temporels", "Bilan mensuel & monotone",
    "Optimisation batterie", "Étude paramétrique du mix"])

# ----------------------------------------------------
# ONGLET 1 : dispatch temporel + SOC + KPI
# ----------------------------------------------------
with tab1:
    st.header("Flux d'énergie sur la période")

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("TAP (autoproduction)", f"{kpi['TAP_pct']:.1f} %")
    k2.metric("TAC (autoconsommation)", f"{kpi['TAC_pct']:.1f} %")
    k3.metric("Import réseau", f"{kpi['import_MWh']:.0f} MWh")
    k4.metric("Écrêtage", f"{kpi['ecretage_MWh']:.0f} MWh")
    k5.metric("Facture nette", f"{kpi['facture_k€']:.1f} k€")

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.72, 0.28], vertical_spacing=0.06,
                        specs=[[{}], [{}]])

    # Empilement : chaque filière puis décharge batterie puis import
    for f, coul in FILIERES.items():
        fig.add_trace(go.Scatter(
            x=df_res.index, y=df_res[f"prod_{f}_MW"], name=f,
            stackgroup="mix", mode="none", fillcolor=rgba(coul, 0.8),
            line=dict(width=0)), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df_res.index, y=df_res["batt_decharge_MW"], name="Batterie (décharge)",
        stackgroup="mix", mode="none", fillcolor=rgba(COULEUR_BATTERIE, 0.67)),
        row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df_res.index, y=df_res["import_MW"], name="Import réseau",
        stackgroup="mix", mode="none", fillcolor=rgba(COULEUR_IMPORT, 0.53)),
        row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df_res.index, y=df_res["conso_MW"], name="Consommation",
        line=dict(color="black", width=2)), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df_res.index, y=-df_res["export_MW"], name="Export (négatif)",
        line=dict(color=COULEUR_EXPORT, width=1)), row=1, col=1)

    fig.add_trace(go.Scatter(
        x=df_res.index, y=df_res["SoC_pct"] if cap_batt > 0 else df_res["SoC_MWh"],
        name="SoC batterie (%)", line=dict(color=COULEUR_BATTERIE, width=2),
        fill="tozeroy"), row=2, col=1)

    fig.update_layout(height=620, hovermode="x unified",
                      legend=dict(orientation="h", yanchor="top", y=-0.12,
                                  xanchor="center", x=0.5),
                      margin=dict(t=30, b=80, l=40, r=40))
    fig.update_yaxes(title_text="Puissance (MW)", row=1, col=1)
    fig.update_yaxes(title_text="SoC (%)", range=[0, 105], row=2, col=1)
    fig.update_xaxes(hoverformat="%d/%m/%Y %H:%M", row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)

    st.caption("L'empilement (filières + batterie + import) recouvre exactement "
               "la courbe noire de consommation ; l'export apparaît en négatif ; "
               "l'écart entre production potentielle et empilée est l'écrêtage.")

