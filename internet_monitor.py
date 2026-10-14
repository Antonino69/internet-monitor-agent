#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

# ------------------------- CONFIGURAZIONE -------------------------

GATEWAY = "192.168.178.1"
LOGICAL_IFACE = "br0"
PHYSICAL_IFACE = "enp6s0"

PING_TARGETS = {
    "cloudflare": "1.1.1.1",
    "google": "8.8.8.8",
}

DNS_TEST_NAME = "www.google.com"
HTTP_URLS = (
    "https://connectivitycheck.gstatic.com/generate_204",
    "https://www.cloudflare.com/cdn-cgi/trace",
)

INTERVAL_SECONDS = 5
PING_TIMEOUT_SECONDS = 2
HTTP_TIMEOUT_SECONDS = 4

# Evita di classificare un singolo pacchetto perso come "interruzione".
FAIL_CONFIRM_SAMPLES = 2
RECOVERY_CONFIRM_SAMPLES = 2

# Soglie diagnostiche modificabili.
GATEWAY_HIGH_MS = 20.0
INTERNET_HIGH_MS = 150.0
DNS_HIGH_MS = 800.0
HTTP_HIGH_MS = 2500.0

RETENTION_DAYS = 30

DB_PATH = Path(os.environ.get("IM_DB", "/var/lib/internet-monitor/monitor.db"))

PING_RE = re.compile(r"time[=<]?\s*([0-9.]+)\s*ms", re.IGNORECASE)


# ------------------------- UTILITY -------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_epoch() -> int:
    return int(time.time())


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def read_int(path: Path) -> int | None:
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def run_cmd(cmd: list[str], timeout: int = 15) -> dict[str, Any]:
    exe = shutil.which(cmd[0])
    if not exe:
        return {"ok": False, "error": f"{cmd[0]} non installato"}

    cmd = [exe, *cmd[1:]]
    try:
        p = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "output": p.stdout[-20000:],
        }
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return {
            "ok": False,
            "error": f"timeout dopo {timeout}s",
            "output": out[-20000:],
        }
    except OSError as e:
        return {"ok": False, "error": str(e)}


# ------------------------- TEST DI RETE -------------------------

def ping(host: str) -> dict[str, Any]:
    result = run_cmd(
        ["ping", "-n", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS), host],
        timeout=PING_TIMEOUT_SECONDS + 2,
    )
    output = result.get("output", "")
    match = PING_RE.search(output)

    return {
        "ok": bool(result.get("ok")),
        "ms": float(match.group(1)) if match else None,
    }


def dns_test() -> dict[str, Any]:
    start = time.monotonic()
    try:
        socket.getaddrinfo(DNS_TEST_NAME, 443, type=socket.SOCK_STREAM)
        return {
            "ok": True,
            "ms": round((time.monotonic() - start) * 1000, 2),
        }
    except OSError as e:
        return {
            "ok": False,
            "ms": None,
            "error": str(e),
        }


def http_one(url: str) -> dict[str, Any]:
    start = time.monotonic()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "internet-monitor/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            resp.read(1)
            return {
                "ok": True,
                "status": int(resp.status),
                "ms": round((time.monotonic() - start) * 1000, 2),
                "url": url,
            }
    except urllib.error.HTTPError as e:
        # Un HTTP 4xx/5xx dimostra comunque che DNS/TCP/TLS/HTTP hanno funzionato.
        return {
            "ok": True,
            "status": int(e.code),
            "ms": round((time.monotonic() - start) * 1000, 2),
            "url": url,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "ms": None,
            "url": url,
            "error": str(e),
        }


def http_test() -> dict[str, Any]:
    attempts = [http_one(url) for url in HTTP_URLS]
    good = [x for x in attempts if x["ok"]]
    if good:
        best = min(good, key=lambda x: x["ms"] if x["ms"] is not None else 10**9)
        return {
            "ok": True,
            "ms": best["ms"],
            "status": best["status"],
            "url": best["url"],
            "attempts": attempts,
        }
    return {
        "ok": False,
        "ms": None,
        "status": None,
        "url": None,
        "attempts": attempts,
    }


def iface_snapshot(iface: str) -> dict[str, Any]:
    base = Path("/sys/class/net") / iface
    stats = base / "statistics"

    return {
        "iface": iface,
        "operstate": read_text(base / "operstate"),
        "carrier": read_int(base / "carrier"),
        "speed_mbps": read_int(base / "speed"),
        "rx_errors": read_int(stats / "rx_errors"),
        "tx_errors": read_int(stats / "tx_errors"),
        "rx_dropped": read_int(stats / "rx_dropped"),
        "tx_dropped": read_int(stats / "tx_dropped"),
        "rx_packets": read_int(stats / "rx_packets"),
        "tx_packets": read_int(stats / "tx_packets"),
    }


def collect_sample() -> dict[str, Any]:
    ts_epoch = now_epoch()
    ts = now_iso()

    physical = iface_snapshot(PHYSICAL_IFACE)
    logical = iface_snapshot(LOGICAL_IFACE)

    gateway = ping(GATEWAY)
    external = {name: ping(host) for name, host in PING_TARGETS.items()}
    dns = dns_test()
    http = http_test()

    sample = {
        "ts_epoch": ts_epoch,
        "ts": ts,
        "physical": physical,
        "logical": logical,
        "gateway": gateway,
        "external": external,
        "dns": dns,
        "http": http,
    }
    sample["state"], sample["note"] = classify(sample)
    return sample


def classify(s: dict[str, Any]) -> tuple[str, str]:
    phy = s["physical"]
    logical = s["logical"]

    if phy.get("carrier") == 0 or phy.get("operstate") == "down":
        return "NIC_DOWN", f"{PHYSICAL_IFACE}: carrier/operstate non attivo"

    if logical.get("operstate") == "down":
        return "BRIDGE_DOWN", f"{LOGICAL_IFACE}: operstate down"

    if not s["gateway"]["ok"]:
        return "LAN_DOWN", f"gateway {GATEWAY} non raggiungibile"

    ext_ok = [v for v in s["external"].values() if v["ok"]]
    if not ext_ok:
        return "INTERNET_DOWN", "gateway OK ma tutti i target Internet ICMP falliscono"

    if not s["dns"]["ok"]:
        return "DNS_DOWN", "Internet ICMP OK ma risoluzione DNS fallisce"

    if not s["http"]["ok"]:
        return "HTTP_DOWN", "ping e DNS OK ma i test HTTPS falliscono"

    high = []

    gw_ms = s["gateway"].get("ms")
    if gw_ms is not None and gw_ms > GATEWAY_HIGH_MS:
        high.append(f"gateway {gw_ms:.1f} ms")

    ext_ms = [v["ms"] for v in ext_ok if v.get("ms") is not None]
    if ext_ms and min(ext_ms) > INTERNET_HIGH_MS:
        high.append(f"Internet {min(ext_ms):.1f} ms")

    dns_ms = s["dns"].get("ms")
    if dns_ms is not None and dns_ms > DNS_HIGH_MS:
        high.append(f"DNS {dns_ms:.1f} ms")

    http_ms = s["http"].get("ms")
    if http_ms is not None and http_ms > HTTP_HIGH_MS:
        high.append(f"HTTPS {http_ms:.1f} ms")

    if high:
        return "DEGRADED", "latenza elevata: " + ", ".join(high)

    return "OK", ""


# ------------------------- DATABASE -------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch INTEGER NOT NULL,
    ts TEXT NOT NULL,
    state TEXT NOT NULL,
    note TEXT,

    phy_operstate TEXT,
    phy_carrier INTEGER,
    phy_speed_mbps INTEGER,
    rx_errors INTEGER,
    tx_errors INTEGER,
    rx_dropped INTEGER,
    tx_dropped INTEGER,

    bridge_operstate TEXT,

    gateway_ok INTEGER,
    gateway_ms REAL,

    cloudflare_ok INTEGER,
    cloudflare_ms REAL,
    google_ok INTEGER,
    google_ms REAL,

    dns_ok INTEGER,
    dns_ms REAL,
    http_ok INTEGER,
    http_ms REAL,
    http_status INTEGER
);

CREATE INDEX IF NOT EXISTS idx_samples_ts_epoch ON samples(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_samples_state ON samples(state);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_epoch INTEGER NOT NULL,
    start_ts TEXT NOT NULL,
    end_epoch INTEGER,
    end_ts TEXT,
    initial_state TEXT NOT NULL,
    last_state TEXT NOT NULL,
    start_note TEXT,
    end_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_incidents_start_epoch ON incidents(start_epoch);

CREATE TABLE IF NOT EXISTS state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch INTEGER NOT NULL,
    ts TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT NOT NULL,
    note TEXT,
    incident_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_state_events_ts_epoch ON state_events(ts_epoch);

CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch INTEGER NOT NULL,
    ts TEXT NOT NULL,
    incident_id INTEGER,
    reason TEXT,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_ts_epoch ON diagnostics(ts_epoch);
"""


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_sample(conn: sqlite3.Connection, s: dict[str, Any]) -> None:
    phy = s["physical"]
    logical = s["logical"]
    cf = s["external"]["cloudflare"]
    gg = s["external"]["google"]

    conn.execute(
        """
        INSERT INTO samples (
            ts_epoch, ts, state, note,
            phy_operstate, phy_carrier, phy_speed_mbps,
            rx_errors, tx_errors, rx_dropped, tx_dropped,
            bridge_operstate,
            gateway_ok, gateway_ms,
            cloudflare_ok, cloudflare_ms,
            google_ok, google_ms,
            dns_ok, dns_ms,
            http_ok, http_ms, http_status
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            s["ts_epoch"], s["ts"], s["state"], s["note"],
            phy.get("operstate"), phy.get("carrier"), phy.get("speed_mbps"),
            phy.get("rx_errors"), phy.get("tx_errors"),
            phy.get("rx_dropped"), phy.get("tx_dropped"),
            logical.get("operstate"),
            int(s["gateway"]["ok"]), s["gateway"].get("ms"),
            int(cf["ok"]), cf.get("ms"),
            int(gg["ok"]), gg.get("ms"),
            int(s["dns"]["ok"]), s["dns"].get("ms"),
            int(s["http"]["ok"]), s["http"].get("ms"),
            s["http"].get("status"),
        ),
    )
    conn.commit()


def find_active_incident(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, last_state
        FROM incidents
        WHERE end_epoch IS NULL
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def open_incident(conn: sqlite3.Connection, state: str, note: str) -> int:
    ts_e = now_epoch()
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO incidents
        (start_epoch, start_ts, initial_state, last_state, start_note)
        VALUES (?,?,?,?,?)
        """,
        (ts_e, ts, state, state, note),
    )
    incident_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO state_events
        (ts_epoch, ts, old_state, new_state, note, incident_id)
        VALUES (?,?,?,?,?,?)
        """,
        (ts_e, ts, "OK", state, note, incident_id),
    )
    conn.commit()
    return incident_id


def change_incident_state(
    conn: sqlite3.Connection,
    incident_id: int | None,
    old_state: str,
    new_state: str,
    note: str,
) -> None:
    ts_e = now_epoch()
    ts = now_iso()
    if incident_id is not None:
        conn.execute(
            "UPDATE incidents SET last_state=? WHERE id=?",
            (new_state, incident_id),
        )
    conn.execute(
        """
        INSERT INTO state_events
        (ts_epoch, ts, old_state, new_state, note, incident_id)
        VALUES (?,?,?,?,?,?)
        """,
        (ts_e, ts, old_state, new_state, note, incident_id),
    )
    conn.commit()


def close_incident(
    conn: sqlite3.Connection,
    incident_id: int,
    old_state: str,
    note: str,
) -> None:
    ts_e = now_epoch()
    ts = now_iso()
    conn.execute(
        """
        UPDATE incidents
        SET end_epoch=?, end_ts=?, end_note=?, last_state=?
        WHERE id=?
        """,
        (ts_e, ts, note, old_state, incident_id),
    )
    conn.execute(
        """
        INSERT INTO state_events
        (ts_epoch, ts, old_state, new_state, note, incident_id)
        VALUES (?,?,?,?,?,?)
        """,
        (ts_e, ts, old_state, "OK", note, incident_id),
    )
    conn.commit()


def cleanup_old_data(conn: sqlite3.Connection) -> None:
    cutoff = now_epoch() - RETENTION_DAYS * 86400
    conn.execute("DELETE FROM samples WHERE ts_epoch < ?", (cutoff,))
    conn.execute("DELETE FROM diagnostics WHERE ts_epoch < ?", (cutoff,))
    conn.execute("DELETE FROM state_events WHERE ts_epoch < ?", (cutoff,))
    conn.commit()


# ------------------------- DIAGNOSTICA INCIDENTI -------------------------

def diagnostic_payload() -> dict[str, Any]:
    commands = {
        "ip_br_link": ["ip", "-br", "link"],
        "ip_addr_br0": ["ip", "addr", "show", "dev", LOGICAL_IFACE],
        "ip_addr_phy": ["ip", "addr", "show", "dev", PHYSICAL_IFACE],
        "ip_route": ["ip", "route"],
        "ip_route_1_1_1_1": ["ip", "route", "get", "1.1.1.1"],
        "ip_neigh": ["ip", "neigh", "show"],
        "ethtool": ["ethtool", PHYSICAL_IFACE],
        "ethtool_stats": ["ethtool", "-S", PHYSICAL_IFACE],
        "resolvectl": ["resolvectl", "status"],
        "dig_local": ["dig", "+time=2", "+tries=1", DNS_TEST_NAME],
        "dig_cloudflare": ["dig", "+time=2", "+tries=1", "@1.1.1.1", DNS_TEST_NAME],
        "traceroute": ["traceroute", "-n", "-w", "1", "-q", "1", "1.1.1.1"],
        "mtr": ["mtr", "-rwzc", "5", "1.1.1.1"],
        "kernel_recent": [
            "journalctl", "-k", "--since", "-2 min",
            "--no-pager", "-n", "200",
        ],
    }

    payload: dict[str, Any] = {
        "captured_at": now_iso(),
        "physical": iface_snapshot(PHYSICAL_IFACE),
        "logical": iface_snapshot(LOGICAL_IFACE),
        "commands": {},
    }

    for name, cmd in commands.items():
        timeout = 20 if name in {"mtr", "traceroute"} else 10
        payload["commands"][name] = run_cmd(cmd, timeout=timeout)

    return payload


def save_diagnostics(incident_id: int | None, reason: str) -> None:
    try:
        payload = diagnostic_payload()
        conn = db_connect()
        conn.execute(
            """
            INSERT INTO diagnostics
            (ts_epoch, ts, incident_id, reason, payload_json)
            VALUES (?,?,?,?,?)
            """,
            (
                now_epoch(),
                now_iso(),
                incident_id,
                reason,
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"{now_iso()} DIAGNOSTICA ERRORE: {e}", flush=True)


def start_diagnostics(incident_id: int | None, reason: str) -> None:
    threading.Thread(
        target=save_diagnostics,
        args=(incident_id, reason),
        daemon=True,
        name="incident-diagnostics",
    ).start()


# ------------------------- LOOP PRINCIPALE -------------------------

def format_sample(s: dict[str, Any]) -> str:
    cf = s["external"]["cloudflare"]
    gg = s["external"]["google"]

    def ms(x: dict[str, Any]) -> str:
        if not x.get("ok"):
            return "FAIL"
        value = x.get("ms")
        return f"{value:.1f}ms" if value is not None else "OK"

    return (
        f"{s['ts']} {s['state']:<13} "
        f"gw={ms(s['gateway']):>8} "
        f"cf={ms(cf):>8} "
        f"gg={ms(gg):>8} "
        f"dns={ms(s['dns']):>8} "
        f"http={ms(s['http']):>8} "
        f"nic={s['physical'].get('operstate')}/{s['physical'].get('carrier')}"
        + (f" | {s['note']}" if s["note"] else "")
    )


def monitor() -> None:
    conn = db_connect()
    active_row = find_active_incident(conn)
    active_incident = int(active_row["id"]) if active_row else None

    stable_state = str(active_row["last_state"]) if active_row else "OK"
    candidate_state: str | None = None
    candidate_count = 0
    last_cleanup = 0.0

    print(
        f"{now_iso()} monitor avviato: "
        f"gateway={GATEWAY}, bridge={LOGICAL_IFACE}, nic={PHYSICAL_IFACE}, "
        f"db={DB_PATH}",
        flush=True,
    )

    while True:
        loop_start = time.monotonic()

        try:
            sample = collect_sample()
            insert_sample(conn, sample)
            print(format_sample(sample), flush=True)

            observed = sample["state"]

            if observed == stable_state:
                candidate_state = None
                candidate_count = 0
            else:
                if observed == candidate_state:
                    candidate_count += 1
                else:
                    candidate_state = observed
                    candidate_count = 1

                needed = (
                    RECOVERY_CONFIRM_SAMPLES
                    if observed == "OK"
                    else FAIL_CONFIRM_SAMPLES
                )

                if candidate_count >= needed:
                    old_state = stable_state
                    stable_state = observed
                    candidate_state = None
                    candidate_count = 0

                    if old_state == "OK" and stable_state != "OK":
                        active_incident = open_incident(
                            conn, stable_state, sample["note"]
                        )
                        print(
                            f"{now_iso()} INCIDENTE #{active_incident} APERTO: "
                            f"{stable_state} - {sample['note']}",
                            flush=True,
                        )
                        start_diagnostics(
                            active_incident,
                            f"apertura incidente: {stable_state}",
                        )

                    elif old_state != "OK" and stable_state == "OK":
                        if active_incident is not None:
                            close_incident(
                                conn,
                                active_incident,
                                old_state,
                                "connessione ripristinata",
                            )
                            print(
                                f"{now_iso()} INCIDENTE #{active_incident} CHIUSO",
                                flush=True,
                            )
                        active_incident = None

                    elif old_state != "OK" and stable_state != "OK":
                        change_incident_state(
                            conn,
                            active_incident,
                            old_state,
                            stable_state,
                            sample["note"],
                        )
                        print(
                            f"{now_iso()} CAMBIO STATO: "
                            f"{old_state} -> {stable_state}",
                            flush=True,
                        )

            if time.monotonic() - last_cleanup > 6 * 3600:
                cleanup_old_data(conn)
                last_cleanup = time.monotonic()

        except KeyboardInterrupt:
            print(f"{now_iso()} monitor terminato", flush=True)
            break
        except Exception as e:
            print(f"{now_iso()} ERRORE LOOP: {e}", flush=True)
            try:
                conn.close()
            except Exception:
                pass
            time.sleep(2)
            conn = db_connect()

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.2, INTERVAL_SECONDS - elapsed))

    conn.close()


# ------------------------- REPORT -------------------------

def summary(hours: int) -> None:
    conn = db_connect()
    cutoff = now_epoch() - hours * 3600

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM samples WHERE ts_epoch >= ?",
        (cutoff,),
    ).fetchone()["n"]

    if not total:
        print(f"Nessun campione nelle ultime {hours} ore.")
        conn.close()
        return

    bad = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM samples
        WHERE ts_epoch >= ?
          AND state IN (
              'NIC_DOWN','BRIDGE_DOWN','LAN_DOWN',
              'INTERNET_DOWN','DNS_DOWN','HTTP_DOWN'
          )
        """,
        (cutoff,),
    ).fetchone()["n"]

    degraded = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM samples
        WHERE ts_epoch >= ? AND state = 'DEGRADED'
        """,
        (cutoff,),
    ).fetchone()["n"]

    agg = conn.execute(
        """
        SELECT
            AVG(gateway_ms) AS avg_gw,
            MAX(gateway_ms) AS max_gw,
            AVG(
                CASE
                    WHEN cloudflare_ms IS NOT NULL AND google_ms IS NOT NULL
                    THEN MIN(cloudflare_ms, google_ms)
                    ELSE COALESCE(cloudflare_ms, google_ms)
                END
            ) AS avg_inet,
            MAX(
                CASE
                    WHEN cloudflare_ms IS NOT NULL AND google_ms IS NOT NULL
                    THEN MAX(cloudflare_ms, google_ms)
                    ELSE COALESCE(cloudflare_ms, google_ms)
                END
            ) AS max_inet,
            AVG(dns_ms) AS avg_dns,
            MAX(dns_ms) AS max_dns,
            AVG(http_ms) AS avg_http,
            MAX(http_ms) AS max_http
        FROM samples
        WHERE ts_epoch >= ?
        """,
        (cutoff,),
    ).fetchone()

    incidents = conn.execute(
        """
        SELECT *
        FROM incidents
        WHERE start_epoch >= ? OR (end_epoch IS NULL AND start_epoch < ?)
        ORDER BY start_epoch DESC
        """,
        (cutoff, cutoff),
    ).fetchall()

    availability = 100.0 * (total - bad) / total

    print(f"Periodo: ultime {hours} ore")
    print(f"Campioni: {total}")
    print(f"Campioni in errore: {bad}")
    print(f"Campioni degradati: {degraded}")
    print(f"Disponibilità campionata: {availability:.4f}%")
    print(
        "Latenza gateway avg/max: "
        f"{(agg['avg_gw'] or 0):.2f}/{(agg['max_gw'] or 0):.2f} ms"
    )
    print(
        "Latenza Internet avg/max: "
        f"{(agg['avg_inet'] or 0):.2f}/{(agg['max_inet'] or 0):.2f} ms"
    )
    print(
        "DNS avg/max: "
        f"{(agg['avg_dns'] or 0):.2f}/{(agg['max_dns'] or 0):.2f} ms"
    )
    print(
        "HTTPS avg/max: "
        f"{(agg['avg_http'] or 0):.2f}/{(agg['max_http'] or 0):.2f} ms"
    )
    print(f"Incidenti: {len(incidents)}")

    for row in incidents[:20]:
        end = row["end_ts"] or "IN CORSO"
        if row["end_epoch"] is None:
            duration = now_epoch() - row["start_epoch"]
        else:
            duration = row["end_epoch"] - row["start_epoch"]
        print(
            f"  #{row['id']} {row['start_ts']} -> {end} "
            f"({duration}s) {row['initial_state']} -> {row['last_state']}"
        )

    conn.close()


def show_incidents(limit: int) -> None:
    conn = db_connect()
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY start_epoch DESC LIMIT ?",
        (limit,),
    ).fetchall()

    if not rows:
        print("Nessun incidente registrato.")
        conn.close()
        return

    for row in rows:
        end = row["end_ts"] or "IN CORSO"
        duration = (
            (row["end_epoch"] or now_epoch()) - row["start_epoch"]
        )
        print(
            f"#{row['id']}  {row['start_ts']} -> {end}  "
            f"{duration}s  {row['initial_state']} -> {row['last_state']}"
        )
        if row["start_note"]:
            print(f"    {row['start_note']}")

    conn.close()


def show_diagnostic(incident_id: int) -> None:
    conn = db_connect()
    row = conn.execute(
        """
        SELECT ts, reason, payload_json
        FROM diagnostics
        WHERE incident_id=?
        ORDER BY ts_epoch DESC
        LIMIT 1
        """,
        (incident_id,),
    ).fetchone()

    if not row:
        print(f"Nessuna diagnostica per incidente #{incident_id}.")
        conn.close()
        return

    print(f"Diagnostica incidente #{incident_id}")
    print(f"Data: {row['ts']}")
    print(f"Motivo: {row['reason']}")
    print(json.dumps(json.loads(row["payload_json"]), indent=2, ensure_ascii=False))
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Monitor continuo della connessione Internet"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="avvia il monitor")

    p_summary = sub.add_parser("summary", help="riepilogo")
    p_summary.add_argument("--hours", type=int, default=24)

    p_inc = sub.add_parser("incidents", help="elenca gli incidenti")
    p_inc.add_argument("--limit", type=int, default=20)

    p_diag = sub.add_parser("diagnostic", help="mostra diagnostica incidente")
    p_diag.add_argument("incident_id", type=int)

    args = parser.parse_args()

    if args.command in (None, "run"):
        monitor()
    elif args.command == "summary":
        summary(args.hours)
    elif args.command == "incidents":
        show_incidents(args.limit)
    elif args.command == "diagnostic":
        show_diagnostic(args.incident_id)


if __name__ == "__main__":
    main()
