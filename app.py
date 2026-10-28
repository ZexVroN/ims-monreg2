from datetime import datetime
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request
import pandas as pd
import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)

# Kredensial & konfigurasi dari .env (tidak di-hardcode)
USERNAME = os.getenv("IMS_USERNAME")
PASSWORD = os.getenv("IMS_PASSWORD")
ENV_COOKIE = os.getenv("IMS_COOKIE")
IMS_LOGIN_URL = os.getenv("IMS_LOGIN_URL")
IMS_DATA_URL = os.getenv("IMS_DATA_URL")  # memakai placeholder {ts} untuk timestamp

# Database & kontak selalu berada di folder yang sama dengan app.py
DB_NAME = os.path.join(BASE_DIR, "ims_data.db")
CONTACTS_FILE = os.path.join(BASE_DIR, "contacts.json")  # jangan taruh di folder static

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

# Penanda sinkronisasi terakhir (dipakai scheduler)
LAST_SYNC = None  # "HH:MM:SS"
LAST_SYNC_EPOCH = None


# ---------------------------------------------------------------------------
# Kontak WhatsApp vendor (dibaca dari contacts.json, tidak pernah dikirim ke browser)
# ---------------------------------------------------------------------------


def norm_name(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def load_contacts():
    """Baca contacts.json -> {nama vendor ternormalisasi: nomor}. Nomor tidak valid dilewati."""
    try:
        with open(CONTACTS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}

    contacts = {}
    for name, val in raw.items():
        num = val.get("phone") if isinstance(val, dict) else val
        digits = re.sub(r"\D+", "", str(num or ""))
        if digits.startswith("0"):
            digits = "62" + digits[1:]
        if len(digits) >= 9:
            contacts[norm_name(name)] = digits
    return contacts


def find_contact(vendor):
    contacts = load_contacts()
    return contacts.get(norm_name(vendor)) or contacts.get("default")


# ---------------------------------------------------------------------------
# Auth opsional & database
# ---------------------------------------------------------------------------


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resolved_incidents (
            IncidentId TEXT,
            ObjectId TEXT,
            SourceProblemName TEXT,
            Area TEXT,
            KANWIL TEXT,
            FLMVENDOR TEXT,
            AtmLocation TEXT,
            CreateTime TEXT,
            ResolvedAt TEXT,
            DurationMinutes INTEGER,
            UNIQUE (IncidentId, ObjectId)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_actions (
            ObjectId TEXT PRIMARY KEY,
            IncidentId TEXT,
            Comment TEXT,
            Status TEXT,
            UpdatedAt TEXT,
            WaTo TEXT,
            WaAt TEXT
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Login IMS & sinkronisasi data
# ---------------------------------------------------------------------------


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

    # Tanpa username/password/alamat login, hanya bisa memakai cookie manual dari .env
    if not USERNAME or not PASSWORD or not IMS_LOGIN_URL:
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

        driver.get(IMS_LOGIN_URL)
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
                driver.save_screenshot(os.path.join(BASE_DIR, "login_gagal.png"))
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


def minutes_since(create_str):
    """Selisih menit sejak tiket dibuat sampai sekarang (None jika tidak valid)."""
    dt = pd.to_datetime(create_str, errors="coerce")
    if pd.isna(dt):
        return None
    now_ts = pd.Timestamp.now(tz=dt.tz) if dt.tz is not None else pd.Timestamp.now()
    return max(0, int((now_ts - dt).total_seconds() // 60))


def find_resolved(prev, df_clean):
    """Mesin yang ada di sync sebelumnya tetapi sudah tidak ada di sync sekarang."""
    if prev.empty or "ObjectId" not in prev.columns:
        return []

    # Pengaman: daftar mendadak kosong biasanya karena error/sesi habis,
    # bukan karena semua insiden selesai bersamaan.
    if len(df_clean) == 0 and len(prev) >= 10:
        print("⚠️ [BACKGROUND TASK] Daftar kosong mendadak, pencatatan solved dilewati.")
        return []

    new_ids = (
        set(df_clean["ObjectId"].astype(str))
        if "ObjectId" in df_clean.columns
        else set()
    )
    gone = prev[~prev["ObjectId"].astype(str).isin(new_ids)]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def val(v):
        return None if pd.isna(v) else str(v)

    rows = []
    for r in gone.to_dict(orient="records"):
        rows.append(
            (
                val(r.get("IncidentId")),
                val(r.get("ObjectId")),
                val(r.get("SourceProblemName")),
                val(r.get("Area")),
                val(r.get("KANWIL")),
                val(r.get("FLMVENDOR")),
                val(r.get("AtmLocation")),
                val(r.get("CreateTime")),
                now_str,
                minutes_since(r.get("CreateTime")),
            )
        )
    return rows


def save_incidents(df_clean):
    """Simpan data ke SQLite, catat snapshot riwayat, dan catat insiden yang selesai."""
    total = len(df_clean)
    critical = int((df_clean["Status"] == "CRITICAL").sum()) if total else 0
    warning = int((df_clean["Status"] == "WARNING").sum()) if total else 0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db()
    try:
        # Baca daftar sebelumnya untuk mendeteksi insiden yang sudah selesai
        try:
            prev = pd.read_sql_query("SELECT * FROM incidents", conn)
        except Exception:
            prev = pd.DataFrame()
        resolved_rows = find_resolved(prev, df_clean)

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
        if resolved_rows:
            conn.executemany(
                "INSERT OR IGNORE INTO resolved_incidents"
                " (IncidentId, ObjectId, SourceProblemName, Area, KANWIL, FLMVENDOR,"
                "  AtmLocation, CreateTime, ResolvedAt, DurationMinutes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                resolved_rows,
            )
            # Mesin yang sudah selesai: hapus komentar/status penanganannya
            conn.executemany(
                "DELETE FROM incident_actions WHERE ObjectId = ?",
                [(row[1],) for row in resolved_rows],
            )
        # Riwayat solved disimpan 30 hari
        conn.execute(
            "DELETE FROM resolved_incidents"
            " WHERE ResolvedAt < datetime('now', 'localtime', '-30 days')"
        )
        conn.commit()
        if resolved_rows:
            print(
                f"✅ [BACKGROUND TASK] {len(resolved_rows)} insiden terdeteksi selesai."
            )
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
        if not IMS_DATA_URL:
            print("❌ [BACKGROUND TASK] IMS_DATA_URL belum diisi di .env")
            return

        cookie_str = get_ims_cookie()
        if not cookie_str:
            print(
                "❌ [BACKGROUND TASK] Gagal mendapatkan cookie SSO / Cookie kosong di .env"
            )
            return

        current_timestamp = int(time.time() * 1000)
        url = IMS_DATA_URL.replace("{ts}", str(current_timestamp))
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


# ---------------------------------------------------------------------------
# Halaman & API
# ---------------------------------------------------------------------------


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
        try:
            df = pd.read_sql_query(
                "SELECT i.*, a.Comment AS ActionComment, a.Status AS ActionStatus,"
                " a.UpdatedAt AS ActionUpdatedAt, a.WaTo AS ActionWaTo,"
                " a.WaAt AS ActionWaAt"
                " FROM incidents i LEFT JOIN incident_actions a"
                " ON a.ObjectId = i.ObjectId",
                conn,
            )
        finally:
            conn.close()

        # Penanda apakah vendor punya kontak WhatsApp (nomornya TIDAK dikirim ke browser)
        try:
            contacts = load_contacts()
            if "FLMVENDOR" in df.columns:
                df["WaReady"] = df["FLMVENDOR"].map(
                    lambda v: bool(
                        contacts.get(norm_name(v)) or contacts.get("default")
                    )
                )
            else:
                df["WaReady"] = False
        except Exception as e:
            print(f"⚠️ Gagal membaca kontak WhatsApp: {e}")
            df["WaReady"] = False

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
        print(f"❌ /api/incidents error: {e}")
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


@app.route("/api/resolved")
def get_resolved():
    """Daftar insiden yang sudah selesai (hilang dari daftar aktif IMS)."""
    try:
        days = int(request.args.get("days", 1))
    except ValueError:
        days = 1
    days = max(1, min(days, 30))

    try:
        conn = get_db()
        df = pd.read_sql_query(
            "SELECT * FROM resolved_incidents"
            " WHERE ResolvedAt >= datetime('now', 'localtime', ?)"
            " ORDER BY ResolvedAt DESC",
            conn,
            params=(f"-{days} days",),
        )
        conn.close()
        df = df.astype(object).where(pd.notnull(df), None)
        return jsonify(
            {"days": days, "count": len(df), "data": df.to_dict(orient="records")}
        )
    except Exception as e:
        return jsonify({"days": days, "count": 0, "data": [], "message": str(e)})


@app.route("/api/actions", methods=["POST"])
def save_action():
    """Simpan komentar penanganan; status otomatis menjadi HANDLED (hijau)."""
    data = request.get_json(silent=True) or {}
    object_id = str(data.get("object_id") or "").strip()
    incident_id = str(data.get("incident_id") or "").strip() or None
    comment = str(data.get("comment") or "").strip()[:1000]
    send_wa = bool(data.get("send_wa"))

    if not object_id:
        return jsonify({"ok": False, "message": "Terminal ID kosong"}), 400
    if not comment:
        return jsonify({"ok": False, "message": "Komentar belum diisi"}), 400

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT FLMVENDOR FROM incidents WHERE ObjectId = ?", (object_id,)
        ).fetchone()
        if not row:
            return jsonify({"ok": False, "message": "Insiden sudah tidak aktif"}), 404

        vendor = row[0]
        if send_wa and not find_contact(vendor):
            return (
                jsonify(
                    {"ok": False, "message": "Kontak WhatsApp vendor belum terdaftar"}
                ),
                400,
            )

        # Yang disimpan hanya nama vendor, bukan nomornya
        wa_to = (vendor or "-") if send_wa else None
        conn.execute(
            """
            INSERT INTO incident_actions
                (ObjectId, IncidentId, Comment, Status, UpdatedAt, WaTo, WaAt)
            VALUES (?, ?, ?, 'HANDLED', ?, ?, ?)
            ON CONFLICT(ObjectId) DO UPDATE SET
                IncidentId = excluded.IncidentId,
                Comment = excluded.Comment,
                Status = 'HANDLED',
                UpdatedAt = excluded.UpdatedAt,
                WaTo = COALESCE(excluded.WaTo, incident_actions.WaTo),
                WaAt = COALESCE(excluded.WaAt, incident_actions.WaAt)
            """,
            (
                object_id,
                incident_id,
                comment,
                now_str,
                wa_to,
                now_str if send_wa else None,
            ),
        )
        conn.commit()
        return jsonify({"ok": True, "updated_at": now_str})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/actions", methods=["DELETE"])
def delete_action():
    """Hapus komentar sehingga status kembali BELUM DITANGANI."""
    data = request.get_json(silent=True) or {}
    object_id = str(data.get("object_id") or "").strip()
    if not object_id:
        return jsonify({"ok": False, "message": "Terminal ID kosong"}), 400

    conn = get_db()
    try:
        conn.execute("DELETE FROM incident_actions WHERE ObjectId = ?", (object_id,))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/wa")
def open_whatsapp():
    """Susun pesan di server lalu alihkan ke WhatsApp. Nomor tidak ada di kode browser."""
    object_id = (request.args.get("oid") or "").strip()

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT i.ObjectId, i.IncidentId, i.Area, i.SourceProblemName,"
            " i.FLMVENDOR, i.AgingStr, a.Comment"
            " FROM incidents i LEFT JOIN incident_actions a ON a.ObjectId = i.ObjectId"
            " WHERE i.ObjectId = ?",
            (object_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row or not row[6]:
        return "Insiden tidak ditemukan atau komentar belum disimpan.", 404

    phone = find_contact(row[4])
    if not phone:
        return "Kontak WhatsApp vendor belum terdaftar.", 404

    text = "\n".join(
        [
            "*[IMS Incident]*",
            f"Terminal: {row[0] or '-'}",
            f"Incident ID: {row[1] or '-'}",
            f"Area: {row[2] or '-'}",
            f"Masalah: {row[3] or '-'}",
            f"Aging: {row[5] or '-'}",
            "",
            f"Komentar: {row[6]}",
        ]
    )
    encoded = quote(text, safe="")
    ua = request.headers.get("User-Agent", "")
    if re.search(r"Android|iPhone|iPad|iPod|Mobile", ua, re.I):
        # HP: buka aplikasi WhatsApp lewat wa.me
        return redirect(f"https://wa.me/{phone}?text={encoded}")
    # Komputer: langsung ke WhatsApp Web (melewati halaman pilihan wa.me)
    return redirect(f"https://web.whatsapp.com/send?phone={phone}&text={encoded}")


if __name__ == "__main__":
    init_db()
    for env_name, env_value in (
        ("IMS_LOGIN_URL", IMS_LOGIN_URL),
        ("IMS_DATA_URL", IMS_DATA_URL),
    ):
        if not env_value:
            print(f"⚠️ {env_name} belum diisi di .env")
    start_background_scheduler()
    # Untuk produksi sebaiknya jalankan via waitress/gunicorn, bukan server bawaan Flask.
    app.run(host=os.getenv("HOST", "0.0.0.0"), port=8050, debug=False)
