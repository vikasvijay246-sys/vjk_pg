# 🏠 PG Manager Pro — Full-Stack Edition

Production-grade, mobile-first Paying Guest Management System.
Built for low-literacy PG owners — icon-heavy, color-coded, big buttons, real-time, E2E encrypted.

---

## ✨ Feature Summary

### Phase 1 & 2 (Core)
| Module | Details |
|--------|---------|
| 🔐 Auth | JWT login/register, role-based (Owner/Worker/Tenant), ECDH public key exchange |
| 👥 Tenants | Add/edit/remove, photo + ID upload, paginated list (search, filter), room allocation |
| 💰 Payments | Mark paid/unpaid per month, dashboard stats, monthly reports |
| 💬 Chat | Real-time 1:1 via Socket.IO + **E2E encryption** (ECDH + AES-GCM) |
| 🔔 Notifications | Real-time push for rent updates, messages, reminders |
| 📁 Files | Upload/download with role-based access control |
| ⚙️ Settings | Language, theme, notification prefs |
| 📱 PWA | Installable, offline-ready service worker |

### Phase 3 (New)
| Module | Details |
|--------|---------|
| 📱 Community Feed | Text, photo, video posts; personal/PG/public visibility |
| 🎬 Short Videos | Upload, stream with byte-range support, thumbnail, duration badge |
| ❤️ Live Reactions | 6 emoji types (❤️🔥😂😮👏😢), floating burst animation, real-time via Socket.IO |
| 💬 Comments | Nested replies, delete, real-time push to all viewers |
| 📢 Notice Board | Owner-posted notices with yellow highlight |
| 🏘️ Multi-PG | Manage multiple properties, per-property stats dashboard |
| 📣 Reminders | App/WhatsApp/SMS rent reminders in EN/TE/HI |
| 📱 UPI QR | Scannable UPI deep-link QR per tenant with rent amount |
| 📄 PDF Reports | Colour-coded professional PDF with summary + tenant table |
| 🌐 i18n | Full Telugu + Hindi UI translation (61 strings each) |

---

## 🚀 Quick Start (3 commands)

```bash
# 1. Install
pip install -r requirements.txt

# 2. Run
python app.py

# 3. Open on phone (same WiFi)
# http://<YOUR_PC_IP>:5000
```

**Demo:** phone `9999900000` · password `owner123`

---

## 📁 Project Structure

```
pg-pro/
├── app.py               ← Flask app factory + seed data
├── config.py            ← Dev / Production config
├── extensions.py        ← Shared extensions (db, jwt, socketio, bcrypt)
├── db_models.py         ← 10 models: User, Tenant, Room, Payment,
│                           Message, Notification, UploadedFile,
│                           UserSettings, Post+Likes+Comments,
│                           PGProperty, ReminderLog
├── requirements.txt
├── api/
│   ├── auth.py          ← Login, register, JWT, ECDH key
│   ├── tenants.py       ← Tenant & Room CRUD + pagination
│   ├── payments.py      ← Payment tracking, reports, dashboard
│   ├── chat.py          ← Chat REST + file messages
│   ├── misc.py          ← Notifications, Files, Settings
│   ├── social.py        ← Feed, posts, likes, comments, video stream
│   └── phase3.py        ← Multi-PG, reminders, UPI QR, PDF, i18n
├── sockets/
│   ├── chat_socket.py   ← Real-time chat events (send, typing, seen)
│   └── social_socket.py ← Live reactions, comments, feed events
├── static/
│   ├── sw.js            ← PWA Service Worker
│   └── manifest.json    ← PWA Manifest
└── templates/
    └── index.html       ← Complete SPA (~2300 lines, zero dependencies)
```

---

## 🌐 Deploy to Render (Free)

1. Push to GitHub
2. Render → New Web Service → connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn --threads 4 -w 1 "app:app"`
5. Environment variables:
```
FLASK_ENV=production
SECRET_KEY=<random 32 char string>
JWT_SECRET_KEY=<another random string>
DATABASE_URL=postgresql://... (from Render Postgres)
```

For WhatsApp reminders in production, add:
```
TWILIO_SID=<your sid>
TWILIO_AUTH=<your auth token>
TWILIO_FROM=whatsapp:+14155238886
```
Then uncomment the Twilio block in `api/phase3.py`.

---

## 🔌 API Reference (all endpoints)

### Auth `/api/auth/`
| POST `/register` | POST `/login` | POST `/refresh` | GET `/me` | POST `/update-key` | GET `/users` |

### Tenants `/api/tenants/`
| GET `?page&per_page&search&paid` | POST | GET `/<id>` | PUT `/<id>` | DELETE `/<id>` |
| GET `/rooms` | POST `/rooms` | GET `/me` |

### Payments `/api/payments/`
| GET `/dashboard` | POST `/<id>/mark` | GET `/report?month=` | GET `/<id>/history` |

### Chat `/api/chat/`
| GET `/conversations` | GET `/history/<peer>` | POST `/send` | GET `/pubkey/<id>` |

### Social `/api/social/`
| GET `/feed?type&page` | POST `/posts` | GET `/posts/<id>` | DELETE `/posts/<id>` |
| POST `/posts/<id>/like` | GET `/posts/<id>/comments` | POST `/posts/<id>/comments` |
| DELETE `/comments/<id>` | GET `/stream/<filename>` |

### Phase 3 `/api/v2/`
| GET/POST `/properties` | PUT `/properties/<id>` | GET `/properties/<id>/summary` |
| POST `/reminders/send` | GET `/reminders/log` |
| GET `/upi-qr?tenant_id&upi_id&amount` | GET `/reports/pdf?month=` |
| GET `/i18n/<lang>` (en/te/hi) |

### Misc
| GET/PUT `/api/settings` | GET/POST `/api/notifications` | POST `/api/files/upload` |
| GET `/api/files/download/<name>` |

---

## 🔒 Security

- Passwords: **bcrypt** hashed
- Chat: **ECDH + AES-GCM** E2E encryption (server stores only ciphertext)
- Auth: **JWT** (7-day access + 30-day refresh tokens)
- Files: Extension whitelist + role-based access
- API: Every endpoint is JWT-protected with role guards
- Soft deletes — no data permanently lost

---

## ⚠️ Notes

- `db_models.py` not `models.py` — avoids PyPI `models` package conflict
- Python 3.13 safe — uses `async_mode="threading"` (no eventlet)
- Videos served with **HTTP Range requests** (mobile-compatible streaming)
- SQLite → PostgreSQL: just set `DATABASE_URL` env var, zero code change
- Uploads: use S3/Cloudinary in production (`static/uploads/` is ephemeral on Render)
