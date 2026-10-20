mod delivery;
mod event_bus;
mod folders;
mod transport;

// Linux-first, deliberately small harness daemon.  The hub remains the
// authority; SQLite is solely a durable, bounded local outbox/session tail.

use std::{
    collections::{BTreeMap, HashMap, VecDeque},
    fs,
    future::IntoFuture,
    io::{Read, Write},
    path::{Path as FsPath, PathBuf},
    process::Command,
    sync::{Arc, Mutex},
    time::Duration,
};

use anyhow::{Context, Result};
use axum::{
    extract::{
        ws::{WebSocket, WebSocketUpgrade},
        Path, Query, State,
    },
    http::{header, HeaderMap, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::{any, get, post},
    Json, Router,
};
use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE},
    Engine,
};
use chrono::Utc;
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use subtle::ConstantTimeEq;
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, BufReader},
    process::Command as TokioCommand,
    sync::{broadcast, mpsc, RwLock},
};
use tokio_tungstenite::{
    connect_async,
    tungstenite::{client::IntoClientRequest, Message},
};
use tracing::{error, warn};
use uuid::Uuid;

const OUTBOX_LIMIT: i64 = 5_000;
const RETENTION_DAYS: i64 = 7;
const RETENTION_PER_SESSION: i64 = 500;

#[derive(Parser, Debug, Clone)]
#[command(
    name = "drover-harnessd",
    version,
    about = "Drover Rust harness daemon"
)]
struct Args {
    #[arg(long)]
    config: Option<PathBuf>,
    #[arg(long)]
    host_id: String,
    #[arg(long)]
    display_name: Option<String>,
    #[arg(long, default_value = "linux")]
    kind: String,
    #[arg(long, default_value = "127.0.0.1:7081")]
    listen: String,
    #[arg(long)]
    local_url: Option<String>,
    #[arg(long)]
    tailscale_url: Option<String>,
    #[arg(long)]
    central_url: Option<String>,
    #[arg(long)]
    host_token: Option<String>,
    #[arg(long)]
    relay: bool,
    #[arg(short, long, action = clap::ArgAction::Count)]
    verbose: u8,
    /// Import drained Python JSONL records into the local SQLite tail, then exit.
    #[arg(long)]
    import_jsonl: Option<PathBuf>,
    /// Validate a Python drain handoff without changing the Rust store.
    #[arg(long, requires = "import_jsonl")]
    import_dry_run: bool,
}

#[derive(Clone)]
struct App {
    cfg: Config,
    db: Arc<Mutex<Connection>>,
    sessions: Arc<RwLock<HashMap<String, Session>>>,
    terminals: Arc<RwLock<HashMap<String, Arc<Terminal>>>>,
    client: Client,
    events: broadcast::Sender<Value>,
    writer: event_bus::StoreWriter,
    operations: Arc<tokio::sync::Mutex<()>>,
    stop: Arc<tokio::sync::Notify>,
}

const TERMINAL_SCROLLBACK_BYTES: usize = 256 * 1024;
const TERMINAL_CHANNEL_CAPACITY: usize = 256;

/// Owns the live half of a shell PTY. Broadcast receivers are bounded; a slow
/// relay client loses repaint-only output rather than delaying the terminal
/// reader or growing host memory without limit.
struct Terminal {
    writer: Mutex<Box<dyn Write + Send>>,
    _master: Mutex<Box<dyn portable_pty::MasterPty + Send>>,
    killer: Mutex<Box<dyn portable_pty::ChildKiller + Send + Sync>>,
    finished: tokio::sync::watch::Sender<bool>,
    scrollback: Mutex<VecDeque<u8>>,
    output: broadcast::Sender<Value>,
}

impl Terminal {
    fn write(&self, data: &[u8]) -> Result<()> {
        let mut writer = self
            .writer
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        writer.write_all(data)?;
        writer.flush()?;
        Ok(())
    }

    fn replay(&self) -> Option<Value> {
        let bytes: Vec<u8> = self
            .scrollback
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .iter()
            .copied()
            .collect();
        let text = String::from_utf8_lossy(&bytes).into_owned();
        (!text.is_empty()).then(|| json!({"type":"output","data":text}))
    }

    fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.output.subscribe()
    }
}

#[derive(Clone)]
struct Config {
    host_id: String,
    display_name: String,
    kind: String,
    token: String,
    central_url: Option<String>,
    local_url: Option<String>,
    tailscale_url: Option<String>,
    consent_path: PathBuf,
    relay: bool,
    worktrees: PathBuf,
    home: PathBuf,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Session {
    session_id: String,
    harness: String,
    mode: String,
    cwd: String,
    status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    model: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    thinking_effort: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    client_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    parent_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    native_session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    worktree: Option<Worktree>,
    #[serde(skip_serializing_if = "Option::is_none")]
    awaiting: Option<String>,
    created_at: String,
    #[serde(default)]
    restart_lost: bool,
    #[serde(default, flatten)]
    extra: BTreeMap<String, Value>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Worktree {
    repo: String,
    path: String,
    branch: String,
    base: String,
}

fn response(status: StatusCode, value: Value) -> Response {
    let mut r = (status, Json(value)).into_response();
    let h = r.headers_mut();
    h.insert("access-control-allow-origin", HeaderValue::from_static("*"));
    h.insert(
        "access-control-allow-methods",
        HeaderValue::from_static("GET, POST, DELETE, OPTIONS"),
    );
    h.insert(
        "access-control-allow-headers",
        HeaderValue::from_static("Authorization, Content-Type"),
    );
    r
}
fn ok(v: Value) -> Response {
    response(StatusCode::OK, v)
}
fn err(status: StatusCode, message: impl Into<String>) -> Response {
    response(status, json!({"error": message.into()}))
}
fn now() -> String {
    // Python's datetime.isoformat() spells UTC as +00:00, not Z; this is part
    // of the cross-implementation event identity input.
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false)
}
fn clean_id() -> String {
    format!("harness-{}", Uuid::new_v4())
}

impl App {
    fn authorized(&self, headers: &HeaderMap) -> bool {
        headers
            .get(header::AUTHORIZATION)
            .and_then(|v| v.to_str().ok())
            .map(|v| {
                bool::from(
                    v.as_bytes()
                        .ct_eq(format!("Bearer {}", self.cfg.token).as_bytes()),
                )
            })
            .unwrap_or(false)
    }
    fn save_session(&self, s: &Session) -> Result<()> {
        let db = self.db.lock().unwrap();
        db.execute_batch("SAVEPOINT session_write")?;
        let result = (|| -> Result<()> {
            db.execute(
                "INSERT INTO sessions(session_id, data, status, created_at) VALUES(?1,?2,?3,?4)
             ON CONFLICT(session_id) DO UPDATE SET data=excluded.data,status=excluded.status",
                params![
                    s.session_id,
                    serde_json::to_string(s)?,
                    s.status,
                    s.created_at
                ],
            )?;
            if let Some(worktree) = &s.worktree {
                db.execute("INSERT INTO worktrees(session_id,data) VALUES(?1,?2) ON CONFLICT(session_id) DO UPDATE SET data=excluded.data",params![s.session_id,serde_json::to_string(worktree)?])?;
            } else {
                db.execute("DELETE FROM worktrees WHERE session_id=?1", [&s.session_id])?;
            }
            Ok(())
        })();
        db.execute_batch(if result.is_ok() {
            "RELEASE session_write"
        } else {
            "ROLLBACK TO session_write; RELEASE session_write"
        })?;
        result
    }
    async fn set_session(&self, s: Session) -> Result<()> {
        self.save_session(&s)?;
        self.sessions.write().await.insert(s.session_id.clone(), s);
        Ok(())
    }
    fn emit(
        &self,
        session_id: &str,
        kind: &str,
        text: Option<&str>,
        payload: Value,
        turn_id: Option<&str>,
    ) -> Result<()> {
        self.writer.append(session_id, kind, text, payload, turn_id)
    }
    /// Preserve handoff identity exactly: Python's event ID and dedup key are
    /// already the hub fence, so recomputing either would make replay unsafe.
    fn import_event(&self, event: &Value) -> Result<bool> {
        let event_id = event
            .get("event_id")
            .and_then(Value::as_str)
            .context("import event_id missing")?;
        let dedup_key = event
            .get("dedup_key")
            .and_then(Value::as_str)
            .context("import dedup_key missing")?;
        let session_id = event
            .get("session_id")
            .and_then(Value::as_str)
            .context("import event session_id missing")?;
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .context("import event type missing")?;
        let seq = event
            .get("seq")
            .and_then(Value::as_i64)
            .context("import event seq missing")?;
        let ts = event
            .get("ts")
            .or_else(|| event.get("created_at"))
            .and_then(Value::as_str)
            .context("import event timestamp missing")?;
        let changed = self.db.lock().unwrap().execute(
            "INSERT OR IGNORE INTO events(event_id,session_id,seq,type,text,payload,turn_id,ts,dedup_key) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9)",
            params![event_id, session_id, seq, kind, event.get("text").and_then(Value::as_str), serde_json::to_string(event.get("payload").unwrap_or(&Value::Null))?, event.get("turn_id").and_then(Value::as_str), ts, dedup_key],
        )?;
        self.db.lock().unwrap().execute("INSERT INTO session_sequences(session_id,next_seq) VALUES(?1,?2) ON CONFLICT(session_id) DO UPDATE SET next_seq=MAX(next_seq,excluded.next_seq)",params![session_id,seq+1])?;
        if let Some(turn_id) = event["turn_id"]
            .as_str()
            .filter(|v| Uuid::parse_str(v).is_ok())
        {
            if kind == "user_input" {
                self.db.lock().unwrap().execute("INSERT OR IGNORE INTO turns(session_id,client_turn_id,turn_id) VALUES(?1,?2,?2)",params![session_id,turn_id])?;
            }
        }
        Ok(changed == 1)
    }
}

fn event_identity(session: &str, seq: i64, kind: &str, ts: &str, payload: &Value) -> String {
    fn strip(v: &Value) -> Value {
        match v {
            Value::Object(m) => Value::Object(
                m.iter()
                    .filter(|(k, _)| *k != "event_id" && *k != "aggregated_output")
                    .map(|(k, v)| (k.clone(), strip(v)))
                    .collect(),
            ),
            Value::Array(a) => Value::Array(a.iter().map(strip).collect()),
            _ => v.clone(),
        }
    }
    let body = python_json(&canonical(&strip(payload)));
    let raw = format!("{session}|{seq}|{kind}|{ts}|{body}");
    format!("{:x}", Sha256::digest(raw.as_bytes()))
}
/// Python's `json.dumps(..., sort_keys=True)` uses the default spaced
/// separators.  This is intentionally not serde_json's compact wire format:
/// it is an input to the established cross-daemon dedup fence.
fn python_json(value: &Value) -> String {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {
            serde_json::to_string(value).unwrap_or_else(|_| "null".into())
        }
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_json)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => format!(
            "{{{}}}",
            values
                .iter()
                .map(|(key, value)| format!(
                    "{}: {}",
                    serde_json::to_string(key).unwrap_or_default(),
                    python_json(value)
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}
fn canonical(v: &Value) -> Value {
    match v {
        Value::Object(m) => {
            let mut out = BTreeMap::new();
            for (k, v) in m {
                out.insert(k.clone(), canonical(v));
            }
            serde_json::to_value(out).unwrap()
        }
        Value::Array(a) => Value::Array(a.iter().map(canonical).collect()),
        _ => v.clone(),
    }
}

fn init_db(path: &FsPath) -> Result<Connection> {
    if let Some(p) = path.parent() {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(p)?;
    }
    let db = Connection::open(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    db.pragma_update(None, "journal_mode", "WAL")?;
    db.busy_timeout(Duration::from_secs(5))?;
    db.execute_batch("CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY,data TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,event_id TEXT UNIQUE NOT NULL,session_id TEXT NOT NULL,seq INTEGER NOT NULL,type TEXT NOT NULL,text TEXT,payload TEXT NOT NULL,turn_id TEXT,ts TEXT NOT NULL,dedup_key TEXT UNIQUE NOT NULL,pushed_at TEXT);
    CREATE INDEX IF NOT EXISTS events_outbox ON events(pushed_at,id); CREATE INDEX IF NOT EXISTS events_session_seq ON events(session_id,seq);")?;
    let version: i64 = db.pragma_query_value(None, "user_version", |r| r.get(0))?;
    anyhow::ensure!(version <= 4, "SQLite schema is newer than this binary");
    if version < 1 {
        db.execute_batch("BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS event_quarantine(event_id TEXT PRIMARY KEY REFERENCES events(event_id),session_id TEXT NOT NULL,reason TEXT NOT NULL,quarantined_at TEXT NOT NULL);
        PRAGMA user_version=1; COMMIT;")?;
    }
    if version < 2 {
        db.execute_batch("BEGIN IMMEDIATE; CREATE TABLE IF NOT EXISTS turns(session_id TEXT NOT NULL,client_turn_id TEXT NOT NULL,turn_id TEXT NOT NULL,PRIMARY KEY(session_id,client_turn_id));
        CREATE TABLE IF NOT EXISTS worktrees(session_id TEXT PRIMARY KEY,data TEXT NOT NULL);
        PRAGMA user_version=2; COMMIT;")?;
    }
    if version < 3 {
        db.execute_batch("BEGIN IMMEDIATE; CREATE TABLE IF NOT EXISTS handoff_records(kind TEXT NOT NULL,record_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(kind,record_id)); PRAGMA user_version=3; COMMIT;")?;
    }
    if version < 4 {
        db.execute_batch("BEGIN IMMEDIATE; CREATE TABLE session_sequences(session_id TEXT PRIMARY KEY,next_seq INTEGER NOT NULL); INSERT INTO session_sequences SELECT session_id,MAX(seq)+1 FROM events GROUP BY session_id; PRAGMA user_version=4; COMMIT;")?;
    }
    Ok(db)
}

fn prune_events(db: &Connection) -> Result<()> {
    // The row cap applies only to acknowledged tails. Pending and quarantined
    // originals are durable until an operator resolves them.
    db.execute("DELETE FROM events WHERE pushed_at IS NOT NULL AND event_id NOT IN (SELECT event_id FROM event_quarantine) AND (id IN (SELECT id FROM events WHERE pushed_at IS NOT NULL ORDER BY id DESC LIMIT -1 OFFSET ?1) OR (julianday(ts) < julianday('now', ?2) AND id NOT IN (SELECT id FROM events e2 WHERE e2.session_id=events.session_id ORDER BY id DESC LIMIT ?3)))",params![OUTBOX_LIMIT,format!("-{} days",RETENTION_DAYS),RETENTION_PER_SESSION])?;
    Ok(())
}

fn config(args: &Args) -> Result<Config> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .context("HOME is required")?;
    let token = args
        .host_token
        .clone()
        .or_else(|| std::env::var("DROVER_API_TOKEN").ok())
        .or_else(|| {
            fs::read_to_string(home.join(".drover/api_token"))
                .ok()
                .map(|s| s.trim().to_string())
        })
        .unwrap_or_default();
    anyhow::ensure!(
        !token.trim().is_empty(),
        "a non-empty host token is required"
    );
    let mut worktrees = home.join(".drover/worktrees");
    let config_path = args
        .config
        .clone()
        .unwrap_or_else(|| home.join(".drover/config.toml"));
    if config_path.exists() {
        let t: toml::Value = fs::read_to_string(&config_path)?
            .parse()
            .context("invalid config TOML")?;
        if let Some(path) = t
            .get("paths")
            .and_then(|x| x.get("worktrees_dir"))
            .and_then(|x| x.as_str())
        {
            worktrees = PathBuf::from(path.replace("~", &home.to_string_lossy()));
        }
    }
    Ok(Config {
        host_id: args.host_id.clone(),
        display_name: args
            .display_name
            .clone()
            .unwrap_or_else(|| args.host_id.clone()),
        kind: args.kind.clone(),
        token,
        central_url: args
            .central_url
            .as_ref()
            .map(|url| url.trim_end_matches('/').to_string()),
        local_url: args.local_url.clone(),
        tailscale_url: args.tailscale_url.clone(),
        consent_path: config_path.with_file_name(format!(
            ".{}.harness-content-consent.json",
            config_path.file_name().unwrap().to_string_lossy()
        )),
        relay: args.relay,
        worktrees,
        home,
    })
}

fn load_sessions(db: &Connection) -> Result<HashMap<String, Session>> {
    let mut st = db.prepare("SELECT data FROM sessions")?;
    let rows = st.query_map([], |r| r.get::<_, String>(0))?;
    let mut out = HashMap::new();
    for row in rows {
        let mut s: Session = serde_json::from_str(&row?)?;
        if s.status == "running" {
            s.status = "errored".into();
            s.restart_lost = true;
            s.awaiting = None;
            s.pid = None;
        }
        out.insert(s.session_id.clone(), s);
    }
    Ok(out)
}

async fn health(State(app): State<App>) -> Response {
    ok(
        json!({"ok":true,"host_id":app.cfg.host_id,"active_sessions":app.sessions.read().await.values().filter(|s|s.status=="running").count()}),
    )
}
async fn options() -> Response {
    response(StatusCode::NO_CONTENT, Value::Null)
}
fn capability_envelope() -> Value {
    json!({"schema_version":1,"lifecycle":{"worktree_inventory":1},"harnesses":[
        {"name":"shell","display_name":"Shell","enabled":true,"capabilities":{"schema_version":1,"harness_id":"shell","launch_modes":["pty"],"interrupt":true,"worktree":false,"interactive_auth":false}},
        {"name":"codex","display_name":"Codex","enabled":true,"capabilities":{"schema_version":1,"harness_id":"codex","launch_modes":["structured"],"interrupt":true,"native_resume":true,"worktree":true,"attachments":["image/png","image/jpeg","image/webp","image/gif"],"interactive_auth":false}}
    ]})
}
async fn capabilities(State(app): State<App>, headers: HeaderMap) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let mut value = capability_envelope();
    value["host_id"] = json!(app.cfg.host_id);
    ok(value)
}

#[derive(Deserialize)]
struct Create {
    harness: String,
    mode: Option<String>,
    cwd: Option<String>,
    command: Option<Vec<String>>,
    prompt: Option<String>,
    initial_input: Option<String>,
    rows: Option<u16>,
    cols: Option<u16>,
    images: Option<Vec<Image>>,
    model: Option<String>,
    #[serde(rename = "thinking_effort")]
    _thinking_effort: Option<String>,
    permission_mode: Option<String>,
    client_session_id: Option<String>,
    source_session_id: Option<String>,
    parent_session_id: Option<String>,
    native_resume: Option<Value>,
}
#[derive(Deserialize)]
struct Image {
    media_type: String,
    data_base64: String,
}
async fn sessions_get(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    if q.get("archived_cursor")
        .is_some_and(|c| URL_SAFE.decode(c).is_err())
    {
        return err(StatusCode::BAD_REQUEST, "invalid archived_cursor");
    };
    let limit = q
        .get("archived")
        .or_else(|| q.get("archived_limit"))
        .and_then(|x| x.parse().ok())
        .unwrap_or(20)
        .min(100);
    let mut all: Vec<_> = app.sessions.read().await.values().cloned().collect();
    all.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    let (mut live, mut arch): (Vec<_>, Vec<_>) =
        all.into_iter().partition(|s| s.status == "running");
    live.append(&mut arch);
    let start = q
        .get("archived_cursor")
        .and_then(|c| URL_SAFE.decode(c).ok())
        .and_then(|x| String::from_utf8(x).ok())
        .and_then(|x| x.parse().ok())
        .unwrap_or(0);
    let active = live.iter().filter(|s| s.status == "running").count();
    let archived: Vec<_> = live.into_iter().filter(|s| s.status != "running").collect();
    let mut result: Vec<Value> = app
        .sessions
        .read()
        .await
        .values()
        .filter(|s| s.status == "running")
        .cloned()
        .map(|s| session_wire(s, &app.cfg.host_id))
        .collect();
    result.sort_by(|a, b| b["created_at"].as_str().cmp(&a["created_at"].as_str()));
    for s in archived.into_iter().skip(start).take(limit) {
        result.push(session_wire(s, &app.cfg.host_id));
    }
    let next = if archived_len(&app).await > start + limit {
        Some(URL_SAFE.encode((start + limit).to_string()))
    } else {
        None
    };
    let _ = active;
    ok(
        json!({"host_id":app.cfg.host_id,"sessions":result,"archived_limit":limit,"next_archived_cursor":next}),
    )
}
async fn archived_len(app: &App) -> usize {
    app.sessions
        .read()
        .await
        .values()
        .filter(|s| s.status != "running")
        .count()
}
fn session_wire(s: Session, host_id: &str) -> Value {
    let mut value = serde_json::to_value(s).unwrap_or_else(|_| json!({}));
    value.as_object_mut().unwrap().remove("restart_lost");
    value["host_id"] = json!(host_id);
    value["registry_synced"] = json!(true);
    value
}

async fn sessions_create(
    State(app): State<App>,
    headers: HeaderMap,
    Json(req): Json<Create>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let _operation = app.operations.lock().await;
    let mode = req.mode.clone().unwrap_or_else(|| {
        if req.harness == "shell" {
            "pty".into()
        } else {
            "structured".into()
        }
    });
    if req.harness != "shell" && req.harness != "codex" {
        return err(
            StatusCode::BAD_REQUEST,
            format!("unknown or disabled harness preset: {}", req.harness),
        );
    }
    if req.harness == "shell" && mode == "structured" {
        return err(
            StatusCode::BAD_REQUEST,
            "harness has no structured driver: shell",
        );
    };
    if req.harness == "codex" && mode != "structured" {
        return err(
            StatusCode::BAD_REQUEST,
            "harness codex has no pty launch mode",
        );
    };
    if let Some(p) = &req.permission_mode {
        if p == "ask" {
            return err(
                StatusCode::BAD_REQUEST,
                "permission_mode ask is not supported yet",
            );
        };
        if p != "auto" {
            return err(StatusCode::BAD_REQUEST, "unknown permission_mode");
        }
    }
    if req
        .model
        .as_ref()
        .is_some_and(|m| m.trim().is_empty() || m.len() > 256)
    {
        return err(StatusCode::BAD_REQUEST, "invalid model");
    }
    if !matches!(mode.as_str(), "pty" | "structured") {
        return err(StatusCode::BAD_REQUEST, "unknown session mode");
    }
    if let Some(images) = &req.images {
        if let Err(e) = validate_images(images) {
            return err(StatusCode::BAD_REQUEST, e);
        }
    }
    let dedup = req.client_session_id.clone().or_else(|| {
        req.source_session_id
            .as_ref()
            .map(|s| format!("handoff:{s}"))
    });
    if let Some(k) = &dedup {
        if let Some(s) = app
            .sessions
            .read()
            .await
            .values()
            .find(|s| s.client_session_id.as_ref() == Some(k))
            .cloned()
        {
            let mut v = session_wire(s, &app.cfg.host_id);
            v["deduplicated"] = Value::Bool(true);
            return response(StatusCode::OK, v);
        }
    }
    let sid = clean_id();
    let original = req.cwd.unwrap_or_else(|| ".".into());
    let mut cwd = original.clone();
    let mut wt = None;
    if req.harness == "codex" {
        match create_worktree(&original, &sid, &app.cfg.worktrees) {
            Ok(Some(w)) => {
                cwd = w.path.clone();
                wt = Some(w)
            }
            Ok(None) => {}
            Err(e) => {
                return err(
                    StatusCode::SERVICE_UNAVAILABLE,
                    format!("worktree isolation unavailable: {e}"),
                )
            }
        }
    }
    let mut s = Session {
        session_id: sid.clone(),
        harness: req.harness.clone(),
        mode: mode.clone(),
        cwd,
        status: "running".into(),
        pid: None,
        model: req.model,
        thinking_effort: req._thinking_effort,
        client_session_id: dedup,
        parent_session_id: req.parent_session_id,
        native_session_id: req.native_resume.and_then(|x| {
            x.get("session_id")
                .and_then(|x| x.as_str())
                .map(str::to_string)
        }),
        worktree: wt,
        awaiting: if mode == "structured" {
            Some("input".into())
        } else {
            None
        },
        extra: BTreeMap::new(),
        restart_lost: false,
        created_at: now(),
    };
    if let Err(error) = app.set_session(s.clone()).await {
        return err(StatusCode::SERVICE_UNAVAILABLE, error.to_string());
    }
    if let Err(error) = app.emit(&sid, "status", None, json!({"status":"started"}), None) {
        return err(StatusCode::SERVICE_UNAVAILABLE, error.to_string());
    }
    if mode == "pty" {
        match spawn_pty(
            req.command.unwrap_or_else(|| vec!["/bin/sh".into()]),
            &s.cwd,
            app.clone(),
            sid.clone(),
        ) {
            Ok((pid, terminal)) => {
                s.pid = Some(pid);
                let _ = terminal
                    ._master
                    .lock()
                    .unwrap()
                    .resize(portable_pty::PtySize {
                        rows: req.rows.unwrap_or(24).clamp(1, 1000),
                        cols: req.cols.unwrap_or(80).clamp(1, 1000),
                        pixel_width: 0,
                        pixel_height: 0,
                    });
                if let Some(input) = req.initial_input {
                    let _ = terminal.write(input.as_bytes());
                }
                let mut finished = terminal.finished.subscribe();
                let app2 = app.clone();
                let sid2 = sid.clone();
                tokio::spawn(async move {
                    if !*finished.borrow_and_update() {
                        let _ = finished.changed().await;
                    }
                    let _operation = app2.operations.lock().await;
                    let session = { app2.sessions.read().await.get(&sid2).cloned() };
                    if let Some(mut session) = session {
                        if session.status == "running" {
                            session.status = "completed".into();
                            session.pid = None;
                            if let Err(error) = app2.set_session(session).await {
                                error!("session store unavailable: {error}");
                            }
                        }
                    }
                    app2.terminals.write().await.remove(&sid2);
                });
                app.terminals.write().await.insert(sid.clone(), terminal);
            }
            Err(e) => {
                s.status = "errored".into();
                let _ = app.set_session(s).await;
                return err(
                    StatusCode::BAD_GATEWAY,
                    format!("terminal launch failed: {e}"),
                );
            }
        }
    }
    if let Err(error) = app.set_session(s.clone()).await {
        return err(StatusCode::SERVICE_UNAVAILABLE, error.to_string());
    }
    let images = req.images.unwrap_or_default();
    if let Some(prompt) = req
        .prompt
        .or_else(|| (!images.is_empty()).then(String::new))
    {
        if let Err(error) =
            run_codex_turn(app.clone(), sid.clone(), prompt, None, None, &images).await
        {
            return err(StatusCode::BAD_GATEWAY, error.to_string());
        }
    }
    response(StatusCode::CREATED, session_wire(s, &app.cfg.host_id))
}

fn validate_images(images: &[Image]) -> std::result::Result<(), String> {
    for i in images {
        if !i.media_type.starts_with("image/") {
            return Err("invalid attachment type".into());
        }
        if base64::engine::general_purpose::STANDARD
            .decode(&i.data_base64)
            .is_err()
        {
            return Err("invalid base64 attachment".into());
        }
    }
    Ok(())
}
fn spawn_pty(
    command: Vec<String>,
    cwd: &str,
    app: App,
    sid: String,
) -> Result<(u32, Arc<Terminal>)> {
    let exe = command.first().context("empty command")?;
    let pair = portable_pty::native_pty_system().openpty(portable_pty::PtySize {
        rows: 24,
        cols: 80,
        pixel_width: 0,
        pixel_height: 0,
    })?;
    let mut cb = portable_pty::CommandBuilder::new(exe);
    for a in command.iter().skip(1) {
        cb.arg(a);
    }
    cb.cwd(cwd);
    let mut child = pair.slave.spawn_command(cb)?;
    drop(pair.slave);
    let pid = child.process_id().unwrap_or(0);
    let mut reader = pair.master.try_clone_reader()?;
    let writer = pair.master.take_writer()?;
    let (output, _) = broadcast::channel(TERMINAL_CHANNEL_CAPACITY);
    let terminal = Arc::new(Terminal {
        killer: Mutex::new(child.clone_killer()),
        finished: tokio::sync::watch::channel(false).0,
        writer: Mutex::new(writer),
        _master: Mutex::new(pair.master),
        scrollback: Mutex::new(VecDeque::new()),
        output: output.clone(),
    });
    let terminal_for_reader = terminal.clone();
    std::thread::Builder::new()
        .name(format!("pty-{pid}"))
        .spawn(move || {
            let mut buffer = [0_u8; 8192];
            loop {
                match reader.read(&mut buffer) {
                    Ok(0) | Err(_) => {
                        let status = child.wait().ok().map(|s| s.exit_code());
                        let _ = app.emit(
                            &sid,
                            "status",
                            Some("exited"),
                            json!({"exited":status}),
                            None,
                        );
                        terminal_for_reader.finished.send_replace(true);
                        let _ = output.send(json!({"type":"exit","exit_code":status}));
                        break;
                    }
                    Ok(count) => {
                        let bytes = &buffer[..count];
                        {
                            let mut scrollback = terminal_for_reader
                                .scrollback
                                .lock()
                                .unwrap_or_else(|error| error.into_inner());
                            scrollback.extend(bytes);
                            while scrollback.len() > TERMINAL_SCROLLBACK_BYTES {
                                scrollback.pop_front();
                            }
                        }
                        let text = String::from_utf8_lossy(bytes);
                        if app
                            .emit(
                                &sid,
                                "assistant_output",
                                Some(&text),
                                json!({"source":"terminal"}),
                                None,
                            )
                            .is_err()
                        {
                            let _ = terminal_for_reader.killer.lock().unwrap().kill();
                            let _ = child.wait();
                            terminal_for_reader.finished.send_replace(true);
                            let _ = output.send(json!({"type":"exit"}));
                            break;
                        }
                        let _ = output.send(json!({"type":"output","data":text}));
                    }
                }
            }
        })?;
    Ok((pid, terminal))
}

async fn session_get(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    match app.sessions.read().await.get(&id).cloned() {
        Some(s) => ok(session_wire(s, &app.cfg.host_id)),
        None => err(
            StatusCode::NOT_FOUND,
            format!("unknown terminal session: {id}"),
        ),
    }
}
async fn terminate(State(app): State<App>, headers: HeaderMap, Path(id): Path<String>) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let _operation = app.operations.lock().await;
    let mut s = match app.sessions.read().await.get(&id).cloned() {
        Some(s) => s,
        None => {
            return err(
                StatusCode::NOT_FOUND,
                format!("unknown terminal session: {id}"),
            )
        }
    };
    if let Some(pid) = s.pid {
        signal_group(pid, libc::SIGTERM);
        for _ in 0..20 {
            if unsafe { libc::kill(pid as i32, 0) } != 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        signal_group(pid, libc::SIGKILL);
        if let Some(terminal) = app.terminals.write().await.remove(&id) {
            let _ = terminal.killer.lock().unwrap().kill();
            let _ = terminal.output.send(json!({"type":"exit"}));
        }
    }
    if let Some(w) = &s.worktree {
        if owns_worktree(&app, &id, w) {
            match cleanup_worktree(w) {
                Ok(true) => s.worktree = None,
                Ok(false) => {}
                Err(error) => warn!("worktree cleanup retained: {error}"),
            }
        }
    }
    s.pid = None;
    s.status = "terminated".into();
    s.awaiting = None;
    if let Err(error) = app.set_session(s).await {
        return err(StatusCode::SERVICE_UNAVAILABLE, error.to_string());
    }
    let _ = app.emit(
        &id,
        "status",
        Some("terminated"),
        json!({"status":"terminated"}),
        None,
    );
    ok(json!({"session_id":id,"host_id":app.cfg.host_id,"status":"terminated","terminated":true}))
}

#[derive(Deserialize)]
struct Turn {
    text: Option<String>,
    images: Option<Vec<Image>>,
    model: Option<String>,
    #[serde(rename = "thinking_effort")]
    _thinking_effort: Option<String>,
    client_turn_id: Option<String>,
}
async fn turn(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(t): Json<Turn>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let _operation = app.operations.lock().await;
    let Some(mut session) = ({ app.sessions.read().await.get(&id).cloned() }) else {
        return err(
            StatusCode::NOT_FOUND,
            format!("unknown structured session: {id}"),
        );
    };
    if session.mode != "structured" || session.status != "running" {
        return err(
            StatusCode::NOT_FOUND,
            format!("unknown structured session: {id}"),
        );
    }
    if let Some(key) = &t.client_turn_id {
        if Uuid::parse_str(key).is_err() {
            return err(StatusCode::BAD_REQUEST, "client_turn_id must be a UUID");
        }
        let existing = app
            .db
            .lock()
            .unwrap()
            .query_row(
                "SELECT turn_id FROM turns WHERE session_id=?1 AND client_turn_id=?2",
                params![id, key],
                |r| r.get::<_, String>(0),
            )
            .ok();
        if let Some(turn_id) = existing {
            return response(StatusCode::ACCEPTED, json!({"turn_id":turn_id}));
        }
    }
    if session.pid.is_some() {
        return err(StatusCode::CONFLICT, "turn already in flight");
    }
    let images = t.images.unwrap_or_default();
    let text = t.text.unwrap_or_default();
    if text.trim().is_empty() && images.is_empty() {
        return err(StatusCode::BAD_REQUEST, "text or images required");
    }
    if let Err(error) = validate_images(&images) {
        return err(StatusCode::BAD_REQUEST, error);
    }
    if let Some(model) = t.model {
        if model.trim().is_empty() || model.len() > 256 {
            return err(StatusCode::BAD_REQUEST, "invalid model");
        }
        session.model = Some(model);
    }
    if let Some(effort) = t._thinking_effort {
        session.thinking_effort = Some(effort);
    }
    if let Err(error) = app.set_session(session).await {
        return err(StatusCode::SERVICE_UNAVAILABLE, error.to_string());
    }
    let turn_id = t
        .client_turn_id
        .clone()
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    if let Err(error) = run_codex_turn(
        app.clone(),
        id,
        text,
        Some(turn_id.clone()),
        t.client_turn_id,
        &images,
    )
    .await
    {
        return err(StatusCode::BAD_GATEWAY, error.to_string());
    }
    response(StatusCode::ACCEPTED, json!({"turn_id":turn_id}))
}

fn save_images(app: &App, sid: &str, images: &[Image]) -> Result<Vec<PathBuf>> {
    let dir = app.cfg.home.join(".drover/attachments").join(sid);
    let mut paths = Vec::new();
    for image in images {
        fs::create_dir_all(&dir)?;
        let path = dir.join(format!("{}.png", Uuid::new_v4()));
        fs::write(&path, STANDARD.decode(&image.data_base64)?)?;
        paths.push(path);
    }
    Ok(paths)
}
fn git(cwd: &str, args: &[&str]) -> Option<String> {
    let result = Command::new("git")
        .arg("-C")
        .arg(cwd)
        .args(args)
        .output()
        .ok()?;
    result
        .status
        .success()
        .then(|| String::from_utf8_lossy(&result.stdout).into_owned())
}
fn git_evidence(cwd: &str, base: &str) -> Value {
    let range = format!("{base}..HEAD");
    let mut paths = std::collections::BTreeSet::new();
    for args in [
        vec![
            "log",
            "--first-parent",
            "-m",
            "--format=",
            "--name-only",
            "--no-renames",
            "--no-relative",
            "-z",
            &range,
            "--",
        ],
        vec![
            "diff",
            "--name-only",
            "--no-renames",
            "--no-relative",
            "-z",
            base,
            "--",
        ],
        vec![
            "diff",
            "--cached",
            "--name-only",
            "--no-renames",
            "--no-relative",
            "-z",
            base,
            "--",
        ],
        vec![
            "ls-files",
            "--others",
            "--exclude-standard",
            "--full-name",
            "-z",
            "--",
            ":/",
        ],
    ] {
        if let Some(output) = git(cwd, &args) {
            paths.extend(
                output
                    .split('\0')
                    .filter(|v| !v.is_empty())
                    .map(str::to_owned),
            );
        }
    }
    json!({"source":"git_diff","base_sha":base,"changes":paths.into_iter().map(|path|json!({"path":path})).collect::<Vec<_>>()})
}
async fn run_codex_turn(
    app: App,
    sid: String,
    text: String,
    turn_id: Option<String>,
    client: Option<String>,
    images: &[Image],
) -> Result<()> {
    let mut session = {
        app.sessions
            .read()
            .await
            .get(&sid)
            .cloned()
            .context("session missing")?
    };
    let turn_id = turn_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    let paths = save_images(&app, &sid, images)?;
    let base = git(&session.cwd, &["rev-parse", "--verify", "HEAD"]).map(|v| v.trim().to_owned());
    let mut cmd = TokioCommand::new("codex");
    if let Some(model) = &session.model {
        cmd.args(["--model", model]);
    }
    if let Some(effort) = &session.thinking_effort {
        cmd.args([
            "-c",
            &format!("model_reasoning_effort={}", serde_json::to_string(effort)?),
        ]);
    }
    cmd.arg("exec");
    if let Some(native) = &session.native_session_id {
        cmd.args([
            "resume",
            native,
            "--json",
            "--skip-git-repo-check",
            "-c",
            "sandbox_mode=danger-full-access",
        ]);
    } else {
        cmd.args([
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
        ]);
    }
    for path in paths {
        cmd.arg("--image").arg(path);
    }
    cmd.arg("--")
        .arg(&text)
        .current_dir(&session.cwd)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .process_group(0);
    let mut child = cmd.spawn()?;
    session.pid = child.id();
    session.awaiting = Some("assistant".into());
    app.save_session(&session)?;
    app.sessions
        .write()
        .await
        .insert(sid.clone(), session.clone());
    if let Some(key) = client {
        app.db.lock().unwrap().execute(
            "INSERT INTO turns(session_id,client_turn_id,turn_id) VALUES(?1,?2,?3)",
            params![sid, key, turn_id],
        )?;
    }
    app.emit(&sid, "user_input", Some(&text), json!({}), Some(&turn_id))?;
    let out = child.stdout.take().context("stdout missing")?;
    let stderr = child.stderr.take().context("stderr missing")?;
    tokio::spawn(async move {
        let stderr_task = tokio::spawn(async move {
            let mut reader = BufReader::new(stderr);
            let mut buffer = [0; 8192];
            while let Ok(n) = reader.read(&mut buffer).await {
                if n == 0 {
                    break;
                }
            }
        });
        let mut lines = BufReader::new(out).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Ok(value) = serde_json::from_str::<Value>(&line) {
                if value["type"] == "thread.started" {
                    if let Some(native) = value["thread_id"].as_str() {
                        let _operation = app.operations.lock().await;
                        let current = { app.sessions.read().await.get(&sid).cloned() };
                        if let Some(mut current) = current {
                            if current.status == "running" {
                                current.native_session_id = Some(native.into());
                                if let Err(error) = app.set_session(current).await {
                                    error!("session store unavailable: {error}");
                                }
                            }
                        }
                    }
                }
                let kind = match value["item"]["type"].as_str() {
                    Some("agent_message") => "assistant_output",
                    Some("command_execution") => "tool_result",
                    Some("reasoning") => "reasoning",
                    _ => "status",
                };
                let text = value
                    .pointer("/item/text")
                    .or_else(|| value.pointer("/item/aggregated_output"))
                    .and_then(Value::as_str);
                if app
                    .emit(&sid, kind, text, value.clone(), Some(&turn_id))
                    .is_err()
                {
                    let _ = child.start_kill();
                    break;
                }
            }
        }
        let result = child.wait().await;
        let _ = stderr_task.await;
        if let Some(base) = base {
            let payload = git_evidence(&session.cwd, &base);
            if payload["changes"].as_array().is_some_and(|v| !v.is_empty()) {
                let _ = app.emit(
                    &sid,
                    "tool_result",
                    Some("Git file changes recorded at turn boundary"),
                    payload,
                    Some(&turn_id),
                );
            }
        }
        let _operation = app.operations.lock().await;
        let current = { app.sessions.read().await.get(&sid).cloned() };
        if let Some(mut current) = current {
            if current.status == "running" {
                current.pid = None;
                current.awaiting = Some("input".into());
                if let Err(error) = app.set_session(current).await {
                    error!("session store unavailable: {error}");
                }
            }
        }
        let code = result.ok().and_then(|s| s.code()).unwrap_or(-1);
        let _ = app.emit(
            &sid,
            "status",
            Some("turn complete"),
            json!({"turn_complete":true,"awaiting":"input","exited":code}),
            Some(&turn_id),
        );
    });
    Ok(())
}

fn signal_group(pid: u32, signal: i32) {
    if pid > 1 {
        unsafe {
            libc::kill(-(pid as i32), signal);
        }
    }
}
async fn interrupt(State(app): State<App>, headers: HeaderMap, Path(id): Path<String>) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let _operation = app.operations.lock().await;
    let Some(session) = ({ app.sessions.read().await.get(&id).cloned() }) else {
        return err(
            StatusCode::NOT_FOUND,
            format!("unknown structured session: {id}"),
        );
    };
    if session.mode != "structured" || session.status != "running" {
        return err(
            StatusCode::NOT_FOUND,
            format!("unknown structured session: {id}"),
        );
    }
    if let Some(pid) = session.pid {
        signal_group(pid, libc::SIGTERM);
    }
    ok(json!({"ok":true}))
}
const RECOVERY_UNAVAILABLE: &str =
    "Session cannot be resumed after the harness restart. Continue it in a new session.";
async fn recover(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Json(body): Json<Value>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let _operation = app.operations.lock().await;
    let Some(mut session) = ({ app.sessions.read().await.get(&id).cloned() }) else {
        return err(StatusCode::CONFLICT, RECOVERY_UNAVAILABLE);
    };
    if session.mode == "structured" && session.status == "running" {
        return ok(
            json!({"session_id":id,"status":"running","recovered":false,"native_session_id":session.native_session_id}),
        );
    }
    let native = body["native_session_id"]
        .as_str()
        .filter(|s| !s.trim().is_empty());
    if session.mode != "structured"
        || session.harness != "codex"
        || session.status != "errored"
        || !session.restart_lost
        || native.is_none()
        || session
            .native_session_id
            .as_deref()
            .is_some_and(|s| Some(s) != native)
        || !FsPath::new(&session.cwd).is_dir()
    {
        return err(StatusCode::CONFLICT, RECOVERY_UNAVAILABLE);
    }
    session.native_session_id = native.map(str::to_owned);
    session.restart_lost = false;
    session.status = "running".into();
    session.awaiting = Some("input".into());
    let reply = json!({"session_id":id,"status":"running","recovered":true,"native_session_id":session.native_session_id});
    if let Err(error) = app.set_session(session).await {
        return err(StatusCode::SERVICE_UNAVAILABLE, error.to_string());
    }
    ok(reply)
}
async fn permission(
    State(app): State<App>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let live = app
        .sessions
        .read()
        .await
        .get(&id)
        .is_some_and(|s| s.mode == "structured" && s.status == "running");
    if live {
        err(StatusCode::CONFLICT, "approvals are not enabled")
    } else {
        err(
            StatusCode::NOT_FOUND,
            format!("unknown structured session: {id}"),
        )
    }
}

async fn catalog(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    err(
        StatusCode::NOT_FOUND,
        format!(
            "unknown or disabled harness preset: {}",
            q.get("harness").map(String::as_str).unwrap_or("")
        ),
    )
}
async fn native_sessions(
    State(app): State<App>,
    headers: HeaderMap,
    Query(q): Query<HashMap<String, String>>,
) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    };
    let h = q.get("harness").map(String::as_str).unwrap_or("");
    let cwd = q.get("cwd").map(String::as_str);
    let mut rows = Vec::new();
    if h == "codex" {
        let root = app.cfg.home.join(".codex/sessions");
        for f in walk_jsonl(&root, 100) {
            if let Ok(t) = fs::read_to_string(f) {
                for l in t.lines() {
                    if let Ok(v) = serde_json::from_str::<Value>(l) {
                        if v.get("type").and_then(Value::as_str) == Some("session_meta") {
                            let p = &v["payload"];
                            if let (Some(id), Some(c)) = (
                                p.get("id").and_then(Value::as_str),
                                p.get("cwd").and_then(Value::as_str),
                            ) {
                                if cwd.is_none_or(|x| x == c) {
                                    rows.push(json!({"harness":"codex","session_id":id,"cwd":c,"native_resume":{"session_id":id}}));
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    ok(json!({"host_id":app.cfg.host_id,"sessions":rows}))
}
fn walk_jsonl(root: &FsPath, limit: usize) -> Vec<PathBuf> {
    fn go(p: &FsPath, o: &mut Vec<PathBuf>, n: usize) {
        if o.len() >= n {
            return;
        }
        if let Ok(rd) = fs::read_dir(p) {
            for e in rd.flatten() {
                let p = e.path();
                if p.is_dir() {
                    go(&p, o, n)
                } else if p.extension().is_some_and(|x| x == "jsonl") {
                    o.push(p)
                }
            }
        }
    }
    let mut o = Vec::new();
    go(root, &mut o, limit);
    o
}
async fn fallback(
    State(app): State<App>,
    headers: HeaderMap,
    method: Method,
    uri: axum::http::Uri,
) -> Response {
    if method == Method::OPTIONS {
        return options().await;
    }
    if uri.path() == "/healthz" {
        return health(State(app)).await;
    }
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    err(StatusCode::NOT_FOUND, "not found")
}

fn create_worktree(cwd: &str, sid: &str, root: &FsPath) -> Result<Option<Worktree>> {
    let output = Command::new("git")
        .args(["-C", cwd, "rev-parse", "--show-toplevel"])
        .output()?;
    if !output.status.success() {
        return Ok(None);
    }
    let repo = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let head = Command::new("git")
        .args(["-C", &repo, "rev-parse", "HEAD"])
        .output()?;
    if !head.status.success() {
        return Ok(None);
    }
    let base = String::from_utf8_lossy(&head.stdout).trim().to_string();
    fs::create_dir_all(root)?;
    let path = root.join(sid);
    let branch = format!("drover/{sid}");
    let a = Command::new("git")
        .args(["-C", &repo, "branch", &branch, &base])
        .status()?;
    anyhow::ensure!(a.success(), "git branch failed");
    let b = Command::new("git")
        .args([
            "-C",
            &repo,
            "worktree",
            "add",
            path.to_str().unwrap(),
            &branch,
        ])
        .status()?;
    if !b.success() {
        let _ = Command::new("git")
            .args(["-C", &repo, "branch", "-D", &branch])
            .status();
        anyhow::bail!("git worktree add failed")
    }
    Ok(Some(Worktree {
        repo,
        path: path.to_string_lossy().into(),
        branch,
        base,
    }))
}
fn owns_worktree(app: &App, id: &str, w: &Worktree) -> bool {
    let path = FsPath::new(&w.path);
    path.file_name().is_some_and(|name| name == id)
        && w.branch == format!("drover/{id}")
        && path.parent().and_then(|p| p.canonicalize().ok())
            == app.cfg.worktrees.canonicalize().ok()
}
fn cleanup_worktree(w: &Worktree) -> Result<bool> {
    if !FsPath::new(&w.path).exists() {
        return Ok(true);
    }
    let head = git(&w.path, &["rev-parse", "HEAD"]).context("inspect worktree HEAD")?;
    let dirty = git(&w.path, &["status", "--porcelain", "--ignored"])
        .context("inspect worktree changes")?;
    if head.trim() != w.base || !dirty.is_empty() {
        return Ok(false);
    }
    let removed = Command::new("git")
        .args(["-C", &w.repo, "worktree", "remove", "--", &w.path])
        .output()?;
    anyhow::ensure!(removed.status.success(), "git worktree remove failed");
    let deleted = Command::new("git")
        .args(["-C", &w.repo, "branch", "-D", "--", &w.branch])
        .output()?;
    anyhow::ensure!(deleted.status.success(), "git branch cleanup failed");
    Ok(true)
}
async fn usage(State(app): State<App>, headers: HeaderMap) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    ok(json!({"accounts":[],"observed_at":now()}))
}
async fn worktree_inventory(State(app): State<App>, headers: HeaderMap) -> Response {
    if !app.authorized(&headers) {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    let trees: Result<Vec<(String, Worktree)>> = {
        let db = app.db.lock().unwrap();
        (|| {
            let mut statement = db.prepare("SELECT session_id,data FROM worktrees")?;
            let rows = statement
                .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
            rows.map(|r| {
                let (sid, data) = r?;
                Ok((sid, serde_json::from_str(&data)?))
            })
            .collect()
        })()
    };
    match trees {
        Ok(trees) => {
            let rows=trees.into_iter().map(|(sid,w)|json!({"session_id":sid,"path":w.path,"repo":w.repo,"branch":w.branch,"base_sha":w.base,"ownership":if owns_worktree(&app,&sid,&w) {"owned"} else {"foreign"},"would_collect":false,"reasons":["retained_until_explicit_review"]})).collect::<Vec<_>>();
            ok(json!({"worktrees":rows,"errors":[],"observed_at":now()}))
        }
        Err(error) => err(StatusCode::SERVICE_UNAVAILABLE, error.to_string()),
    }
}
fn claim_store(root: &FsPath) -> Result<fs::File> {
    use fs2::FileExt;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .truncate(false)
        .open(root.join(".store-owner.lock"))?;
    lock.try_lock_exclusive()
        .context("Rust store is already in use; stop the daemon before import")?;
    Ok(lock)
}
fn claim_worktrees(root: &FsPath) -> Result<fs::File> {
    use fs2::FileExt;
    fs::create_dir_all(root)?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .truncate(false)
        .open(root.join(".drover-owner.lock"))?;
    lock.try_lock_exclusive()
        .context("worktrees directory is already in use")?;
    Ok(lock)
}

async fn heartbeat_once(app: &App) -> Result<bool> {
    let Some(base) = &app.cfg.central_url else {
        return Ok(true);
    };
    let body = json!({"host_id":app.cfg.host_id,"display_name":app.cfg.display_name,"kind":app.cfg.kind,"local_url":app.cfg.local_url,"tailscale_url":app.cfg.tailscale_url,"status":"online","connection_kind":if app.cfg.relay{"relay"}else{"direct"},"capabilities":capability_envelope(),"agent_version":env!("CARGO_PKG_VERSION"),"harnessd_impl":"rust"});
    let reply = app
        .client
        .post(format!("{base}/harness/hosts"))
        .bearer_auth(&app.cfg.token)
        .json(&body)
        .send()
        .await?;
    if reply.status() == StatusCode::CONFLICT {
        warn!("host retired; refusing registration and delivery");
        return Ok(false);
    }
    anyhow::ensure!(
        reply.status().is_success(),
        "heartbeat HTTP {}",
        reply.status()
    );
    let bytes = reply.bytes().await?;
    anyhow::ensure!(bytes.len() <= 65536, "heartbeat response too large");
    let reply: Value = serde_json::from_slice(&bytes)?;
    if let Some(consent) = reply.get("content_consent") {
        anyhow::ensure!(
            consent.as_object().is_some_and(|v| v.len() == 2)
                && consent["enabled"].is_boolean()
                && consent["epoch"].as_u64().is_some(),
            "invalid content consent"
        );
        let old = fs::read(&app.cfg.consent_path)
            .ok()
            .and_then(|v| serde_json::from_slice::<Value>(&v).ok())
            .unwrap_or(json!({"enabled":false,"epoch":0}));
        if consent["epoch"].as_u64() > old["epoch"].as_u64() {
            if let Some(parent) = app.cfg.consent_path.parent() {
                fs::create_dir_all(parent)?;
            }
            let temp = app.cfg.consent_path.with_extension("json.tmp");
            fs::write(&temp, serde_json::to_vec(consent)?)?;
            fs::rename(temp, &app.cfg.consent_path)?;
        }
    }
    if reply["relay_required"] == true && !app.cfg.relay {
        warn!("host requires --relay; refusing direct mode");
        return Ok(false);
    }
    Ok(true)
}
async fn heartbeat(app: App) {
    loop {
        tokio::time::sleep(Duration::from_secs(15)).await;
        match heartbeat_once(&app).await {
            Ok(false) => {
                app.stop.notify_one();
                return;
            }
            Err(error) => warn!("heartbeat unavailable: {error}"),
            _ => {}
        }
    }
}

async fn authenticate(
    State(app): State<App>,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    if request.uri().path() != "/healthz"
        && request.method() != Method::OPTIONS
        && !app.authorized(request.headers())
    {
        return err(StatusCode::UNAUTHORIZED, "authentication required");
    }
    next.run(request).await
}
fn router(app: App) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/capabilities", get(capabilities))
        .route(
            "/sessions",
            get(sessions_get).post(sessions_create).options(options),
        )
        .route("/sessions/:id", get(session_get).delete(terminate))
        .route("/sessions/:id/terminate", post(terminate))
        .route("/sessions/:id/turns", post(turn))
        .route("/sessions/:id/interrupt", post(interrupt))
        .route("/sessions/:id/recover", post(recover))
        .route("/sessions/:id/permission", post(permission))
        .route("/sessions/:id/terminal", get(transport::terminal_upgrade))
        .route("/fs/list", get(folders::list))
        .route("/fs/create", post(folders::create))
        .route("/fs/exists", post(folders::exists))
        .route("/fs/complete", get(folders::complete))
        .route("/providers/usage", get(usage))
        .route("/lifecycle/worktrees", get(worktree_inventory))
        .route("/model-catalog", get(catalog))
        .route("/native-sessions", get(native_sessions))
        .fallback(any(fallback))
        .layer(axum::middleware::from_fn_with_state(
            app.clone(),
            authenticate,
        ))
        .with_state(app)
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    tracing_subscriber::fmt()
        .with_env_filter("warn")
        .with_writer(std::io::stderr)
        .init();
    let cfg = config(&args)?;
    let dbpath = cfg.home.join(".drover/harnessd/harnessd.sqlite");
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(dbpath.parent().unwrap())?;
    fs::set_permissions(dbpath.parent().unwrap(), fs::Permissions::from_mode(0o700))?;
    let _store_owner = claim_store(dbpath.parent().unwrap())?;
    let db = init_db(&dbpath)?;
    let sessions = load_sessions(&db)?;
    let db = Arc::new(Mutex::new(db));
    let events = broadcast::channel(256).0;
    let writer = event_bus::StoreWriter::start(db.clone(), events.clone())?;
    let app = App {
        cfg: cfg.clone(),
        db,
        sessions: Arc::new(RwLock::new(sessions)),
        terminals: Arc::new(RwLock::new(HashMap::new())),
        events,
        writer,
        operations: Arc::new(tokio::sync::Mutex::new(())),
        stop: Arc::new(tokio::sync::Notify::new()),
        client: Client::builder().timeout(Duration::from_secs(10)).build()?,
    };
    if let Some(path) = args.import_jsonl {
        let counts = import_jsonl(&app, &path, args.import_dry_run)?;
        println!(
            "IMPORT sessions={} events={} skipped={} dry_run={}",
            counts.0, counts.1, counts.2, args.import_dry_run
        );
        return Ok(());
    }
    let _worktree_owner = claim_worktrees(&cfg.worktrees)?;
    let previous: Vec<_> = app.sessions.read().await.values().cloned().collect();
    for mut session in previous {
        if let Some(worktree) = &session.worktree {
            // Only reclaim records whose canonical path and branch prove this
            // daemon owns them. Edited/committed trees remain recoverable.
            if !session.restart_lost && owns_worktree(&app, &session.session_id, worktree) {
                match cleanup_worktree(worktree) {
                    Ok(true) => session.worktree = None,
                    Ok(false) => {}
                    Err(error) => warn!("startup worktree retained: {error}"),
                }
            }
        }
        app.set_session(session).await?;
    }
    let listener = tokio::net::TcpListener::bind(&args.listen).await?;
    println!("LISTENING {}", listener.local_addr()?);
    std::io::stdout().flush()?;
    match heartbeat_once(&app).await {
        Ok(false) => return Ok(()),
        Err(error) => warn!("initial heartbeat unavailable: {error}"),
        _ => {}
    }
    tokio::spawn(heartbeat(app.clone()));
    if cfg.relay {
        tokio::spawn(transport::relay(app.clone()));
    }
    tokio::spawn(delivery::run(app.clone()));
    tokio::select! {
        result = axum::serve(listener, router(app.clone())).into_future() => result?,
        _ = app.stop.notified() => {},
        _ = tokio::signal::ctrl_c() => {},
        _ = async { tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap().recv().await; } => {},
    }
    let ids: Vec<_> = app
        .sessions
        .read()
        .await
        .values()
        .filter(|s| s.status == "running")
        .map(|s| s.session_id.clone())
        .collect();
    for id in ids {
        let mut headers = HeaderMap::new();
        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_str(&format!("Bearer {}", app.cfg.token))?,
        );
        terminate(State(app.clone()), headers, Path(id)).await;
    }
    Ok(())
}
fn import_jsonl(app: &App, path: &FsPath, dry_run: bool) -> Result<(usize, usize, usize)> {
    app.db.lock().unwrap().execute_batch("SAVEPOINT handoff")?;
    let result = import_records(app, path);
    let finish = if result.is_err() || dry_run {
        "ROLLBACK TO handoff; RELEASE handoff"
    } else {
        "RELEASE handoff"
    };
    app.db.lock().unwrap().execute_batch(finish)?;
    result
}
fn import_records(app: &App, path: &FsPath) -> Result<(usize, usize, usize)> {
    let mut sessions = 0;
    let mut events = 0;
    let skipped = 0;
    for line in fs::read_to_string(path)?
        .lines()
        .filter(|line| !line.trim().is_empty())
    {
        let v: Value = serde_json::from_str(line)?;
        let kind = v
            .get("kind")
            .or_else(|| v.get("record_type"))
            .and_then(Value::as_str);
        if kind == Some("session") {
            let row = v.get("session").unwrap_or(&v);
            let session_id = row.get("session_id").and_then(Value::as_str).unwrap_or("");
            anyhow::ensure!(!session_id.is_empty(), "import session_id missing");
            let mut session = Session {
                session_id: session_id.to_string(),
                harness: row
                    .get("harness")
                    .and_then(Value::as_str)
                    .unwrap_or("shell")
                    .to_string(),
                mode: row
                    .get("mode")
                    .and_then(Value::as_str)
                    .unwrap_or("pty")
                    .to_string(),
                cwd: row
                    .get("cwd")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                status: row
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("errored")
                    .to_string(),
                pid: None,
                model: row.get("model").and_then(Value::as_str).map(str::to_string),
                thinking_effort: row
                    .get("thinking_effort")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                client_session_id: row
                    .get("client_session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                parent_session_id: row
                    .get("parent_session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                native_session_id: row
                    .get("native_session_id")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                worktree: row
                    .get("worktree")
                    .cloned()
                    .and_then(|x| serde_json::from_value(x).ok()),
                awaiting: row
                    .get("awaiting")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                extra: BTreeMap::new(),
                restart_lost: true,
                created_at: row
                    .get("created_at")
                    .or_else(|| row.get("started_at"))
                    .and_then(Value::as_str)
                    .unwrap_or("1970-01-01T00:00:00+00:00")
                    .to_string(),
            };
            if session.status == "running" || session.status == "starting" {
                session.status = "errored".into();
                session.awaiting = None;
            }
            let known = serde_json::to_value(&session)?;
            if let Some(fields) = row.as_object() {
                session.extra = fields
                    .iter()
                    .filter(|(k, _)| {
                        !known.as_object().unwrap().contains_key(*k)
                            && ![
                                "pid",
                                "awaiting",
                                "worktree",
                                "model",
                                "thinking_effort",
                                "client_session_id",
                                "parent_session_id",
                                "native_session_id",
                            ]
                            .contains(&k.as_str())
                    })
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();
            }
            sessions += 1;
            // Re-imports never overwrite state produced after cutover.
            let exists = app.db.lock().unwrap().query_row(
                "SELECT count(*) FROM sessions WHERE session_id=?1",
                [session_id],
                |r| r.get::<_, i64>(0),
            )? > 0;
            if !exists {
                app.save_session(&session)?;
            }
            continue;
        }
        if matches!(kind, Some("worktree" | "lifecycle" | "quarantine")) {
            let kind = kind.unwrap();
            let record = v.get("record").unwrap_or(&v);
            let id = record
                .get("operation_id")
                .or_else(|| record.get("event_id"))
                .or_else(|| record.get("path"))
                .and_then(Value::as_str)
                .context("handoff record identity missing")?;
            app.db.lock().unwrap().execute(
                "INSERT OR IGNORE INTO handoff_records(kind,record_id,data) VALUES(?1,?2,?3)",
                params![kind, id, serde_json::to_string(record)?],
            )?;
            if kind == "quarantine" {
                app.db.lock().unwrap().execute("INSERT OR IGNORE INTO event_quarantine(event_id,session_id,reason,quarantined_at) VALUES(?1,?2,?3,?4)",params![id,record["session_id"].as_str().context("quarantine session missing")?,record["reason"].as_str().context("quarantine reason missing")?,record["quarantined_at"].as_str().unwrap_or(&now())])?;
            }
            if kind == "worktree"
                && ["repo", "path", "branch", "base_sha"]
                    .iter()
                    .all(|k| record[k].is_string())
            {
                if let Some(sid) = record["session_id"].as_str() {
                    let worktree = Worktree {
                        repo: record["repo"]
                            .as_str()
                            .context("worktree repo missing")?
                            .into(),
                        path: record["path"]
                            .as_str()
                            .context("worktree path missing")?
                            .into(),
                        branch: record["branch"]
                            .as_str()
                            .context("worktree branch missing")?
                            .into(),
                        base: record["base_sha"]
                            .as_str()
                            .context("worktree base missing")?
                            .into(),
                    };
                    app.db.lock().unwrap().execute(
                        "INSERT OR IGNORE INTO worktrees(session_id,data) VALUES(?1,?2)",
                        params![sid, serde_json::to_string(&worktree)?],
                    )?;
                    let raw = app
                        .db
                        .lock()
                        .unwrap()
                        .query_row(
                            "SELECT data FROM sessions WHERE session_id=?1",
                            [sid],
                            |r| r.get::<_, String>(0),
                        )
                        .ok();
                    if let Some(raw) = raw {
                        let mut session: Session = serde_json::from_str(&raw)?;
                        session.worktree = Some(worktree);
                        app.save_session(&session)?;
                    }
                }
            }
            continue;
        }
        anyhow::ensure!(
            kind.is_none() || kind == Some("event"),
            "unknown handoff record kind"
        );
        if app.import_event(&v)? {
            events += 1;
        }
    }
    Ok((sessions, events, skipped))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn event_hash_matches_python_vector() {
        let v = json!({"event_id":"new","aggregated_output":"drop","nested":{"event_id":"nested","ok":true}});
        assert_eq!(
            event_identity("s-1", 7, "tool_result", "2026-01-02T03:04:05+00:00", &v),
            "2b707256b02eac2ef61f23ffabb3c232899851bc38cd933ed163d79e4713cfad"
        );
    }
}
