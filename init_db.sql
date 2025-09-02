CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username TEXT,
    boxberry_login TEXT,
    boxberry_password TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS parcels (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id),
    tracking_number TEXT,
    recipient_name TEXT,
    recipient_surname TEXT,
    last_status TEXT,
    last_update TIMESTAMP,
    raw_json TEXT
);
