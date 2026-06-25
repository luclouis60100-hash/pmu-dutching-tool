"""
Scraper pour récupérer records km depuis Paris-Turf
Version explorateur de champs
"""

import requests
import json
import re
import unicodedata
from datetime import datetime
from bs4 import BeautifulSoup

PARISTURF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.paris-turf.com/",
}

TURFOMANIA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": "https://www.turfomania.fr/",
}

def slugify(s):
    """Convertir une chaîne en slug"""
    s = s.lower().strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s

def get_paristurf_data(date_str, num_r, num_c):
    """Récupère les pronos et records Paris-Turf"""
    try:
        sess = requests.Session()
        sess.headers.update(PARISTURF_HEADERS)
        
        date_fmt_pt = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        if date_fmt_pt == today_str:
            pt_home = "https://www.paris-turf.com/"
        else:
            pt_home = f"https://www.paris-turf.com/programme-courses/{date_fmt_pt}"
        
        print(f"[Paris-Turf] Chargement: {pt_home}")
        
        r0 = sess.get(pt_home, timeout=15)
        soup0 = BeautifulSoup(r0.text, "html.parser")
        
        script0 = soup0.find("script", id="__NEXT_DATA__")
        if not script0:
            return {"tips": [], "records": {}}
        
        data0 = json.loads(script0.string)
        state0 = data0.get("props", {}).get("pageProps", {}).get("initialState", {})
        rcs = state0.get("raceCardsState", {})
        
        all_meetings = rcs.get("meetings", {})
        all_races = rcs.get("races", {})
        
        meetings = []
        races = []
        found_date = None
        
        for d_key in all_meetings:
            m_list = all_meetings[d_key]
            hit = next((m for m in m_list if m.get("pmuNumber") == num_r), None)
            if hit:
                meetings = m_list
                races = all_races.get(d_key, [])
                found_date = d_key
                break
        
        if not found_date:
            print(f"[Paris-Turf] Meeting R{num_r} non trouvé")
            return {"tips": [], "records": {}}
        
        target_meeting = next((m for m in meetings if m.get("pmuNumber") == num_r), None)
        if not target_meeting:
            return {"tips": [], "records": {}}
        
        meet_id = target_meeting["id"]
        meet_name = target_meeting.get("name", "")
        
        target_race = next((r for r in races if r.get("meetingId") == meet_id and r.get("number") == num_c), None)
        if not target_race:
            return {"tips": [], "records": {}}
        
        race_uuid = target_race.get("uuid", "")
        race_name = target_race.get("name", "")
        race_id = str(target_race.get("id", ""))
        
        pt_url = f"https://www.paris-turf.com/course/{slugify(meet_name)}-{slugify(race_name)}-idc-{race_uuid}"
        print(f"[Paris-Turf] R{num_r}C{num_c}: {pt_url[-60:]}")
        
        r1 = sess.get(pt_url, timeout=15)
        soup1 = BeautifulSoup(r1.text, "html.parser")
        script1 = soup1.find("script", id="__NEXT_DATA__")
        
        if not script1:
            return {"tips": [], "records": {}}
        
        data1 = json.loads(script1.string)
        state1 = data1.get("props", {}).get("pageProps", {}).get("initialState", {})
        cur = state1.get("currentPageState", {})
        
        # Extraire les tips
        web_tips = cur.get("webTips") or {}
        tips_raw = web_tips.get("tips", {})
        tips = []
        
        for cat in ["A", "S", "C", "O", "G"]:
            t = tips_raw.get(cat)
            if not t:
                continue
            saddles = [int(x.strip()) for x in t.get("saddleList", "").split(",") if x.strip()]
            names = [x.strip() for x in t.get("nameList", "").split(",")]
            label = t.get("typeLabelParisTurf", cat)
            
            for i, num in enumerate(saddles):
                tips.append({
                    "rang": len(tips) + 1,
                    "num": num,
                    "nom": names[i] if i < len(names) else f"N°{num}",
                    "categorie": label,
                    "cat": cat
                })
                if len(tips) >= 5:
                    break
            if len(tips) >= 5:
                break
        
        # Extraire les records
        recs = {}
        runners_data = state1.get("raceCardsState", {}).get("runners", {})
        
        print(f"[DEBUG] race_id = {race_id}")
        
        if race_id in runners_data:
            print(f"[DEBUG] Found {len(runners_data[race_id])} runners")
            
            for idx, runner in enumerate(runners_data[race_id]):
                print(f"\n[DEBUG] Runner #{idx}:")
                print(f"[DEBUG]   Keys: {list(runner.keys())[:15]}")
                
                # Essayer plusieurs champs pour le numéro
                hnum = None
                for field in ["horseNumber", "number", "saddle", "saddleNumber", 
                             "saddle_number", "runnerNumber", "runnernumber", 
                             "position", "saddleCloth"]:
                    val = runner.get(field)
                    if val is not None:
                        print(f"[DEBUG]   {field} = {val}")
                        if isinstance(val, int) and val > 0 and val < 100:
                            hnum = val
                            print(f"[DEBUG]   ✓ Using {field} as hnum = {hnum}")
                            break
                
                if not hnum:
                    print(f"[DEBUG]   ❌ No hnum found!")
                    continue
                
                # Chercher les records
                for rtype in ["harness", "distance", "flat"]:
                    rec = (runner.get("records") or {}).get(rtype, {})
                    redkm = rec.get("redkm") if rec else None
                    if redkm:
                        recs[hnum] = redkm
                        print(f"[DEBUG]   Saved: {hnum} = {redkm} ({rtype})")
                        break
        
        result = {
            "tips": tips,
            "records": recs,
            "author": web_tips.get("author", "Paris-Turf"),
            "text": (web_tips.get("text", "") or "")[:200]
        }
        
        print(f"[✓] Paris-Turf R{num_r}C{num_c}: {len(tips)} tips, {len(recs)} records")
        return result
    
    except Exception as e:
        print(f"[Erreur Paris-Turf] {str(e)}")
        import traceback
        traceback.print_exc()
        return {"tips": [], "records": {}}

def get_paristurf_pronos(date_str, num_r, num_c):
    """Wrapper"""
    return get_paristurf_data(date_str, num_r, num_c)

def get_records_km(date_str, num_r, num_c, horse_names):
    """Récupère les records km"""
    try:
        print(f"[DEBUG] get_records_km: R{num_r}C{num_c}")
        data = get_paristurf_data(date_str, num_r, num_c)
        records = data.get("records", {})
        result = {str(num): record for num, record in records.items()}
        print(f"[✓] Records: {len(result)}")
        return result
    except Exception as e:
        print(f"[Erreur Records] {str(e)}")
        import traceback
        traceback.print_exc()
        return {}

def get_turfomania_pronos(date_str, num_r, num_c):
    """Pronos Turfomania"""
    try:
        sess = requests.Session()
        sess.headers.update(TURFOMANIA_HEADERS)
        
        d = datetime.strptime(date_str, "%Y%m%d")
        jour = d.strftime("%A").lower()[:3]
        mois = d.strftime("%B").lower()[:3]
        
        urls = [
            f"https://www.turfomania.fr/pronostics-pmu-{date_str[0:4]}{date_str[4:6]}{date_str[6:8]}-r{num_r}-c{num_c}.html",
            f"https://www.turfomania.fr/pronostics/{jour}-{d.day:02d}-{mois}-{d.year}/r{num_r}-c{num_c}",
        ]
        
        for url in urls:
            try:
                print(f"[Turfomania] Essai: {url[-60:]}")
                r = sess.get(url, timeout=10)
                soup = BeautifulSoup(r.text, "html.parser")
                pronos = []
                
                pattern = r'N°\s*(\d+)\s*(?:-\s*)?([A-Z][A-Za-z\s-]*)'
                for match in re.finditer(pattern, r.text):
                    num = int(match.group(1))
                    nom = match.group(2).strip()
                    pronos.append({"num": num, "nom": nom})
                
                if pronos:
                    print(f"[✓] Turfomania R{num_r}C{num_c}: {len(pronos)}")
                    return {"pronos": pronos[:5], "source": "Turfomania"}
            
            except Exception as e:
                print(f"[Turfomania] Erreur: {e}")
                continue
        
        return {"pronos": [], "source": "Turfomania"}
    
    except Exception as e:
        print(f"[Erreur Turfomania] {str(e)}")
        return {"pronos": [], "source": "Turfomania"}

# ============================================
# RÉSULTATS ET COTES
# ============================================

def get_race_results(date_str, num_r, num_c):
    """Récupère les résultats définitifs et cotes depuis l'API PMU"""
    try:
        # Format date : YYYYMMDD → convertir en date_str pour l'API
        # Exemple : 25062026 (DDMMYYYY dans date_str) → à transformer
        
        # Construire l'URL API PMU
        api_url = f"https://online.turfinfo.api.pmu.fr/rest/client/61/programme/{date_str}/R{num_r}/C{num_c}/rapports-definitifs?specialisation=INTERNET&combinaisonEnTableau=true"
        
        print(f"[Race Results] Fetching: {api_url[-80:]}")
        
        sess = requests.Session()
        sess.headers.update(PARISTURF_HEADERS)
        
        r = sess.get(api_url, timeout=15)
        
        if r.status_code != 200:
            print(f"[Race Results] HTTP {r.status_code}")
            return {"arrivee": [], "cotes_gagnant": {}, "cotes_place": {}, "status": "error"}
        
        data = r.json()
        if not isinstance(data, list):
            return {"arrivee": [], "cotes_gagnant": {}, "cotes_place": {}, "status": "error"}
        
        result = {
            "arrivee": [],
            "cotes_gagnant": {},
            "cotes_place": {},
            "status": "success"
        }
        
        # Extraire les cotes
        for pari_group in data:
            type_pari = pari_group.get("typePari", "")
            rapports = pari_group.get("rapports", [])
            
            # Simple Gagnant
            if type_pari == "E_SIMPLE_GAGNANT":
                for rapport in rapports:
                    combinaison = rapport.get("combinaison", [])
                    dividende = rapport.get("dividendePourUnEuro", 0)
                    if combinaison:
                        num = combinaison[0]
                        result["cotes_gagnant"][num] = round(dividende / 100, 2)
            
            # Simple Placé
            elif type_pari == "E_SIMPLE_PLACE":
                for rapport in rapports:
                    combinaison = rapport.get("combinaison", [])
                    dividende = rapport.get("dividendePourUnEuro", 0)
                    if combinaison:
                        num = combinaison[0]
                        if num not in result["cotes_place"]:
                            result["cotes_place"][num] = round(dividende / 100, 2)
        
        print(f"[✓] Race Results R{num_r}C{num_c}: {len(result['cotes_gagnant'])} gagnant, {len(result['cotes_place'])} placé")
        return result
    
    except Exception as e:
        print(f"[Erreur Race Results] {str(e)}")
        import traceback
        traceback.print_exc()
        return {"arrivee": [], "cotes_gagnant": {}, "cotes_place": {}, "status": "error"}