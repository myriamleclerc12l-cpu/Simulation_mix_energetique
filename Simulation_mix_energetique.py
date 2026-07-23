# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ==========================================
# 1. PARAMÈTRES ET CONFIGURATION
# ==========================================
st.set_page_config(page_title="Mix Énergétique Communal", layout="wide")

FILIERES_DISPOS = {
    "Éolien": {"couleur": "#1f77b4", "defaut_MW": 1.5},
    "Hydro":  {"couleur": "#17becf", "defaut_MW": 1.0},
    "PV":     {"couleur": "#ff7f0e", "defaut_MW": 6.0},
}


COULEUR_BATTERIE = "#2ca02c"
COULEUR_IMPORT   = "#d62728"
COULEUR_EXPORT   = "#9467bd"

def rgba(hex_couleur, alpha=1.0):
    h = hex_couleur.lstrip("#")
    r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"

# ==========================================
# 2. DONNÉES : DÉMO OU FICHIERS RÉELS
# ==========================================
@st.cache_data
def generer_donnees_demo(annee=2025, conso_annuelle_GWh=20.0, graine=42):
    """Génère des profils synthétiques sur une année."""
    rng = np.random.default_rng(graine)
    t = pd.date_range(f"{annee}-01-01", periods=8760, freq="h")
    jour, heure = t.dayofyear.values, t.hour.values

    # Consommation
    saison = 1.0 + 0.35 * np.cos(2 * np.pi * (jour - 15) / 365)
    prof_j = (0.75 + 0.20 * np.exp(-0.5 * ((heure - 8) / 2.0) ** 2)
                   + 0.30 * np.exp(-0.5 * ((heure - 19) / 2.5) ** 2))
    weekend = np.where(t.dayofweek.values >= 5, 0.92, 1.0)
    conso = saison * prof_j * weekend * (1 + 0.04 * rng.standard_normal(len(t)))
    conso = np.clip(conso, 0.2, None)
    conso = (conso / conso.sum()) * conso_annuelle_GWh * 1000

    # Éolien
    vent = np.zeros(len(t)); vent[0] = 7.0
    for i in range(1, len(t)):
        vent[i] = 0.985 * vent[i-1] + 0.015 * 7.5 + 0.55 * rng.standard_normal()
    vent = np.clip(vent * (1 + 0.25 * np.cos(2*np.pi*(jour-20)/365)), 0, None)
    fc_eol = np.where(vent < 3, 0, np.where(vent < 12, ((vent-3)/9)**3, np.where(vent < 25, 1.0, 0.0)))

    # Hydro
    base = 0.55 + 0.35 * np.cos(2 * np.pi * (jour - 60) / 365)
    crues = np.zeros(len(t))
    for i in range(1, len(t)):
        crues[i] = 0.992 * crues[i-1] + (1.2 if rng.random() < 0.0006 else 0.0)
    fc_hyd = np.where(base + crues < 0.15, 0.0, np.clip(base + crues, 0, 1))

    # PV
    decl = np.radians(23.45 * np.sin(2 * np.pi * (284 + jour) / 365))
    lat = np.radians(44.0)
    sin_elev = (np.sin(lat)*np.sin(decl) + np.cos(lat)*np.cos(decl)*np.cos(np.radians(15*(heure+0.5-12))))
    ciel = np.clip(sin_elev, 0, None) ** 1.15
    n_j = int(np.ceil(len(t)/24)); neb = np.zeros(n_j); neb[0] = 0.85
    for i in range(1, n_j):
        neb[i] = np.clip(0.5*neb[i-1] + 0.5*rng.uniform(0.45, 1.0), 0.2, 1.0)
    fc_pv = np.clip(0.95 * ciel * np.repeat(neb, 24)[:len(t)], 0, 1)

    return pd.DataFrame({"conso_MW": conso, "fc_Éolien": fc_eol, "fc_Hydro": fc_hyd, "fc_PV": fc_pv}, index=t)

@st.cache_data
def charger_fichier(fichier, nom_colonne):
    """Lit un CSV/Excel robuste aux formats français."""
    if fichier.name.lower().endswith(".csv"):
        df = pd.read_csv(fichier, sep=None, engine="python")
        if df.shape[1] >= 2 and df.iloc[:, 1].dtype == object:
            fichier.seek(0)
            df = pd.read_csv(fichier, sep=None, engine="python", decimal=",")
    else:
        df = pd.read_excel(fichier)
    
    df = df.iloc[:, :2].copy()
    df.columns = ["date", nom_colonne]
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.tz_localize(None)
    df = df.dropna(subset=["date"]).set_index("date")
    df[nom_colonne] = pd.to_numeric(df[nom_colonne].astype(str).str.replace(",", ".", regex=False), errors="coerce").fillna(0)
    
    if df.index.has_duplicates:
        df = df.groupby(level=0).mean()
    return df.sort_index()[nom_colonne]

# ==========================================
# 3. MOTEUR DE SIMULATION
# ==========================================
def simuler_mix(df, capacites_MW, cap_batt_MWh, p_batt_MW, rend_charge=0.95, rend_decharge=0.95, p_max_import_MW=1e9, p_max_export_MW=1e9):
    dt = ((df.index[1] - df.index[0]).total_seconds() / 3600.0 if len(df) > 1 else 1.0)
    conso = df["conso_MW"].values
    
    # Ne prendre que les filières actives
    prods = {f: cap * df[f"fc_{f}"].values for f, cap in capacites_MW.items() if cap > 0 and f"fc_{f}" in df.columns}
    prod_tot = np.sum(list(prods.values()), axis=0) if prods else np.zeros(len(df))

    soc = 0.0
    n = len(df)
    soc_h, batt_ch, batt_dech = np.empty(n), np.empty(n), np.empty(n)
    imp, exp, ecret, direct = np.empty(n), np.empty(n), np.empty(n), np.empty(n)

    for i in range(n):
        c, p = conso[i], prod_tot[i]
        flux_direct = min(c, p)
        surplus, deficit = p - flux_direct, c - flux_direct
        ch = dech = 0.0

        if surplus > 0 and cap_batt_MWh > 0:
            p_place = (cap_batt_MWh - soc) / (rend_charge * dt)
            ch = min(surplus, p_batt_MW, max(p_place, 0.0))
            soc += ch * rend_charge * dt
            surplus -= ch
        elif deficit > 0 and cap_batt_MWh > 0:
            p_dispo = soc * rend_decharge / dt
            dech = min(deficit, p_batt_MW, max(p_dispo, 0.0))
            soc -= dech / rend_decharge * dt
            deficit -= dech

        e = min(surplus, p_max_export_MW)
        imp[i], exp[i], ecret[i], direct[i] = min(deficit, p_max_import_MW), e, surplus - e, flux_direct
        soc_h[i], batt_ch[i], batt_dech[i] = soc, ch, dech

    df_res = df.copy()
    for f in prods:
        df_res[f"prod_{f}_MW"] = prods[f]
    df_res["prod_totale_MW"] = prod_tot
    df_res["autoconso_directe_MW"] = direct
    df_res["batt_charge_MW"] = batt_ch
    df_res["batt_decharge_MW"] = batt_dech
    df_res["SoC_MWh"] = soc_h
    df_res["SoC_pct"] = (soc_h / cap_batt_MWh * 100) if cap_batt_MWh > 0 else 0.0
    df_res["import_MW"], df_res["export_MW"], df_res["ecretage_MW"] = imp, exp, ecret
    return df_res, dt

def calculer_kpi(df_res, dt, prix_import=80.0, prix_export=40.0):
    E = lambda col: float(df_res[col].sum() * dt) if col in df_res.columns else 0.0
    conso, prod = E("conso_MW"), E("prod_totale_MW")
    imp, exp, ecr = E("import_MW"), E("export_MW"), E("ecretage_MW")
    autoconso = E("autoconso_directe_MW") + E("batt_decharge_MW")
    
    return {
        "conso_MWh": conso, "prod_MWh": prod, "import_MWh": imp, "export_MWh": exp, 
        "ecretage_MWh": ecr, "autoconso_MWh": autoconso,
        "TAP_pct": (autoconso / conso * 100) if conso > 0 else 0,
        "TAC_pct": ((prod - exp - ecr) / prod * 100) if prod > 0 else 0,
        "facture_k€": (imp * prix_import - exp * prix_export) / 1000
    }

# ==========================================
# 4. INTERFACE UTILISATEUR
# ==========================================
def main():
    st.title(" Simulateur de Mix Énergétique Communal")

    # --- BARRE LATÉRALE : PARAMÈTRES TECHNIQUES ---
    st.sidebar.header("Paramètres de l'étude")
    
    with st.sidebar.expander(" Capacités installées (MW)", expanded=True):
        capacites = {}
        for f, infos in FILIERES_DISPOS.items():
            if st.checkbox(f"Activer {f}", value=True):
                capacites[f] = st.slider(f"Puissance {f}", 0.0, 20.0, infos["defaut_MW"], 0.1)
            else:
                capacites[f] = 0.0

    with st.sidebar.expander(" Stockage (Batterie)", expanded=True):
        cap_batt = st.slider("Capacité (MWh)", 0.0, 50.0, 4.0, 0.5)
        p_batt = cap_batt / 2.0 if st.checkbox("Puissance = capacité / 2", True) else st.slider("Puissance (MW)", 0.0, 25.0, 1.0, 0.1)
        rend_unitaire = np.sqrt(st.slider("Rendement aller-retour (%)", 70, 100, 90, 1) / 100.0)

    with st.sidebar.expander(" Réseau & Tarification", expanded=False):
        prix_imp = st.number_input("Prix d'achat (€/MWh)", value=80.0)
        prix_exp = st.number_input("Prix de vente (€/MWh)", value=40.0)
        p_max_imp = st.number_input("Import max (MW)", value=10.0)
        p_max_exp = st.number_input("Export max (MW)", value=10.0)

    # --- ZONE PRINCIPALE : IMPORT DES DONNÉES ---
    mode = st.radio("Source des données :", ["Données de démonstration", "Fichiers réels"], horizontal=True)
    
    if mode == "Données de démonstration":
        conso_GWh = st.number_input("Consommation annuelle cible (GWh)", 1.0, 200.0, 20.0)
        df_complet = generer_donnees_demo(conso_annuelle_GWh=conso_GWh)
    else:
        st.info("Importez un fichier CSV/Excel contenant 2 colonnes : Date | Valeur.")
        c1, c2 = st.columns(2)
        f_conso = c1.file_uploader("Fichier Consommation (kW)")
        
        f_prods = {}
        active_sources = [f for f, cap in capacites.items() if cap > 0]
        
        for f in active_sources:
            f_prods[f] = c2.file_uploader(f"Fichier Facteur de charge {f} (0 à 1)")
            
        if not f_conso or any(f_prods[f] is None for f in active_sources):
            st.warning("Veuillez importer tous les fichiers requis selon les filières activées.")
            st.stop()
            
        # Traitement des fichiers
        series = {"conso_MW": charger_fichier(f_conso, "v") / 1000.0}
        for f in active_sources:
            series[f"fc_{f}"] = charger_fichier(f_prods[f], "v").clip(0, 1)
            
        df_complet = pd.DataFrame(series).interpolate(limit=4).fillna(0)

    # --- SIMULATION ET AFFICHAGE ---
    df_res, dt = simuler_mix(df_complet, capacites, cap_batt, p_batt, rend_unitaire, rend_unitaire, p_max_imp, p_max_exp)
    kpi = calculer_kpi(df_res, dt, prix_imp, prix_exp)

    tab1, tab2, tab3 = st.tabs(["Flux temporels", "Bilan mensuel", "Analyse de sensibilité"])

    # ONGLET 1 : DÉTAIL HORAIRE
    
    # ONGLET 1 : DÉTAIL HORAIRE
    # ONGLET 1 : DÉTAIL HORAIRE
    # ONGLET 1 : DÉTAIL HORAIRE
    with tab1:
        st.info(" **Comment lire :** L'objectif est que les couleurs (production) touchent exactement la ligne noire (consommation). Le rouge comble les déficits (import). Le violet sous le 0 montre les surplus (export).")
        
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("TAP (Autoproduction)", f"{kpi['TAP_pct']:.1f} %")
        c2.metric("TAC (Autoconsommation)", f"{kpi['TAC_pct']:.1f} %")
        c3.metric("Import", f"{kpi['import_MWh']:.0f} MWh")
        c4.metric("Écrêtage", f"{kpi['ecretage_MWh']:.0f} MWh")
        c5.metric("Facture nette", f"{kpi['facture_k€']:.1f} k€")

        st.write("---")
        
        # --- COMMANDES DE NAVIGATION (Date précise ou Période) ---
        st.write(" **Choisissez la période à afficher sur le graphique :**")
        col_deb, col_fin = st.columns(2)
        
        d_min, d_max = df_res.index.min().date(), df_res.index.max().date()
        
        date_debut = col_deb.date_input("Date de début :", value=d_min, min_value=d_min, max_value=d_max)
        # Par défaut, on affiche une semaine pour que ce soit lisible, mais tu peux changer !
        date_fin = col_fin.date_input("Date de fin :", value=d_min + pd.Timedelta(days=7), min_value=date_debut, max_value=d_max)

        # On filtre les données pour le graphique
        # On ajoute 1 jour à la date de fin pour inclure la dernière journée jusqu'à 23h59
        fin_incluse = pd.to_datetime(date_fin) + pd.Timedelta(days=1)
        masque = (df_res.index >= pd.to_datetime(date_debut)) & (df_res.index < fin_incluse)
        df_zoom = df_res[masque]
        # ------------------------------

        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25], vertical_spacing=0.05)
        
        # Empilement des productions
        for f in [f for f in capacites if capacites[f] > 0]:
            fig.add_trace(go.Scatter(x=df_zoom.index, y=df_zoom[f"prod_{f}_MW"], name=f, stackgroup="mix", fillcolor=rgba(FILIERES_DISPOS[f]["couleur"], 0.8), mode="none"), row=1, col=1)
            
        fig.add_trace(go.Scatter(x=df_zoom.index, y=df_zoom["batt_decharge_MW"], name="Décharge Bat.", stackgroup="mix", fillcolor=rgba(COULEUR_BATTERIE, 0.7), mode="none"), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_zoom.index, y=df_zoom["import_MW"], name="Import", stackgroup="mix", fillcolor=rgba(COULEUR_IMPORT, 0.5), mode="none"), row=1, col=1)
        
        # Lignes superposées (Conso et Export)
        fig.add_trace(go.Scatter(x=df_zoom.index, y=df_zoom["conso_MW"], name="Consommation", line=dict(color="black", width=2)), row=1, col=1)
        fig.add_trace(go.Scatter(x=df_zoom.index, y=-df_zoom["export_MW"], name="Export", fill="tozeroy", line=dict(color=COULEUR_EXPORT, width=1)), row=1, col=1)
        
        # Courbe de Batterie (SoC)
        fig.add_trace(go.Scatter(x=df_zoom.index, y=df_zoom["SoC_pct"], name="SoC (%)", line=dict(color=COULEUR_BATTERIE, width=2), fill="tozeroy"), row=2, col=1)

        fig.update_layout(height=600, hovermode="x unified", margin=dict(t=20, b=20), legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        fig.update_yaxes(title_text="Puissance (MW)", row=1, col=1)
        fig.update_yaxes(title_text="SoC Batterie (%)", range=[0, 105], row=2, col=1)
        
        st.plotly_chart(fig, use_container_width=True)
       

    # ONGLET 2 : BILAN MENSUEL
    with tab2:
        st.subheader("Bilan Énergétique Mensuel")
        # Rééchantillonnage mensuel (MWh)
        df_mois = df_res[["conso_MW", "prod_totale_MW", "import_MW", "export_MW"]].resample('ME').sum() * dt
        
        fig_bar = go.Figure()
        fig_bar.add_trace(go.Bar(x=df_mois.index, y=df_mois["conso_MW"], name="Consommation", marker_color="black"))
        fig_bar.add_trace(go.Bar(x=df_mois.index, y=df_mois["prod_totale_MW"], name="Production Totale", marker_color="#ff7f0e"))
        fig_bar.add_trace(go.Bar(x=df_mois.index, y=df_mois["import_MW"], name="Import", marker_color=COULEUR_IMPORT))
        fig_bar.add_trace(go.Bar(x=df_mois.index, y=-df_mois["export_MW"], name="Export", marker_color=COULEUR_EXPORT))
        
        fig_bar.update_layout(barmode='group', height=500, yaxis_title="Énergie (MWh)")
        st.plotly_chart(fig_bar, use_container_width=True)

    # ONGLET 3 : OPTIMISATION / SENSIBILITÉ
    with tab3:
        st.subheader("Sensibilité à la capacité de la batterie")
        st.write("Ce graphique simule l'impact de l'ajout de stockage sur vos KPI, à capacités de production constantes.")
        
        if st.button("Lancer l'analyse de sensibilité"):
            tailles_batt = np.arange(0, 21, 2) # De 0 à 20 MWh
            resultats_tap = []
            
            barre_progression = st.progress(0)
            for i, cap in enumerate(tailles_batt):
                df_sim, dt_sim = simuler_mix(df_complet, capacites, cap, cap/2, rend_unitaire, rend_unitaire, p_max_imp, p_max_exp)
                k = calculer_kpi(df_sim, dt_sim, prix_imp, prix_exp)
                resultats_tap.append(k["TAP_pct"])
                barre_progression.progress((i + 1) / len(tailles_batt))
                
            fig_sens = go.Figure()
            fig_sens.add_trace(go.Scatter(x=tailles_batt, y=resultats_tap, mode='lines+markers', line=dict(color=COULEUR_BATTERIE)))
            fig_sens.update_layout(xaxis_title="Capacité Batterie (MWh)", yaxis_title="Taux d'Autoproduction (TAP %)", height=400)
            st.plotly_chart(fig_sens, use_container_width=True)

if __name__ == "__main__":
    main()

