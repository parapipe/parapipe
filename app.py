import base64
import hmac
import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).parent
DB = os.environ.get("QUEUE_DB", str(ROOT / "data/queue.sqlite3"))

TRANSITIONS = {
    "waiting": {"serving", "cancelled"},
    "serving": {"done", "cancelled"},
    "done": set(),
    "cancelled": set(),
}


def connect():
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def initialize():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)

    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                duration INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'waiting',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS bookings_start
            ON bookings(starts_at)
        """)


def slots_for_day(day):
    selected = date.fromisoformat(day)
    anchor_value = os.environ.get("NAIL_EARLY_DATE")

    if not anchor_value:
        raise ValueError(
            "กรุณาตั้ง NAIL_EARLY_DATE เป็นวันที่ร้านเริ่มคิว 07:00"
        )

    anchor = date.fromisoformat(anchor_value)
    first_hour = 7 if (selected - anchor).days % 2 == 0 else 8

    return [
        f"{selected.isoformat()}T{hour:02d}:00"
        for hour in range(first_hour, 20, 2)
    ]


def save_booking(data, booking_id=None):
    if not isinstance(data, dict):
        raise ValueError("ข้อมูลต้องเป็น JSON object")

    fields = {}

    for key, limit in [
        ("customer", 100),
        ("phone", 30),
        ("service", 100),
        ("notes", 1000),
    ]:
        value = data.get(key, "")

        if (
            not isinstance(value, str)
            or len(value.strip()) > limit
            or (key != "notes" and not value.strip())
        ):
            raise ValueError(
                "กรุณาตรวจสอบชื่อ เบอร์โทร บริการ และหมายเหตุ"
            )

        fields[key] = value.strip()

    if not re.fullmatch(r"[+0-9 ()-]{6,30}", fields["phone"]):
        raise ValueError("เบอร์โทรไม่ถูกต้อง")

    try:
        start = datetime.strptime(
            data.get("starts_at", ""),
            "%Y-%m-%dT%H:%M",
        )

        duration = data.get("duration", 120)

        if type(duration) is not int or duration != 120:
            raise ValueError()

    except (ValueError, TypeError):
        raise ValueError(
            "วันเวลาไม่ถูกต้อง คิวทำเล็บใช้เวลา 120 นาที"
        )

    starts_at = start.strftime("%Y-%m-%dT%H:%M")

    if starts_at not in slots_for_day(start.date().isoformat()):
        raise ValueError(
            "เวลานี้ไม่อยู่ในรอบคิวของร้าน กรุณาเลือกเวลาจากตาราง"
        )

    fields.update(starts_at=starts_at, duration=duration)

    end = (
        start + timedelta(minutes=duration)
    ).strftime("%Y-%m-%dT%H:%M")

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        if booking_id is not None:
            current = db.execute(
                "SELECT * FROM bookings WHERE id=?",
                (booking_id,),
            ).fetchone()

            if current is None:
                raise LookupError("ไม่พบรายการ")

            if current["status"] != "waiting":
                raise ValueError(
                    "แก้ไขได้เฉพาะคิวที่รอรับบริการ"
                )

        overlap = db.execute(
            """
            SELECT id
            FROM bookings
            WHERE status IN ('waiting', 'serving')
              AND id != ?
              AND starts_at < ?
              AND datetime(
                    starts_at,
                    '+' || duration || ' minutes'
                  ) > datetime(?)
            """,
            (booking_id or 0, end, starts_at),
        ).fetchone()

        if overlap:
            raise ValueError(
                "ช่วงเวลานี้มีคิวแล้ว กรุณาเลือกเวลาอื่น"
            )

        if booking_id is None:
            columns = ",".join(fields)
            placeholders = ",".join("?" for _ in fields)

            booking_id = db.execute(
                f"""
                INSERT INTO bookings ({columns})
                VALUES ({placeholders})
                """,
                tuple(fields.values()),
            ).lastrowid

        else:
            assignments = ",".join(
                key + "=?" for key in fields
            )

            db.execute(
                f"""
                UPDATE bookings
                SET {assignments}
                WHERE id=?
                """,
                (*fields.values(), booking_id),
            )

        row = db.execute(
            "SELECT * FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()

        return dict(row)


def set_status(booking_id, status):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            "SELECT * FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()

        if row is None:
            raise LookupError("ไม่พบรายการ")

        if (
            not isinstance(status, str)
            or status not in TRANSITIONS[row["status"]]
        ):
            raise ValueError(
                "ไม่สามารถเปลี่ยนสถานะนี้ได้"
            )

        if status == "serving":
            active = db.execute(
                """
                SELECT id FROM bookings
                WHERE status='serving'
                """
            ).fetchone()

            if active:
                raise ValueError(
                    "มีคิวกำลังรับบริการอยู่ กรุณาปิดคิวเดิมก่อน"
                )

        db.execute(
            "UPDATE bookings SET status=? WHERE id=?",
            (status, booking_id),
        )

        updated = db.execute(
            "SELECT * FROM bookings WHERE id=?",
            (booking_id,),
        ).fetchone()

        return dict(updated)


class Handler(BaseHTTPRequestHandler):
    def reply(
        self,
        status,
        data,
        content_type="application/json; charset=utf-8",
    ):
        if content_type.startswith("application/json"):
            body = json.dumps(
                data,
                ensure_ascii=False,
            ).encode("utf-8")
        else:
            body = data

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "style-src 'self'; "
            "script-src 'self'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        credentials = (
            "admin:" + os.environ["ADMIN_PASSWORD"]
        ).encode("utf-8")

        expected = (
            "Basic "
            + base64.b64encode(credentials).decode("ascii")
        )

        if hmac.compare_digest(
            self.headers.get("Authorization", ""),
            expected,
        ):
            return True

        self.send_response(401)
        self.send_header(
            "WWW-Authenticate",
            'Basic realm="Queue Admin", charset="UTF-8"',
        )
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        url = urlsplit(self.path)

        if url.path == "/healthz":
            self.reply(200, {"status": "ok"})
            return

        if not self.authorized():
            return

        if url.path == "/api/config":
            self.reply(
                200,
                {
                    "demo_mode":
                    os.environ.get("QUEUE_DEMO_MODE") == "1"
                },
            )

        elif url.path == "/api/slots":
            try:
                day = parse_qs(url.query).get(
                    "date", [""]
                )[0]

                self.reply(200, slots_for_day(day))

            except ValueError as exc:
                self.reply(400, {"error": str(exc)})

        elif url.path == "/api/bookings":
            day = parse_qs(url.query).get(
                "date", [""]
            )[0]

            with connect() as db:
                rows = db.execute(
                    """
                    SELECT *
                    FROM bookings
                    WHERE substr(starts_at, 1, 10)=?
                    ORDER BY starts_at, id
                    """,
                    (day,),
                ).fetchall()

            self.reply(
                200,
                [dict(row) for row in rows],
            )

        elif url.path in ("/", "/app.js", "/style.css"):
            resources = {
                "/": (
                    "index.html",
                    "text/html; charset=utf-8",
                ),
                "/app.js": (
                    "app.js",
                    "text/javascript; charset=utf-8",
                ),
                "/style.css": (
                    "style.css",
                    "text/css; charset=utf-8",
                ),
            }

            name, content_type = resources[url.path]

            self.reply(
                200,
                ASSETS[name].encode("utf-8"),
                content_type,
            )

        else:
            self.reply(404, {"error": "ไม่พบหน้า"})

    def mutate(self):
        if not self.authorized():
            return

        if (
            self.headers.get("Sec-Fetch-Site") == "cross-site"
            or self.headers.get("X-Queue-Admin") != "1"
        ):
            self.reply(
                403,
                {"error": "คำขอไม่ได้รับอนุญาต"},
            )
            return

        try:
            length = int(
                self.headers.get("Content-Length", "0")
            )

            if not 0 < length <= 16384:
                raise ValueError("ขนาดข้อมูลไม่ถูกต้อง")

            data = json.loads(
                self.rfile.read(length)
            )

            if not isinstance(data, dict):
                raise ValueError(
                    "ข้อมูลต้องเป็น JSON object"
                )

            path = urlsplit(self.path).path

            match = re.fullmatch(
                r"/api/bookings/(\d+)(/status)?",
                path,
            )

            if (
                self.command == "POST"
                and path == "/api/bookings"
            ):
                self.reply(
                    201,
                    save_booking(data),
                )

            elif self.command == "PATCH" and match:
                booking_id = int(match[1])

                if match[2]:
                    result = set_status(
                        booking_id,
                        data.get("status"),
                    )
                else:
                    result = save_booking(
                        data,
                        booking_id,
                    )

                self.reply(200, result)

            else:
                self.reply(
                    404,
                    {"error": "ไม่พบรายการ"},
                )

        except LookupError as exc:
            self.reply(404, {"error": str(exc)})

        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
        ) as exc:
            self.reply(400, {"error": str(exc)})

        except sqlite3.Error:
            self.reply(
                503,
                {
                    "error":
                    "ฐานข้อมูลไม่พร้อม กรุณาลองใหม่"
                },
            )

    do_POST = mutate
    do_PATCH = mutate


ASSETS = {
    "index.html": r"""
<!doctype html>
<html lang="th">
<head>
    <meta charset="utf-8">
    <meta name="viewport"
          content="width=device-width, initial-scale=1">
    <title>จัดการคิว | Nail Queue</title>
    <link rel="stylesheet" href="/style.css">
    <script src="/app.js" defer></script>
</head>
<body>
<header>
    <span>N / NAIL QUEUE</span>
    <small>ร้านทำเล็บ · คิวละ 2 ชั่วโมง</small>
</header>

<main>
    <p id="demo-warning"
       class="demo-warning"
       role="note"
       hidden>
        เว็บทดลองฟรี — ข้อมูลคิวอาจหายเมื่อระบบพักเครื่อง
        หรือรีสตาร์ต กรุณาใช้ข้อมูลสมมติเท่านั้น
    </p>

    <div class="heading">
        <div>
            <p class="eyebrow">ภาพรวมการให้บริการ</p>
            <h1>ตารางนัดทำเล็บ</h1>
            <p>จัดตารางนัดหมายและดูแลทุกคิวในที่เดียว</p>
        </div>

        <label>
            วันที่ให้บริการ
            <input id="day" type="date" required>
        </label>
    </div>

    <section id="stats"
             class="stats"
             aria-label="สรุปคิว"></section>

    <div class="layout">
        <section class="card">
            <h2 id="formTitle">เพิ่มการจองใหม่</h2>

            <form id="booking">
                <label>
                    ชื่อลูกค้า
                    <input name="customer"
                           maxlength="100"
                           required>
                </label>

                <label>
                    เบอร์ติดต่อ
                    <input name="phone"
                           type="tel"
                           maxlength="30"
                           required>
                </label>

                <label>
                    บริการ
                    <input name="service"
                           maxlength="100"
                           placeholder="ทาสีเจล / ต่อเล็บ / เพ้นท์เล็บ"
                           required>
                </label>

                <div class="pair">
                    <label>
                        รอบเวลา
                        <select name="starts_at"
                                required></select>
                    </label>

                    <label>
                        ระยะเวลา (นาที)
                        <input name="duration"
                               type="number"
                               value="120"
                               readonly
                               required>
                    </label>
                </div>

                <label>
                    หมายเหตุ
                    <textarea name="notes"
                              maxlength="1000"
                              rows="3"></textarea>
                </label>

                <button type="submit" id="save">
                    บันทึกการจอง
                </button>

                <button type="button"
                        class="secondary"
                        id="reset"
                        hidden>
                    ยกเลิกการแก้ไข
                </button>
            </form>
        </section>

        <section class="card">
            <div class="heading">
                <h2>รายการคิว</h2>
                <button id="refresh" class="secondary">
                    รีเฟรช
                </button>
            </div>

            <div class="pair">
                <input id="search"
                       placeholder="ค้นหาชื่อ เบอร์ หรือบริการ"
                       aria-label="ค้นหาคิว">

                <select id="status" aria-label="กรองสถานะ">
                    <option value="">ทุกสถานะ</option>
                    <option value="waiting">รอรับบริการ</option>
                    <option value="serving">กำลังรับบริการ</option>
                    <option value="done">เสร็จสิ้น</option>
                    <option value="cancelled">ยกเลิก</option>
                </select>
            </div>

            <p id="message"
               role="status"
               aria-live="polite"></p>

            <div id="list"></div>
        </section>
    </div>

    <footer>
        เวลานัดหมายใช้เขตเวลาไทย (Asia/Bangkok)
        · ข้อมูลบันทึกในฐานข้อมูล SQLite
    </footer>
</main>
</body>
</html>
""",

    "style.css": r"""
:root {
    font-family: system-ui, sans-serif;
    color: #19352e;
    background: #f3f6f4;
}

* { box-sizing: border-box; }
body { margin: 0; }

header {
    background: #143e32;
    color: white;
    padding: 22px 5%;
    display: flex;
    justify-content: space-between;
    gap: 16px;
}

header span {
    font-weight: 800;
    letter-spacing: 2px;
}

header small { color: #d3e6da; }

main {
    max-width: 1250px;
    margin: auto;
    padding: 35px 24px;
}

h1 { font-size: 34px; margin: 4px 0 10px; }
h2 { font-size: 20px; margin: 0 0 20px; }
p, small, footer { color: #61756c; }

.eyebrow {
    font-size: 12px;
    color: #26805e;
    letter-spacing: 2px;
}

.heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
}

.stats {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
    margin: 25px 0;
}

.stat, .card {
    background: white;
    border: 1px solid #dee8e1;
    border-radius: 16px;
    padding: 23px;
}

.stat strong {
    display: block;
    font-size: 30px;
    margin-top: 8px;
}

.layout {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 24px;
}

label {
    display: block;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 16px;
}

input, textarea, select {
    font: inherit;
    display: block;
    width: 100%;
    border: 1px solid #cddbd2;
    border-radius: 8px;
    padding: 11px;
    background: white;
    color: #19352e;
    margin-top: 7px;
}

input:focus, textarea:focus, select:focus {
    outline: 2px solid #4d9e79;
}

.pair { display: flex; gap: 12px; }
.pair > * { flex: 1; min-width: 0; }

button {
    font: inherit;
    border: 0;
    border-radius: 8px;
    padding: 11px 16px;
    background: #237c58;
    color: white;
    cursor: pointer;
}

button:hover { filter: brightness(.93); }
button:disabled { opacity: .5; cursor: wait; }
form button { width: 100%; margin-top: 8px; }

.secondary {
    background: #edf3ef;
    color: #285c43;
}

.booking {
    border-top: 1px solid #e5ece7;
    padding: 20px 0;
}

.booking h3 { margin: 5px 0; font-size: 17px; }

.booking p {
    margin: 8px 0;
    font-size: 14px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}

.actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 15px;
}

.actions button { font-size: 13px; }

.badge {
    display: inline-block;
    background: #eef2ef;
    color: #506758;
    padding: 4px 9px;
    border-radius: 20px;
    font-size: 12px;
}

.badge.serving { background: #e0f3e7; color: #227340; }
.badge.waiting { background: #fff2d6; color: #88641a; }

#message { color: #a23c2f; }

footer {
    text-align: center;
    font-size: 12px;
    margin: 30px 0;
}

.empty {
    text-align: center;
    padding: 40px;
    color: #718277;
}

.demo-warning {
    background: #fff2d6;
    border: 1px solid #e7c785;
    color: #775011;
    padding: 16px;
    border-radius: 10px;
}

@media (max-width: 850px) {
    .layout { grid-template-columns: 1fr; }
    .stats { grid-template-columns: repeat(2, 1fr); }
    header, .heading { flex-wrap: wrap; }
    main { padding: 20px 16px; }
    .heading > label { width: 100%; }
}
""",

    "app.js": r"""
const $ = selector => document.querySelector(selector);

const labels = {
    waiting: 'รอรับบริการ',
    serving: 'กำลังรับบริการ',
    done: 'เสร็จสิ้น',
    cancelled: 'ยกเลิก'
};

let bookings = [];
let slots = [];
let editing = null;
let requestId = 0;

const today = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Bangkok',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
}).format(new Date());

$('#day').value = today;

const form = $('#booking');

async function api(path, method = 'GET', data) {
    const response = await fetch(path, {
        method,
        headers: {
            'Content-Type': 'application/json',
            'X-Queue-Admin': '1'
        },
        body: data ? JSON.stringify(data) : undefined
    });

    if (response.status === 401) {
        throw new Error('กรุณาเข้าสู่ระบบใหม่');
    }

    const result = await response.json();

    if (!response.ok) {
        throw new Error(result.error || 'เกิดข้อผิดพลาด');
    }

    return result;
}

function el(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text;

    if (className) {
        node.className = className;
    }

    return node;
}

function render() {
    const stats = Object.entries(labels).map(([key, label]) => {
        const node = el('div', label, 'stat');
        const count = bookings.filter(
            booking => booking.status === key
        ).length;

        node.append(el('strong', count));
        return node;
    });

    $('#stats').replaceChildren(...stats);

    const query = $('#search').value.trim().toLowerCase();
    const status = $('#status').value;

    const rows = bookings.filter(booking => {
        const matchesStatus = !status || booking.status === status;

        const matchesQuery = [
            booking.customer,
            booking.phone,
            booking.service
        ].some(value => value.toLowerCase().includes(query));

        return matchesStatus && matchesQuery;
    });

    $('#list').replaceChildren();

    if (!rows.length) {
        $('#list').append(
            el(
                'p',
                'ไม่มีรายการคิวในวันที่หรือเงื่อนไขที่เลือก',
                'empty'
            )
        );
    }

    for (const booking of rows) {
        const node = el('article', '', 'booking');

        node.append(
            el(
                'span',
                labels[booking.status],
                'badge ' + booking.status
            ),
            el(
                'h3',
                booking.starts_at.slice(11)
                + ' · ' + booking.customer
            ),
            el(
                'p',
                booking.service
                + ' · ' + booking.duration + ' นาที'
                + ' · ' + booking.phone
            )
        );

        if (booking.notes) {
            node.append(el('p', booking.notes));
        }

        const actions = el('div', '', 'actions');

        function addAction(label, action) {
            const button = el('button', label, 'secondary');

            button.onclick = async () => {
                button.disabled = true;

                try {
                    await action();
                } catch (error) {
                    $('#message').textContent = error.message;
                } finally {
                    button.disabled = false;
                }
            };

            actions.append(button);
        }

        if (booking.status === 'waiting') {
            addAction('แก้ไข / เลื่อนเวลา', () => {
                editing = booking.id;

                for (const key of [
                    'customer',
                    'phone',
                    'service',
                    'starts_at',
                    'duration',
                    'notes'
                ]) {
                    form.elements[key].value = booking[key];
                }

                $('#formTitle').textContent =
                    'แก้ไขการจอง #' + booking.id;

                $('#reset').hidden = false;
                form.scrollIntoView({ behavior: 'smooth' });
            });

            addAction(
                'เริ่มรับบริการ',
                () => update(booking, 'serving')
            );
        }

        if (booking.status === 'serving') {
            addAction(
                'เสร็จสิ้น',
                () => update(booking, 'done')
            );
        }

        if (['waiting', 'serving'].includes(booking.status)) {
            addAction('ยกเลิกคิว', async () => {
                if (confirm(
                    'ยืนยันยกเลิกคิวของ '
                    + booking.customer + '?'
                )) {
                    await update(booking, 'cancelled');
                }
            });
        }

        node.append(actions);
        $('#list').append(node);
    }
}

function renderSlots() {
    const chosen = form.elements.starts_at.value;

    const options = slots.map(value => {
        const endHour = String(
            Number(value.slice(11, 13)) + 2
        ).padStart(2, '0');

        const option = el(
            'option',
            value.slice(11) + ' – ' + endHour + ':00'
        );

        option.value = value;
        return option;
    });

    form.elements.starts_at.replaceChildren(...options);

    if (slots.includes(chosen)) {
        form.elements.starts_at.value = chosen;
    }
}

async function load() {
    const id = ++requestId;
    $('#message').textContent = '';

    try {
        const day = encodeURIComponent($('#day').value);

        const [rows, available, config] = await Promise.all([
            api('/api/bookings?date=' + day),
            api('/api/slots?date=' + day),
            api('/api/config')
        ]);

        if (id === requestId) {
            $('#demo-warning').hidden = !config.demo_mode;
            bookings = rows;
            slots = available;
            renderSlots();
            render();
        }
    } catch (error) {
        if (id === requestId) {
            bookings = [];
            render();
            $('#message').textContent = error.message;
        }
    }
}

async function update(booking, status) {
    await api(
        '/api/bookings/' + booking.id + '/status',
        'PATCH',
        { status }
    );

    await load();
}

function reset() {
    editing = null;
    form.reset();
    renderSlots();
    $('#formTitle').textContent = 'เพิ่มการจองใหม่';
    $('#reset').hidden = true;
}

form.onsubmit = async event => {
    event.preventDefault();

    $('#save').disabled = true;
    $('#message').textContent = '';

    try {
        const data = Object.fromEntries(new FormData(form));
        data.duration = Number(data.duration);

        await api(
            '/api/bookings' + (editing ? '/' + editing : ''),
            editing ? 'PATCH' : 'POST',
            data
        );

        $('#day').value = data.starts_at.slice(0, 10);

        reset();
        await load();

        $('#message').textContent = 'บันทึกการจองแล้ว';
    } catch (error) {
        $('#message').textContent = error.message;
    } finally {
        $('#save').disabled = false;
    }
};

$('#reset').onclick = reset;

$('#day').onchange = () => {
    reset();
    load();
};

$('#refresh').onclick = load;
$('#search').oninput = render;
$('#status').onchange = render;

load();
"""
}


if __name__ == "__main__":
    if len(os.environ.get("ADMIN_PASSWORD", "")) < 12:
        raise SystemExit(
            "Set ADMIN_PASSWORD to at least 12 characters before starting."
        )

    slots_for_day(date.today().isoformat())
    initialize()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))

    print(f"Starting Nail Queue on port {port}", flush=True)

    ThreadingHTTPServer(
        (host, port),
        Handler,
    ).serve_forever()
