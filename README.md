# ระบบจองคิวร้านทำเล็บ

คิวละ 2 ชั่วโมง สลับวันเริ่ม 07:00 และ 08:00
รองรับเพิ่มจอง เลื่อนเวลา ยกเลิก และจัดการสถานะคิว

## ตั้งค่าบน Render

- Runtime: Python 3
- Build Command: python3 -m py_compile app.py
- Start Command: HOST=0.0.0.0 python3 app.py
- Health Check Path: /healthz

## Environment Variables

- ADMIN_PASSWORD: ตั้งรหัสผ่านอย่างน้อย 12 ตัวอักษร
- NAIL_EARLY_DATE: 2026-10-30
- QUEUE_DEMO_MODE: 1

เข้าสู่ระบบด้วยชื่อผู้ใช้ admin และรหัสผ่านที่ตั้งไว้

หมายเหตุ: บริการฟรีชุดนี้ใช้ทดลอง ข้อมูลคิวอาจหายเมื่อรีสตาร์ต
