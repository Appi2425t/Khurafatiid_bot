import aiosqlite
import os


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or os.getenv("DB_PATH", "./data/dashboard.db")

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    totp_secret TEXT NOT NULL,
                    status TEXT DEFAULT 'available',
                    assigned_to INTEGER,
                    assigned_at TIMESTAMP,
                    otp_shown INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    txn_id TEXT,
                    screenshot_file_id TEXT,
                    status TEXT DEFAULT 'pending',
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    processed_at TIMESTAMP,
                    processed_by INTEGER,
                    note TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS withdraw_wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL,
                    address TEXT NOT NULL,
                    network TEXT DEFAULT 'USDT TRC20',
                    active INTEGER DEFAULT 1
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_contact (
                    id INTEGER PRIMARY KEY,
                    telegram TEXT,
                    phone TEXT,
                    note TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notify_group (
                    id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS allowed_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    used INTEGER DEFAULT 0,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.commit()

    # --- Account CRUD ---

    async def add_account(self, account_id: str, password: str, totp_secret: str) -> bool:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO accounts (account_id, password, totp_secret) VALUES (?, ?, ?)",
                    (account_id, password, totp_secret)
                )
                await db.commit()
            return True
        except Exception:
            return False

    async def upsert_account(self, account_id: str, password: str, totp_secret: str):
        """Insert or update an account (used during Excel upload)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO accounts (account_id, password, totp_secret, status)
                   VALUES (?, ?, ?, 'available')
                   ON CONFLICT(account_id) DO UPDATE SET
                   password = ?, totp_secret = ?""",
                (account_id, password, totp_secret, password, totp_secret)
            )
            await db.commit()

    async def get_all_accounts(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM accounts ORDER BY status ASC, id ASC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_available_account(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM accounts WHERE status = 'available' LIMIT 1"
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def assign_account(self, account_id: str, user_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE accounts SET status = 'assigned', assigned_to = ?, assigned_at = CURRENT_TIMESTAMP WHERE account_id = ?",
                (user_id, account_id)
            )
            await db.commit()

    async def get_user_account(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM accounts WHERE assigned_to = ? AND status = 'assigned'",
                (user_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def remove_account(self, account_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM accounts WHERE account_id = ?",
                (account_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def reset_all(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE accounts SET status = 'available', assigned_to = NULL, assigned_at = NULL"
            )
            await db.commit()

    async def reset_account(self, account_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE accounts SET status = 'available', assigned_to = NULL, assigned_at = NULL WHERE account_id = ?",
                (account_id,)
            )
            await db.commit()

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts")
            total = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'available'")
            available = (await cursor.fetchone())[0]
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE status = 'assigned'")
            assigned = (await cursor.fetchone())[0]
            return {"total": total, "available": available, "assigned": assigned}

    # --- Admins ---
    async def get_admins(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admins ORDER BY added_at ASC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def add_admin(self, user_id: int, added_by: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)",
                (user_id, added_by)
            )
            await db.commit()

    async def remove_admin(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def is_db_admin(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    async def mark_otp_shown(self, account_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE accounts SET otp_shown = 1 WHERE account_id = ?", (account_id,))
            await db.commit()

    async def run_migrations(self):
        """Add new columns to existing databases."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("PRAGMA table_info(accounts)")
            cols = [row[1] for row in await cursor.fetchall()]
            if "otp_shown" not in cols:
                await db.execute("ALTER TABLE accounts ADD COLUMN otp_shown INTEGER DEFAULT 0")

            # check admin_contact table exists
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin_contact'")
            if not await cursor.fetchone():
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS admin_contact (
                        id INTEGER PRIMARY KEY,
                        telegram TEXT,
                        phone TEXT,
                        note TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # check notify_group table exists
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notify_group'")
            if not await cursor.fetchone():
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS notify_group (
                        id INTEGER PRIMARY KEY,
                        chat_id INTEGER NOT NULL
                    )
                """)

            # check allowed_users table exists
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='allowed_users'")
            if not await cursor.fetchone():
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS allowed_users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        phone TEXT NOT NULL UNIQUE,
                        name TEXT NOT NULL,
                        used INTEGER DEFAULT 0,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # check withdrawals screenshot column
            cursor = await db.execute("PRAGMA table_info(withdrawals)")
            cols = [row[1] for row in await cursor.fetchall()]
            if "screenshot_file_id" not in cols:
                await db.execute("ALTER TABLE withdrawals ADD COLUMN screenshot_file_id TEXT")

            await db.commit()

    # --- Withdraw Wallets ---
    async def get_active_wallet(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM withdraw_wallets WHERE active = 1 ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_wallets(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM withdraw_wallets ORDER BY id DESC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def add_wallet(self, label: str, address: str, network: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO withdraw_wallets (label, address, network) VALUES (?, ?, ?)",
                (label, address, network)
            )
            await db.commit()
            return cursor.lastrowid

    async def delete_wallet(self, wallet_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM withdraw_wallets WHERE id = ?", (wallet_id,))
            await db.commit()
            return cursor.rowcount > 0

    # --- Withdrawals ---
    async def create_withdrawal(self, user_id: int, account_id: str) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "INSERT INTO withdrawals (user_id, account_id) VALUES (?, ?)",
                (user_id, account_id)
            )
            await db.commit()
            return cursor.lastrowid

    async def update_withdrawal_txn(self, withdrawal_id: int, txn_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE withdrawals SET txn_id = ? WHERE id = ?",
                (txn_id, withdrawal_id)
            )
            await db.commit()

    async def update_withdrawal_screenshot(self, withdrawal_id: int, file_id: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE withdrawals SET screenshot_file_id = ? WHERE id = ?",
                (file_id, withdrawal_id)
            )
            await db.commit()

    async def get_notify_group_chat_id(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT chat_id FROM notify_group WHERE id = 1")
            row = await cursor.fetchone()
            return row[0] if row else None

    async def get_pending_withdrawals(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM withdrawals WHERE status = 'pending' ORDER BY requested_at ASC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_user_withdrawal(self, user_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM withdrawals WHERE user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def approve_withdrawal(self, withdrawal_id: int, processed_by: int, note: str = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE withdrawals SET status = 'approved', processed_at = CURRENT_TIMESTAMP, processed_by = ?, note = ? WHERE id = ?",
                (processed_by, note, withdrawal_id)
            )
            await db.commit()

    async def reject_withdrawal(self, withdrawal_id: int, processed_by: int, note: str = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE withdrawals SET status = 'rejected', processed_at = CURRENT_TIMESTAMP, processed_by = ?, note = ? WHERE id = ?",
                (processed_by, note, withdrawal_id)
            )
            await db.commit()

    async def get_withdrawal_by_id(self, withdrawal_id: int) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    # --- Admin Contact ---
    async def set_admin_contact(self, telegram: str = None, phone: str = None, note: str = None):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO admin_contact (id, telegram, phone, note, updated_at)
                   VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(id) DO UPDATE SET
                   telegram = ?, phone = ?, note = ?, updated_at = CURRENT_TIMESTAMP""",
                (telegram, phone, note, telegram, phone, note)
            )
            await db.commit()

    async def get_admin_contact(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_contact WHERE id = 1")
            row = await cursor.fetchone()
            return dict(row) if row else None

    # --- Notification Group ---
    async def set_notify_group_id(self, chat_id: int):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO notify_group (id, chat_id) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET chat_id = ?""",
                (chat_id, chat_id)
            )
            await db.commit()

    # --- Allowed Users (Phone Whitelist) ---
    async def check_allowed_phone(self, phone: str) -> dict:
        """Normalize and check if phone is in whitelist. Returns user dict or None."""
        normalized = ''.join(c for c in phone if c.isdigit())
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Match last 10 digits to handle country code variations
            cursor = await db.execute("SELECT * FROM allowed_users")
            rows = await cursor.fetchall()
            for row in rows:
                stored = ''.join(c for c in row["phone"] if c.isdigit())
                # Match if last 10 digits are same
                if normalized[-10:] == stored[-10:]:
                    return dict(row)
            return None

    async def mark_phone_used(self, phone: str):
        normalized = ''.join(c for c in phone if c.isdigit())
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM allowed_users")
            rows = await cursor.fetchall()
            for row in rows:
                stored = ''.join(c for c in row["phone"] if c.isdigit())
                if normalized[-10:] == stored[-10:]:
                    await db.execute("UPDATE allowed_users SET used = 1 WHERE id = ?", (row[0],))
                    await db.commit()
                    return

    async def get_all_allowed_users(self) -> list:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM allowed_users ORDER BY id ASC")
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def upsert_allowed_user(self, phone: str, name: str):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO allowed_users (phone, name) VALUES (?, ?)
                   ON CONFLICT(phone) DO UPDATE SET name = ?""",
                (phone, name, name)
            )
            await db.commit()

    async def remove_allowed_user(self, phone: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM allowed_users WHERE phone = ?", (phone,))
            await db.commit()
            return cursor.rowcount > 0

    async def clear_allowed_users(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM allowed_users")
            await db.commit()
