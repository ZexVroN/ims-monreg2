from datetime import datetime
import hmac
import os
import sqlite3
import threading
import time

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request
import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

load_dotenv()

app = Flask(__name__)

# Kredensial & konfigurasi dari .env (tidak di-hardcode)
USERNAME = os.getenv("IMS_USERNAME")
PASSWORD = os.getenv("IMS_PASSWORD")
ENV_COOKIE = os.getenv("IMS_COOKIE")
DB_NAME = "ims_data.db"

# Opsional: login dasar untuk dashboard (aktif jika keduanya diisi di .env)
DASH_USER = os.getenv("DASH_USER")
DASH_PASS = os.getenv("DASH_PASS")

# Total populasi mesin Region 2 (bisa diubah lewat .env: TOTAL_MACHINES=848)
TOTAL_REGIONAL_MACHINES = int(os.getenv("TOTAL_MACHINES", "848"))

SYNC_INTERVAL_SECONDS = 300  # sinkronisasi tiap 5 menit
HISTORY_KEEP_HOURS = 72  # riwayat snapshot disimpan 3 hari

COLUMNS = [
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

# CACHE MEMORI COOKIE SSO
CACHED_COOKIE = ENV_COOKIE
LAST_COOKIE_TIME = time.time() if ENV_COOKIE else 0
COOKIE_EXPIRY_SECONDS = 1800  # 30 menit

# Info sinkronisasi terakhir (dikirim ke frontend)
LAST_SYNC = None  # "HH:MM:SS"
LAST_SYNC_EPOCH = None


@app.before_request
def require_auth():
    """Basic auth opsional. Aktif hanya jika DASH_USER & DASH_PASS diisi."""
    if not DASH_USER or not DASH_PASS:
        return None
    auth = request.authorization
    ok = (
        auth is not None
        and hmac.compare_digest(auth.username or "", DASH_USER)
        and hmac.compare_digest(auth.password or "", DASH_PASS)
    )
    if not ok:
        return Response(
            "Login diperlukan",
            401,
            {"WWW-Authenticate": 'Basic realm="IMS Dashboard"'},
        )
    return None


def get_db():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Inisialisasi tabel SQLite jika belum ada."""
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            IncidentId TEXT,
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            ts TEXT PRIMARY KEY,
            total INTEGER,
            critical INTEGER,
            warning INTEGER
        )
    """)
    conn.commit()
    conn.close()


def invalidate_cookie():
    """Buang cookie dari cache agar siklus berikutnya login ulang."""
    global CACHED_COOKIE, LAST_COOKIE_TIME
    CACHED_COOKIE = None
    LAST_COOKIE_TIME = 0


def get_ims_cookie():
    """Mendapatkan cookie SSO IMS (reuse cache atau login Selenium)."""
    global CACHED_COOKIE, LAST_COOKIE_TIME

    if CACHED_COOKIE and (time.time() - LAST_COOKIE_TIME < COOKIE_EXPIRY_SECONDS):
        return CACHED_COOKIE

    # Tanpa username/password, hanya bisa memakai cookie manual dari .env
    if not USERNAME or not PASSWORD:
        return ENV_COOKIE

    print("🌐 [BACKGROUND TASK] Menjalankan Login otomatis Selenium ke IMS...")
    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1366,768")

        driver = webdriver.Chrome(options=options)
        wait = WebDriverWait(driver, 30)

        driver.get(
            "https://newatmnet.bankmandiri.co.id/sso/a/login.html?serviceUrl=https://newatmnet.bankmandiri.co.id/IMSClient/?appKey=IMS&applicationName=IMS#/"
        )
        username_input = wait.until(
            EC.presence_of_element_located((By.NAME, "username"))
        )
        username_input.send_keys(USERNAME)
        driver.find_element(By.NAME, "password").send_keys(PASSWORD)
        driver.find_element(By.CSS_SELECTOR, "[data-testid='signbtn']").click()

        # URL login sendiri mengandung "IMSClient" (di serviceUrl), jadi tunggu
        # sampai benar-benar keluar dari halaman SSO.
        wait.until(
            lambda d: "IMSClient" in d.current_url and "/sso/" not in d.current_url
        )
        print(f"ℹ️ [BACKGROUND TASK] URL setelah login: {driver.current_url}")

        # Tunggu sampai cookie benar-benar terpasang
        wait.until(lambda d: len(d.get_cookies()) > 0)
        time.sleep(3)

        selenium_cookies = driver.get_cookies()
        if not selenium_cookies:
            raise RuntimeError("Login selesai tetapi cookie kosong")

        CACHED_COOKIE = "; ".join(
            [f"{c['name']}={c['value']}" for c in selenium_cookies]
        )
        LAST_COOKIE_TIME = time.time()
        print(
            f"✅ [BACKGROUND TASK] Login Selenium Sukses ({len(selenium_cookies)} cookie)!"
        )
        return CACHED_COOKIE
    except Exception as e:
        print(
            f"❌ [BACKGROUND TASK] Gagal Login Selenium: {e}. Menggunakan cadangan"
            " cookie dari .env"
        )
        if driver is not None:
            try:
                print(f"ℹ️ [BACKGROUND TASK] URL terakhir: {driver.current_url}")
                driver.save_screenshot("login_gagal.png")
            except Exception:
                pass
        return ENV_COOKIE
    finally:
        if driver is not None:
            driver.quit()


def process_ims_data(raw_incidents):
    """Pembersihan data Pandas dan deduplikasi unit mesin unik (ObjectId)."""
    if not raw_incidents:
        return pd.DataFrame(columns=COLUMNS)

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

    # Deduplikasi: 1 mesin (ObjectId) hanya 1 baris
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

                aging_str_list.append(
                    f"{str(hari).zfill(2)}d {str(jam).zfill(2)}h {str(menit).zfill(2)}m"
                )

                total_jam = jam + (hari * 24)
                aging_hours_list.append(total_jam)
                aging_minutes_list.append(menit)

                if total_detik > 3600:  # > 1 jam -> merah
                    status_list.append("CRITICAL")
                    priority_list.append("CRITICAL")
                elif total_detik > 1800:  # > 30 menit s/d 1 jam -> orange
                    status_list.append("WARNING")
                    priority_list.append("HIGH")
                else:  # <= 30 menit -> hijau
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

    existing_cols = [c for c in COLUMNS if c in df.columns]
    return df[existing_cols]


def save_incidents(df_clean):
    """Simpan data ke SQLite tanpa jendela 'tabel hilang' + catat snapshot riwayat."""
    total = len(df_clean)
    critical = int((df_clean["Status"] == "CRITICAL").sum()) if total else 0
    warning = int((df_clean["Status"] == "WARNING").sum()) if total else 0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    try:
        # 1) tulis ke tabel sementara
        df_clean.to_sql("incidents_new", conn, if_exists="replace", index=False)
        # 2) tukar tabel dalam satu transaksi
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS incidents")
        conn.execute("ALTER TABLE incidents_new RENAME TO incidents")
        conn.execute(
            "INSERT OR REPLACE INTO history (ts, total, critical, warning)"
            " VALUES (?, ?, ?, ?)",
            (ts, total, critical, warning),
        )
        conn.execute(
            "DELETE FROM history WHERE ts < datetime('now', 'localtime', ?)",
            (f"-{HISTORY_KEEP_HOURS} hours",),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_ims_job():
    """Background task sinkronisasi otomatis ke SQLite."""
    global LAST_SYNC, LAST_SYNC_EPOCH
    print("\n🔄 [BACKGROUND TASK] Memulai siklus sinkronisasi data dari IMS...")

    try:
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

        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code != 200 or not response.text.strip():
            print(f"⚠️ [BACKGROUND TASK] HTTP Response error: {response.status_code}")
            invalidate_cookie()
            return

        try:
            data_json = response.json()
        except ValueError:
            # Biasanya halaman login (sesi SSO kedaluwarsa), bukan JSON
            print("⚠️ [BACKGROUND TASK] Respons bukan JSON, sesi dianggap kedaluwarsa.")
            invalidate_cookie()
            return

        if not isinstance(data_json, dict) or "data" not in data_json:
            print("⚠️ [BACKGROUND TASK] Format respons tidak dikenali.")
            invalidate_cookie()
            return

        incidents = data_json.get("data") or []
        df_clean = process_ims_data(incidents)
        df_clean["CreateTime"] = df_clean["CreateTime"].astype(str)
        save_incidents(df_clean)

        LAST_SYNC = datetime.now().strftime("%H:%M:%S")
        LAST_SYNC_EPOCH = time.time()
        print(
            f"⚡ [BACKGROUND TASK] Sukses memperbarui {len(df_clean)} unit"
            " mesin ke SQLite!"
        )
    except Exception as e:
        print(f"❌ [BACKGROUND TASK] Error saat sinkronisasi: {e}")
        invalidate_cookie()


def start_background_scheduler():
    def loop():
        while True:
            before = LAST_SYNC_EPOCH
            try:
                fetch_ims_job()
            except Exception as e:  # jaga agar thread tidak pernah mati
                print(f"❌ [BACKGROUND TASK] Error tak terduga: {e}")
            berhasil = LAST_SYNC_EPOCH != before
            time.sleep(SYNC_INTERVAL_SECONDS if berhasil else 60)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


@app.route("/")
def index():
    return render_template("index.html")


def read_last_sync():
    """Waktu sync sukses terakhir dari tabel history (tahan restart)."""
    try:
        conn = get_db()
        row = conn.execute("SELECT MAX(ts) FROM history").fetchone()
        conn.close()
        if row and row[0]:
            dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%H:%M:%S"), int((datetime.now() - dt).total_seconds())
    except Exception:
        pass
    return None, None


@app.route("/api/incidents")
def get_incidents():
    """Baca SQLite lokal + hitung Availability berbasis total mesin Region 2."""
    synced_at, sync_age = read_last_sync()
    try:
        conn = get_db()
        df = pd.read_sql_query("SELECT * FROM incidents", conn)
        conn.close()

        # NaN tidak valid di JSON -> ganti None
        df = df.astype(object).where(pd.notnull(df), None)
        result_json = df.to_dict(orient="records")
        total_problems = len(result_json)

        healthy_machines = max(0, TOTAL_REGIONAL_MACHINES - total_problems)
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
                "synced_at": synced_at,
                "sync_age_seconds": sync_age,
                "data": result_json,
            }
        )
    except Exception as e:
        return (
            jsonify(
                {
                    "source": "LOCAL_SQLITE_CACHE",
                    "count": 0,
                    "total_machines": TOTAL_REGIONAL_MACHINES,
                    "availability": 100.00,
                    "synced_at": synced_at,
                    "sync_age_seconds": sync_age,
                    "data": [],
                    "message": f"Database belum terisi: {e}",
                }
            ),
            200,
        )


@app.route("/api/history")
def get_history():
    """Puncak jumlah insiden per kategori, per 15 menit, 24 jam terakhir."""
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT strftime('%Y-%m-%d %H:', ts)
                   || printf('%02d', (CAST(strftime('%M', ts) AS INTEGER) / 15) * 15)
                   AS bucket,
                   MAX(total - critical - warning) AS normal,
                   MAX(warning) AS warning,
                   MAX(critical) AS critical
            FROM history
            WHERE ts >= datetime('now', 'localtime', '-24 hours')
            GROUP BY bucket
            ORDER BY bucket
        """).fetchall()
        conn.close()
        return jsonify(
            {
                "labels": [r[0][11:] for r in rows],
                "normal": [r[1] for r in rows],
                "warning": [r[2] for r in rows],
                "critical": [r[3] for r in rows],
            }
        )
    except Exception as e:
        return jsonify(
            {
                "labels": [],
                "normal": [],
                "warning": [],
                "critical": [],
                "message": str(e),
            }
        )


if __name__ == "__main__":
    init_db()
    start_background_scheduler()
    # Untuk produksi sebaiknya jalankan via waitress/gunicorn, bukan server bawaan Flask.
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=8050, debug=False)
