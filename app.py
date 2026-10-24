from datetime import datetime
import os
import sqlite3
import threading
import time
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template
import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

app = Flask(__name__)

# Ambil kredensial dan cookie secara aman dari file .env (tidak di-hardcode)
USERNAME = os.getenv("IMS_USERNAME")
PASSWORD = os.getenv("IMS_PASSWORD")
ENV_COOKIE = os.getenv("IMS_COOKIE")
DB_NAME = "ims_data.db"

# CACHE MEMORI COOKIE SSO
CACHED_COOKIE = (
    ENV_COOKIE  # Jika ada cookie manual di .env, langsung jadikan cache awal
)
LAST_COOKIE_TIME = time.time() if ENV_COOKIE else 0
COOKIE_EXPIRY_SECONDS = 1800  # 30 Menit sesi Cookie


def init_db():
    """Inisialisasi tabel database SQLite jika belum ada."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            IncidentId TEXT PRIMARY KEY,
            ObjectId TEXT,
            SourceProblemName TEXT,
            Status TEXT,
            PriorityText TEXT,
            AssignedParty TEXT,
            CreateTime TEXT,
            KANWIL TEXT,
            Area TEXT,
            FLMVENDOR TEXT,
            AtmLocation TEXT,
            AgeHours INTEGER,
            AgeMinutes INTEGER,
            AgingStr TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_ims_cookie():
    """Mendapatkan cookie SSO IMS.

    Reuse cookie dari .env atau hasil login Selenium jika masih valid.
    """
    global CACHED_COOKIE, LAST_COOKIE_TIME
    now = time.time()

    if CACHED_COOKIE and (now - LAST_COOKIE_TIME < COOKIE_EXPIRY_SECONDS):
        return CACHED_COOKIE

    # Jika tidak ada username/password, kembalikan cookie yang ada
    if not USERNAME or not PASSWORD:
        return ENV_COOKIE

    print("🌐 [BACKGROUND TASK] Menjalankan Login otomatis Selenium ke IMS...")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 15)

    try:
        driver.get(
            "https://newatmnet.bankmandiri.co.id/sso/a/login.html?serviceUrl=https://newatmnet.bankmandiri.co.id/IMSClient/?appKey=IMS&applicationName=IMS#/"
        )
        username_input = wait.until(
            EC.presence_of_element_located((By.NAME, "username"))
        )
        username_input.send_keys(USERNAME)
        driver.find_element(By.NAME, "password").send_keys(PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "[data-testid='signbtn']").click()

        wait.until(EC.url_contains("IMSClient"))
        time.sleep(2)

        selenium_cookies = driver.get_cookies()
        CACHED_COOKIE = "; ".join(
            [f"{c['name']}={c['value']}" for c in selenium_cookies]
        )
        LAST_COOKIE_TIME = now
        print("✅ [BACKGROUND TASK] Login Selenium Sukses & Cookie Berhasil Disimpan!")
        return CACHED_COOKIE
    except Exception as e:
        print(
            f"❌ [BACKGROUND TASK] Gagal Login Selenium: {e}. Menggunakan cadangan"
            " cookie dari .env"
        )
        return ENV_COOKIE
    finally:
        driver.quit()


def process_ims_data(raw_incidents):
    """Pembersihan data Pandas dan deduplikasi unit mesin unik (ObjectId)."""
    if not raw_incidents:
        return pd.DataFrame()

    df = pd.DataFrame(raw_incidents)

    if "CustomAtmData" in df.columns and df["CustomAtmData"].notna().any():
        custom_data = pd.json_normalize(df["CustomAtmData"].dropna().tolist())
        df = df.drop(columns=["CustomAtmData"]).reset_index(drop=True)
        df = pd.concat([df, custom_data], axis=1)

    if "AtmStat" in df.columns and df["AtmStat"].notna().any():
        atm_stat_data = pd.json_normalize(df["AtmStat"].dropna().tolist())
        df = df.drop(columns=["AtmStat"]).reset_index(drop=True)
        df = pd.concat([df, atm_stat_data], axis=1)

    df = df.loc[:, ~df.columns.duplicated()]

    if "SourceProblemName" in df.columns:
        rename_map = {
            "ATM.SLMMachine.Hardware Problem Manual": "Hardware Problem Manual",
            "ATM.SLMMachine.Pending Part": "Pending Part",
            "ATM.Others.Matilistrik PLN": "Mati Listrik",
        }
        df["SourceProblemName"] = df["SourceProblemName"].replace(rename_map)
        is_no_trx = (
            df["SourceProblemName"]
            .astype(str)
            .str.contains(r"No Trx", case=False, na=False)
        )
        df.loc[is_no_trx, "SourceProblemName"] = "No Trx"

    if "CreateTime" in df.columns:
        df["CreateTime"] = pd.to_datetime(df["CreateTime"], errors="coerce")
    else:
        df["CreateTime"] = pd.NaT

    # Deduplikasi: Pastikan 1 mesin (ObjectId) hanya 1 baris
    if "ObjectId" in df.columns and "SourceProblemName" in df.columns:
        problem_clean = (
            df["SourceProblemName"]
            .astype(str)
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
            .str.lower()
        )
        pattern = "approved linked incident manual"
        df["Is_Ignored_Problem"] = problem_clean.str.contains(
            pattern, regex=True, na=False
        )

        df = df.sort_values(
            by=["ObjectId", "Is_Ignored_Problem", "CreateTime"],
            ascending=[True, True, False],
            na_position="last",
        )
        df = df.drop_duplicates(subset=["ObjectId"], keep="first")
        df = df.drop(columns=["Is_Ignored_Problem"])

    waktu_sekarang = pd.Timestamp.now()
    aging_hours_list, aging_minutes_list, aging_str_list = [], [], []
    status_list, priority_list = [], []

    for dt in df["CreateTime"]:
        if pd.isna(dt):
            aging_hours_list.append(0)
            aging_minutes_list.append(0)
            aging_str_list.append("00d 00h 00m")
            status_list.append("NORMAL")
            priority_list.append("MEDIUM")
        else:
            try:
                now_ts = (
                    pd.Timestamp.now(tz=dt.tz) if dt.tz is not None else waktu_sekarang
                )
                total_detik = max(0, int((now_ts - dt).total_seconds()))

                hari = total_detik // 86400
                sisa = total_detik % 86400
                jam = sisa // 3600
                menit = (sisa % 3600) // 60

                aging_str = (
                    f"{str(hari).zfill(2)}d {str(jam).zfill(2)}h {str(menit).zfill(2)}m"
                )
                aging_str_list.append(aging_str)

                total_jam = jam + (hari * 24)
                aging_hours_list.append(total_jam)
                aging_minutes_list.append(menit)

                if total_jam >= 4:
                    status_list.append("CRITICAL")
                    priority_list.append("CRITICAL")
                elif total_jam >= 1:
                    status_list.append("WARNING")
                    priority_list.append("HIGH")
                else:
                    status_list.append("NORMAL")
                    priority_list.append("MEDIUM")
            except Exception:
                aging_hours_list.append(0)
                aging_minutes_list.append(0)
                aging_str_list.append("00d 00h 00m")
                status_list.append("NORMAL")
                priority_list.append("MEDIUM")

    df["AgingStr"] = aging_str_list
    df["AgeHours"] = aging_hours_list
    df["AgeMinutes"] = aging_minutes_list
    df["Status"] = status_list
    df["PriorityText"] = priority_list

    cols_to_keep = [
        "IncidentId",
        "ObjectId",
        "SourceProblemName",
        "Status",
        "PriorityText",
        "AssignedParty",
        "CreateTime",
        "KANWIL",
        "Area",
        "FLMVENDOR",
        "AtmLocation",
        "AgeHours",
        "AgeMinutes",
        "AgingStr",
    ]
    existing_cols = [c for c in cols_to_keep if c in df.columns]
    return df[existing_cols]


def fetch_ims_job():
    """Background task sinkronisasi otomatis ke SQLite."""
    print("\n🔄 [BACKGROUND TASK] Memulai siklus sinkronisasi data dari IMS...")
    cookie_str = get_ims_cookie()

    if not cookie_str:
        print(
            "❌ [BACKGROUND TASK] Gagal mendapatkan cookie SSO / Cookie kosong di .env"
        )
        return

    current_timestamp = int(time.time() * 1000)
    url = f"https://newatmnet.bankmandiri.co.id/IMSClient/IMS/DxDataGridLoadIncidents?skip=0&take=5000&requireTotalCount=true&sort=%5B%7B%22selector%22%3A%22IncidentId%22%2C%22desc%22%3Atrue%7D%5D&totalSummary=%5B%5D&_={current_timestamp}"
    headers = {
        "Cookie": cookie_str,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200 and response.text.strip():
            data_json = response.json()
            incidents = data_json.get("data", [])

            if incidents:
                df_clean = process_ims_data(incidents)
                df_clean["CreateTime"] = df_clean["CreateTime"].astype(str)

                conn = sqlite3.connect(DB_NAME)
                df_clean.to_sql("incidents", conn, if_exists="replace", index=False)
                conn.close()
                print(
                    f"⚡ [BACKGROUND TASK] Sukses memperbarui {len(df_clean)} unit"
                    " mesin ke SQLite!"
                )
            else:
                print("⚠️ [BACKGROUND TASK] Data insiden kosong dari IMS.")
        else:
            print(f"⚠️ [BACKGROUND TASK] HTTP Response error: {response.status_code}")
    except Exception as e:
        print(f"❌ [BACKGROUND TASK] Error saat sinkronisasi: {e}")


def start_background_scheduler():
    def loop():
        while True:
            fetch_ims_job()
            time.sleep(300)  # Setiap 5 menit

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/incidents")
def get_incidents():
    """Endpoint kilat membaca SQLite lokal + kalkulasi Availability berbasis 848 mesin."""
    try:
        conn = sqlite3.connect(DB_NAME)
        df = pd.read_sql_query("SELECT * FROM incidents", conn)
        conn.close()

        result_json = df.to_dict(orient="records")
        total_problems = len(result_json)

        # Total populasi mesin Region 2
        TOTAL_REGIONAL_MACHINES = 848

        # Hitung jumlah mesin yang sehat/normal (Total Mesin - Total Problem)
        # Pastikan tidak minus jika suatu saat data problem melebihi 848
        healthy_machines = max(0, TOTAL_REGIONAL_MACHINES - total_problems)

        # Kalkulasi Availability yang benar: (Mesin Sehat / Total Mesin) * 100%
        if TOTAL_REGIONAL_MACHINES > 0:
            availability_rate = (healthy_machines / TOTAL_REGIONAL_MACHINES) * 100
        else:
            availability_rate = 100.00

        return jsonify(
            {
                "source": "LOCAL_SQLITE_CACHE",
                "count": total_problems,
                "total_machines": TOTAL_REGIONAL_MACHINES,
                "availability": round(availability_rate, 2),
                "data": result_json,
            }
        )
    except Exception as e:
        return (
            jsonify(
                {
                    "source": "LOCAL_SQLITE_CACHE",
                    "count": 0,
                    "total_machines": 848,
                    "availability": 0.00,
                    "data": [],
                    "message": f"Database belum terisi: {e}",
                }
            ),
            200,
        )


if __name__ == "__main__":
    init_db()
    start_background_scheduler()
    app.run(host="0.0.0.0", port=8050, debug=True)
